#!/usr/bin/env python3
"""Prometheus exporter for the RabbitMQ Management HTTP API."""

import argparse
import logging
import sys
import time

import requests
from prometheus_client import start_http_server
from prometheus_client.core import (
    REGISTRY,
    CounterMetricFamily,
    GaugeMetricFamily,
)

LOG = logging.getLogger("rabbitmq_exporter")
REQUEST_TIMEOUT = 5

# (metric name without _total, help, JSON path)
OVERVIEW_COUNTERS = [
    ("rabbitmq_messages_published",
     "Total number of messages published to the cluster since node start.",
     ("message_stats", "publish")),
    ("rabbitmq_messages_delivered",
     "Total number of messages delivered to consumers (with ack) plus basic.get calls since node start.",
     ("message_stats", "deliver_get")),
    ("rabbitmq_messages_acknowledged",
     "Total number of messages acknowledged by consumers since node start.",
     ("message_stats", "ack")),
    ("rabbitmq_messages_confirmed",
     "Total number of messages confirmed by the broker since node start.",
     ("message_stats", "confirm")),
]

OVERVIEW_GAUGES = [
    ("rabbitmq_queue_totals_messages",
     "Total number of messages (ready plus unacknowledged) across all queues in the cluster.",
     ("queue_totals", "messages")),
    ("rabbitmq_queue_totals_messages_ready",
     "Total number of messages ready for delivery across all queues in the cluster.",
     ("queue_totals", "messages_ready")),
    ("rabbitmq_queue_totals_messages_unacknowledged",
     "Total number of messages delivered but not yet acknowledged across all queues in the cluster.",
     ("queue_totals", "messages_unacknowledged")),
    ("rabbitmq_object_totals_connections",
     "Current number of open connections in the cluster.",
     ("object_totals", "connections")),
    ("rabbitmq_object_totals_channels",
     "Current number of open channels in the cluster.",
     ("object_totals", "channels")),
    ("rabbitmq_object_totals_exchanges",
     "Current number of exchanges in the cluster.",
     ("object_totals", "exchanges")),
    ("rabbitmq_object_totals_queues",
     "Current number of queues in the cluster.",
     ("object_totals", "queues")),
    ("rabbitmq_object_totals_consumers",
     "Current number of consumers in the cluster.",
     ("object_totals", "consumers")),
]

QUEUE_GAUGES = [
    ("rabbitmq_queue_messages",
     "Total number of messages (ready plus unacknowledged) in the queue.",
     ("messages",)),
    ("rabbitmq_queue_messages_ready",
     "Number of messages ready for delivery in the queue.",
     ("messages_ready",)),
    ("rabbitmq_queue_messages_unacknowledged",
     "Number of messages delivered but awaiting acknowledgement in the queue.",
     ("messages_unacknowledged",)),
    ("rabbitmq_queue_consumers",
     "Number of consumers attached to the queue.",
     ("consumers",)),
    ("rabbitmq_queue_memory_bytes",
     "Bytes of memory used by the queue's Erlang process.",
     ("memory",)),
]

QUEUE_COUNTERS = [
    ("rabbitmq_queue_messages_published",
     "Total number of messages published to this queue since it was declared.",
     ("message_stats", "publish")),
]

NODE_GAUGES = [
    ("rabbitmq_node_mem_used_bytes",
     "Bytes of memory used by the node's Erlang runtime.",
     ("mem_used",)),
    ("rabbitmq_node_fd_used",
     "Number of file descriptors currently used by the node.",
     ("fd_used",)),
    ("rabbitmq_node_fd_total",
     "Total number of file descriptors available to the node.",
     ("fd_total",)),
    ("rabbitmq_node_sockets_used",
     "Number of file descriptors used as sockets by the node.",
     ("sockets_used",)),
    ("rabbitmq_node_sockets_total",
     "Total number of file descriptors available for use as sockets by the node.",
     ("sockets_total",)),
    ("rabbitmq_node_processes_used",
     "Number of Erlang processes currently used on the node.",
     ("proc_used",)),
    ("rabbitmq_node_processes_total",
     "Total number of Erlang processes available on the node.",
     ("proc_total",)),
    ("rabbitmq_node_uptime_milliseconds",
     "Milliseconds since the node was started.",
     ("uptime",)),
    ("rabbitmq_node_disk_free_bytes",
     "Bytes of free disk space available on the node's disk monitor partition.",
     ("disk_free",)),
]


def get_path(obj, path):
    """Walk a nested dict path and return a float, or None if absent/non-numeric."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    if isinstance(cur, bool) or cur is None:
        return None
    if isinstance(cur, (int, float)):
        return float(cur)
    if isinstance(cur, str):
        try:
            return float(cur)
        except ValueError:
            return None
    return None


class RabbitMQCollector:
    def __init__(self, target, username=None, password=None):
        self.target = target.rstrip("/")
        self.session = requests.Session()
        if username is not None:
            self.session.auth = (username, password if password is not None else "")
        self.session.headers.update({"Accept": "application/json"})

    def _fetch(self, path):
        url = self.target + path
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            LOG.error("Request to %s failed: %s", url, exc)
        except ValueError as exc:
            LOG.error("Invalid JSON from %s: %s", url, exc)
        return None

    def describe(self):
        # Avoid polling the target at registration time.
        return []

    def collect(self):
        yield from self._collect_overview()
        yield from self._collect_queues()
        yield from self._collect_nodes()

    def _collect_overview(self):
        data = self._fetch("/api/overview")
        if not isinstance(data, dict):
            if data is not None:
                LOG.error("Unexpected /api/overview payload type: %s", type(data).__name__)
            return
        for name, help_text, path in OVERVIEW_COUNTERS:
            value = get_path(data, path)
            if value is None:
                continue
            fam = CounterMetricFamily(name, help_text)
            fam.add_metric([], value)
            yield fam
        for name, help_text, path in OVERVIEW_GAUGES:
            value = get_path(data, path)
            if value is None:
                continue
            fam = GaugeMetricFamily(name, help_text)
            fam.add_metric([], value)
            yield fam

    def _collect_queues(self):
        data = self._fetch("/api/queues")
        if not isinstance(data, list):
            if data is not None:
                LOG.error("Unexpected /api/queues payload type: %s", type(data).__name__)
            return
        labels = ["vhost", "queue"]
        gauges = {n: GaugeMetricFamily(n, h, labels=labels) for n, h, _ in QUEUE_GAUGES}
        counters = {n: CounterMetricFamily(n, h, labels=labels) for n, h, _ in QUEUE_COUNTERS}
        for q in data:
            if not isinstance(q, dict):
                continue
            qname = q.get("name")
            vhost = q.get("vhost")
            if qname is None or vhost is None:
                continue
            label_values = [str(vhost), str(qname)]
            for name, _, path in QUEUE_GAUGES:
                value = get_path(q, path)
                if value is not None:
                    gauges[name].add_metric(label_values, value)
            for name, _, path in QUEUE_COUNTERS:
                value = get_path(q, path)
                if value is not None:
                    counters[name].add_metric(label_values, value)
        for fam in list(gauges.values()) + list(counters.values()):
            if fam.samples:
                yield fam

    def _collect_nodes(self):
        data = self._fetch("/api/nodes")
        if not isinstance(data, list):
            if data is not None:
                LOG.error("Unexpected /api/nodes payload type: %s", type(data).__name__)
            return
        gauges = {n: GaugeMetricFamily(n, h, labels=["node"]) for n, h, _ in NODE_GAUGES}
        for node in data:
            if not isinstance(node, dict):
                continue
            node_name = node.get("name")
            if node_name is None:
                continue
            for name, _, path in NODE_GAUGES:
                value = get_path(node, path)
                if value is not None:
                    gauges[name].add_metric([str(node_name)], value)
        for fam in gauges.values():
            if fam.samples:
                yield fam


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="RabbitMQ Prometheus exporter")
    parser.add_argument("--port", type=int, required=True, help="Port to serve /metrics on")
    parser.add_argument("--target", required=True,
                        help="Base URL of the RabbitMQ management API, e.g. http://localhost:15672")
    parser.add_argument("--username", default=None, help="Basic auth username")
    parser.add_argument("--password", default=None, help="Basic auth password")
    return parser.parse_args(argv)


def main():
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    collector = RabbitMQCollector(args.target, args.username, args.password)
    REGISTRY.register(collector)
    start_http_server(args.port)
    LOG.info("Serving metrics on :%d for target %s", args.port, args.target)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
