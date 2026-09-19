#!/usr/bin/env python3
"""Prometheus exporter for the RabbitMQ management HTTP API.

Usage:
    python exporter.py --port PORT --target BASE_URL [--username U] [--password P]
"""

import argparse
import math
import sys
import time

import requests
from prometheus_client import CollectorRegistry, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

REQUEST_TIMEOUT = 5.0

QUEUE_LABELS = ["queue", "vhost", "state"]
NODE_LABELS = ["node"]


def log(msg):
    sys.stderr.write("[rabbitmq-exporter] %s\n" % msg)
    sys.stderr.flush()


def to_number(value):
    """Convert a JSON value to a float, or return None if not numeric."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
    elif isinstance(value, str):
        try:
            f = float(value)
        except ValueError:
            return None
    else:
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def dig(obj, *path):
    """Safely follow a path of dict keys; return None if any step is missing."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
        if cur is None:
            return None
    return cur


def dig_any(obj, *paths):
    """Try multiple candidate paths (each a tuple of keys); return the first
    non-None numeric-ish value found."""
    for path in paths:
        val = dig(obj, *path)
        if val is not None:
            return val
    return None


def to_bool_number(value):
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return 1.0 if value else 0.0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "running"):
            return 1.0
        if v in ("false", "0", "no"):
            return 0.0
    return None


# (metric name, help, list of candidate json paths) for /api/overview
# The management API has used both "message_stats" and "msg_stats" as the
# key for this object across versions, so both are tried.
OVERVIEW_COUNTERS = [
    ("rabbitmq_messages_published_total",
     "Total number of messages published to the broker, cluster-wide, as reported by the contacted node.",
     [("msg_stats", "publish"), ("message_stats", "publish")]),
    ("rabbitmq_messages_delivered_total",
     "Total number of messages delivered to consumers in acknowledgement mode plus basic.get calls, cluster-wide.",
     [("msg_stats", "deliver_get"), ("message_stats", "deliver_get")]),
    ("rabbitmq_messages_acknowledged_total",
     "Total number of messages acknowledged by consumers, cluster-wide.",
     [("msg_stats", "ack"), ("message_stats", "ack")]),
    ("rabbitmq_messages_confirmed_total",
     "Total number of messages confirmed by the broker to publishers, cluster-wide.",
     [("msg_stats", "confirm"), ("message_stats", "confirm")]),
]

OVERVIEW_GAUGES = [
    ("rabbitmq_cluster_messages",
     "Total number of messages currently in all queues across the cluster (ready + unacknowledged).",
     [("queue_totals", "messages")]),
    ("rabbitmq_cluster_messages_ready",
     "Total number of messages ready for delivery across all queues in the cluster.",
     [("queue_totals", "messages_ready")]),
    ("rabbitmq_cluster_messages_unacknowledged",
     "Total number of messages delivered but not yet acknowledged across all queues in the cluster.",
     [("queue_totals", "messages_unacknowledged")]),
    ("rabbitmq_cluster_connections",
     "Current number of client connections open across the cluster.",
     [("object_totals", "connections")]),
    ("rabbitmq_cluster_channels",
     "Current number of channels open across the cluster.",
     [("object_totals", "channels")]),
    ("rabbitmq_cluster_exchanges",
     "Current number of exchanges declared across the cluster.",
     [("object_totals", "exchanges")]),
    ("rabbitmq_cluster_queues",
     "Current number of queues declared across the cluster.",
     [("object_totals", "queues")]),
    ("rabbitmq_cluster_consumers",
     "Current number of consumers subscribed across the cluster.",
     [("object_totals", "consumers")]),
]

QUEUE_GAUGES = [
    ("rabbitmq_queue_messages",
     "Total number of messages currently in this queue (ready + unacknowledged).",
     [("messages",)]),
    ("rabbitmq_queue_messages_ready",
     "Number of messages in this queue that are ready to be delivered to consumers.",
     [("messages_ready",)]),
    ("rabbitmq_queue_messages_unacknowledged",
     "Number of messages in this queue delivered to consumers but not yet acknowledged.",
     [("messages_unacknowledged",)]),
    ("rabbitmq_queue_consumers",
     "Number of consumers currently subscribed to this queue.",
     [("consumers",)]),
    ("rabbitmq_queue_memory_bytes",
     "Bytes of memory used by the runtime process for this queue.",
     # Newer management API responses nest this under memory_details.bytes;
     # older ones expose a flat "memory" field. Try both.
     [("memory_details", "bytes"), ("memory",)]),
]

QUEUE_COUNTERS = [
    ("rabbitmq_queue_messages_published_total",
     "Total number of messages published to this queue since it was declared. "
     "Absent until the first publish occurs on the queue.",
     [("message_stats", "publish"), ("msg_stats", "publish")]),
]

NODE_GAUGES = [
    ("rabbitmq_node_mem_used_bytes",
     "Bytes of memory currently used by this node.",
     [("mem_used",)]),
    ("rabbitmq_node_fd_used",
     "Number of file descriptors currently used by this node.",
     [("fd_used",)]),
    ("rabbitmq_node_fd_total",
     "Total number of file descriptors available to this node.",
     [("fd_total",)]),
    ("rabbitmq_node_sockets_used",
     "Number of network sockets currently used by this node.",
     [("sockets_used",)]),
    ("rabbitmq_node_sockets_total",
     "Total number of network sockets available to this node.",
     [("sockets_total",)]),
    ("rabbitmq_node_processes_used",
     "Number of Erlang processes currently used by this node.",
     [("proc_used",)]),
    ("rabbitmq_node_processes_total",
     "Total number of Erlang processes available to this node (process limit).",
     [("proc_total",)]),
    ("rabbitmq_node_uptime_milliseconds",
     "Milliseconds since this node started. Not modeled as a counter because the value resets "
     "to zero on node restart rather than accumulating monotonically for the life of the exporter.",
     [("uptime",)]),
    ("rabbitmq_node_disk_free_bytes",
     "Bytes of free disk space available on the volume used by this node.",
     [("disk_free",)]),
]


class RabbitMQCollector:
    def __init__(self, target, username=None, password=None):
        self.base_url = target.rstrip("/")
        if username is not None or password is not None:
            self.auth = (username or "", password or "")
        else:
            self.auth = None

    def _fetch(self, path):
        url = self.base_url + path
        try:
            resp = requests.get(
                url,
                auth=self.auth,
                timeout=REQUEST_TIMEOUT,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as exc:
            log("error fetching %s: %s" % (url, exc))
        except ValueError as exc:
            log("invalid JSON from %s: %s" % (url, exc))
        return None

    def collect(self):
        for fam in self._collect_overview():
            yield fam
        for fam in self._collect_queues():
            yield fam
        for fam in self._collect_nodes():
            yield fam

    # ---- /api/overview -------------------------------------------------
    def _collect_overview(self):
        data = self._fetch("/api/overview")
        if not isinstance(data, dict):
            if data is not None:
                log("unexpected /api/overview payload type: %s" % type(data).__name__)
            return []
        node = data.get("node")
        node = str(node) if node is not None else ""
        families = []
        for name, help_text, paths in OVERVIEW_COUNTERS:
            value = to_number(dig_any(data, *paths))
            if value is None:
                continue
            fam = CounterMetricFamily(name, help_text, labels=NODE_LABELS)
            fam.add_metric([node], value)
            families.append(fam)
        for name, help_text, paths in OVERVIEW_GAUGES:
            value = to_number(dig_any(data, *paths))
            if value is None:
                continue
            fam = GaugeMetricFamily(name, help_text, labels=NODE_LABELS)
            fam.add_metric([node], value)
            families.append(fam)
        return families

    # ---- /api/queues ---------------------------------------------------
    def _collect_queues(self):
        data = self._fetch("/api/queues")
        if not isinstance(data, list):
            if data is not None:
                log("unexpected /api/queues payload type: %s" % type(data).__name__)
            return []
        gauges = {
            name: GaugeMetricFamily(name, help_text, labels=QUEUE_LABELS)
            for name, help_text, _ in QUEUE_GAUGES
        }
        counters = {
            name: CounterMetricFamily(name, help_text, labels=QUEUE_LABELS)
            for name, help_text, _ in QUEUE_COUNTERS
        }
        has_samples = set()
        seen = set()
        for q in data:
            if not isinstance(q, dict):
                continue
            qname = q.get("name")
            if qname is None:
                continue
            vhost = q.get("vhost")
            key = (str(qname), "" if vhost is None else str(vhost))
            if key in seen:
                continue
            seen.add(key)
            state = q.get("state")
            state = str(state) if state else "down"
            labels = [key[0], key[1], state]
            for name, _, paths in QUEUE_GAUGES:
                value = to_number(dig_any(q, *paths))
                if value is None:
                    continue
                gauges[name].add_metric(labels, value)
                has_samples.add(name)
            for name, _, paths in QUEUE_COUNTERS:
                value = to_number(dig_any(q, *paths))
                if value is None:
                    continue
                counters[name].add_metric(labels, value)
                has_samples.add(name)
        families = []
        for name, _, _ in QUEUE_GAUGES:
            if name in has_samples:
                families.append(gauges[name])
        for name, _, _ in QUEUE_COUNTERS:
            if name in has_samples:
                families.append(counters[name])
        return families

    # ---- /api/nodes ----------------------------------------------------
    def _collect_nodes(self):
        data = self._fetch("/api/nodes")
        if not isinstance(data, list):
            if data is not None:
                log("unexpected /api/nodes payload type: %s" % type(data).__name__)
            return []
        running = GaugeMetricFamily(
            "rabbitmq_node_running",
            "Whether this node is currently running, as reported by the cluster "
            "(1 = running, 0 = not running).",
            labels=NODE_LABELS,
        )
        running_has = False
        gauges = {
            name: GaugeMetricFamily(name, help_text, labels=NODE_LABELS)
            for name, help_text, _ in NODE_GAUGES
        }
        has_samples = set()
        seen = set()
        for n in data:
            if not isinstance(n, dict):
                continue
            node_name = n.get("name")
            if node_name is None:
                continue
            node_name = str(node_name)
            if node_name in seen:
                continue
            seen.add(node_name)
            r = to_bool_number(n.get("running"))
            if r is not None:
                running.add_metric([node_name], r)
                running_has = True
            for name, _, paths in NODE_GAUGES:
                value = to_number(dig_any(n, *paths))
                if value is None:
                    continue
                gauges[name].add_metric([node_name], value)
                has_samples.add(name)
        families = []
        if running_has:
            families.append(running)
        for name, _, _ in NODE_GAUGES:
            if name in has_samples:
                families.append(gauges[name])
        return families


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Prometheus exporter for RabbitMQ management API")
    parser.add_argument("--port", type=int, required=True, help="Port to serve /metrics on")
    parser.add_argument("--target", required=True, help="Base URL of the RabbitMQ management API")
    parser.add_argument("--username", default=None, help="HTTP Basic auth username")
    parser.add_argument("--password", default=None, help="HTTP Basic auth password")
    return parser.parse_args(argv)


def main():
    args = parse_args()
    registry = CollectorRegistry()
    registry.register(RabbitMQCollector(args.target, args.username, args.password))
    start_http_server(args.port, registry=registry)
    log("serving metrics on :%d for target %s" % (args.port, args.target))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
