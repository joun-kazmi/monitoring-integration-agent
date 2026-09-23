import json

from miagent.redact import MAX_LIST_ITEMS, redact_body, redact_text


def test_json_keeps_shape_and_numbers_masks_identifiers():
    body = json.dumps({
        "node": "rabbit@db-01.prod.example.com",
        "peer": "10.2.3.4:5672",
        "admin_password": "hunter2",
        "mgmt": "https://mq.internal.example.com/api",
        "id": "0b7c6d1e-1111-2222-3333-444455556666",
        "version": "3.13.7",
        "state": "running",
        "stats": {"publish": 42, "ok": True, "rate": None},
    })
    out = json.loads(redact_body(body))
    assert out == {
        "node": "<user@host>",
        "peer": "<ipv4>",
        "admin_password": "<redacted>",
        "mgmt": "<url>",
        "id": "<uuid>",
        "version": "3.13.7",
        "state": "running",
        "stats": {"publish": 42, "ok": True, "rate": None},
    }


def test_long_lists_are_trimmed_with_a_note():
    out = redact_body(json.dumps([{"n": i} for i in range(10)]))
    note, _, rest = out.partition("\n")
    assert "10 items" in note
    assert len(json.loads(rest)) == MAX_LIST_ITEMS


def test_non_json_text_is_pattern_redacted():
    out = redact_text("connect to broker.corp.example.com (192.168.1.9) as ops@example.com")
    assert "example" not in out and "192.168" not in out
    assert "<host>" in out and "<ipv4>" in out and "<user@host>" in out
