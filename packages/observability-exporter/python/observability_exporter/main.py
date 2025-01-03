# -*- mode: python; python-indent: 4 -*-
"""Main NSO package module

This is the entry point for the NSO package, it is invoked by NSO on startup
and sets up our ptrace processing pipeline based on configuration read from
NSO.
"""
import logging
import multiprocessing
import time
import traceback
import grpc
from influxdb import InfluxDBClient

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter as grpcOTLPSpanExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter as httpOTLPSpanExporter,
)
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter as httpOTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter as grpcOTLPMetricExporter

import ncs
import _ncs

from . import background_process
from . import dictsubscriber
from . import ptrace
from . import nso_metrics

# InfluxDBClient uses urllib3 which is quite chatty
logging.getLogger("urllib3").setLevel(logging.WARNING)


def decrypt_string(node, value):
    """Decrypts an encrypted tailf:aes-cfb-128-encrypted-string type leaf

    :param node: any maagic Node
    :param value: the encrypted leaf value
    """
    if value is None:
        return None

    if isinstance(node._backend, ncs.maagic._TransactionBackend):
        node._backend.maapi.install_crypto_keys()
    elif isinstance(node._backend, ncs.maagic._MaapiBackend):
        node._backend.install_crypto_keys()
    else:
        raise ValueError("Unknown MaagicBackend for leaf")

    return _ncs.decrypt(value)  # pylint: disable=no-member


def NotifReader(qbuf):
    """Read notifications from NSO API and put on q"""
    log = logging.getLogger("NotifReader")
    conf = dictsubscriber.DictSubscriber(
        app=None,
        log=log,
        subscriptions=[
            (
                "extra_tags",
                "/progress:progress/observability-exporter:export/extra-tags",
            ),
        ],
    )
    with ncs.maapi.single_write_trans("observability-exporter", "system") as t:
        root = ncs.maagic.get_root(t)
        export_config = root.progress.export
        cfg_include_diffset = bool(export_config.include_diffset)

        if "ptrace-export-diffset-reader" not in root.kickers.data_kicker:
            kicker_diffset = root.kickers.data_kicker.create(
                "ptrace-export-diffset-reader"
            )
            kicker_diffset.monitor = (
                "/progress:progress/observability-exporter:export/include-diffset"
            )
            kicker_diffset.kick_node = "./.."
            kicker_diffset.action_name = "restart-reader"

            t.apply()

        # While not super scientific, the 200k value wasn't entirely picked out
        # of thin air.  We've seen how NSO can produce 35k events per second,
        # typically during device interaction heavy segments (like small
        # changesets to many devices). Our processing pipeline can normally do
        # something on the order of 30k-40k events per second on a fast CPU but
        # there are also bad cases when this drops. Had a scenario where it went
        # all the way down to 1k events per second (now fixed). To handle such a
        # burst and "work it off" during an extended time period, we have a
        # pretty hefty buffer.
        ptrace.q_putter(ptrace.notifs_reader(cfg_include_diffset, conf=conf), qbuf, 200000)


def FTObservabilityExporter(qbuf):
    """A fault-tolerant wrapper of the main processing pipeline

    Catch any encountered exceptions, log em and restart the pipeline. Sleeps
    for a little while to avoid busy-dying.
    """
    log = logging.getLogger("observability-exporter")
    while True:
        try:
            ObservabilityExporter(qbuf)
        # pylint:disable=broad-except
        except Exception:
            log.error("Unhandled exception, will restart after short sleep...")
            log.error(traceback.format_exc())
            time.sleep(1)


def ObservabilityExporter(qbuf):
    """Function for running as NSO background worker to subscribe to NSO
    progress-trace notification and export to tracing system
    """
    log = logging.getLogger("observability-exporter")
    log.info("Observablity progress-trace exporter started")

    metrics_q = []
    need_kicker_conf = False

    # Read in configuration from CDB
    with ncs.maapi.single_read_trans("observability-exporter", "system") as t:
        root = ncs.maagic.get_root(t)
        nso_version = root.ncs_state.version
        export_config = root.progress.export
        cfg_influxdb = export_config.influxdb.exists()
        if cfg_influxdb:
            if ptrace.is_ipv6(export_config.influxdb.host):
                influx_host = f"[{export_config.influxdb.host}]"
            else:
                influx_host = export_config.influxdb.host

            influx_client = InfluxDBClient(
                influx_host,
                export_config.influxdb.port,
                export_config.influxdb.username,
                decrypt_string(root, export_config.influxdb.password),
                export_config.influxdb.database,
            )
            influx_exporter = ptrace.InfluxdbExporter(influx_client, metrics_q)
            influx_exporter.start()

        cfg_otlp = export_config.otlp.exists()
        if cfg_otlp:
            otlp_endpoint = export_config.otlp.endpoint
            certificate_file = export_config.otlp.server_certificate_path
            transport = export_config.otlp.transport

            # Headers for otlp
            headers = {header.name: header.value for header in export_config.otlp.headers}
            if export_config.otlp.host is not None and ptrace.is_ipv6(export_config.otlp.host):
                otlp_host = f"[{export_config.otlp.host}]"
            else:
                otlp_host = export_config.otlp.host
            log.info(f"Setting up otlp {transport} exporter")
            if transport in ["https", "http"]:
                if otlp_endpoint is not None:
                    endpoint = f"{otlp_endpoint}"
                else:
                    otlp_port = export_config.otlp.port or 4318
                    endpoint = f"{transport}://{otlp_host}:{otlp_port}/v1/traces"
                otlp_exporter = httpOTLPSpanExporter(
                    endpoint=endpoint,
                    certificate_file=certificate_file if transport == "https" else None,
                    headers=headers
                )

            elif transport in ["grpc-secure", "grpc"]:
                if otlp_endpoint is not None:
                    endpoint = f"{otlp_endpoint}"
                else:
                    otlp_port = export_config.otlp.port or 4317
                    endpoint = f"{otlp_host}:{otlp_port}"
                if certificate_file is not None:
                    try:
                        with open(certificate_file, 'rb') as f:
                            certificate_data = f.read()
                            credentials = grpc.ssl_channel_credentials(root_certificates=certificate_data)
                    except FileNotFoundError as f:
                        raise FileNotFoundError(
                            f"Certificate file for Metrics is not found. Check the path of the certificate {f}"
                        ) from f
                metadata = ", ".join([f"{key}={value}" for key, value in headers.items()])
                otlp_exporter = grpcOTLPSpanExporter(
                    endpoint=endpoint,
                    credentials=credentials if transport == "grpc-secure" else None,
                    headers=metadata,
                    insecure=(transport == "grpc")
                )
            else:
                raise NotImplementedError(f"Not a supported transport {transport}")

            if export_config.otlp.metrics.exists() and nso_version >= "6":
                metrics_host = export_config.otlp.metrics.host
                metrics_endpoint = export_config.otlp.metrics.endpoint
                metrics_certificate_file = export_config.otlp.metrics.server_certificate_path
                interval = export_config.otlp.metrics.export_interval
                if transport in ["https", "http"]:
                    if metrics_endpoint is not None:
                        endpoint = f"{metrics_endpoint}"
                    else:
                        api = "/v1/metrics"
                        metrics_port = export_config.otlp.metrics.port or 4318
                        endpoint = f"{transport}://{metrics_host}:{metrics_port}{api}"
                    metrics_exporter = httpOTLPMetricExporter(
                        endpoint=endpoint,
                        certificate_file=metrics_certificate_file if transport == "https" else None,
                        headers=headers
                    )

                elif transport in ["grpc-secure", "grpc"]:
                    if metrics_endpoint is not None:
                        endpoint = f"{metrics_endpoint}"
                    else:
                        metrics_port = export_config.otlp.metrics.port or 4317
                        endpoint = f"{metrics_host}:{metrics_port}"
                    if metrics_certificate_file is not None:
                        try:
                            with open(metrics_certificate_file, 'rb') as f:
                                certificate_data = f.read()
                            credentials = grpc.ssl_channel_credentials(root_certificates=certificate_data)
                        except FileNotFoundError as f:
                            raise FileNotFoundError(
                                f"Certificate file for Metrics is not found. Check the path of the certificate {f}"
                            ) from f
                    metadata = ", ".join([f"{key}={value}" for key, value in headers.items()])
                    metrics_exporter = grpcOTLPMetricExporter(
                        endpoint=endpoint,
                        credentials=credentials if transport == "grpc-secure" else None,
                        headers=metadata,
                        insecure=(transport == "grpc")
                    )

                else:
                    raise NotImplementedError(f"Not a supported transport {transport}")

                metrics_reader = PeriodicExportingMetricReader(metrics_exporter, export_interval_millis=interval * 1_000)
                metrics_provider = MeterProvider(metric_readers=[metrics_reader])
                metrics_meter = metrics_provider.get_meter("NSO Metrics")

                nso_metrics.setup_metric_exporters(metrics_meter)
            else:
                log.warning("NSO metrics are only supported in NSO version 6.0 and later")

            trace.set_tracer_provider(
                TracerProvider(resource=Resource.create({SERVICE_NAME: export_config.otlp.service_name}))
            )
            trace.get_tracer_provider().add_span_processor(
                SimpleSpanProcessor(otlp_exporter)
            )
            tracer = trace.get_tracer("observability-exporter")

        if (
            "ptrace-export-influxdb" not in root.kickers.data_kicker
            or "ptrace-export-otlp" not in root.kickers.data_kicker
        ):
            need_kicker_conf = True

    # install kickers to restart ourselves on configuration changes
    if need_kicker_conf:
        with ncs.maapi.single_write_trans(
            "observability-exporter-kicker", "system"
        ) as t:
            root = ncs.maagic.get_root(t)
            kicker_influx = root.kickers.data_kicker.create("ptrace-export-influxdb")
            kicker_influx.monitor = (
                "/progress:progress/observability-exporter:export/influxdb"
            )
            kicker_influx.kick_node = "./.."
            kicker_influx.action_name = "restart"

            kicker_otlp = root.kickers.data_kicker.create("ptrace-export-otlp")
            kicker_otlp.monitor = (
                "/progress:progress/observability-exporter:export/otlp"
            )
            kicker_otlp.kick_node = "./.."
            kicker_otlp.action_name = "restart"

            # Delete a kicker installed by previous versions of this package
            try:
                root.kickers.data_kicker.delete("ptrace-export-diffset")
                root.kickers.data_kicker.delete("ptrace-export-jaeger")
            # pylint:disable=broad-except
            except Exception:
                pass

            t.apply()

    stream = ptrace.get_pipeline(ptrace.q_getter(qbuf), nso_version)

    # export to otlp collector via opentelemetry library
    if cfg_otlp:
        stream = ptrace.export_otel(tracer, stream)

    # export to influxdb
    if cfg_influxdb:
        stream = ptrace.export_influx_span(metrics_q, stream)
        stream = ptrace.export_influx_span_count(metrics_q, stream)
        stream = ptrace.export_influx_tlock(metrics_q, stream)
        stream = ptrace.export_influx_transaction(metrics_q, stream)

    # dummy consumer
    for event in stream:
        pass


class Main(ncs.application.Application):
    nreader = None
    worker = None

    def setup(self):
        qbuf = multiprocessing.Manager().Queue()
        self.nreader = background_process.Process(
            self,
            NotifReader,
            (qbuf,),
            config_path="/progress:progress/observability-exporter:export/enabled",
            run_during_upgrade=True,
        )
        self.worker = background_process.Process(
            self,
            FTObservabilityExporter,
            (qbuf,),
            config_path="/progress:progress/observability-exporter:export/enabled",
            run_during_upgrade=True,
        )
        background_process.register_restart_action(
            self,
            "ptrace-export-restart",
            self.worker
        )
        background_process.register_restart_action(
            self,
            "ptrace-reader-restart",
            self.nreader
        )
        self.nreader.start()
        self.worker.start()

    def teardown(self):
        self.nreader.stop()
        self.worker.stop()
