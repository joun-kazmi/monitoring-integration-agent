"""Faithful local stand-in for the RabbitMQ management HTTP API.

Serves /api/overview, /api/queues, /api/nodes with the real response shapes
and HTTP basic auth (guest/guest), on port 15672 by default. Values drift a
little between requests so counters actually increase.

Usage: python3 mock_server.py [port] [--break]

With --break, the API simulates an upstream shape change (renamed and
nested fields) to exercise the repair loop.
"""

import base64
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

START = time.time()
BROKEN = "--break" in sys.argv


def _publish_total() -> int:
    # monotonically increasing ~50 msg/s
    return 102_345 + int((time.time() - START) * 50)


def overview():
    pub = _publish_total()
    stats_key = "msg_stats" if BROKEN else "message_stats"
    return {
        "rabbitmq_version": "3.13.7",
        "erlang_version": "26.2",
        "node": "rabbit@mockhost",
        stats_key: {
            "publish": pub,
            "deliver_get": pub - 4335,
            "ack": pub - 4355,
            "confirm": pub - 45,
        },
        "queue_totals": {
            "messages": 335,
            "messages_ready": 320,
            "messages_unacknowledged": 15,
        },
        "object_totals": {
            "connections": 4,
            "channels": 8,
            "exchanges": 12,
            "queues": 3,
            "consumers": 5,
        },
    }


def queues():
    pub = _publish_total()
    items = _queue_items(pub)
    if BROKEN:
        for q in items:
            q["memory_details"] = {"bytes": q.pop("memory")}
    return items


def _queue_items(pub):
    return [
        {
            "name": "orders", "vhost": "/", "state": "running",
            "messages": 120, "messages_ready": 118, "messages_unacknowledged": 2,
            "consumers": 2, "memory": 187312,
            "message_stats": {"publish": pub // 2},
        },
        {
            "name": "emails", "vhost": "/", "state": "running",
            "messages": 15, "messages_ready": 2, "messages_unacknowledged": 13,
            "consumers": 3, "memory": 90112,
            "message_stats": {"publish": pub // 3},
        },
        {
            "name": "audit", "vhost": "prod", "state": "running",
            "messages": 200, "messages_ready": 200, "messages_unacknowledged": 0,
            "consumers": 0, "memory": 254008,
            "message_stats": {"publish": pub // 5},
        },
    ]


def nodes():
    return [
        {
            "name": "rabbit@mockhost", "running": True,
            "mem_used": 142_606_336, "fd_used": 42, "fd_total": 1048576,
            "sockets_used": 6, "sockets_total": 943626,
            "proc_used": 512, "proc_total": 1048576,
            "uptime": int((time.time() - START) * 1000),
            "disk_free": 53_687_091_200,
        }
    ]


ROUTES = {"/api/overview": overview, "/api/queues": queues, "/api/nodes": nodes}
AUTH_OK = "Basic " + base64.b64encode(b"guest:guest").decode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("Authorization") != AUTH_OK:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="RabbitMQ Management"')
            self.end_headers()
            self.wfile.write(b"Not authorised")
            return
        fn = ROUTES.get(self.path.split("?")[0])
        if fn is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Object Not Found")
            return
        body = json.dumps(fn()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # quiet
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 15672
    print(f"mock rabbitmq management api on :{port}")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
