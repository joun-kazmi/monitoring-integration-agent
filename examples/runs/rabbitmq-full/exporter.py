#!/usr/bin/env python3
"""Prometheus exporter for the RabbitMQ Management HTTP API.

Usage:
    python exporter.py --port PORT --target BASE_URL [--username U] [--password P]
"""

import argparse
import sys
import time

import requests
from prometheus_client import start_http_server
from prometheus_client.core import (
    CollectorRegistry,
    CounterMetricFamily,
    GaugeMetricFamily,
)

REQUEST_TIMEOUT = 5

QUEUE_LABELS = ["queue", "vhost"]
NODE_LABELS = ["node"]

# (metric name, help, JSON path within /api/overview)
OVERVIEW_COUNTERS = [
    ("rabbitmq_messages_published_total",
     "Total number of messages published across the broker (cumulative).",
     ("message_stats", "publish")),
    ("rabbitmq_messages_delivered_total",
     "Total messages delivered to consumers in acknowledgement mode plus basic.get (cumulative).",
     ("message_stats", "deliver_get")),
    ("rabbitmq_messages_acknowledged_total",
     "Total number of messages acknowledged by consumers (cumulative).",
     ("message_stats", "ack")),
    ("rabbitmq_messages_confirmed_total",
     "Total number of messages confirmed to publishers (cumulative).",
     ("message_stats", "confirm")),
]

OVERVIEW_GAUGES = [
    ("rabbitmq_messages",
     "Total number of messages in all queues.",
     ("queue_totals", "messages")),
    ("rabbitmq_messages_ready",
     "Number of messages ready for delivery across all queues.",
     ("queue_totals", "messages_ready")),
    ("rabbitmq_messages_unacknowledged",
     "Number of messages delivered but not yet acknowledged across all queues.",
     ("queue_totals", "messages_unacknowledged")),
    ("rabbitmq_connections",
     "Current number of connections.",
     ("object_totals", "connections")),
    ("rabbitmq_channels",
     "Current number of channels.",
     ("object_totals", "channels")),
    ("rabbitmq_exchanges",
     "Current number of exchanges.",
     ("object_totals", "exchanges")),
    ("rabbitmq_queues",
     "Current number of queues.",
     ("object_totals", "queues")),
    ("rabbitmq_consumers",
     "Current number of consumers.",
     ("object_totals", "consumers")),
]

QUEUE_GAUGES = [
    ("rabbitmq_queue_messages",
     "Total number of messages in the queue (ready plus unacknowledged).",
     ("messages",)),
    ("rabbitmq_queue_messages_ready",
     "Number of messages in the queue ready for delivery.",
     ("messages_ready",)),
    ("rabbitmq_queue_messages_unacknowledged",
     "Number of messages in the queue delivered to consumers and awaiting acknowledgement.",
     ("messages_unacknowledged",)),
    ("rabbitmq_queue_consumers",
     "Number of consumers on the queue.",
     ("consumers",)),
    ("rabbitmq_queue_memory_bytes",
     "Bytes of memory used by the queue process.",
     ("memory",)),
]

QUEUE_COUNTERS = [
    ("rabbitmq_queue_messages_published_total",
     "Total number of messages published to the queue (cumulative).",
     ("message_stats", "publish")),
]

NODE_GAUGES = [
    ("rabbitmq_node_memory_used_bytes",
     "Memory used by the node, in bytes.",
     ("mem_used",)),
    ("rabbitmq_node_fd_used",
     "Number of file descriptors in use by the node.",
     ("fd_used",)),
    ("rabbitmq_node_fd_total",
     "File descriptor limit of the node.",
     ("fd_total",)),
    ("rabbitmq_node_sockets_used",
     "Number of sockets in use by the node.",
     ("sockets_used",)),
    ("rabbitmq_node_sockets_total",
     "Socket limit of the node.",
     ("sockets_total",)),
    ("rabbitmq_node_processes_used",
     "Number of Erlang processes in use on the node.",
     ("proc_used",)),
    ("rabbitmq_node_processes_total",
     "Erlang process limit of the node.",
     ("proc_total",)),
    ("rabbitmq_node_uptime_milliseconds",
     "Milliseconds since the node started.",
     ("uptime",)),
    ("rabbitmq_node_disk_free_bytes",
     "Free disk space on the node, in bytes.",
     ("disk_free",)),
]

NODE_RUNNING_NAME = "rabbitmq_node_running"
NODE_RUNNING_HELP = (
    "Whether the node is running (1) or not (0). "
    "Converted from the boolean 'running' field."
)


def log(msg):
    print(f"[rabbitmq-exporter] {msg}", file=sys.stderr, flush=True)


def dig(obj, path):
    """Walk a nested dict by key path; return None if any step is missing."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def to_number(value):
    """Convert a JSON value to float; return None for non-numeric values."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def to_bool_number(value):
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return 1.0 if value else 0.0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes"):
            return 1.0
        if lowered in ("false", "0", "no"):
            return 0.0
    return None


class RabbitMQCollector:
    def __init__(self, target, username=None, password=None):
        self.target = target.rstrip("/")
        self.session = requests.Session()
        if username is not None or password is not None:
            self.session.auth = (username or "", password or "")
        self.session.headers.update({"Accept": "application/json"})

    def _get(self, path):
        url = f"{self.target}{path}"
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            log(f"request to {url} failed: {exc}")
        except ValueError as exc:
            log(f"invalid JSON from {url}: {exc}")
        return None

    def describe(self):
        # Avoid triggering a scrape of the target at registration time.
        return []

    def collect(self):
        yield from self._collect_overview()
        yield from self._collect_queues()
        yield from self._collect_nodes()

    def _collect_overview(self):
        data = self._get("/api/overview")
        if not isinstance(data, dict):
            if data is not None:
                log("unexpected /api/overview payload type; skipping")
            return
        for name, help_text, path in OVERVIEW_COUNTERS:
            value = to_number(dig(data, path))
            if value is None:
                continue
            fam = CounterMetricFamily(name, help_text)
            fam.add_metric([], value)
            yield fam
        for name, help_text, path in OVERVIEW_GAUGES:
            value = to_number(dig(data, path))
            if value is None:
                continue
            fam = GaugeMetricFamily(name, help_text)
            fam.add_metric([], value)
            yield fam

    def _collect_queues(self):
        data = self._get("/api/queues")
        if not isinstance(data, list):
            if data is not None:
                log("unexpected /api/queues payload type; skipping")
            return
        gauges = {
            name: GaugeMetricFamily(name, help_text, labels=QUEUE_LABELS)
            for name, help_text, _ in QUEUE_GAUGES
        }
        counters = {
            name: CounterMetricFamily(name, help_text, labels=QUEUE_LABELS)
            for name, help_text, _ in QUEUE_COUNTERS
        }
        for item in data:
            if not isinstance(item, dict):
                continue
            qname = item.get("name")
            vhost = item.get("vhost")
            if qname is None or vhost is None:
                continue
            labels = [str(qname), str(vhost)]
            for name, _, path in QUEUE_GAUGES:
                value = to_number(dig(item, path))
                if value is not None:
                    gauges[name].add_metric(labels, value)
            for name, _, path in QUEUE_COUNTERS:
                value = to_number(dig(item, path))
                if value is not None:
                    counters[name].add_metric(labels, value)
        for name, _, _ in QUEUE_GAUGES:
            if gauges[name].samples:
                yield gauges[name]
        for name, _, _ in QUEUE_COUNTERS:
            if counters[name].samples:
                yield counters[name]

    def _collect_nodes(self):
        data = self._get("/api/nodes")
        if not isinstance(data, list):
            if data is not None:
                log("unexpected /api/nodes payload type; skipping")
            return
        gauges = {
            name: GaugeMetricFamily(name, help_text, labels=NODE_LABELS)
            for name, help_text, _ in NODE_GAUGES
        }
        running = GaugeMetricFamily(
            NODE_RUNNING_NAME, NODE_RUNNING_HELP, labels=NODE_LABELS
        )
        for item in data:
            if not isinstance(item, dict):
                continue
            node = item.get("name")
            if node is None:
                continue
            labels = [str(node)]
            for name, _, path in NODE_GAUGES:
                value = to_number(dig(item, path))
                if value is not None:
                    gauges[name].add_metric(labels, value)
            run_val = to_bool_number(item.get("running"))
            if run_val is not None:
                running.add_metric(labels, run_val)
        for name, _, _ in NODE_GAUGES:
            if gauges[name].samples:
                yield gauges[name]
        if running.samples:
            yield running


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Prometheus exporter for the RabbitMQ Management HTTP API."
    )
    parser.add_argument("--port", type=int, required=True,
                        help="Port to serve /metrics on.")
    parser.add_argument("--target", required=True,
                        help="Base URL of the RabbitMQ management API, e.g. http://127.0.0.1:15672")
    parser.add_argument("--username", default=None, help="Basic auth username.")
    parser.add_argument("--password", default=None, help="Basic auth password.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    registry = CollectorRegistry()
    registry.register(RabbitMQCollector(args.target, args.username, args.password))
    start_http_server(args.port, registry=registry)
    log(f"serving metrics on :{args.port}/metrics for target {args.target}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
