#!/usr/bin/env python3
"""NSO progress-trace processing functions

The overall design here is that of a data processing pipeline based on Python
generators. Multiple processing steps are connected together by passing in a
"stream" object, which is another generator.

A source or spout is used to ingest data into the pipeline. There are two types
of sources:
- csv_reader
  - reads progress-trace events from a CSV file
- get_ptrace_notifs
  - reads progress-trace events from the NSO notification API

Then one or more processing functions can be connected, for example, the
csv_writer consumes progress-trace events from the input stream and writes them
to an output CSV file. Conceptually, you could thus do:

csv_writer("output.csv", csv_reader("input.csv"))

In practice, that isn't possible because the csv_writer expects more columns
than provided by the csv_reader. That sounds weird, shouldn't input columns
always align with output columns? Well, over time there have been changes to
the NSO progress-trace CSV format. In NSO 5.4 there are 18 columns while in NSO
5.3 there were only 15. There is a processing function called uplift_53 which
will do its best to "uplift" events from the NSO 5.3 format to the NSO 5.4
format. However, that is still not enough, csv_writer expects 22 columns, of
which 4 are entirely new ones, namely:
- trace-id
- span-id
- parent
- structured

We think it would be a good idea for NSO to export at least the first three
extra columns but as it doesn't yet do that, we internally add them. This is
done by the function transmogrify(). It can be thought of as uplifting the
events to a next-generation version format. Having these extra columns makes it
trivial to implement an exporter. Look at how small and elegant the
export_otel() function is. This is thanks to the transmogrify() function doing
the heavy lifting of parsing the progress-trace events and presenting it in a
cleaner format. Similarly, the add_tlock_holder() function adds extra virtual
spans (well, emitting start & stop events) for when the transaction lock is
held. That should probably be done by NSO instead and perhaps it will in a
future release.

Note how due to Pythons syntax (which is really quite a common style of
programming language syntax) we build the pipeline "backwards", starting with
defining the output function and as we go deeper into the function arguments we
nest the calls to the earlier parts of the processing pipeline.

For example, this call in Python:

  csv_writer("output.csv", transmogrify(uplift_53(csv_reader("input.csv"))))

would typically be visualized like so:

    +------------+    +----------+    +--------------+    +------------+
    | csv_reader | -> | uplift53 | -> | transmogrify | -> | csv_writer |
    +------------+    +----------+    +--------------+    +------------+

Also note that the pipeline can never fork out. It would be natural to build a
pipeline with two exporters (to OTLP and InfluxDB) connected after the last
non-exporter processing step, like so:

                                              +------+
                                         +--> | otlp |
    +------------+    +--------------+   |    +------+
    | csv_reader | -> | transmogrify | --+
    +------------+    +--------------+   |    +--------+
                                         +--> | influx |
                                              +--------+

However, this is simply not possible while using Python generators the way we
are. Instead, the exporters are chained after each other.

    +------------+    +--------------+    +------+    +--------+
    | csv_reader | -> | transmogrify | -> | otlp | -> | influx |
    +------------+    +--------------+    +------+    +--------+
                                               |             |
                                               v             v
                                        exported data   exported data

This means each exporter must also still act as a generator and yield events so
that we can in fact hook up more things after it.
"""

import csv
import datetime
import logging
import random
import re
import secrets
import socket
import sys
import threading
import time
import uuid

from queue import Empty

import parsedatetime

from influxdb import InfluxDBClient

from opentelemetry import trace as trace_api
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter as grpcOTLPSpanExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter as httpOTLPSpanExporter,
)
from opentelemetry.sdk import trace as trace_sdk
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace.status import Status, StatusCode

# InfluxDBClient uses urllib3 which is quite chatty
logging.getLogger("urllib3").setLevel(logging.WARNING)


def is_ipv6(address):
    """Check if an address is an IPv6 address"""
    try:
        socket.inet_pton(socket.AF_INET6, address)
    except (socket.error, UnicodeEncodeError):
        return False
    return True


# This is a mapping of notif API header fields to the 5.4 CSV format fields
header_notif2csv = {
    "type": "EVENT TYPE",
    "trace_id": "TRACE ID",
    "span_id": "SPAN ID",
    "parent_span_id": "PARENT SPAN ID",
    "timestamp": "TIMESTAMP",
    "duration": "DURATION",
    "usid": "SESSION ID",
    "tid": "TRANSACTION ID",
    "datastore": "DATASTORE",
    "context": "CONTEXT",
    "subsystem": "SUBSYSTEM",
    "phase": "PHASE",
    "service": "SERVICE",
    "service_phase": "SERVICE PHASE",
    "commit_queue_id": "COMMIT QUEUE ID",
    "node": "NODE",
    "device": "DEVICE",
    "device_phase": "DEVICE PHASE",
    "package": "PACKAGE",
    "msg": "MESSAGE",
    "annotation": "ANNOTATION",
    "component": "COMPONENT",
    "state": "STATE",
    "struct": "STRUCTURED",
    "link_trace_id": "LINK TRACE ID",
    "link_span_id": "LINK SPAN ID",
}
# This maps from the 5.4 CSV format to the notif fields
header_csv2notif = {val: key for key, val in header_notif2csv.items()}
# NSO 5.3 calls it "TID" instead of "TRANSACTION ID", so we simply add another
# mapping.
# NOTE: if there are more differences in future, consider separate dict
header_csv2notif["TID"] = "tid"

state_stores = {}


def prettify(event):
    """Prettify event for printing by converting timestamp to human readable time

    This makes it considerable easier to debug things.
    """
    res = event.copy()
    ts = datetime.datetime.fromtimestamp(event["timestamp"] / 1000000)
    res["htimestamp"] = ts.strftime("%Y-%m-%dT%H:%M:%S.%f")
    return res


def prune_oldest(name, state_store, max_length, log):
    while len(state_store) > max_length:
        oldest = next(iter(state_store))
        log.warning(f"Excessive state in {name}. Pruning oldest entry: {oldest}")
        del state_store[oldest]


def raw_csv_reader(filename):
    """Read ptrace events from CSV file

    Internally we deal with things in the notification API format and so we
    need to convert keys and values to the notif API format before outputting.
    """
    log = logging.getLogger("csv_reader")
    with open(filename, encoding="utf-8") as csvfile:
        csvreader = csv.DictReader(csvfile)
        ignored_fields = set()
        # NSO 6.1 includes attributes in progress trace csv file which are rows that come after
        # a span that only have the attribute name and attribute value fields set.
        # start,2023-07-18T16:59:25.535818,,1,2,3,562,126445,running,cli,service-manager,"post-modification",,service,"/slow-service[name='slow11']",,
        # ,,,,,,,,,,,,,service_operation,"create",,
        # ,,,,,,,,,,,,,service_phase,"post-modification",,
        # Since we don't know how many attributes a span will have, we buffer the last seen span,
        # in order to gather all its attributes, until a new span is encountered.
        prev_event = None

        for row in csvreader:
            # Starting in NSO 6.1, csv trace files have rows that only have the
            # ATTRIBUTE NAME and ATTRIBUTE VALUE headers. These rows belong
            # to the last complete row(prev_event). Here we check for such rows by arbitrarily
            # checking that two mandatory fields are not set and that the attribute
            # name field is set. If we want to be more robust, we could check that only
            # the attribute fields are set.
            if row.get("EVENT TYPE", None) == "" and row.get("TIMESTAMP", None) == "" and row.get("ATTRIBUTE NAME", None) is not None:
                if prev_event is None or "attributes" not in prev_event:  # pylint:disable=unsupported-membership-test
                    log.warning(f"Found attribute only row but parent event could not be found or \
                                previous event does not have any attributes. Skipping {row}")
                else:
                    attr_name = row["ATTRIBUTE NAME"]
                    attr_value = row["ATTRIBUTE VALUE"]

                    # check if attribute is already a field we keep track of in an event and
                    # populate that field instead of adding it to attributes dict.
                    # e.g. if attribute is commit_queue_id, then we populate
                    # the event's commit_queue_id entry.
                    if attr_name in header_notif2csv:
                        prev_event[attr_name] = attr_value  # pylint:disable=unsupported-assignment-operation
                    else:
                        prev_event["attributes"][attr_name] = attr_value  # pylint:disable=unsubscriptable-object

                continue

            event = {}
            for key, val in row.items():
                if key in ignored_fields:
                    continue
                # Treat empty strings as None
                if val == "":
                    val = None

                # create attributes dictionary to store all attributes of event
                if key == "ATTRIBUTE NAME":
                    event["attributes"] = {}

                    if val is not None:
                        attr_name = val
                        attr_value = row.get("ATTRIBUTE VALUE", "")

                        # check if attribute is already a field we keep track of in an event and
                        # populate that field instead of adding it to attributes dict.
                        # e.g. if attribute is commit_queue_id, then we populate
                        # the event's commit_queue_id entry.
                        if attr_name in header_notif2csv:
                            event[attr_name] = attr_value
                        else:
                            event["attributes"][attr_name] = attr_value
                    continue

                if key == "ATTRIBUTE VALUE":
                    # we already handled this field above
                    continue

                if key == "TIMESTAMP":
                    # convert timestamp
                    ts = datetime.datetime.strptime(val, "%Y-%m-%dT%H:%M:%S.%f")
                    val = int(ts.strftime("%s%f"))
                elif key == "DURATION":
                    # Internal duration is in microseconds, convert from
                    # seconds. Like we have a transaction taking 122
                    # milliseconds, which is written as 0.122 in the test CSV
                    # file and internally it is 122000 (i.e. µs - microseconds)
                    try:
                        val = float(val) * 1000000.0
                    # pylint:disable=broad-except
                    except Exception:
                        val = None
                elif key in ("TRANSACTION ID", "TID"):
                    # Transaction ID should be an ?integer
                    if val is not None:
                        val = int(val)
                    else:
                        # TID can be empty in certain spans like commit-queue in progres-trace file
                        # The default value of TID is progress events is 0 so we do the same here
                        val = 0
                elif key in ("TRACE ID", "LINK TRACE ID"):
                    if val is None:
                        pass
                    else:
                        try:
                            val = uuid.UUID(val)
                        except ValueError:
                            log.warning(f"Invalid UUID value, ignoring: {val}")
                            val = None

                try:
                    event[header_csv2notif[key]] = val
                except KeyError:
                    log.warning(f"Unknown CSV field: {key}, ignoring")
                    ignored_fields.add(key)
                    continue
            # Insert defaults
            for k in header_notif2csv:
                if k not in event:
                    event[k] = None

            if prev_event is not None:
                yield prev_event

            prev_event = event

        if prev_event is not None:
            yield prev_event


def csv_reader(filename, extra_tags=None):
    """A buffering CSV reader to reorder events for monotonic time

    The raw CSV file might not have events ordered by time. We fix that by
    introducing a buffer and comparing the timestamps of events to the previous
    events timestamp, possibly reordering events in the buffer before yielding.

    We buffer 100 messages, which ought to be enough for correctly ordering
    things since we don't expect the out-of-ordering to be that large.

    We can't guarantee perfectly correct ordering since sometimes multiple
    events have the same timestamp and then ordering between them is not
    possible to determine by the timestamp alone. Understanding the events can
    act as a hint in which order they ought to happen, but we don't attempt to
    implement that here.
    """
    if extra_tags is None:
        extra_tags = {}
    buf = []
    state_stores[id(buf)] = {"name": "csv_reader.buf", "obj": buf}
    csv_stream = raw_csv_reader(filename)
    last_ts = None
    for event in csv_stream:
        event["extra_tags"] = extra_tags
        if last_ts is None:
            buf.append(event)
        elif event["timestamp"] >= last_ts:
            buf.append(event)
        else:
            # need to place this earlier, go backwards in buffer until we
            # find an event that is older than ours
            for pos in range(1, len(buf) + 1):
                if event["timestamp"] >= buf[-pos]["timestamp"]:
                    buf.insert(-(pos - 1), event)
                    break
        last_ts = event["timestamp"]

        if len(buf) > 100:
            yield buf.pop(0)

    # on end of input stream we export the rest in buf
    while len(buf) > 0:
        yield buf.pop(0)


def csv_writer(filename, stream):
    """Write ptrace events to CSV file

    Internally we deal with things in the notification API format and so we
    need to convert certain keys and values to the CSV format before writing.
    """
    csv_fields = [
        "EVENT TYPE",
        "TRACE ID",
        "SPAN ID",
        "PARENT SPAN ID",
        "TIMESTAMP",
        "DURATION",
        "SESSION ID",
        "TRANSACTION ID",
        "DATASTORE",
        "CONTEXT",
        "SUBSYSTEM",
        "PHASE",
        "SERVICE",
        "SERVICE PHASE",
        "COMMIT QUEUE ID",
        "NODE",
        "DEVICE",
        "DEVICE PHASE",
        "PACKAGE",
        "MESSAGE",
        "ANNOTATION",
        "COMPONENT",
        "STATE",
        "STRUCTURED",
        "ATTRIBUTE NAME",
        "ATTRIBUTE VALUE",
        "LINK TRACE ID",
        "LINK SPAN ID",
    ]
    with open(filename, "w", encoding="utf-8", newline="") as csvfile:
        csvwriter = csv.DictWriter(csvfile, csv_fields)
        csvwriter.writeheader()
        for event in stream:
            row = {}
            for key, val in event.items():
                if key == "timestamp":
                    # convert timestamp from internal microsecond timestamp to
                    # "human readable". We use 6 digits of precision, i.e.
                    # microseconds, rather than 3 / milliseconds as NSO writes.
                    ts = datetime.datetime.fromtimestamp(val / 1000000)
                    val = ts.strftime("%Y-%m-%dT%H:%M:%S.%f")
                elif key == "duration":
                    # Internal duration is in microseconds, convert to seconds.
                    # Like we have a transaction taking 122 milliseconds, which
                    # is written as 0.122 in the test CSV file and internally
                    # it is 122000 (i.e. µs - microseconds)
                    try:
                        numval = float(val) / 1000000.0
                        # We prefer full microsecond precision over NSO which
                        # truncates at milliseconds. Hopefully NSO will increase
                        # precision one day in the CSV export (it is
                        # microseconds internally).
                        val = f"{numval:.6f}"
                    # pylint:disable=broad-except
                    except Exception:
                        pass

                try:
                    row[header_notif2csv[key]] = val
                except KeyError:
                    pass

            # add attribute to row
            if len(event.get("attributes", {})) > 0:
                attr_name, attr_val = event["attributes"].popitem()
                row["ATTRIBUTE NAME"] = attr_name
                row["ATTRIBUTE VALUE"] = attr_val

            csvwriter.writerow(row)

            # write extra attributes in separate rows
            for attr_name, attr_val in event.get("attributes", {}).items():
                row.clear()
                row["ATTRIBUTE NAME"] = attr_name
                row["ATTRIBUTE VALUE"] = attr_val
                csvwriter.writerow(row)


def q_putter(stream, q, qlimit):
    """Reads events from a stream and places them on a queue

    If there are more than qlimit number of items already in the queue, we drop
    the event. We log but are rather careful not to log to often since that
    could have a detrimental effect on performance.
    """
    log = logging.getLogger("q_putter")

    dropped = 0
    last = time.time()
    for event in stream:
        if q.qsize() <= qlimit:
            q.put(event)
        else:
            dropped += 1

        if dropped > 0 and time.time() > last + 1:
            log.info(f"Dropped {dropped} events due to full queue")
            last = time.time()
            dropped = 0


def q_getter(q):
    """Read events from a queue and yield as a generator

    Works well together with q_putter!
    """
    while True:
        try:
            # Use non-blocking get_nowait to prevent corrupting queue when this process
            # is restarted via HA fail over or directly using restart action while
            # it waits for an item. In our case after the process restarts, the
            # first event from first trace does not make it to/it's dropped from the queue
            # and leads to trace not being exported as it's incomplete.
            # This is known to happen as stated in the python docs warning
            # https://docs.python.org/3.8/library/multiprocessing.html#multiprocessing.Process.terminate
            # I suspect this only happens when the queue puts this process to sleep while waiting
            # and the process is terminated.
            yield q.get_nowait()
        except Empty:
            time.sleep(0.1)


def get_ptrace_notifs(include_diffset):
    """Generator that yields NSO progress-trace events, coming from the NSO
    notification API that we subscribed to.

    This is a raw interface not intended for direct use. You probably want the
    notifs_reader instead!
    """
    import select
    import ncs
    from _ncs import events
    import _ncs

    log = logging.getLogger("get_ptrace_notifs")
    # set up listener for NSO notifications
    event_sock = socket.socket()
    mask = events.NOTIF_AUDIT + events.NOTIF_PROGRESS + events.NOTIF_USER_SESSION
    if include_diffset:
        mask += events.NOTIF_COMMIT_DIFF
    noexists = _ncs.Value(init=1, type=_ncs.C_NOEXISTS)
    notif_data = _ncs.events.NotificationsData(
        heartbeat_interval=1000,
        health_check_interval=1000,
        stream_name="whatever",
        start_time=noexists,
        stop_time=noexists,
        verbosity=ncs.VERBOSITY_VERY_VERBOSE,
    )
    events.notifications_connect2(
        event_sock, mask, ip="127.0.0.1", port=ncs.NCS_PORT, data=notif_data
    )

    audit_log = {}
    user_sessions = {}
    diffsets = {}
    state_stores[id(audit_log)] = {"name": "get_ptrace_notifs.audit_log", "obj": audit_log}
    state_stores[id(user_sessions)] = {"name": "get_ptrace_notifs.user_sessions", "obj": user_sessions}
    state_stores[id(diffsets)] = {"name": "get_ptrace_notifs.diffsets", "obj": diffsets}

    def process_commit_diff(event):
        tctx = event["commit_diff"]["tctx"]
        diffset = []

        def diff_iter(kp, op, oldv, newv):
            if op == ncs.MOP_CREATED:
                diffline = f"{kp} created"
            elif op == ncs.MOP_DELETED:
                diffline = f"{kp} deleted"
            elif op == ncs.MOP_MODIFIED:
                diffline = f"{kp} = {newv}"
            elif op == ncs.MOP_VALUE_SET:
                diffline = f"{kp} = {newv}"
            elif op == ncs.MOP_MOVED_AFTER:
                diffline = f"{kp} = {newv}"
            elif op == ncs.MOP_ATTR_SET:
                diffline = f"{kp} = {newv}"

            if oldv is not None:
                diffline += f" (old: {oldv})"
            diffset.append(diffline)

        with ncs.maapi.Maapi() as m:
            with m.attach(tctx) as t:
                t.diff_iterate(diff_iter, 0)

        events.diff_notification_done(event_sock, tctx)

        diffsets[tctx.th] = "\n".join(diffset)

    def process_audit(event):
        # save audit log items to our own log keyed by usid. Next
        # time we see a progress trace event for that usid, we pop
        # the entire audit log for that usid and attaches it to that
        # progress trace event. Thus the first progress trace event
        # in a trace will get the audit log attached.
        aev = event["audit"]
        if aev["usid"] not in audit_log:
            audit_log[aev["usid"]] = []
        audit_log[aev["usid"]].append(aev["msg"])

    def process_user_session(event):
        # Keep track of user sessions so we can resolve usid to the
        # username
        uinfo = event["user_sess"]["uinfo"]
        user_sessions[uinfo.usid] = {
            "username": uinfo.username,
            "last_seen": datetime.datetime.now(),
        }

    e2p = {
        events.NOTIF_AUDIT: process_audit,
        events.NOTIF_COMMIT_DIFF: process_commit_diff,
        events.NOTIF_USER_SESSION: process_user_session,
    }

    periodic_cleanup_ticker = 0
    while True:
        (readables, _, _) = select.select([event_sock], [], [], 0.1)
        for readable in readables:
            if readable == event_sock:
                event_dict = events.read_notification(event_sock)

                if event_dict["type"] in e2p:
                    e2p[event_dict["type"]](event_dict)
                    continue
                if event_dict["type"] != events.NOTIF_PROGRESS:
                    raise ValueError(f"Unhandled event type: {event_dict['type']}")

                # from here on, it should only be NOTIF_PROGRESS...
                pev = event_dict["progress"]

                # fill in defaults
                if "duration" not in pev:
                    pev["duration"] = None

                if pev["msg"] == "applying transaction":
                    if pev["tid"] in diffsets:
                        pev["diffset"] = diffsets[pev["tid"]]
                        del diffsets[pev["tid"]]

                # enrich event with resolved username, usid -> username
                if pev["usid"] in user_sessions:
                    pev["username"] = user_sessions[pev["usid"]]["username"]
                    # keep track of when we last used this
                    user_sessions[pev["usid"]]["last_seen"] = datetime.datetime.now()

                # attach audit log to first ptrace event with same usid
                if pev["usid"] in audit_log:
                    pev["audit_log"] = audit_log[pev["usid"]].copy()
                    del audit_log[pev["usid"]]

                type_map = {1: "start", 2: "stop", 3: "info"}
                pev["type"] = type_map[pev["type"]]
                # mapping obtained from confd_dbname
                datastore_map = {
                    0: "no-db",
                    1: "candidate",
                    2: "running",
                    3: "startup",
                    4: "operational",
                    5: "transaction",
                    6: "pre-commit-running",
                    7: "intended",
                }
                pev["datastore"] = datastore_map[pev["datastore"]]

                if "trace_id" in pev:
                    try:
                        pev["trace_id"] = uuid.UUID(pev["trace_id"])
                    except (TypeError, ValueError) as ex:
                        log.warning(
                            f"Could not convert trace_id: '{pev['trace_id']}' to UUID. Skipping"
                            f" trace: {pev['msg']}. Reason: {ex}"
                        )
                        continue
                else:
                    pev["trace_id"] = None

                # Insert defaults
                attrs = pev.get("attributes", {})
                for k in header_notif2csv:
                    if k not in pev:
                        pev[k] = attrs.get(k, None)

                links = pev.get("links", None)
                if links is not None and len(links) > 0:
                    link = pev["links"][0]
                    pev["link_trace_id"] = uuid.UUID(link.trace_id)
                    pev["link_span_id"] = link.span_id

                yield pev

        if readables == []:
            # we feed dummy events if there are no real events, this is so
            # that the downstream consumer of events is always "woken up"
            # with the arrival of a new event, at least every 0.1 seconds
            yield None

        prune_oldest("audit_log", audit_log, 1000, log)
        prune_oldest("diffsets", diffsets, 1000, log)

        periodic_cleanup_ticker += 1
        if periodic_cleanup_ticker == 1000:
            periodic_cleanup_ticker = 0

            # remove user sessions that haven't been looked up, i.e. have been
            # inactive, for 12 hours
            to_remove = {}
            for k, v in user_sessions.items():
                if v["last_seen"] < datetime.datetime.now() - datetime.timedelta(
                    hours=12
                ):
                    to_remove[k] = True
            if len(to_remove) > 0:
                log.warning(f"Excessive user_session state({len(user_sessions)}). Removing sessions inactive for 12 hours.")
            for k in to_remove:
                del user_sessions[k]


def notifs_reader(include_diffset, notif_feeder=None, conf=None):
    """NSO progress-trace reader for the internal notification API

    Use this to get progress trace notifications!

    It introduces a buffer in order to reorder events according to their
    timestamp. This makes parsing considerably easier. It is built to work in
    tandem with the get_ptrace_notifs() function, as it guarantees to yield an
    event every 100ms at a minimum and thus we can keep our guarantee.

    Somewhat surprisingly, the NSO notification API might not yield
    notifications in the order reflected by their timestamps. For example, a
    newly arrived message could have a timestamp that is older than the
    previous message. At least, this is what can be observed in the
    progress-trace CSV files and I believe the same thing happens over the
    notifications API.

    It is considerably easier to parse messages that follow monotonic time, so
    we aim to achieve monotonic time by reordering messages. For this we keep a
    buffer of messages for 100 ms, which gives us a chance to reorder messages.
    It is highly unlikely messages would be reordered across more than 100ms.
    """
    if notif_feeder is None:
        notif_feeder = get_ptrace_notifs(include_diffset)

    buf = []
    state_stores[id(buf)] = {"name": "notifs_reader.buf", "obj": buf}
    last_ts = None
    for event in notif_feeder:
        # yield events older than 100ms
        dt_cut = datetime.datetime.now() - datetime.timedelta(milliseconds=100)
        cut = int(dt_cut.strftime("%s%f"))
        while len(buf) > 0:
            e = buf[0]
            if e["timestamp"] < cut:
                yield e
            else:
                break
            buf.pop(0)

        # skip the dummy events, they just serve to wake us up so we can yield
        # old events
        if event is None:
            continue

        if conf is not None:
            cfg_extra_tags = {}
            for key, data in conf.extra_tags.items():
                if "name" in data and "value" in data:
                    cfg_extra_tags[data["name"]] = data["value"]
            event["extra_tags"] = cfg_extra_tags
        else:
            event["extra_tags"] = {}

        if last_ts is None:
            buf.append(event)
        elif event["timestamp"] >= last_ts:
            buf.append(event)
        else:
            # need to place this earlier, go backwards in buffer until we
            # find an event that is older than ours
            for pos in range(1, len(buf) + 1):
                if event["timestamp"] >= buf[-pos]["timestamp"]:
                    buf.insert(-(pos - 1), event)
                    break

        last_ts = event["timestamp"]

    # when end of feed is reached, we come here, and yield the remaining buffer
    for e in buf:
        yield e


def uplift_53(stream):
    """Uplift progress-trace format from < 5.4 format to 5.4 format

    Large changes were made to the progress trace format in NSO 5.4. The bulk of
    the logic and assumptions on data structures in the Observability Exporter
    are based on the NSO 5.4 format and does not work with older input data.

    uplift_53 is meant to allow analysis of progres trace data from NSO 5.3 and
    earlier versions by first upgrading the format to NSO 5.4. It is however not
    a perfect process, there is a lot of information that is simply not
    available and we can't just guess / generate data out of thin air.

    In essence, NSO 5.3 is not a supported format, but if you have a NSO 5.3 or
    earlier progress trace CSV file and need to analyze it, this is your best
    shot.
    """
    log = logging.getLogger("uplift_53")
    # Names of spans that have balanced start and stop messages
    span_names = {
        "applying FASTMAP reverse diff-set",
        "applying transaction",
        "check configuration policies",
        "check data kickers",
        "create",
        "creating rollback file",
        "grabbing transaction lock",
        "mark inactive",
        "post-modification",
        "pre validate",
        "pre-modification",
        "run dependency-triggered validation",
        "run pre-transform validation",
        "run service",
        "run transforms and transaction hooks",
        "run validation over the changeset",
        "saving FASTMAP reverse diff-set and applying changes",
    }

    # Known info messages. Anything not in here and not matching a list of
    # regexps will result in a warning - possibly something we want to look
    # into.
    known_info = {
        "all commit subscription notifications acknowledged",
        "commit",
        "conflict deleting zombie, adding re-deploy to sequential side effect queue",
        "nano service deleted",
        "prepare",
        "re-deploy merged in queue",
        "re-deploy queued",
        "received commit from all (available) slaves",
        "received prepare from all (available) slaves",
        "releasing device lock",
        "releasing transaction lock",
        "send NED close",
        "send NED commit",
        "send NED connect",
        "send NED get-trans-id",
        "send NED initialize",
        "send NED is-alive",
        "send NED noconnect",
        "send NED persist",
        "send NED prepare",
        "send NED prepare-dry",
        "send NED reconnect",
        "send NED revert",
        "send NED show",
        "send NED show-partial",
        "send NED uninitialize",
        "sending confirmed commit",
        "sending confirming commit",
        "sending edit-config",
        "transaction empty",
        "write-start",
        "zombie deleted",
    }

    known_info_re = {
        "transaction lock queue length:",
        "SNMP connect to",
        "(SNMP|SSH) connecting to",
        "reuse SSH connection",
        "SNMP USM engine id",
        "evaluated behaviour tree pre-condition",
        "component {",
        "delivering commit subscription notifications",
    }

    start_seen = {}
    state_stores[id(start_seen)] = {"name": "uplift_53.start_seen", "obj": start_seen}

    for event in stream:
        durm = re.search(r"(.*) \[([0-9]+) ms\]$", event["msg"])
        if durm:
            if event["duration"] is not None:
                log.warning(f"got duration in msg but duration field set: {event}")
            event["msg"] = durm.group(1)
            event["duration"] = durm.group(2)

        if event["type"] is None:
            if re.match("(entering|leaving) (.+) phase", event["msg"]):
                m = re.match("(entering|leaving) (.+) phase", event["msg"])
                msg_map = {"entering": "start", "leaving": "stop"}
                event["type"] = msg_map[m.group(1)]
                event["msg"] = m.group(2)

            elif event["msg"].endswith("..."):
                event["type"] = "start"
                event["msg"] = event["msg"].replace("...", "")
                start_seen[event["msg"]] = True

            elif re.search(" (done|error|ok)$", event["msg"]):
                event["type"] = "stop"
                event["msg"] = re.sub(" (done|error|ok)$", "", event["msg"])
                if event["msg"] in start_seen:
                    del start_seen[event["msg"]]

            elif event["msg"] in span_names:
                if event["msg"] in start_seen:
                    event["type"] = "stop"
                    del start_seen[event["msg"]]
                else:
                    event["type"] = "start"
                    start_seen[event["msg"]] = True
            else:
                event["type"] = "info"
                if [i for i in known_info_re if re.match(i, event["msg"])]:
                    pass
                elif re.match("evaluated behaviour tree", event["msg"]) or re.match(
                    "component.*state", event["msg"]
                ):
                    pass
                elif re.search("NED error reply", event["msg"]):
                    # TODO: consider info message for now but this should
                    # probably be considered a traceback? I think opentelemetry
                    # has special kind of handling for that kind of thing.
                    pass
                elif event["msg"] not in known_info:
                    log.warning(
                        f"WARNING: unknown message, assuming info: {event['msg']}"
                    )

        if event["msg"] == "service create":
            event["msg"] = "create"

        yield event

        prune_oldest("start_seen", start_seen, 10000, log)


def lift_cq_item(stream):
    """In future releases of NSO commit queue items will be in their
    own trace, as opposed to how they are emitted now as of NSO 6.1 where commit queue spans are
    still under the trace that started the commit. To have consistent
    output data from all current and future versions of NSO we lift up commit
    queue items into their own trace by using their commit_queue_id as the identifier and
    generating a new trace-id.
    We also keep track of their original parent trace and span so that we can create
    a bidirectional link between them and make it easier to find out from what trace and span
    the commit queue item originated.

    For exmple, a commit queue item, all spans with a COMMIT QUEUE ID, would be lifted from the following trace:
    EVENT TYPE,TRACE ID,SPAN ID,PARENT SPAN ID,TIMESTAMP,DURATION,SESSION ID,TRANSACTION ID,DATASTORE,CONTEXT,SUBSYSTEM,PHASE,SERVICE,SERVICE PHASE,COMMIT QUEUE ID,NODE,DEVICE,DEVICE PHASE,PACKAGE,MESSAGE,ANNOTATION,COMPONENT,STATE,STRUCTURED # noqa: E501
    start,644fd37a-fdb9-44ce-a1ed-1b9f76a4ca7c,122e3ca126ceb030,,2023-06-24T05:38:18.427922,,153,432,running,rest,,,,,,,,,,restconf edit,,,,{} # noqa: E501
    start,644fd37a-fdb9-44ce-a1ed-1b9f76a4ca7c,0c7b128d5429901e,122e3ca126ceb030,2023-06-24T05:38:18.433373,,153,432,running,rest,,,,,,,,,,applying transaction,,,,{} # noqa: E501
    start,644fd37a-fdb9-44ce-a1ed-1b9f76a4ca7c,e77e04f6470aef8d,0c7b128d5429901e,2023-06-24T05:38:18.433469,,153,432,running,rest,,validate,,,,,,,,validate,,,,{} # noqa: E501
    ...
    start,644fd37a-fdb9-44ce-a1ed-1b9f76a4ca7c,681b3381a66e3207,0c7b128d5429901e,2023-06-24T05:38:18.500855,,153,432,running,rest,,commit,,,,,,,,commit,,,,{} # noqa: E501
    start,30dffc5e-09ff-4928-b624-91de96aa5576,ed86e69603b8975b,,2023-06-24T05:38:18.503685,,153,432,running,rest,,,,,1687585098490,,,,,commit queue,,,,{} # noqa: E501
    start,30dffc5e-09ff-4928-b624-91de96aa5576,55a05c3dc0e91d6f,ed86e69603b8975b,2023-06-24T05:38:18.503685,,153,432,running,rest,,,,,1687585098490,,,,,commit queue waiting,,,,{} # noqa: E501
    stop,30dffc5e-09ff-4928-b624-91de96aa5576,55a05c3dc0e91d6f,ed86e69603b8975b,2023-06-24T05:38:18.550069,0.046384,153,432,running,rest,,,,,1687585098490,,,,,commit queue waiting,,,,{} # noqa: E501
    start,30dffc5e-09ff-4928-b624-91de96aa5576,0e5e7365233e9468,ed86e69603b8975b,2023-06-24T05:38:18.550069,,153,432,running,rest,,,,,1687585098490,,,,,commit queue executing,,,,{} # noqa: E501
    info,644fd37a-fdb9-44ce-a1ed-1b9f76a4ca7c,681b3381a66e3207,0c7b128d5429901e,2023-06-24T05:38:18.551185,,153,432,running,rest,,,,,,,,,,releasing transaction lock,,,,{} # noqa: E501
    info,644fd37a-fdb9-44ce-a1ed-1b9f76a4ca7c,681b3381a66e3207,0c7b128d5429901e,2023-06-24T05:38:18.551336,,153,432,running,rest,,,,,,,,,,transaction lock queue length: 3 (executing transaction 434),,,,{} # noqa: E501
    stop,644fd37a-fdb9-44ce-a1ed-1b9f76a4ca7c,681b3381a66e3207,0c7b128d5429901e,2023-06-24T05:38:18.551429,0.050574,153,432,running,rest,,commit,,,,,,,,commit,,,,{}, # noqa: E501
    stop,644fd37a-fdb9-44ce-a1ed-1b9f76a4ca7c,0c7b128d5429901e,122e3ca126ceb030,2023-06-24T05:38:18.551464,0.118091,153,432,running,rest,,,,,,,,,,applying transaction,,,,{} # noqa: E501
    start,30dffc5e-09ff-4928-b624-91de96aa5576,d74442cf7afbba5e,0e5e7365233e9468,2023-06-24T05:38:18.564768,,153,432,running,rest,,,,,1687585098490,,XR00,,,calculating southbound diff,,,,{} # noqa: E501
    stop,644fd37a-fdb9-44ce-a1ed-1b9f76a4ca7c,122e3ca126ceb030,,2023-06-24T05:38:18.569248,0.141326,153,432,running,rest,,,,,,,,,,restconf edit,,,,{} # noqa: E501

    to a new trace. Notice the new TRACE ID and the first span having a LINK TRACE ID and LINK SPAN ID field,
    which links to the original trace and the original parent span of the commit queue item, respectively.

    EVENT TYPE,TRACE ID,SPAN ID,PARENT SPAN ID,TIMESTAMP,DURATION,SESSION ID,TRANSACTION ID,DATASTORE,CONTEXT,SUBSYSTEM,PHASE,SERVICE,SERVICE PHASE,COMMIT QUEUE ID,NODE,DEVICE,DEVICE PHASE,PACKAGE,MESSAGE,ANNOTATION,COMPONENT,STATE,STRUCTURED,LINK TRACE ID,LINK SPAN ID # noqa: E501
    start,30dffc5e-09ff-4928-b624-91de96aa5576,ed86e69603b8975b,,2023-06-24T05:38:18.503685,,153,432,running,rest,,,,,1687585098490,,,,,commit queue,,,,{},644fd37a-fdb9-44ce-a1ed-1b9f76a4ca7c,681b3381a66e3207 # noqa: E501
    start,30dffc5e-09ff-4928-b624-91de96aa5576,55a05c3dc0e91d6f,ed86e69603b8975b,2023-06-24T05:38:18.503685,,153,432,running,rest,,,,,1687585098490,,,,,commit queue waiting,,,,{},, # noqa: E501
    stop,30dffc5e-09ff-4928-b624-91de96aa5576,55a05c3dc0e91d6f,ed86e69603b8975b,2023-06-24T05:38:18.550069,0.046384,153,432,running,rest,,,,,1687585098490,,,,,commit queue waiting,,,,{},, # noqa: E501
    start,30dffc5e-09ff-4928-b624-91de96aa5576,0e5e7365233e9468,ed86e69603b8975b,2023-06-24T05:38:18.550069,,153,432,running,rest,,,,,1687585098490,,,,,commit queue executing,,,,{},, # noqa: E501
    ...
    stop,30dffc5e-09ff-4928-b624-91de96aa5576,0e5e7365233e9468,ed86e69603b8975b,2023-06-24T05:38:22.268610,3.718541,153,432,running,rest,,,,,1687585098490,,,,,commit queue executing,,,,{},, # noqa: E501
    stop,30dffc5e-09ff-4928-b624-91de96aa5576,ed86e69603b8975b,,2023-06-24T05:38:22.268610,3.764925,153,432,running,rest,,,,,1687585098490,,,,,commit queue,,,,{},, # noqa: E501

    See ENG-32275
    """

    log = logging.getLogger("lift_cq_item")
    cq_items = {}
    state_stores[id(cq_items)] = {"name": "lift_cq_item.cq_items", "obj": cq_items}

    for original_event in stream:
        if original_event["commit_queue_id"] is not None:
            cq_key = original_event["commit_queue_id"]
            # use copy of event to not interfere with build up of trace in pipeline
            # specifically, the add_cq_span function re-uses the same event to emit
            # start and stop events with different messages, so changing the trace_id of the
            # start event here will also "change" it for the stop event before being emitted,
            # which will cause inconsistent trace_id warning in the transmogrify function.
            new_event = original_event.copy()

            if cq_key not in cq_items:
                cq_items[cq_key] = {"trace_id": uuid.uuid4(), "cq_root_msg": new_event["msg"]}
                # save trace and span ids of parent in original trace to create link
                if original_event["link_trace_id"] is None:
                    new_event["link_trace_id"] = original_event["trace_id"]
                    new_event["link_span_id"] = original_event["parent_span_id"]
                new_event["parent_span_id"] = None

            cq_item = cq_items[cq_key]
            new_event["trace_id"] = cq_item["trace_id"]

            # clean up state when stop span of commit queue root is received
            if new_event["type"] == "stop" and new_event["msg"] == cq_item["cq_root_msg"]:
                new_event["parent_span_id"] = None
                del cq_items[cq_key]

            yield new_event
        else:
            yield original_event

        prune_oldest("cq_items", cq_items, 100000, log)


def transmogrify(stream):
    """Transmogrify the current (5.4) NSO progress-trace format into its ideal
    shape and form.

    OpenTelemtry is considered as the ideal tracing format here, since that is
    the format we use for export and what is accepted and adopted as internal
    representation in many common observability systems. NSO 6.1 natively
    implements the same concepts, which means that:
    - there is always a trace-id
    - all spans have a span-id
    - spans have a parent-span-id to denote parent / child relations

    For NSO 6.1, we do not need the transmogrify function at all and it is not
    used as part of the processing pipeline. transmogrify is used for NSO 6.0
    and earlier, where we need to figure our parent / child relationships
    outside of NSO and that is the task of the transmogrify function.

    Without a native span-id and parent-span-id in our input data, we have to
    match up the start and stop events to form spans and keep track of spans in
    order to determine the parent / child relationship. We use the transaction
    id (tid), user session id (usid) and "dimension" for organizing the data, so
    for pairing up start / stop events or considering a parent span, we only
    look at spans with the same tid / usid / dimension. Events appear to be
    happening sequentially within a dimension and so it becomes a tractable
    problem to determine parent / child relationships in a deterministic way.

    There is generally a 1:1 mapping between NSO transactions and our traces.
    This is however not true for trans-in-trans, i.e. sub-transactions, most
    commonly used for service create where we map the child transaction to the
    same trace-id as the parent transaction. This means progress-trace spans
    emitted inside of create will belong to the same trace as for the whole
    transaction that created a service, which looks much better!
    """
    log = logging.getLogger("transmogrify")
    # this contains depth and spans keyed per tid
    traces = {}
    state_stores[id(traces)] = {"name": "transmogrify.traces", "obj": traces}

    # keep track of currently running service create spans
    srv_create = {}
    state_stores[id(srv_create)] = {"name": "transmogrify.srv_create", "obj": srv_create}
    # flag is True if a trace should be buffered until completion if
    # a trace_id was expected to be set by NSO but was not
    buffering = False
    # prevent unnecessary buffering of traces when spans will never have a
    # trace_id set by NSO. This can happen in NSO 5.4 and when trace-id is
    # disabled in ncs.conf. Since we cannot query ncs.conf trace-id configuration,
    # we use basic heuristics to figure out if trace_id should be expected in trace
    # by looking at the spans and if we find a trace_id was set by NSO, then we enable
    # this flag for the remainder of the runtime, which will then correctly buffer traces with
    # no trace_id when it should have had a trace_id set by NSO
    expected_trace_id = False

    def open_spans(trace):
        """Check if there are open spans in a trace

        Returns True if there are and False if not.
        """
        for dim in trace["dimensions"].values():
            if (len(dim["open_spans"]) == 1 and trace["fake_root"] is not None):
                fake_root_msg = trace["fake_root"]["msg"]
                if (None, fake_root_msg) in dim["span_by_eid"]:
                    # ignore counting the fake root span
                    pass
                else:
                    return True
            else:
                if len(dim["open_spans"]) > 0:
                    return True

        return False

    def inner(stream):
        for event in stream:
            nonlocal buffering, expected_trace_id
            if not expected_trace_id and (event['type'] == "start" and event['trace_id']):
                expected_trace_id = True

            buffering = False
            trace = None
            tkey = (event["usid"], event["tid"])
            # an event is uniquely identified by the subsystem and the message
            eid = (event["subsystem"], event["msg"])
            # If this is the first time we see this tid, we do a quick check to try
            # to determine if this is a trans-in-trans used for service create, in
            # which case we attempt to map it to its parent transaction trace id
            # instead! We do this by checking the service attribute and if it
            # exists in the srv_create dict, it means we are currently in the
            # create span of that service, in which case this event must be part of
            # a service create trans-in-trans!
            if tkey not in traces and event.get("service", None) in srv_create:
                tkey = srv_create[event["service"]]

            # Check if it's an existing transaction we know about
            if tkey not in traces:
                # create struct for our transaction, keyed by tid

                # During normal configuration transactions the first span, i.e. the
                # root span, we get from NSO is "applying transaction". This
                # isn't really the start of a transaction but rather towards the
                # end, when the client is attempting to commit the transaction.
                # In the OpenTelemtry (and others) trace format, there must be a
                # single root span in a trace. It is not allowed to have
                # multiple top level spans. committing a transaction might fail,
                # for example due to data validation failures, after which the
                # commit can be attempted again. These show up as multiple
                # "applying transaction" spans. In order to fulfill the
                # single-root-span promise, we create a fake root span for the
                # transaction which wraps the "applying transaction" spans. If
                # NSO one days emits more spans earlier on in a transaction,
                # this could possibly be removed, see ENG-25179
                # For other events, like re-deploy, there is always only one
                # such event, and thus one top level span in a trace. Some
                # actions, like check-sync and sync-from allows running in
                # parallel across multiple devices and thus need to be wrapped
                # in a fake root span in which case we reuse the same span name.
                # If a sync-from is reattempted it happens in a separate
                # transaction and thus does not require wrapping in a fake root
                # span.
                if event["msg"] in {"restconf edit", "restconf get", "re-deploy"}:
                    # no need to wrap these in fake root transactions
                    pass
                elif event["tid"] == -1:
                    pass
                else:
                    # Create virtual root span
                    root_span = event.copy()
                    root_span["type"] = "start"
                    if root_span["trace_id"] is None:
                        root_span["trace_id"] = uuid.uuid4()
                        buffering = expected_trace_id
                    root_span["span_id"] = secrets.token_hex(8)
                    root_span["parent_span_id"] = None
                    # Generally synthetic root span inherit the name of the
                    # natural root span(s), though appended with ' root' in
                    # order to avoid naming collisions. For example:
                    # + check-sync root   (new synthetic root span)
                    # +-- check-sync device=FOO
                    # +-- check-sync device=BAR
                    #
                    # Note how a check-sync operation on multiple devices would
                    # violate the single root span constraint, which is why we
                    # wrap it up. For check-sync on a single device, the
                    # synthetic root span serves no purpose but we prefer to be
                    # consistent.
                    #
                    # For normal transactions, i.e. 'applied transaction' we
                    # name the synthetic root span 'transaction'.
                    # + transaction root  (new synthetic root span)
                    # +-- applying transaction
                    #   +-- ...
                    # +-- applying transaction  (if first one failed)
                    #   +-- ...
                    #
                    # Sometimes we see bare info events are the root in
                    # operational transactions and create an 'oper transactional
                    # root' span for that.
                    if event["msg"] == "applying transaction":
                        root_span["msg"] = "transaction"
                    elif (event["type"] == "info"
                          and event["datastore"] == "operational"):
                        root_span["msg"] = "oper transaction"
                    # Append ' root' to make it unique and not collide with the
                    # span we just copied.
                    root_span["msg"] += " root"
                    root_span["struct"] = {}
                    yield root_span
                    traces[tkey]["fake_root"] = root_span

            trace = traces.get(tkey, None)
            yield event
            if trace is None:
                continue

            # We determine the end of the transaction when there are no locks held
            # and there are no open spans. This is so far the best heuristics we
            # could come up with.
            stop = False
            # NOTE: we check len(locks) first since it is cheaper than open_spans()
            if len(locks) == 0 and not open_spans(trace):
                stop = True
            elif event["type"] == "stop" and eid == trace["start_eid"]:
                log.warning(
                    f"End of transaction detected by start_eid ({eid}) but not at depth 0 / "
                    f"locks kept for {tkey}\n{our_dim['span_by_eid']}"
                )
                stop = True

            if stop:
                if trace["fake_root"] is not None:
                    root_span = trace["fake_root"]
                    # Create event for the root span stop event
                    root_stop = root_span.copy()
                    root_stop["type"] = "stop"
                    root_stop["timestamp"] = event["timestamp"]
                    root_stop["duration"] = event["timestamp"] - root_span["timestamp"]
                    yield root_stop
                del traces[tkey]

    for event in inner(stream):
        tkey = (event["usid"], event["tid"])
        eid = (event["subsystem"], event["msg"])
        # If this is the first time we see this tid, we do a quick check to try
        # to determine if this is a trans-in-trans used for service create, in
        # which case we attempt to map it to its parent transaction trace id
        # instead! We do this by checking the service attribute and if it
        # exists in the srv_create dict, it means we are currently in the
        # create span of that service, in which case this event must be part of
        # a service create trans-in-trans!
        if tkey not in traces and event.get("service", None) in srv_create:
            tkey = srv_create[event["service"]]

        # Check if it's an existing transaction we know about
        if tkey not in traces:
            # Use existing trace_id if we have one (which should already be a
            # uuid.UUID object), otherwise generate new one...
            if event["trace_id"] is not None:
                trace_id = event["trace_id"]
            else:
                trace_id = uuid.uuid4()
                buffering = expected_trace_id

            traces[tkey] = {
                "trace_id": trace_id,
                "buffering": buffering,
                "cq_done": False,
                "dimensions": {},
                "span_buffer": [],
                "locks": {},
                "fake_root": None,
                "start_eid": None,
                "first_event": event.copy(),
                "top_dim": (event["commit_queue_id"], event["device"]),
            }
        trace = traces[tkey]
        if trace["buffering"]:
            trace["span_buffer"].append(event)

        locks = trace["locks"]
        if event["trace_id"] is None:
            event["trace_id"] = trace["trace_id"]
        event["struct"] = {}

        if event["trace_id"] != trace["trace_id"]:
            # There is a known inconsistency where re-deploy queued info events
            # have a trace-id but the encompassing "executing side effect" span
            # does not have a trace-id set at all and so we end up generating
            # one, which is then different from the trace-id on the info event.
            # To achieve consistent trace-ids, we rewrite the one on the info
            # event. This is bad and we should not rewrite but we need to figure
            # out if the lack of trace-id on the span is a bug or not before
            # investing more time in fixes and workarounds.
            if re.match("re-deploy queued", event["msg"]):
                event["trace_id"] = trace["trace_id"]
            elif trace["buffering"]:
                # It can be the case that a trace does not initially have its
                # trace_id set by NSO so we generate one, but later spans in the trace
                # might have their trace_id set by NSO.
                # e.g. a sync-from action with no trace_id set by NSO that also has a transaction
                # in between which does have trace_id set by NSO.
                # So we make use of the NSO set trace_id and bubble it up to all other
                # spans in the trace that came before this span that way we use the same trace_id
                # for the entire trace. If more than one trace_id set by NSO is found in single trace
                # then the last one found will be used for the entire trace.
                trace["trace_id"] = event["trace_id"]
                for span in trace["span_buffer"]:
                    span["trace_id"] = event["trace_id"]
            else:
                log.warning(f"Inconsistent trace_id within matched trace for event {event}")

        # we use the concept of a 'dimension' to separate spans
        # NSO will essentially do sequential processing of most things except
        # for device interaction. Most device interaction is done concurrently
        # between devices, though sequentially for each device. Thus, when
        # tracking events and their structure, we use the device name as a
        # "dimension" to keep concurrent operations apart. For non-device
        # spans, we use an empty string '' as our dimension.
        dim_key = (event["commit_queue_id"], event["device"])

        if dim_key not in trace["dimensions"]:
            trace["dimensions"][dim_key] = {"open_spans": {}, "span_by_eid": {}}
        our_dim = trace["dimensions"][dim_key]

        is_root_span = False
        if event["parent_span_id"] is None:
            if len(trace["dimensions"][dim_key]["open_spans"]) == 0:
                # no open spans in dimension? means we are at depth 0 in our dim...

                # are we the top dimension?
                if dim_key == trace["top_dim"]:
                    is_root_span = True
                    event["parent_span_id"] = None
                    parent_spans = {}
                #                elif event.get('commit_queue_id', '') != '':
                #                    # Special handling for commit queue items, which we
                #                    # explicitly attach to the fake 'commit queue' span we have
                #                    # emitted.
                #                    parent_spans = {trace['cq_span']['span_id']: trace['cq_span']}
                else:
                    # Check if it belongs to open commit queue dim
                    # otherwise, attach to the top dimension
                    cq_dim_key = (event["commit_queue_id"], None)
                    cq_dim = trace["dimensions"].get(cq_dim_key, None)
                    if event["commit_queue_id"] is not None and cq_dim is not None \
                            and len(cq_dim["open_spans"]) != 0:
                        parent_spans = trace["dimensions"][cq_dim_key]["open_spans"]
                    else:
                        parent_spans = trace["dimensions"][trace["top_dim"]]["open_spans"]
            else:
                # there are open spans in our dimension
                parent_spans = trace["dimensions"][dim_key]["open_spans"]

            for span_id, span in reversed(list(parent_spans.items())):
                if span["msg"] in {"holding service write lock", "holding transaction lock"}:
                    # we don't think the holding transaction lock span is eligible
                    # to be a parent of other spans, thus ignoring it here
                    continue
                event["parent_span_id"] = span_id
                break

            if not is_root_span and event["parent_span_id"] is None:
                log.error(f"Unable to identify parent for {event}")

        # keep track of transaction lock
        if event["msg"] == "holding transaction lock":
            if event["type"] == "start":
                if "" in locks:
                    log.warning(
                        f"start/holding transaction lock: tlock already held - {event}"
                    )
                locks[""] = True
            elif event["type"] == "stop":
                if "" not in locks:
                    log.error(
                        f"stop/holding transaction lock: tlock not held - {event}"
                    )
                locks.pop("", None)

        # keep track of create per service
        if event["msg"] == "create" and event["service"] is not None:
            if event["type"] == "start":
                srv_create[event["service"]] = tkey

            if event["type"] == "stop":
                srv_create.pop(event["service"], None)

        if event["type"] == "start":  # start of a span
            # sanity
            if eid in our_dim["span_by_eid"]:
                log.warning(
                    f"span dim collision on {dim_key}/{eid}\n{prettify(event)}\nother event: "
                    f"{prettify(our_dim['span_by_eid'][eid])}"
                )
            # keep a list of open spans in this transaction
            our_dim["open_spans"][event["span_id"]] = event
            our_dim["span_by_eid"][eid] = event
        elif event["type"] == "stop":  # end of a span
            if (
                event["msg"] == "taking device lock"
                and "annotation" in event
                and event["annotation"] is None
            ):
                locks[event["device"]] = True
            elif event["msg"] == "holding device lock":
                locks.pop(event["device"], None)

            # attempt matching up stop event to start event through span_id, if
            # set, otherwise we do it by eid (event-id)
            # TODO: guess the span_id match should not be by dimension but per
            # trace?
            if event["span_id"] is not None:
                if event["span_id"] in our_dim["open_spans"]:
                    match = our_dim["open_spans"][event["span_id"]]
                else:
                    log.warning(f"Unable to match up stop event based on span_id {event}")
                    log.debug(trace)
                    continue
            else:
                try:
                    match = our_dim["span_by_eid"][eid]
                except KeyError:
                    log.warning(f"Unable to match up stop event - discarding: {event}")
                    continue
                event["span_id"] = match["span_id"]

            event["parent_span_id"] = match["parent_span_id"]

            our_dim["open_spans"].pop(event["span_id"], None)
            our_dim["span_by_eid"].pop(eid, None)

            # Last span of trace
            if event["span_id"] == trace["first_event"]["span_id"]:
                # The spans of a trace that did not initially have its trace_id set
                # by NSO are held until the last span of the trace is seen
                # before we export them because we either use
                # a generated trace_id or re-use a trace_id set by NSO in span
                # for the whole trace.
                if trace["buffering"]:
                    for span in trace["span_buffer"]:
                        yield span

        elif event["type"] == "info":
            if event["msg"] == "releasing device lock":
                locks.pop(event["device"], None)

            match = None
            # match up with last open span for our dimension
            if len(our_dim["open_spans"]) > 0:
                match_key = list(our_dim["open_spans"].keys())[-1]
                match = our_dim["open_spans"][match_key]

            if match is None:
                if re.match("re-deploy queued", event["msg"]):
                    # silently ignore, we don't know what to do with this event
                    pass
                else:
                    log.warning(
                        "Unable to associate info event with last open span. Event: "
                        f"{prettify(event)}"
                    )
            else:
                # inherit span_id & parent span from matched up span
                event["span_id"] = match["span_id"]
                event["parent_span_id"] = match["parent_span_id"]
        else:
            raise ValueError(f"Unhandled progress-trace event type {event}")

        if not trace["buffering"]:
            yield event

        # bounded state within trace
        # These seems so outrageously wrong that something must be off, let's
        # give up on this trace entirely
        if len(trace["locks"]) > 100000:
            log.warning(f"Excessive trace[lock] state. Discarding trace: {trace}")
            del traces[tkey]

        if len(trace["dimensions"][dim_key]["open_spans"]) > 100000:
            log.warning(f"Excessive trace[dimensions][dim_key][open_spans] state. Discarding trace: {trace}")
            del traces[tkey]

        if len(trace["dimensions"][dim_key]["span_by_eid"]) > 100000:
            log.warning(f"Excessive trace[dimensions][dim_key][span_by_eid] state. Discarding trace: {trace}")
            del traces[tkey]

        if len(trace["dimensions"]) > 100000:
            log.warning(f"Excessive trace[dimensions] state. Discarding trace: {trace}")
            del traces[tkey]

        if len(trace["span_buffer"]) > 10000:
            log.warning(f"Excessive trace[span_buffer] state. Discarding trace: {trace}")
            del traces[tkey]

        # Bounded state for all traces
        prune_oldest("traces", traces, 5000, log)
        prune_oldest("srv_create", srv_create, 5000, log)

    # When we exit the main iteration of the stream, it means we've processed
    # all events. We shouldn't have any state left!
    # If we are started in the middle of a trace stream so that we've missed
    # some start messages or are cut off before we see some stop messages, we
    # might have leftover state. When run as a NSO package we might want to be
    # more tolerant to such scenarios and simply ignore these failures.
    if len(traces) > 0:
        log.warning(
            f"Unclean transaction state at end. {len(traces)} trailing transactions"
        )
        log.debug(traces)


def add_cq_spans(stream):
    """Rewrite commit queue info events into proper spans
    Between "waiting" and "executing" we create "commit-queue waiting".
    Between "executing" and "completed" we create "commit-queue executing"
    We possibly error out during "waiting" through the "deleted" event or during
    "executing" through the "failed" event, which will then be propagated
    through the event annotation.

    See ENG-27214
    """
    log = logging.getLogger("add_cq_spans")
    cq_states = {}
    state_stores[id(cq_states)] = {"name": "add_cq_spans.cq_states", "obj": cq_states}

    for event in stream:
        tkey = (event["usid"], event["tid"])
        if event.get("commit_queue_id", None) is not None:
            if tkey not in cq_states:
                cqr = event.copy()
                cqr["type"] = "start"
                cqr["msg"] = "commit queue"
                yield cqr.copy()
                cq_states[tkey] = {"cur_span": None, "cq_root": cqr}
            cqs = cq_states[tkey]
            cur = cqs["cur_span"]

            if event["msg"] in ("executing", "locked", "waiting"):
                # Rewrite info event to a span by changing event type to start
                # and later by emitting another stop event. Also give span a
                # more descriptive name by including commit queue in the name.
                event["type"] = "start"
                event["msg"] = "commit queue " + event["msg"]

                # check for currently open span and close it
                if cur:
                    if event["msg"] == cur["msg"]:
                        continue
                    # emit the saved stop span for the currently open span
                    # fill in duration first (based on the existing timestamp of the
                    # stop_span, which is really the timestamp of the start event
                    # for this span) and then rewrite the timestamp to the current
                    # events timestamp
                    # cqs holds the start event, so rewrite it to become the stop event
                    cur["type"] = "stop"
                    cur["duration"] = event["timestamp"] - cur["timestamp"]
                    cur["timestamp"] = event["timestamp"]
                    yield cur

                cqs["cur_span"] = event.copy()

            elif event["msg"] in ("completed", "deleted", "failed"):
                # this is the last message from this CQ item, either completing
                # it or failing / deleting it. We want to close the currently
                # open span, either "waiting" or "executing", by sending a stop
                # message..

                if cur is None:
                    log.warning(
                        f"Expected to have a start span stored in cqs for {event}"
                    )
                    continue

                # if deleted or failed, we set the annotation
                if event["msg"] in ("deleted", "failed"):
                    cur["annotation"] = event["msg"]

                cur["type"] = "stop"
                # fill in duration
                cur["duration"] = event["timestamp"] - cur["timestamp"]
                # emit the saved stop span for the currently open span
                cur["timestamp"] = event["timestamp"]
                yield cur
                cqr = cqs["cq_root"]
                cqr["type"] = "stop"
                # fill in duration
                cqr["duration"] = event["timestamp"] - cqr["timestamp"]
                # emit the saved stop span for the currently open span
                cqr["timestamp"] = event["timestamp"]
                yield cqr
                # clean up any state
                del cq_states[tkey]
                # do not emit this info event since the above stop span is the
                # new ending event
                continue

        yield event

        prune_oldest("cq_states", cq_states, 100000, log)


def fix_dev_snapshot(stream):
    """Correct device snapshot messages

    This is fixed in NSO 5.5 and later through ENG-24451
    """
    for event in stream:
        m = re.match("create device (.+) snapshot", event["msg"])
        if m:
            event["msg"] = "create snapshot"
            event["device"] = m.group(1)
        yield event


def fix_ha_config_msg(stream):
    """Event message for HA node configuration for start and stop events are not matching.
    Change in message descriptions in 5.x and 6.x due to bias-free language platform fix in 6.x.
        master(5.x) --> primary(6.x)
        slave(5.x)  --> secondary(6.x)
    In NSO 5.x,
        Start event message is "New slave configuration db will be synched"
        Stop event message is "New slave configuration has been synched"
    In NSO 6.0/6.0.x,
        Start event message is "New secondary configuration db will be synched"
        Stop event message is "New secondary configuration has been synched"

    This function will set the original start/stop event's message to the following:
    For NSO 5.x, it will be set as "New slave configuration sync operation".
    For NSO 6.0/6.0.x, it will be set as "New secondary configuration sync operation".

    This issue is not seen in 6.1 and higher.
    """
    for event in stream:
        m = re.match("New (slave|secondary) configuration.*synched$", event["msg"])
        if m:
            event["msg"] = "New " + m.group(1) + " configuration sync operation"
        yield event


def fix_oper_commit_stop(stream):
    """Correct transactions on operational datastore that are missing the
    commit stop event

    We track transactions on the operational datastore and if we haven't seen a
    commit message before the 'applying transaction' stop message, we emit a
    commit stop event. The generated fake event gets the same timestamp as the
    'applying transaction' stop message. We only track transactions starting
    with an 'applying transaction' message. There are also other transactions
    on the operational datastore, like doing connect on a device but as such
    transactions don't have a commit step we don't track them.

    See ENG-25236
    """
    log = logging.getLogger("fix_oper_commit_stop")
    transactions = {}
    state_stores[id(transactions)] = {"name": "fix_oper_commit_stop.transactions", "obj": transactions}
    for event in stream:
        tkey = (event["usid"], event["tid"])
        if (
            "datastore" in event
            and event["datastore"] == "operational"
            and event["type"] == "start"
            and event["msg"] == "applying transaction"
        ):
            if tkey in transactions:
                log.warning(
                    "Seeing start of transaction [start/applying transaction] but transaction "
                    f"already in state for {event['tid']}"
                )
            # track operational transactions starting with an 'applying
            # transaction' event
            transactions[tkey] = {"commit_start_event": None, "commit_stop_seen": False}

        if tkey in transactions:
            if event["type"] == "start" and event["msg"] == "commit":
                transactions[tkey]["commit_start_event"] = event
            elif event["type"] == "stop" and event["msg"] == "commit":
                transactions[tkey]["commit_stop_seen"] = True
            elif event["type"] == "stop" and event["msg"] == "applying transaction":
                # Not all transactions have a commit event. The typical example
                # being if they are empty. At the end of the transaction, i.e.
                # at stop 'applying transaction' event, if we have seen the
                # commit start event but have NOT seen a commit stop event, we
                # emit a fake one.
                if (
                    transactions[tkey]["commit_start_event"] is not None
                    and transactions[tkey]["commit_stop_seen"] is False
                ):
                    # we've reached the end of the transaction but haven't seen the
                    # stop commit message, so we insert a fake one
                    commit_stop = event.copy()
                    commit_stop["msg"] = "commit"
                    start_event = transactions[tkey]["commit_start_event"]
                    commit_stop["duration"] = (
                        commit_stop["timestamp"] - start_event["timestamp"]
                    )
                    yield commit_stop
                del transactions[tkey]
        yield event

        prune_oldest("transactions", transactions, 1000, log)


def fix_ooo_write_start_stop(stream):
    """Correct event order for write-start stop event on operational datastore

    Some transactions on the operational datastore appear to get the stop event
    for 'applying transaction' and 'write-start' mixed up, e.g.:

        EVENT TYPE,TIMESTAMP,DURATION,SESSION ID,TRANSACTION ID,DATASTORE,CONTEXT,SUBSYSTEM,PHASE,SERVICE,SERVICE PHASE,COMMIT QUEUE ID,NODE,DEVICE,DEVICE PHASE,PACKAGE,MESSAGE,ANNOTATION # noqa: E501
        start,2020-10-16T10:30:58.474,,720571,176697257,operational,system,,,,,,,,,,"applying transaction", # noqa: E501
        start,2020-10-16T10:30:58.474,,720571,176697257,operational,system,,write-start,,,,,,,,"write-start", # noqa: E501
        info,2020-10-16T10:30:58.475,,720571,176697257,operational,system,cdb,write-start,,,,,,,,"write-start", # noqa: E501
        start,2020-10-16T10:30:58.475,,720571,176697257,operational,system,,,,,,,,,,"check data kickers", # noqa: E501
        stop,2020-10-16T10:30:58.475,0.000,720571,176697257,operational,system,,,,,,,,,,"check data kickers", # noqa: E501
        info,2020-10-16T10:30:58.480,,720571,176697257,operational,system,,,,,,,,,,"transaction empty", # noqa: E501
        stop,2020-10-16T10:30:58.480,0.006,720571,176697257,operational,system,,,,,,,,,,"applying transaction","empty" # noqa: E501
        stop,2020-10-16T10:30:58.480,0.006,720571,176697257,operational,system,,write-start,,,,,,,,"write-start", # noqa: E501

    We fix this by reordering the last two events.

    We track transactions on the operational datastore that starts with an
    'applying transaction' event. If we have seen the 'write-start' start
    message we should also expect to see the corresponding stop event for
    'write-start' before the end of the transaction ('applying transaction'
    stop event). If we haven't seen the 'write-start' stop event by the time we
    see the stop event for 'applying transaction' we will hold the message and
    emit them in the revers order, i.e. get the correct order.

    See ENG-25241
    """
    log = logging.getLogger("fix_ooo_write_start_stop")
    transactions = {}
    state_stores[id(transactions)] = {"name": "fix_ooo_write_start_stop.transactions", "obj": transactions}
    for event in stream:
        tkey = (event["usid"], event["tid"])
        if (
            "datastore" in event
            and event["datastore"] == "operational"
            and event["type"] == "start"
            and event["msg"] == "applying transaction"
        ):
            if tkey in transactions:
                log.warning(
                    "Seeing start of transaction [start/applying transaction] but transaction"
                    f"already in state for {event['tid']}"
                )
            # track operational transactions starting with an 'applying
            # transaction' event
            transactions[tkey] = {
                "applying_transaction_stop_event": None,
                "write_start_stop_seen": False,
                "write_start_start_seen": False,
            }

        if tkey in transactions:
            if event["type"] == "start" and event["msg"] == "write-start":
                transactions[tkey]["write_start_start_seen"] = True
            elif event["type"] == "stop" and event["msg"] == "write-start":
                transactions[tkey]["write_start_stop_seen"] = True
                # if we haven't yet seen the 'applying transaction' stop event,
                # it means messages are in the right order
                if transactions[tkey]["applying_transaction_stop_event"] is None:
                    pass
                else:
                    # we are on the write-start message, which is out-of-order.
                    # Emit the write-start first followed by the stop 'applying
                    # transaction' event. This means the end of the transaction
                    # so remove our state and explicitly skip the normal yield,
                    # which would otherwise result in duplicate event.
                    yield event
                    yield transactions[tkey]["applying_transaction_stop_event"]
                    del transactions[tkey]
                    continue

            elif event["type"] == "stop" and event["msg"] == "applying transaction":
                # Seeing the stop event for 'applying transaction' means we are
                # now at the end of the transaction. If we have seen the
                # write-start start message but not yet the write-start stop
                # message, it likely means out-of-order.
                if (
                    transactions[tkey]["write_start_start_seen"] is True
                    and transactions[tkey]["write_start_stop_seen"] is False
                ):
                    transactions[tkey]["applying_transaction_stop_event"] = event
                    # explicitly do not yield the event
                    continue

                del transactions[tkey]

        yield event

        prune_oldest("transactions", transactions, 1000, log)


def fix_restconf_type(stream):
    """Various restconf events should use start/stop type

    NSO emits some RESTCONF related events, like 'restconf edit' around a
    RESTCONF transaction and while they represent a span, they have the type
    'info'. We change to 'start'/'stop' since that is what it should be!
    ENG-25328 for changing the event type.
    """
    for event in stream:
        if event["type"] == "info" and event["msg"] in (
            "restconf edit",
            "restconf get",
        ):
            if event["duration"] is None:
                event["type"] = "start"
                event["subsystem"] = "restconf"
            else:
                event["type"] = "stop"
        yield event


def fix_nano_events(stream):
    """Separate instance data from nano-service progress-trace events

    Nano-service related progress-trace events contain variable instance data
    like the component name and name of the state.
    """
    log = logging.getLogger("fix-nano-events")
    for event in stream:
        # saving FASTMAP nano reverse diff-set and applying changes component {ncs:self self}: state ncs:init # noqa: E501
        # component {ncs:self self}: state ncs:init status change not-reached -> reached
        # component {ncs:self self}: state ncs:ready precondition evaluated to false
        # component {ncs:self self}: state abc:dhcp
        m = re.match(
            "(?P<msg1>.*?) {(?P<component>[^}]+)}: state (?P<state>[^ ]+)(?P<msg2>.*?)$",
            event["msg"],
        )
        if m:
            event["msg"] = re.sub(
                " component state$", "", f"{m.group('msg1')} state{m.group('msg2')}"
            )
            event["component"] = m.group("component")
            event["state"] = m.group("state")

        m = re.match(
            "(?P<msg>executing side effect) for (?P<kp>.+) op (?P<op>[^ ]+)$",
            event["msg"],
        )
        if m:
            event["msg"] = f"{m.group('msg')} on {m.group('op')}"
            # break down keypath to service, component & state
            n = re.match(
                "(?P<service>.*)/plan/component{(?P<component>[^}]+)}/state{(?P<state>[^}]+)}",
                m.group("kp"),
            )
            if n:
                event["component"] = n.group("component")
                event["state"] = n.group("state")
            else:
                log.error(f"Unable to parse keypath {m.group('kp')}")

        yield event


def fix_check_conflicts(stream):
    """Separate variable instance data in 'check conflicts' events"""
    for event in stream:
        # check conflicts (with transaction 1234)
        m = re.match(r"check conflicts \(with transaction ([0-9]+)\)", event["msg"])
        if m:
            event["msg"] = "check conflicts"
            event["other_tid"] = m.group(1)
        yield event


def filter_restconf_get(stream):
    """There is a bug causing unbalanced "restconf get" spans, i.e. the stop
    event is missing. ENG-27607 tracks this issue.
    """
    for event in stream:
        if event["msg"] == "restconf get":
            continue
        yield event


def add_span_id(stream):
    """Add a span-id field to events lacking one"""
    for event in stream:
        if event["type"] == "start":
            if event["span_id"] is None:
                event["span_id"] = secrets.token_hex(8)

        yield event


def add_tlock_holder(stream):
    """Adds virtual span for when transaction-lock is held"""
    log = logging.getLogger("add-tlock-holder")
    grabbing_lock = {}
    state_stores[id(grabbing_lock)] = {"name": "add_tlock_holder.grabbing_lock", "obj": grabbing_lock}
    # the span that is holding the transaction-lock or None, when it is not
    # held / we don't know (we might have started up in the middle of an
    # ongoing transaction)
    hold_span = None

    def reorder_tlock(stream):
        """NSO might deliver progress trace events out of order. We already
        attempt to reorder events by timestamp but it might not be enough.
        Specifically for the transaction lock related spans, through expert
        knowledge, we can do a better job of reordering.

        If we see the 'grabbing transaction lock' message when the lock is held
        (hold_span is not None), we really expect to see the "releasing
        transaction lock" message first and so we buffer it until we get the
        "releasing transaction lock" event.

        Note how we check annotation. It is legit to see a grabbing transaction
        lock event with annotation = 'error' as this indicates the transaction
        was cancelled / stopped waiting for the transaction lock. This is
        naturally allowed to happen while someone else is holding the
        transaction lock.
        """
        buf = None
        for event in stream:
            if event["msg"] == "releasing transaction lock" and buf is not None:
                yield event
                yield buf
                buf = None

            elif (
                event["msg"] == "grabbing transaction lock"
                and event["type"] == "stop"
                and event["annotation"] is None
                and hold_span
            ):
                buf = event

            else:
                yield event

    for event in reorder_tlock(stream):
        tkey = (event["usid"], event["tid"])
        yield event.copy()  # yield a copy so we can work with the original event

        if event["msg"] == "grabbing transaction lock":
            if event["type"] == "start":
                grabbing_lock[tkey] = event
            elif event["type"] == "stop":
                grabbing_lock.pop(tkey, None)

        if event["type"] == "stop" and event["msg"] == "grabbing transaction lock":
            if event["annotation"] == "error":
                # if grabbing transaction lock span was aborted or similar, we
                # can't assume this transaction got the transaction lock
                continue
            # start of holding transaction lock span
            hold_span = event.copy()
            hold_span["type"] = "start"
            hold_span["span_id"] = secrets.token_hex(8)
            hold_span["parent_span_id"] = event["parent_span_id"]
            hold_span["duration"] = None
            hold_span["msg"] = "holding transaction lock"
            yield hold_span
            # emit virtual (fake) info message for other transactions waiting
            # on the transaction lock with info on that the lock holder has
            # changed
            for glkey, glevent in grabbing_lock.items():
                gl_span = glevent.copy()
                gl_span["type"] = "info"
                gl_span["timestamp"] = event["timestamp"]
                gl_span["msg"] = "transaction lock holder changed"
                gl_span["struct"] = {"tlock_holder": hold_span["tid"]}

        if event["type"] == "info" and event["msg"] == "releasing transaction lock":
            if hold_span is None:
                # This might happen if we are (re)started while a transaction is
                # ongoing in NSO.
                log.warning(
                    "saw end of holding transaction lock [info/releasing transaction lock] "
                    f"but transaction-lock not held for transaction {event}"
                )
            elif event["tid"] != hold_span["tid"]:
                # This should never happend - indicative of bug in otel-exporter.
                log.warning(
                    "saw end of holding transaction lock [info/releasing transaction lock] "
                    f"but transaction-lock held by another tid: {event} {hold_span}"
                )
            else:
                hold_stop = hold_span.copy()
                hold_stop["type"] = "stop"
                hold_stop["duration"] = event["timestamp"] - hold_span["timestamp"]
                hold_stop["timestamp"] = event["timestamp"]
                yield hold_stop
                hold_span = None

        prune_oldest("grabbing_lock", grabbing_lock, 10000, log)


def export_otel(tracer, stream):
    """Export to OTel collector

    This is what an ideal OTel export function looks like. If the NSO
    progress-trace output is shaped the right way, implementing the export
    function is as simple as this. Currently, the ptrace format does not look
    like this and we are reliant on the transmogrify function to reshape the
    events to emulate the ideal NSO ptrace format. It is likely desirable to
    move the functionality of the transmogrify function into NSO.
    """
    log = logging.getLogger("export-otel")
    traces = {}
    state_stores[id(traces)] = {"name": "export_otel.traces", "obj": traces}

    for event in stream:
        if event["trace_id"] not in traces:
            # TODO: ensure event is root span? Only a root span (that does not
            # have a parent-span-id set) should be valid as the first span/event
            # of a trace.
            if event["parent_span_id"] is not None:
                log.warning(f"First seen event for trace: {event['trace_id']} unexpectedly has parent span set, event: {prettify(event)}")
            traces[event["trace_id"]] = {"spans": {}}
        trace = traces[event["trace_id"]]

        # NSO 6.1 places certain information in attributes, which is a k/v dict
        # in itself, we ignore it here but later add in those values, as to
        # flatten the output and not have nested dicts
        ignore_tags = (
            "type",
            "trace_id",
            "span_id",
            "parent_span_id",
            "timestamp",
            "duration",
            "struct",
            "extra_tags",
            "attributes",
            "link_trace_id",
            "link_span_id",
            "links",
        )
        kvs = {k: v for k, v in event.items() if k not in ignore_tags and v is not None}
        attrs = {k: v for k, v in event.get("attributes", {}).items() if k not in ignore_tags and v is not None}
        kvs.update(attrs)
        kvs.update(event.get("extra_tags", {}))

        if event["type"] == "start":
            if event["parent_span_id"] is None:
                parent_span_context = None
            else:
                try:
                    parent_span_context = trace["spans"][event["parent_span_id"]].get_span_context()
                except KeyError:
                    # We don't have the OTel span for this events parent. There
                    # are two reasons (perhaps more?):
                    # - we have incomplete state, like we started up while an
                    #   NSO transaction was on-going and thus we never saw the
                    #   earlier span
                    # - bug in NSO, like mismatching span-id of actual parent
                    #   and reported parent-span-id
                    # We should strive to fix all NSO bugs and there is very
                    # little we can do about the former case of incomplete
                    # state. Rather than export broken data, we simply ignore
                    # these events. This means whole traces might be missing but
                    # at least we don't get weirdly looking traces with only
                    # half the spans.
                    log.warning(f"Unable to find OTel span for event with parent-span-id: {event['parent_span_id']}, discarding. Event: {prettify(event)}")
                    yield event
                    continue

            span_context = trace_api.SpanContext(
                event["trace_id"].int,
                int(event["span_id"], 16),
                is_remote=False,
                trace_flags=trace_api.TraceFlags(trace_api.TraceFlags.SAMPLED),
            )

            # Create link between child(current span) and parent span(link_span_id) as well
            # as updating the parent span to include a link to its child, if parent span has
            # not yet been ended(exported).
            span_links = ()
            link_trace_id = event.get("link_trace_id", None)
            link_span_id = event.get("link_span_id", None)
            if link_trace_id is not None and link_trace_id in traces \
                    and link_span_id in traces[link_trace_id]["spans"]:
                link_trace = traces[link_trace_id]
                link_span = link_trace["spans"][link_span_id]
                link_span_links = link_span.links
                link_span_links = (*link_span_links, trace_api.Link(span_context))
                span_links = (trace_api.Link(link_span.get_span_context()), )

                # Links of a span cannot be updated after span creation so re-creating
                # span is the only way to add links.
                if link_span.end_time is None:
                    updated_span = trace_sdk._Span(
                        name=link_span.name,
                        context=link_span.context,
                        parent=link_span.parent,
                        sampler=tracer.sampler,
                        resource=tracer.resource,
                        attributes=link_span.attributes,
                        span_processor=tracer.span_processor,
                        kind=trace_api.SpanKind.INTERNAL,
                        links=link_span_links,
                        instrumentation_info=tracer.instrumentation_info,
                        set_status_on_exception=True,
                    )
                    updated_span.start(start_time=link_span.start_time, parent_context=None)
                    link_trace["spans"][event["link_span_id"]] = updated_span

            span = trace_sdk._Span(
                name=event["msg"],
                context=span_context,
                parent=parent_span_context,
                sampler=tracer.sampler,
                resource=tracer.resource,
                attributes=kvs,
                span_processor=tracer.span_processor,
                kind=trace_api.SpanKind.INTERNAL,
                links=span_links,
                instrumentation_info=tracer.instrumentation_info,
                set_status_on_exception=True,
            )
            span.start(start_time=event["timestamp"] * 1000, parent_context=None)

            trace["spans"][event["span_id"]] = span

        elif event["type"] == "stop":
            try:
                span = trace["spans"][event["span_id"]]
                # we normally set all attributes when creating the span, but the
                # diffset isn't known until the end, thus we update it here
                if "diffset" in event:
                    span.set_attribute("diffset", event["diffset"])

                if event["annotation"] is not None:
                    span.set_status(
                        Status(
                            status_code=StatusCode.ERROR,
                            description=event["annotation"],
                        )
                    )

                if span.end_time is not None:
                    log.warning(
                        f"Got stop event for an already ended span. \nEvent: {prettify(event)} \nSpan: {span}"
                    )
                else:
                    span.end(event["timestamp"] * 1000)

                # This is top span for this trace, since we are seeing its stop
                # event the trace is done and we clean up our state...
                if event["parent_span_id"] is None:
                    del traces[event["trace_id"]]
            except KeyError:
                log.warning(f"Unable to find matching span for: {prettify(event)}")

        elif event["type"] == "info":
            try:
                span = trace["spans"][event["span_id"]]
                if span.end_time is not None:
                    log.warning(
                        f"Got event for an ended span. \nEvent: {prettify(event)} \nSpan: {span}"
                    )
                else:
                    span.add_event(event["msg"], kvs, event["timestamp"] * 1000)
            except KeyError:
                log.warning(
                    "Unable to associate info event with an existing span - "
                    f"discarding {prettify(event)}"
                )

        else:
            raise ValueError(f"Unhandled progress-trace event type {event['type']}")

        yield event

        prune_oldest("traces", traces, 10000, log)


def export_influx_span(metrics_q, stream, timezone=None):
    """Export span based metrics to InfluxDB"""
    log = logging.getLogger("export_influx_span")
    hostname = socket.gethostname()
    root_spans = {}
    state_stores[id(root_spans)] = {"name": "export_influx_span.root_spans", "obj": root_spans}
    if timezone is None:
        timezone = datetime.datetime.now(datetime.timezone.utc).astimezone().tzinfo

    for event in stream:
        if event["type"] == "start" and event["tid"] not in root_spans:
            root_spans[event["tid"]] = event["msg"]
        elif event["type"] == "stop":
            # Build default tags dict based on config, which is using a
            # DictSubscriber and thus can change between iterations. Also adding in
            # "host" tag for keeping track of which host exported this data.
            tags = {"host": hostname}
            tags.update(event["extra_tags"])

            tags.update({"name": event["msg"]})
            service_type = re.sub("\\[[^\\]]+\\]", "", event["service"] or "")
            tags.update({"service_type": service_type})
            tags.update({"component": event["component"] or ""})
            tags.update({"root_span": root_spans.get(event["tid"], "")})

            fields = {
                "duration": float(event["duration"]) / 1000,
                "tid": int(event["tid"]),
                "trace_id": str(event["trace_id"] or "0").replace("-", ""),
                "service": event["service"] or "",
                "device": event["device"] or "",
                "datastore": event["datastore"] or "",
            }
            metrics_q.append(
                {
                    "time": datetime.datetime.fromtimestamp(
                        event["timestamp"] / 1000000,
                        timezone
                    ),
                    "measurement": "span",
                    "tags": tags,
                    "fields": fields,
                }
            )

            if event["tid"] in root_spans and root_spans[event["tid"]] == event["msg"]:
                del root_spans[event["tid"]]
        yield event

        prune_oldest("root_spans", root_spans, 10000, log)


def export_influx_span_count(metrics_q, stream, timezone=None):
    """Export count of concurrent spans to InfluxDB"""
    log = logging.getLogger("export_influx_span_count")
    hostname = socket.gethostname()
    open_spans = {}
    state_stores[id(open_spans)] = {"name": "export_influx_span_count.open_spans", "obj": open_spans}
    if timezone is None:
        timezone = datetime.datetime.now(datetime.timezone.utc).astimezone().tzinfo

    for event in stream:
        if event["type"] == "info":
            # explicit shortcircuit
            continue

        # Build default tags dict based on config, which is using a
        # DictSubscriber and thus can change between iterations. Also adding in
        # "host" tag for keeping track of which host exported this data.
        tags = {"host": hostname}
        tags.update(event["extra_tags"])

        tags.update({"name": event["msg"]})

        if event["type"] == "start":
            if event["msg"] not in open_spans:
                open_spans[event["msg"]] = 0
            open_spans[event["msg"]] += 1

        if event["type"] == "stop":
            if event["msg"] not in open_spans:
                open_spans[event["msg"]] = 0
            else:
                open_spans[event["msg"]] -= 1

        fields = {"count": open_spans[event["msg"]]}
        metrics_q.append(
            {
                "time": datetime.datetime.fromtimestamp(event["timestamp"] / 1000000, timezone),
                "measurement": "span-count",
                "tags": tags,
                "fields": fields,
            }
        )

        yield event

        prune_oldest("open_spans", open_spans, 10000, log)


def export_influx_transaction(metrics_q, stream, timezone=None):
    """Export metrics summarized per transaction to InfluxDB

    The duration of spans are summarized and exported at the end time of the
    transaction. Each span name becomes a field and its value is the summary of
    time of that span type during the transaction. For example, if there are
    two create spans each taking 2 seconds, the 'create' field for this
    transaction will be 4.
    """
    log = logging.getLogger("export_influx_transaction")
    hostname = socket.gethostname()
    if timezone is None:
        timezone = datetime.datetime.now(datetime.timezone.utc).astimezone().tzinfo

    traces = {}
    state_stores[id(traces)] = {"name": "export_influx_transaction.traces", "obj": traces}
    for event in stream:
        tkey = (event["usid"], event["tid"])
        if event["type"] == "stop":
            # Build default tags dict based on config, which is using a
            # DictSubscriber and thus can change between iterations. Also adding in
            # "host" tag for keeping track of which host exported this data.
            tags = {"host": hostname}
            tags.update(event["extra_tags"])

            if tkey not in traces:
                traces[tkey] = {}

            if event["msg"] not in traces[tkey]:
                traces[tkey][event["msg"]] = 0

            traces[tkey][event["msg"]] += float(event["duration"]) / 1000

            # no parent means this is a root span
            if event["parent_span_id"] is None:
                fields = traces[tkey]
                fields.update(
                    {"tid": int(event["tid"]), "datastore": event["datastore"]}
                )
                metrics_q.append(
                    {
                        "time": datetime.datetime.fromtimestamp(
                            event["timestamp"] / 1000000,
                            timezone
                        ),
                        "measurement": "transaction",
                        "tags": tags,
                        "fields": fields,
                    }
                )
                # clean up
                del traces[tkey]
        yield event

        prune_oldest("traces", traces, 10000, log)


def export_influx_tlock(metrics_q, stream, timezone=None):
    """Export transaction-lock related metrics to InfluxDB

    This tracks metrics associated with the transaction-lock itself;
    - transaction-lock held or not
    - transaction-lock queue-length
    """
    log = logging.getLogger("export_influx_tlock")
    hostname = socket.gethostname()
    if timezone is None:
        timezone = datetime.datetime.now(datetime.timezone.utc).astimezone().tzinfo

    held = 0
    waiting = {}
    state_stores[id(waiting)] = {"name": "export_influx_tlock.waiting", "obj": waiting}
    tlock_qlen = 0

    # create database if it doesn't already exist
    for event in stream:
        tkey = (event["usid"], event["tid"])
        if event["msg"] in {"holding transaction lock", "grabbing transaction lock"}:
            # Build default tags dict based on config, which is using a
            # DictSubscriber and thus can change between iterations. Also adding in
            # "host" tag for keeping track of which host exported this data.
            tags = {"host": hostname}
            tags.update(event["extra_tags"])

            tags.update({"name": event["msg"]})

            if event["msg"] == "grabbing transaction lock":
                if event["type"] == "start" and held == 1:
                    # if tlock is held, this transaction trying to grab the
                    # tlock will be waiting, so we add it to our list of
                    # waiting transactions and increase the queue length. Note
                    # how we don't increase the queue-length when the lock is
                    # not held, since the transaction asking for the lock will
                    # be able to get it immediately and thus won't have to wait
                    # - it is never in the queue.
                    waiting[tkey] = {"start": event["timestamp"]}
                    tlock_qlen += 1
                if event["type"] == "stop" and tkey in waiting:
                    # Only decrease for transactions we've identified are
                    # waiting, i.e. when we identified that our transactions
                    # was added to and increased the length of the queue.
                    # For example, when the lock is not held and we see the
                    # start of grabbing transaction lock, we don't increase the
                    # queue length, since we will immediately grab the lock.
                    # Then we must also not decrement
                    waiting.pop(tkey, None)
                    tlock_qlen -= 1

                metrics_q.append(
                    {
                        "time": datetime.datetime.fromtimestamp(
                            event["timestamp"] / 1000000.0,
                            timezone
                        ),
                        "measurement": "transaction-lock",
                        "tags": tags,
                        "fields": {
                            "held": held,
                            "queue-length": tlock_qlen,
                            "tid": int(event["tid"]),
                        },
                    }
                )

            if event["msg"] == "holding transaction lock":
                type_map = {"start": 1, "stop": 0}
                held = type_map[event["type"]]

                metrics_q.append(
                    {
                        "time": datetime.datetime.fromtimestamp(
                            event["timestamp"] / 1000000.0,
                            timezone
                        ),
                        "measurement": "transaction-lock",
                        "tags": tags,
                        "fields": {
                            "held": held,
                            "queue-length": tlock_qlen,
                            "tid": int(event["tid"]),
                        },
                    }
                )

        yield event

        prune_oldest("waiting", waiting, 10000, log)

    if len(waiting) > 0:
        log.warning("Unclean waiting transaction state at end.")
        log.debug(waiting)


def sniffer(filename):
    """Takes a guess at the CSV version format of a file"""
    with open(filename, encoding="utf-8") as csvfile:
        csvreader = csv.DictReader(csvfile)
        fields = csvreader.fieldnames

    version = None
    if fields == [
        "EVENT TYPE",
        "TRACE ID",
        "SPAN ID",
        "PARENT SPAN ID",
        "TIMESTAMP",
        "DURATION",
        "SESSION ID",
        "TRANSACTION ID",
        "DATASTORE",
        "CONTEXT",
        "SUBSYSTEM",
        "PHASE",
        "SERVICE",
        "SERVICE PHASE",
        "COMMIT QUEUE ID",
        "NODE",
        "DEVICE",
        "DEVICE PHASE",
        "PACKAGE",
        "MESSAGE",
        "ANNOTATION",
        "COMPONENT",
        "STATE",
        "STRUCTURED",
        "ATTRIBUTE NAME",
        "ATTRIBUTE VALUE",
        "LINK TRACE ID",
        "LINK SPAN ID",
    ]:
        version = "NG2"
    elif fields == [
        "EVENT TYPE",
        "TRACE ID",
        "SPAN ID",
        "PARENT SPAN ID",
        "TIMESTAMP",
        "DURATION",
        "SESSION ID",
        "TRANSACTION ID",
        "DATASTORE",
        "CONTEXT",
        "SUBSYSTEM",
        "PHASE",
        "SERVICE",
        "SERVICE PHASE",
        "COMMIT QUEUE ID",
        "NODE",
        "DEVICE",
        "DEVICE PHASE",
        "PACKAGE",
        "MESSAGE",
        "ANNOTATION",
        "COMPONENT",
        "STATE",
        "STRUCTURED",
    ]:
        version = "NG1"
    elif fields == [
        "EVENT TYPE",
        "TIMESTAMP",
        "DURATION",
        "TRACE ID",
        "SPAN ID",
        "PARENT SPAN ID",
        "SESSION ID",
        "TRANSACTION ID",
        "DATASTORE",
        "CONTEXT",
        "SUBSYSTEM",
        "MESSAGE",
        "ANNOTATION",
        "ATTRIBUTE NAME",
        "ATTRIBUTE VALUE",
        "LINK TRACE ID",
        "LINK SPAN ID",
    ]:
        version = "6.1"
    elif fields == [
        "EVENT TYPE",
        "TIMESTAMP",
        "DURATION",
        "SESSION ID",
        "TRANSACTION ID",
        "DATASTORE",
        "CONTEXT",
        "TRACE ID",
        "SUBSYSTEM",
        "PHASE",
        "SERVICE",
        "SERVICE PHASE",
        "COMMIT QUEUE ID",
        "NODE",
        "DEVICE",
        "DEVICE PHASE",
        "PACKAGE",
        "MESSAGE",
        "ANNOTATION"
    ]:
        version = "6.0"
    elif fields == [
        "EVENT TYPE",
        "TIMESTAMP",
        "DURATION",
        "SESSION ID",
        "TRANSACTION ID",
        "DATASTORE",
        "CONTEXT",
        "SUBSYSTEM",
        "PHASE",
        "SERVICE",
        "SERVICE PHASE",
        "COMMIT QUEUE ID",
        "NODE",
        "DEVICE",
        "DEVICE PHASE",
        "PACKAGE",
        "MESSAGE",
        "ANNOTATION",
    ]:
        version = "5.4"
    elif fields == [
        "TIMESTAMP",
        "TID",
        "SESSION ID",
        "CONTEXT",
        "SUBSYSTEM",
        "PHASE",
        "SERVICE",
        "SERVICE PHASE",
        "COMMIT QUEUE ID",
        "NODE",
        "DEVICE",
        "DEVICE PHASE",
        "PACKAGE",
        "DURATION",
        "MESSAGE",
    ]:
        version = "5.3"

    return version, fields


def get_pipeline(stream, nso_version):
    """Constructs a pipeline of fixup processing steps based on the NSO version

    All these processing steps can be thought of as fixups that are required in
    order to correct the NSO progress trace data into its ideal shape. The
    fix_versions identify in which NSO version that something is fixed and so
    the fixup function is not required in the pipeline, conversely, it does not
    need to be added for later versions. The ideal state is to have 0 fixups!
    """
    log = logging.getLogger("observability-exporter")

    def parse_version(raw_version: str):
        """Split a string containing a NSO version to a list of integers

        "5.7.3" -> [5,7,3]
        "5.8.1.4" -> [5,8,1,4]

        Unlike a simple int(split()), this tolerates "extra cruft" in the NSO
        version, for example nightly builds like
        6.1_221220.132939547.a603ed415af2 is interpreted as 6.1
        """
        version_re = r"([0-9]+(\.[0-9]+){1,3})(.*)"
        m = re.match(version_re, raw_version)
        if not m:
            log.error(f"Unable to determine NSO version from: {raw_version}")
            return [0, 0]
        num_version = [int(x) for x in m.group(1).split(".")]
        return num_version

    def is_fixed(ver, fix_versions):
        """Check whether something is fixed given the current NSO version and a list of fix-versions
        """
        nver = parse_version(ver)
        if fix_versions == []:
            return False

        # try to find matching major.minor version match and compare
        latest_majmin = None
        for fver in fix_versions:
            if len(fver) == 2:
                if latest_majmin is None or fver > latest_majmin:
                    latest_majmin = fver
            if nver[0:2] == fver[0:2] and nver >= fver:
                return True
        # if the latest fix_version is a major/minor branch, like 5.7, we can
        # assume that the fix has also landed in later major/minor braches like
        # 5.8 whereas if the last fix version is 5.7.2, then it is specifically
        # on the 5.7 after 5.8 was forked out
        if latest_majmin and nver >= latest_majmin:
            return True

        return False

    fixups = [
        [filter_restconf_get, []],
        [fix_check_conflicts, []],
        [fix_nano_events, []],
        [fix_restconf_type, [
            [5, 4, 2],
            [5, 5],
        ]],
        [fix_ooo_write_start_stop, [
            [5, 1, 6],
            [5, 2, 5],
            [5, 3, 4],
            [5, 4, 2],
            [5, 5]
        ]],
        [fix_oper_commit_stop, [
            [5, 1, 6],
            [5, 2, 5],
            [5, 3, 4],
            [5, 4, 2],
            [5, 5]
        ]],
        [fix_dev_snapshot, [
            [5, 4, 2],
            [5, 5]
        ]],
        [fix_ha_config_msg, [
            [6, 1]
        ]],
        [add_cq_spans, [
            [6, 1]
        ]],
        [add_tlock_holder, [
            [6, 0]
        ]],
        [add_span_id, [
            [6, 1]
        ]],
        [transmogrify, [
            [6, 1]
        ]],
        [lift_cq_item, []],
    ]
    for fixup in fixups:
        fn, fix_versions = fixup
        if not is_fixed(nso_version, fix_versions):
            log.debug(f"Adding processor: {fn}")
            stream = fn(stream)

    return stream


def csv_ptrace_reader(filename, extra_tags=None):
    """Generator that yields progress-trace events as read from a
    progress-trace CSV file

    This will try to workaround deficiencies in older CSV exports where the
    type field isn't present. We try to recreate it based on the messages but
    this is somewhat error prone, at least theoretically, so don't be surprised
    if this breaks.
    """
    sniff_ver, fields = sniffer(filename)
    if sniff_ver is None:
        raise ValueError(f"Unable to determine NSO version from CSV file {filename}")
    stream = csv_reader(filename, extra_tags)
    if sniff_ver == "5.3":
        stream = uplift_53(stream)
    if sniff_ver not in ["NG1", "NG2"]:
        stream = get_pipeline(stream, sniff_ver)

    for row in stream:
        yield row


class InfluxdbExporter(threading.Thread):
    """Exports metrics from our queue to InfluxDB

    Runs as a separate thread to avoid blocking the main thread with the
    otherwise synchronous export to InfluxDB. This could potentially be
    replaced by an async library for InfluxDB export.
    """

    def __init__(self, influx_client, metrics_q):
        super().__init__()
        self.client = influx_client
        self.q = metrics_q
        self.exit_flag = False
        self.log = logging.getLogger("influxdb-exporter")
        try:
            # pylint:disable=broad-except
            self.client.create_database(self.client._database)
        except Exception:
            pass

    def run(self):
        while not self.exit_flag:
            try:
                sleep = self.export()
                if sleep:
                    time.sleep(0.1)
            # pylint:disable=broad-except
            except Exception as exc:
                self.log.exception(exc)
                # Sleep to avoid busy polling on potentially failing function
                time.sleep(1)

    def export(self, batch=500):
        # InfluxDB page states optimal batch size for line protocol is 5000.
        # What API is the influxdb client library using? We use a more
        # conservative batch size of 500 here.
        dps = []
        res = False
        i = 0
        while True:
            try:
                dps.append(self.q.pop())
            except IndexError:
                # If we hit end of queue, return true so we can sleep a bit
                # before next export run
                res = True
                break
            if batch is not None and i > batch:
                break
            i += 1
        if len(dps) > 0:
            self.client.write_points(dps)
        return res

    def stop(self):
        # Tell thread to exit
        self.exit_flag = True
        self.log.info(
            f"Shutting down. {len(self.q)} still in queue, exporting in batches..."
        )
        # Export remaining things on queue
        while True:
            eoq = self.export()
            if eoq:
                self.log.info("Metrics queue emptied. Exiting.")
                break


def retime(stream, start):
    """Retime progress-trace to start right now

    Rewrites the timestamps of events. The first events timestamp is changed to
    right now and then all subsequent events timestamps are rewritten so that
    their relative time to the first event is maintained.
    """
    orig_start_ts = None
    cal = parsedatetime.Calendar()
    new_start, _ = cal.parseDT(start)
    new_start_ts = int(new_start.strftime("%s%f"))
    for event in stream:
        if orig_start_ts is None:
            # store original event, before rewriting the timestamp so we can
            # compute the delta for subsequent events
            orig_start_ts = event["timestamp"]

        delta = int(event["timestamp"] - orig_start_ts)
        new = event.copy()
        new["timestamp"] = new_start_ts + delta

        yield new


def rando(stream):
    """Statefully randomize IDs: usid, tid, trace_id & span_id

    This is useful for creating mock data without having to manually work with
    the data. The same input file can be replayed multiple times with randomized
    IDs.

    An ID is replaced by a random value the first time it is encountered. All
    later occurrences are replaced with the same value so the semantics of the
    original trace is maintained.

    trace_id and span_id are just random values (as they should be).

    usid and tid are random but in increasing values.

    This might be rather memory intensive on large trace files as the state is
    never cleaned up. The envisaged use case is mostly focused on repeating
    smaller trace files though, so this should not be much of an actual issue.
    """
    rewrite_usid = {}
    rewrite_tid = {}
    rewrite_trace_id = {}
    rewrite_span_id = {}
    last_usid = random.randint(0, 9999)
    last_tid = random.randint(0, 9999)
    for original_event in stream:
        event = original_event.copy()

        if event["usid"] not in rewrite_usid:
            rewrite_usid[event["usid"]] = random.randint(last_usid, last_usid + 100)
            last_usid = rewrite_usid[event["usid"]]
        event["usid"] = rewrite_usid[event["usid"]]

        if event["tid"] not in rewrite_tid:
            rewrite_tid[event["tid"]] = random.randint(last_tid, last_tid + 100)
            last_tid = rewrite_tid[event["tid"]]
        event["tid"] = rewrite_tid[event["tid"]]

        if event["trace_id"] not in rewrite_trace_id:
            rewrite_trace_id[event["trace_id"]] = uuid.uuid4()
        event["trace_id"] = rewrite_trace_id[event["trace_id"]]

        if event["span_id"] not in rewrite_span_id:
            rewrite_span_id[event["span_id"]] = secrets.token_hex(8)
        event["span_id"] = rewrite_span_id[event["span_id"]]
        if event["parent_span_id"] is not None:
            event["parent_span_id"] = rewrite_span_id[event["parent_span_id"]]

        yield event


def determo(stream):
    """Make output deterministic by using sequential identifiers

    Normally trace-id and span-id are random values, which makes it difficult
    to compare files for testing purposes.

    This function will replace those values with deterministic counter values,
    like the first transaction will get tid=1, second tid=2. The first span is
    span-id=1 etc. Thus the same input file will always generate the same
    output file. We save the original value mapping so we can also remap the
    parent span value. This makes the output deterministic and thus more easily
    testable.

    Tweaking the output specifically for testing is usually not a good idea
    since we then aren't testing the "real thing". However, this is a very
    small function that is not intertwined with the rest of the program since
    it is actually implemented as a post-processor, so the risk of new bugs in
    this function is very low. The way we map values, any bugs introduced
    elsewhere will still come through and be visible in the output - it will
    just look slightly differently. If anything, I think the use of this
    function will make it easier to understand changes in our parsing and
    output - it will increase quality. For example, before it was very
    difficult to tell randomness updates versus actual updates to our test
    expected output files.
    """
    fields = {"trace_id", "span_id"}

    rewrite = {field: {} for field in fields}
    last = {field: 0 for field in fields}

    for original_event in stream:
        event = original_event.copy()
        for field in fields:
            if field not in event:
                continue
            if str(event[field]) not in rewrite[field]:
                rewrite[field][str(event[field])] = last[field]
                last[field] += 1
            event[field] = rewrite[field][str(event[field])]

        if "parent_span_id" in event and event["parent_span_id"] is not None:
            event["parent_span_id"] = rewrite["span_id"][event["parent_span_id"]]

        if "link_trace_id" in event and event["link_trace_id"] is not None:
            event["link_trace_id"] = rewrite["trace_id"][str(event["link_trace_id"])]

        if "link_span_id" in event and event["link_span_id"] is not None:
            event["link_span_id"] = rewrite["span_id"][event["link_span_id"]]

        yield event


def assert_output_invariants(stream):
    """Assert that certain invariants are upheld for output data

    This function can be applied to the stream to assert that we uphold
    invariants for the output data. For example, trace_id might not be set in
    the input data or it could have a value. When we process such data, we
    should set trace_id if it not set and when we generate virtual spans we
    should similarly copy trace_id so that it is always populated. By piping in
    this function towards the end of a processing pipeline, we can ensure that
    we output correct data. In testing, trace_id is usually modified by the
    determo module in order to make the test data testable but this could also
    potentially hide bugs, whereas this function is a better check for that.
    """
    for event in stream:
        if not isinstance(event["trace_id"], uuid.UUID):
            raise ValueError(f"trace_id must be uuid.UUID: {event}")
        yield event


def jointraces(stream):
    """Join all traces into one big trace

    Useful to display transactions next to each other and visualize their
    intercation with each other, for example how only one transactoin can hold a
    lock at a certain time.
    """
    trace_id = uuid.uuid4()
    root_span = None
    last_event = None
    for event in stream:
        if not root_span:
            # Create virtual root span
            root_span = event.copy()
            root_span["type"] = "start"
            root_span["trace_id"] = trace_id
            root_span["span_id"] = secrets.token_hex(8)
            root_span["parent_span_id"] = ""
            root_span["msg"] = "onetrace"
            root_span["struct"] = {}
            yield root_span

        if event.get("parent_span_id", None) is None:
            event["parent_span_id"] = root_span["span_id"]

        event["trace_id"] = trace_id

        yield event
        last_event = event

    # Create event for the root span stop event
    if root_span is not None and last_event is not None:
        root_stop = root_span.copy()
        root_stop["type"] = "stop"
        root_stop["timestamp"] = last_event["timestamp"]
        root_stop["duration"] = last_event["timestamp"] - root_span["timestamp"]
        yield root_stop


def progress_printer(stream, csvfile):
    """Display progress bar of processing CSV file

    Since we are acting on stream, which is a generator, we don't know what the
    total amount of entries is. Thus, we peek into the CSV file to first get
    the total number of lines. This isn't entirely exact as other stages in the
    processing pipeline might add more messages, thus yielding a higher total
    number of events than in the CSV file.
    """
    from rich.progress import (
        BarColumn,
        Progress,
        ProgressColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )
    from rich.text import Text

    class ProcessingSpeedColumn(ProgressColumn):
        """Renders human readable transfer speed."""

        def render(self, task):
            """Show data transfer speed."""
            speed = task.finished_speed or task.speed
            if speed is None:
                return Text("?", style="progress.data.speed")
            # data_speed = filesize.decimal(int(speed))
            return Text(f"{speed:.2f}/s", style="progress.data.speed")

    # First count number of lines in CSV file - this should be fairly quick
    with Progress(
        "{task.description}",
        "{task.total}",
        BarColumn(),
        ProcessingSpeedColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
    ) as progress:
        task1 = progress.add_task("[green]Processing CSV...", start=False)
        lines = 0
        with open(csvfile, "rt", encoding="utf-8") as file:
            for i, l in enumerate(file):
                lines = i + 1
        progress.update(task1, total=lines)
        progress.start_task(task1)
        for event in stream:
            progress.update(task1, advance=1)


def f_span_name_re(stream, name_re):
    for event in stream:
        if re.search(name_re, event["msg"]):
            yield event


def span_view(stream):
    from rich.live import Live
    from rich.bar import Bar
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text
    from rich.color import Color
    from rich.style import Style
    import math

    def draw_bar(barw, dur, start, end):
        if dur == 0:
            return "\n"
        step_sizes = [
            {"secs": 3600, "unit": "h", "div": 1},
            {"secs": 1800, "unit": "m", "div": 30},
            {"secs": 120, "unit": "m", "div": 2},
            {"secs": 60, "unit": "m", "div": 1},
            {"secs": 30, "unit": "s", "div": 30},
            {"secs": 20, "unit": "s", "div": 20},
            {"secs": 10, "unit": "s", "div": 10},
            {"secs": 5, "unit": "s", "div": 5},
            {"secs": 3, "unit": "s", "div": 3},
            {"secs": 1, "unit": "s", "div": 1},
            {"secs": 0.5, "unit": "s", "div": 0.5},
            {"secs": 0.2, "unit": "s", "div": 0.2},
            {"secs": 0.1, "unit": "s", "div": 0.1},
        ]
        min_markers = 3
        step_size = None
        for cms in step_sizes:
            step_size = cms
            if dur // cms["secs"] >= min_markers:
                break
        # How wide is our entire bar in characters? What is the displayed duration?
        # Compute what each char represents in time
        # 90/37 = 2.432
        char_width = float(dur) / barw
        # How many characters do we need to represent one time marker width?
        num_chars = math.floor(cms["secs"] / char_width)
        mark_bar = ""
        tick_bar = ""
        mark_i = 0
        while True:
            mark = f"{mark_i*step_size['div']:.1f}{step_size['unit']}"
            if mark_i == 0:
                mark_ms = datetime.datetime.fromtimestamp((end / 1000000)).strftime("%f")[:3]
                mark = datetime.datetime.fromtimestamp((end / 1000000)).strftime("%H:%M:%S.") + mark_ms

            if len(mark_bar) + len(mark) > barw:
                break
            mark_bar = mark + mark_bar
            if mark_i == 0:
                tick_bar = " " + tick_bar
            else:
                tick_bar = "|" + tick_bar

            mark_remain = min(barw - len(mark_bar), int(num_chars - len(mark)))
            mark_bar = (" " * mark_remain) + mark_bar
            tick_bar = (" " * min(barw - len(tick_bar), int(num_chars - 1))) + tick_bar
            mark_i += 1

            if len(mark_bar) >= barw:
                break
        mark_bar = " " * (barw - len(mark_bar)) + mark_bar
        tick_bar = " " * (barw - len(tick_bar)) + tick_bar

        return mark_bar + "\n" + tick_bar

    class RichTraceView(threading.Thread):
        # noqa: R0902 pylint: disable=too-many-instance-attributes
        # They're all needed and any other design would be less intuitive.
        fps = 15

        def __init__(self, buf):
            super().__init__()
            self.buf = buf
            self.console = Console()
            self.shutdown = False
            self.last_buf_end = None
            self.last_begin = None
            self.last_end = None
            self.last_draw = None
            self.ticks_since_data = 0

        def run(self):
            def gen_table(buf):
                table = Table()
                table.add_column("trace-id", width=12, no_wrap=True)
                table.add_column("event", width=30, no_wrap=True)
                table.add_column("Duration", min_width=8, justify="right", no_wrap=True)
                barw = self.console.width - (12 + 30 + 8 + 13)
                span_text = Text('')
                table.add_column(span_text, width=barw, no_wrap=True)

                nr_items = self.console.height - 5
                # find bounds
                buf_begin = None
                buf_end = None
                open_spans = False
                buf_lacking = nr_items
                for eid, ev in list(buf.items())[-nr_items:]:
                    buf_lacking -= 1
                    if buf_begin is None:
                        if ev["type"] == "start":
                            buf_begin = ev["timestamp"]
                        elif ev["type"] == "stop":
                            buf_begin = ev["timestamp"] - ev["duration"]
                        else:
                            pass
                    if ev["type"] == "start":
                        open_spans = True
                    buf_end = ev["timestamp"]
                for i in range(buf_lacking):
                    table.add_row("", "", "", "")
                if buf_begin is None or buf_end is None:
                    span_text.append("\n")
                    return table

                if buf_end == self.last_buf_end:
                    self.ticks_since_data += 1
                else:
                    self.ticks_since_data = 0

                if self.last_begin is None:
                    self.last_begin = buf_begin
                if self.last_end is None:
                    self.last_end = buf_end

                # The begin time of the graph is basically the start timestamp
                # of the first displayed span. Since the table is limited in
                # heigh we only show a limited number of spans. When a span that
                # was displayed goes off the screen, the begin time will change.
                # It might mean a big jump in begin time which means the graph
                # will look "jumpy". In order to smooth this, we compute the
                # target begin time and knowing what the last displayed begin
                # timestamp was, we compute a step to move, in the right
                # direction so that we over time will approach. The step size is
                # proportionate to the size of the display time of the entire
                # graph, which should make this smooth regardless of time scale.
                #
                # maximum time we can move in one step
                max_step = (self.last_end - self.last_begin) / self.fps
                # buf_begin is our target time, which we want to display
                # last_begin is what we last displayed
                # begin_diff is thus the diff between where we currently are and where we should be
                begin_diff = buf_begin - self.last_begin
                begin_step = min(max_step, begin_diff)
                begin = self.last_begin + begin_step

                end = buf_end
                if open_spans:
                    end += self.ticks_since_data * (1000000 / self.fps)
                end = max(end, self.last_end)
                size = end - begin

                span_text.append(draw_bar(barw, size / 1000000, begin, end))

                # update for next cycle
                self.last_buf_end = buf_end
                self.last_begin = begin
                self.last_end = end

                for eid, ev in list(buf.items())[-nr_items:]:
                    if ev["type"] == "start":
                        start = ev["timestamp"] - begin
                        stop = end
                        color = "red"
                        dur = ""
                    elif ev["type"] == "stop":
                        start = (ev["timestamp"] - ev["duration"]) - begin
                        stop = ev["timestamp"] - begin
                        color = traces[ev["trace_id"]]["color"]
                        dur = f'{ev["duration"] / 1000.0:.3f}'
                    else:
                        continue
                    span = Bar(begin=start, end=stop, size=size, color=color)
                    row_style = Style(color=traces[ev["trace_id"]]["color"])
                    table.add_row(str(ev["trace_id"]).split("-")[4], ev["msg"], dur, span, style=row_style)
                return table

            with Live(gen_table(self.buf), refresh_per_second=self.fps, console=self.console) as live:
                self.last_draw = datetime.datetime.now().timestamp()
                while not self.shutdown:
                    live.update(gen_table(self.buf))
                    now = datetime.datetime.now().timestamp()
                    target = self.last_draw
                    while not (target - now > 0):
                        target += 1.0 / self.fps
                    sleep_time = target - now
                    if sleep_time > 0:
                        time.sleep(sleep_time)
                    self.last_draw = target

        def stop(self):
            self.shutdown = True

    traces = {}

    from collections import OrderedDict
    # Keep state of open traces and their spans, ordered when they appeared
    buf = OrderedDict()
    buf_size = 1000

    trace_view = RichTraceView(buf)
    trace_view.start()

    random.seed(0)
    try:
        while True:
            # TODO: we want to peek into stream to see if there is something
            # more, in case we can consume multiple events before redrawing,
            # however, grabbing the next event is blocking so if there is
            # nothing there we will hang and risk not updating the display
            for event in stream:
                if event["type"] not in ("start", "stop"):
                    continue
                if event["trace_id"] not in traces:
                    traces[event["trace_id"]] = {
                        "color": Color.from_ansi(random.randint(2, 16))
                    }
                    random.randint(2, 200)

                eid = (event["trace_id"], event["span_id"])
                buf[eid] = event
                if len(buf) > buf_size:
                    buf.popitem(False)
    except KeyboardInterrupt:
        trace_view.stop()


def memprofiler(stream):
    from guppy import hpy

    h = hpy()
    i = 0
    for event in stream:
        i += 1
        if i > 100:
            i = 0
            print(h.heap().byrcs)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--log-file", help="Specify a file to log output to")
    parser.add_argument("--export-otlp", default=False, action="store_true", help="Export data to an OpenTelemetry collector")
    parser.add_argument("--export-influxdb", default=False, action="store_true", help="Export data to InfluxDB")
    parser.add_argument("--otlp-host", default="localhost", help="Specify the OTLP collector host")
    parser.add_argument("--otlp-port", type=int, default=4318, help="Specify the OTLP collector host.")
    parser.add_argument("--otlp-transport", default="http", help="Specify the OTLP transport type http/grpc")
    parser.add_argument("--influxdb-host", help="Specify the InfluxDB host")
    parser.add_argument("--influxdb-port", type=int, default=8086, help="Specify the InfluxDB port")
    parser.add_argument("--influxdb-db", default="nso", help="Specify the InfluxDB database")
    parser.add_argument("--influxdb-username", help="Specify the InfluxDB username")
    parser.add_argument("--influxdb-password", help="Specify the InfluxDB password")
    parser.add_argument("--influxdb-drop", action="store_true", help="Drop existing InfluxDB database")
    parser.add_argument("--live", action="store_true", help="Enable live mode to fetch NSO spans in real-time")
    parser.add_argument("--csv", help="Specify a CSV file that contains NSO spans")
    parser.add_argument("--csv-out", help="Specify an output file to save uplifted spans")
    parser.add_argument("--filter-span-name-re",
                        help="Rewrite timestamps of events where the first event timestamp is changed\n"
                        "to now and subsequent events maintain relative time.")
    parser.add_argument("--extra-tags", action="append", default=[], help="Specify extra tags that should be added to spans. e.g. key=value")
    parser.add_argument("--retime", help="Shifts trace event timestamps while preserving relative time gaps")
    parser.add_argument("--rando", action="store_true", help="Statefully randomize IDs: usid, tid, trace_id & span_id")
    parser.add_argument("--determo", action="store_true", help="Replace random identifiers i.e. trace_id and span_id with sequential one")
    parser.add_argument("--assert-invariants", action="store_true", help="Ensure consistency and integrity in the output data")
    parser.add_argument("--one-trace", action="store_true", help="Join all traces into one big trace")
    parser.add_argument("--span-view", action="store_true", help="Generates a live visualization of event spans")
    parser.add_argument("--richgraph", action="store_true")
    parser.add_argument("--memprofile", action="store_true", help="Profile memory usage by printing memory statistics")
    parser.add_argument("--timezone", help="Specify timezone to make naive timestamps timezone aware when exporting to InfluxDB, e.g. Americas/Los_Angeles")
    args = parser.parse_args()

    log_level = logging.INFO
    if args.debug:
        log_level = logging.DEBUG
    from rich.logging import RichHandler

    if args.richgraph:
        # https://www.youtube.com/watch?v=tf-5TCYXRx8 is a video presentation by
        # Kristian Larsson showing the use of the span view, it was recorded
        # when the terminal based span view had just been built and it was then
        # called --richgraph. It was renamed to --span-view before being merged
        # to main branch but in order for folks to be able to follow along after
        # seeing the video, we guide them right.
        print("--richgraph is now called --span-view")
        sys.exit(1)

    log_handlers = [RichHandler()]

    if args.log_file:
        print("Adding file log handler")
        log_handlers.append(logging.FileHandler(args.log_file))

    logging.basicConfig(
        level=log_level, format="%(message)s", datefmt="[%X]", handlers=log_handlers
    )

    extra_tags = {}
    for et in args.extra_tags:
        extra_tags[et.split("=")[0]] = et.split("=")[1]

    if args.csv or args.live:
        if args.otlp_transport == "http":
            otlp_exporter = httpOTLPSpanExporter(
                endpoint=f"http://{args.otlp_host}:{args.otlp_port}/v1/traces",
            )
        elif args.otlp_transport == "grpc":
            otlp_exporter = grpcOTLPSpanExporter(
                endpoint=f"http://{args.otlp_host}:{args.otlp_port}", insecure=True
            )
        else:
            raise NotImplementedError(
                f"Not a supported transport {args.otlp.transport}"
            )

        trace_api.set_tracer_provider(
            TracerProvider(resource=Resource.create({SERVICE_NAME: "NSO"}))
        )
        trace_api.get_tracer_provider().add_span_processor(
            SimpleSpanProcessor(otlp_exporter)
        )
        tracer = trace_api.get_tracer("observability-exporter")
        influx_client = InfluxDBClient(
            args.influxdb_host,
            args.influxdb_port,
            args.influxdb_username,
            args.influxdb_password,
            args.influxdb_db,
        )

        if args.influxdb_drop:
            influx_client.drop_database("nso")

        if args.csv:
            stream = csv_ptrace_reader(args.csv, extra_tags)
        elif args.live:
            import ncs
            stream = notifs_reader(False)
            with ncs.maapi.single_read_trans("observability-exporter", "system") as t:
                root = ncs.maagic.get_root(t)
                nso_version = root.ncs_state.version
            stream = get_pipeline(stream, nso_version)

        if args.assert_invariants:
            stream = assert_output_invariants(stream)

        if args.filter_span_name_re:
            stream = f_span_name_re(stream, args.filter_span_name_re)

        # funky transforms, mostly for debugging and development
        if args.retime:
            stream = retime(stream, args.retime)
        if args.rando:
            stream = rando(stream)
        if args.determo:
            stream = determo(stream)
        if args.one_trace:
            stream = jointraces(stream)

        influx_exporter = None
        if args.export_otlp:
            stream = export_otel(tracer, stream)
        if args.export_influxdb:
            metrics_q = []
            timezone = None
            if args.timezone:
                import pytz
                timezone = pytz.timezone(args.timezone)
            influx_exporter = InfluxdbExporter(influx_client, metrics_q)
            influx_exporter.start()
            stream = export_influx_span(metrics_q, stream, timezone)
            stream = export_influx_span_count(
                metrics_q, stream, timezone
            )
            stream = export_influx_tlock(metrics_q, stream, timezone)
            stream = export_influx_transaction(
                metrics_q, stream, timezone
            )

        try:
            if args.csv_out:
                csv_writer(args.csv_out, stream)
            elif args.span_view:
                span_view(stream)
            elif args.memprofile:
                memprofiler(stream)
            else:
                progress_printer(stream, args.csv)
        except KeyboardInterrupt:
            pass

        if influx_exporter is not None:
            influx_exporter.stop()
            influx_exporter.join()
