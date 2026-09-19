<!-- source: examples/rabbitmq/docs.md (kind=text) -->
# RabbitMQ Management HTTP API (excerpt)

The RabbitMQ management plugin provides an HTTP API on port 15672.
All endpoints require HTTP Basic authentication (default user `guest` /
password `guest`, only usable from localhost). All responses are JSON.

## GET /api/overview

Various random bits of information that describe the whole system.

Response fields (subset):

- `rabbitmq_version` (string), `erlang_version` (string), `node` (string)
- `message_stats.publish` — count of messages published (cumulative counter)
- `message_stats.deliver_get` — count of messages delivered to consumers in
  acknowledgement mode plus basic.get (cumulative counter)
- `message_stats.ack` — count of messages acknowledged (cumulative counter)
- `message_stats.confirm` — count of messages confirmed (cumulative counter)
- `queue_totals.messages` — total messages in all queues (gauge)
- `queue_totals.messages_ready` — messages ready for delivery (gauge)
- `queue_totals.messages_unacknowledged` — messages delivered but not yet
  acknowledged (gauge)
- `object_totals.connections`, `object_totals.channels`,
  `object_totals.exchanges`, `object_totals.queues`,
  `object_totals.consumers` — current counts of each object type (gauges)

Example:

```json
{
  "rabbitmq_version": "3.13.7",
  "erlang_version": "26.2",
  "node": "rabbit@myhost",
  "message_stats": {"publish": 102345, "deliver_get": 98010, "ack": 97990, "confirm": 102300},
  "queue_totals": {"messages": 335, "messages_ready": 320, "messages_unacknowledged": 15},
  "object_totals": {"connections": 4, "channels": 8, "exchanges": 12, "queues": 3, "consumers": 5}
}
```

## GET /api/queues

A list of all queues across all vhosts.

Each element (subset of fields):

- `name` (string) — queue name
- `vhost` (string) — virtual host
- `state` (string) — e.g. "running"
- `messages` — total message count in the queue (gauge)
- `messages_ready` — messages ready for delivery (gauge)
- `messages_unacknowledged` — delivered, awaiting ack (gauge)
- `consumers` — number of consumers (gauge)
- `memory` — bytes of memory used by the queue process (gauge)
- `message_stats.publish` — messages published to this queue (cumulative counter)

Example element:

```json
{
  "name": "orders", "vhost": "/", "state": "running",
  "messages": 120, "messages_ready": 118, "messages_unacknowledged": 2,
  "consumers": 2, "memory": 187312,
  "message_stats": {"publish": 50231}
}
```

## GET /api/nodes

A list of nodes in the cluster.

Each element (subset): `name` (string), `running` (bool),
`mem_used` (bytes, gauge), `fd_used` / `fd_total` (gauges),
`sockets_used` / `sockets_total` (gauges), `proc_used` / `proc_total`
(gauges), `uptime` (milliseconds since node start), `disk_free` (bytes).
