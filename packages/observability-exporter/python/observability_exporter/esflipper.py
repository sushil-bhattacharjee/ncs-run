#!/usr/bin/env python3
import uuid

from elasticsearch import Elasticsearch

# res = es.get(index="test-index", id=1)
# print(res['_source'])

# es.indices.refresh(index="test-index")


def get_unprocessed_transactions(es):
    """Returns a list of unprocessed transactions"""
    # TODO: need to check transaction span has ended - if
    # 'duration' exists? or is it initialized to 0? so check value > 0?
    query = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"operationName": "transaction"}},
                ],
                "must_not": {"exists": {"field": "nsoProcessed"}},
            }
        }
    }
    res = es.search(index="jaeger-span-*", body=query, size=50)
    return res["hits"]["hits"]


def upsert_service_op(es, span):
    """Upserts Jaeger service op in Elasticsearch"""
    index = span["_index"].replace("-span-", "-service-")
    # see if it already exists
    query = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"serviceName": "NSO-service"}},
                    {"term": {"operationName": span["_source"]["operationName"]}},
                ]
            }
        }
    }
    res = es.search(index=index, body=query, size=100)
    if res["hits"]["total"]["value"] == 0:
        es.index(
            index=index,
            body={
                "serviceName": "NSO-service",
                "operationName": span["_source"]["operationName"],
            },
            refresh=True,
        )


def upsert_fake_root_span(es, span, pst_trace_id, service):
    """Upserts (inserts or updates if exists) a fake root span
    The fake root span encompasses the entire history, i.e. all transactions,
    of a service.

    returns the span
    """
    index = span["_index"]
    # see if it already exists
    query = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"traceID": pst_trace_id}},
                    {"term": {"operationName": "service-trace"}},
                ]
            }
        }
    }
    res = es.search(index=index, body=query, size=100)
    if res["hits"]["total"]["value"] == 0:
        print("creating fake root service-trace span")
        body = span["_source"].copy()
        body["operationName"] = "service-trace"
        body["spanID"] = uuid.uuid4().hex[0:16]
        body["traceID"] = pst_trace_id
        body["nsoProcessed"] = True
        body["nsoService"] = service
        body["process"]["serviceName"] = "NSO-service"
        body["references"] = []
        # add a tag to make it searchable
        # TODO: this doesn't actually work, in that Jaeger won't allow
        # searching on this string - /slow-service[name='S1'] - perhaps too
        # many weird characters? ES full text indexing f*ing things up?
        body["tags"].append(
            {"key": "service-trace", "type": "string", "value": service}
        )
        new_span = es.index(index=index, body=body, refresh=True)

        root_span = es.get(index, new_span["_id"])
    else:
        print("found existing fake root service-trace span")
        root_span = res["hits"]["hits"][0]

    new_root_doc = extend_fake_root_span(es, root_span, span)
    return es.get(index, new_root_doc["_id"])


def extend_fake_root_span(es, root_doc, span_doc):
    # ensure service-trace span covers all children
    root = root_doc["_source"]
    span = span_doc["_source"]

    new_body = root.copy()
    if root["startTime"] > span["startTime"]:
        print("EXTENDING START")
        old_end = root["startTime"] + root["duration"]
        new_body["startTime"] = span["startTime"]
        new_body["startTimeMillis"] = span["startTimeMillis"]
        new_body["duration"] = old_end - span["startTime"]
    root_end = root["startTime"] + root["duration"]
    span_end = span["startTime"] + span["duration"]
    if root_end < span_end:
        print("EXTENDING END")
        new_body["duration"] += span_end - root_end

    new_root = es.index(
        index=root_doc["_index"], id=root_doc["_id"], body=new_body, refresh=True
    )

    return new_root


def flupsert_span(es, span, pst_trace_id, service):
    """Flip and upsert span"""
    index = span["_index"]
    # see if it already exists
    query = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"traceID": pst_trace_id}},
                    {"term": {"spanID": span["_source"]["spanID"]}},
                ]
            }
        }
    }
    res = es.search(index=index, body=query, size=100)
    if res["hits"]["total"]["value"] == 0:
        body = span["_source"].copy()
        if body["operationName"] == "transaction":
            fake_root = upsert_fake_root_span(es, span, pst_trace_id, service)
            if body["references"] != []:
                raise ValueError(
                    "What, the transaction span should have no references!"
                )
            body["references"] = [
                {
                    "refType": "CHILD_OF",
                    "traceID": pst_trace_id,
                    "spanID": fake_root["_source"]["spanID"],
                }
            ]

        old_trace_id = body["traceID"]
        body["traceID"] = pst_trace_id
        for ref in body["references"]:
            if ref["traceID"] == old_trace_id:
                ref["traceID"] = pst_trace_id
        body["nsoProcessed"] = True
        body["nsoService"] = service
        body["process"]["serviceName"] = "NSO-service"
        # TODO: rewrite various other things?
        es.index(index=index, body=body, refresh=True)
    else:
        print("already found a node, NOOP")
    # TODO: mark span as processed


def get_spans_by_trace_id(es, trace_id):
    query = {
        "query": {
            "bool": {
                "must": {
                    "term": {"traceID": trace_id},
                },
            }
        }
    }
    res = es.search(index="jaeger-span-*", body=query, size=10000)
    if res["hits"]["total"]["value"] == 10000:
        raise ValueError(
            "Unable to handle more than 10k hits in get_spans_by_trace_id()"
        )
    return res["hits"]["hits"]


def get_pst_trace_id(es, service):
    query = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"operationName": "service-trace"}},
                    {"term": {"nsoService": service}},
                ]
            }
        }
    }
    res = es.search(index="jaeger-span-*", body=query, size=100)
    if res["hits"]["total"]["value"] > 0:
        trace_id = res["hits"]["hits"][0]["_source"]["traceID"]
        print(f"Found existing PST trace-id {trace_id} for {service}")
    else:
        trace_id = uuid.uuid4().hex
        print(f"Generating new PST trace-id {trace_id} for {service}")
    return trace_id


def fix_index_mapping(es, span):
    index_name = span["_index"]
    index = es.indices.get(index_name)
    mapping = index[index_name]["mappings"]
    mapping["properties"]["nsoService"] = {"type": "keyword"}
    mapping["properties"]["nsoProcessed"] = {"type": "boolean"}
    es.indices.put_mapping(mapping, index=index_name)


def work(es):
    # 1. find transactions that have ended but have not yet been converted
    # we do this by finding the transaction span, which is the root span - if
    # it has ended, the transaction should be over
    for trans_span in get_unprocessed_transactions(es):
        # add serviceTrace to our elasticsearch index
        fix_index_mapping(es, trans_span)
        # grab the trace_id of this transaction that we want to process
        trace_id = trans_span["_source"]["traceID"]
        # get all spans for the given trace
        spans = get_spans_by_trace_id(es, trace_id)
        # first pass to find services
        services = set()
        for span in spans:
            for tag in span["_source"]["tags"]:
                if tag["key"] == "service":
                    services.add(tag["value"])
        print(services)

        for service in services:
            pst_trace_id = get_pst_trace_id(es, service)
            print(f"PST TRACE ID: {pst_trace_id}")
            # TODO: per service; # figure out pst trace-id
            for span in spans:
                upsert_service_op(es, span)
                flupsert_span(es, span, pst_trace_id, service)


def list_services(es):
    res = es.search(index="jaeger-service-*", size=5000)
    for i in res["hits"]["hits"]:
        print(i)


def list_spans(es):
    res = es.search(index="jaeger-span-*", size=5000)
    for i in res["hits"]["hits"]:
        print(i)


def wipe_pst(es):
    """Wipe all per-service-traces in Elasticsearch"""
    print("Wiping per-service-traces...")
    from rich.progress import Progress

    with Progress() as progress:
        task1 = progress.add_task("[green]Removing service records...", start=False)
        task2 = progress.add_task("[green]Removing span records...", start=False)

        query = {"query": {"bool": {"must": {"exists": {"field": "nsoService"}}}}}
        res1 = es.search(index="jaeger-service-*", body=query, size=10000)
        res2 = es.search(index="jaeger-span-*", body=query, size=10000)
        progress.update(task1, total=res1["hits"]["total"]["value"])
        progress.update(task2, total=res2["hits"]["total"]["value"])
        progress.start_task(task1)
        progress.start_task(task2)
        for i in res1["hits"]["hits"]:
            progress.update(task1, advance=1)
            es.delete(index=i["_index"], id=i["_id"], refresh=True)
        for i in res2["hits"]["hits"]:
            progress.update(task2, advance=1)
            es.delete(index=i["_index"], id=i["_id"], refresh=True)


def wipe_all(es):
    """Wipe everything Jaeger related in Elasticsearch"""
    print("Wiping everything...")
    from rich.progress import Progress

    with Progress() as progress:
        # task1 = progress.add_task("[green]Removing service records...", start=False)
        task2 = progress.add_task("[green]Removing span records...", start=False)

        # res1 = es.search(index="jaeger-service-*", size=10000)
        res2 = es.search(index="jaeger-span-*", size=10000)
        # progress.update(task1, total=res1['hits']['total']['value'])
        progress.update(task2, total=res2["hits"]["total"]["value"])
        print(res2["hits"]["total"]["value"])
        # progress.start_task(task1)
        progress.start_task(task2)
        # for i in res1['hits']['hits']:
        #    progress.update(task1, advance=1)
        #    es.delete(index=i['_index'], id=i['_id'], refresh=True)
        for i in res2["hits"]["hits"]:
            progress.update(task2, advance=1)
            es.delete(index=i["_index"], id=i["_id"])


def search_foo(es):
    query = {
        "query": {
            "bool": {
                "must": {
                    "term": {"operationName": "create"},
                },
            }
        }
    }
    res = es.search(index="jaeger-span-*", body=query, size=5000)
    for i in res["hits"]["hits"]:
        print(i)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--es-host", required=True)
    parser.add_argument("--es-port", type=int, default=9200)
    parser.add_argument("--list-services", action="store_true")
    parser.add_argument("--list-spans", action="store_true")
    parser.add_argument("--wipe-all", action="store_true")
    parser.add_argument("--wipe-pst", action="store_true")
    parser.add_argument("--work", action="store_true")
    parser.add_argument("--search-foo", action="store_true")
    args = parser.parse_args()

    es = Elasticsearch([{"host": args.es_host, "port": args.es_port}])

    if args.wipe_all:
        wipe_all(es)

    if args.wipe_pst:
        wipe_pst(es)

    if args.list_services:
        list_services(es)

    if args.list_spans:
        list_spans(es)

    if args.search_foo:
        search_foo(es)

    if args.work:
        work(es)
