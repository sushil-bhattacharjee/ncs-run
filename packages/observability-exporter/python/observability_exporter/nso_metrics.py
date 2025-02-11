from dataclasses import dataclass
import json
import logging
import re
import socket
from typing import Dict

import _ncs
import ncs
from opentelemetry.metrics import Observation


@dataclass
class MetricInfo:
    name: str
    leaf_path: str
    attrs: Dict[str, str]
    description: str


def metric_dur_to_sec(val):
    """Convert a metric value of type string to an int

    There are NSO metrics that are using the XSD schema type for duration, which
    uses a string representation of a duration broken down into years, months,
    days, hours, minutes, seconds and fractions of a second.


    The duration data type is used to specify a time interval.

    The time interval is specified in the following form "PnYnMnDTnHnMnS" where:

    P indicates the period (required)
    nY indicates the number of years
    nM indicates the number of months
    nD indicates the number of days
    T indicates the start of a time section (required if you are going to specify hours, minutes, or seconds)
    nH indicates the number of hours
    nM indicates the number of minutes
    nS indicates the number of seconds

    For example:
    - P1Y2M10DT2H30M means 1 year, 2 months, 10 days, 2 hours and 30 minutes.
    - PT19H means 19 hours.
    - P3DT4H means 3 days and 4 hours.
    - PT0.5S means 0.5 seconds.
    """
    if val is None:
        return 0

    if not isinstance(val, str):
        raise ValueError(f"Value {val} is not a string")

    if not val.startswith("P"):
        raise ValueError(f"Value {val} does not start with P")

    m = re.match(r"P(\d+Y)?(\d+M)?(\d+D)?(T(\d+H)?(\d+M)?(\d+(\.[0-9]+)?S)?)?", val)
    if not m:
        raise ValueError(f"Value {val} is not a valid duration string")

    years = int(m.group(1)[:-1]) if m.group(1) else 0
    months = int(m.group(2)[:-1]) if m.group(2) else 0
    days = int(m.group(3)[:-1]) if m.group(3) else 0
    hours = int(m.group(5)[:-1]) if m.group(5) else 0
    minutes = int(m.group(6)[:-1]) if m.group(6) else 0
    seconds = float(m.group(7)[:-1]) if m.group(7) else 0

    return float(
        (years * 365 * 24 * 60 * 60)
        + (months * 30 * 24 * 60 * 60)
        + (days * 24 * 60 * 60)
        + (hours * 60 * 60)
        + (minutes * 60)
        + seconds)


def get_leafs_paths(node, path=None, list_keys=None):
    """Recursively traverse the metrics config and return a list of leaf paths

    {"data": {
        "tailf-ncs:metric": {
            "sysadmin": {
                "counter": {
                    "counter1": 1234,
                    "counter2": 5678,
                    "datastore": [
                        {
                            "name": "running",
                            "commit": 32,
                        },
                        ...
                    ]
    ...

    becomes:
    [
        (["tailf-ncs:metric", "sysadmin", "counter", "counter1"], []),
        (["tailf-ncs:metric", "sysadmin", "counter", "counter2"]), []),
        (
            ["tailf-ncs:metric", "sysadmin", "counter", "datastore{running}", "commit"],
            [
                (
                    ['tailf-ncs:metric', 'sysadmin', 'counter', 'transaction', 'datastore', 'name'],
                    'running'
                )
            ]
        ),
        ...
    ]
    """
    if path is None:
        path = []

    if list_keys is None:
        list_keys = []

    log = logging.getLogger("get_leafs_paths")

    paths = []
    for k, v in node.items():
        if isinstance(v, int):
            paths.append((path + [k], list_keys))
        elif isinstance(v, str):
            try:
                # Check if value is a number
                int(v)
            except ValueError:
                try:
                    metric_dur_to_sec(v)
                except ValueError:
                    m_path = "/" + "/".join(path)
                    log.warning(f"metric's {m_path}/{k} value: {v} is not a number so it will not be exported ")
            paths.append((path + [k], list_keys))
        elif isinstance(v, list):
            # This is a YANG list, which looks mostly like a dict
            for ylist_elem in v:
                # assumes first element is list key
                list_key = next(iter(ylist_elem.items()))
                ylist_elem.pop(list_key[0])

                # keep track of keys for use as attributes in instruments later
                key = path + [k, list_key[0]]
                list_keys2 = list_keys + [(key, list_key[1])]
                path2 = path + [k + '{' + list_key[1] + '}']

                paths.extend(get_leafs_paths(ylist_elem, path2, list_keys2))
        elif isinstance(v, dict):
            paths.extend(get_leafs_paths(v, path + [k], list_keys))
        else:
            log.warning(f"Unhandled value {path} of type {type(v)} with value {v}")
    return paths


def nso_read_json(th, path):
    """Read config and oper data from NSO in JSON format
    """
    flags = (_ncs.maapi.CONFIG_JSON | _ncs.maapi.CONFIG_OPER_ONLY)
    config_id = th.save_config(flags, path)
    config_sock = socket.socket()
    ncs_ip, ncs_port = th.maapi.msock.getpeername()

    _ncs.stream_connect(config_sock, config_id, 0, ncs_ip, ncs_port)
    data = b""

    while True:
        buf = config_sock.recv(4096)
        if not buf:
            break
        data += buf

    config_sock.close()
    if th.maapi.save_config_result(config_id) != 0:
        raise RuntimeError("Error reading NSO Metrics config")

    return json.loads(data.decode("utf-8"))


def export_metric_value(metric_path, attrs):
    """Callback function to export the value of a metric

    This is called by the OpenTelemetry SDK when a metric is observed.
    """
    log = logging.getLogger("nso_metrics")

    while True:
        with ncs.maapi.single_read_trans("observability-exporter-metrics", "system", db=ncs.OPERATIONAL) as th:
            value = ncs.maagic.get_node(th, metric_path)

        if value is None:
            yield []
        elif isinstance(value, int):
            yield [Observation(value, attrs)]
        elif isinstance(value, str):
            try:
                yield [Observation(int(value), attrs)]
            except ValueError:
                try:
                    yield [Observation(metric_dur_to_sec(value), attrs)]
                except ValueError:
                    log.warning(f"Metric {metric_path} value {value} is not a number")
                    yield []


def get_instrument_name_from_path(path):
    '''Removes first 3 path components to make it shorter and try to keep it below 63 char length,
    limit dictacted by OTLP Metric Framework, remove list keys, if any, and add "nso_" prefix.

    path = ["tailf-ncs:metric", "sysadmin", "counter", "transaction", "datastore{running}", "commit"]
    becomes:
    "nso_transaction_datastore_commit"
    '''
    # /tailf:ncs-metric/sysadmin/counter/transaction/datastore{running}/commit ->
    # "transaction_datastore_commit"
    name = "_".join(path[3:]).replace("-", "_")
    name = "nso_" + re.sub(r"{.*}", "", name)

    return name


def create_instruments(metrics_path, instrument):
    log = logging.getLogger("nso_metrics")
    metrics = {}

    with ncs.maapi.single_read_trans("observability-exporter-metrics", "system", db=ncs.OPERATIONAL) as th:
        metrics_config = nso_read_json(th, metrics_path)
        log.info(f"metrics config\n {metrics_config}")

        for path, list_keys in get_leafs_paths(metrics_config["data"]):
            log.debug(f"Creating instrument for: {path} keys: {list_keys}")
            name = get_instrument_name_from_path(path)

            # if name is too long, skip instrument creation instead of crashing
            if len(name) > 63:
                log.warning(f"Metric {name} does not meet name length requirement, it will not be exported")
                continue

            # use list keys as attributes for metric, instead of havin list key in metric name
            # list_keys = [(['tailf-ncs:metric', 'sysadmin', 'counter', 'transaction', 'datastore', 'name'], 'operational')]
            # ->
            # attrs = {"transaction_datastore_name": "operational"}
            attrs = {}
            for k, v in list_keys:
                attr_name = "_".join(k[3:]).replace("-", "_")
                attrs[attr_name] = v

            # replace path[0] = "tailf-ncs:metric" with "/metric/"
            leaf_path = "/metric/" + "/".join(path[1:])
            # the following paths:
            # /metric/sysadmin/counter/transaction/datastore{running}/commit
            # /metric/sysadmin/counter/transaction/datastore{operational}/commit
            # result in the same metric name:
            # "nso_transaction_datastore_commit" thus only one instrument can be created and
            # in order to distinguish between the two we use the list keys in the paths as labels.
            # This results in one instrument polling from 2 or more paths(callbacks).
            metric = MetricInfo(name, leaf_path, attrs, "NSO Server Metric.")
            if name in metrics:
                metrics[name].append(metric)
            else:
                metrics[name] = [metric]

    # create intruments
    for metric_name, metric_infos in metrics.items():
        callbacks = []
        description = None
        # create callbacks with unique leaf paths
        for metric in metric_infos:
            callbacks.append(export_metric_value(metric.leaf_path, metric.attrs))
            description = metric.description
        instrument(metric_name, callbacks=callbacks, description=description)


def setup_metric_exporters(metrics_meter):
    log = logging.getLogger("nso_metrics")
    log.info("Creating NSO Metrics exporters")

    # Setup instruments for all NSO metrics, though we ignore the counter-rate &
    # gauge-rate since the receiving collector is better suited to calculate the
    # rates anyway.
    create_instruments("/metric/debug/counter", metrics_meter.create_observable_counter)
    create_instruments("/metric/debug/gauge", metrics_meter.create_observable_gauge)
    create_instruments("/metric/developer/counter", metrics_meter.create_observable_counter)
    create_instruments("/metric/developer/gauge", metrics_meter.create_observable_gauge)
    create_instruments("/metric/sysadmin/counter", metrics_meter.create_observable_counter)
    create_instruments("/metric/sysadmin/gauge", metrics_meter.create_observable_gauge)

    log.info("Finished creating NSO Metrics exporters")
