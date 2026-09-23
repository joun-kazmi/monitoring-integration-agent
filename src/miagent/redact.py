"""Redaction of live target responses before they reach an LLM (no LLM).

Repair needs the *shape* of an upstream response — keys, nesting, value
types, numbers — not the operator's hostnames or addresses. JSON bodies are
walked structurally: keys, numbers, bools and nulls pass through; strings
matching identifying patterns are replaced with typed placeholders; values
under secret-looking keys are dropped; long lists are trimmed. Non-JSON
bodies get the same string patterns applied as text.

Best-effort, NOT a confidentiality boundary: it catches common shapes, and
plain identifiers (queue names, vhost names) pass through by design. Use
``--live-samples off`` when the target's data must not leave the machine.
"""

from __future__ import annotations

import json
import re
from typing import Any

MAX_LIST_ITEMS = 3

# Value dropped whatever its type (string, number, list, object). Errs toward
# dropping: a numeric field like `auth_failures` loses its value (key kept).
_SECRET_KEY = re.compile(
    r"passw|passphrase|^pass$|secret|token|api_?key|auth|cookie|credential|"
    r"private_?key|session_?id", re.I)
# String values masked even when they don't look like a hostname/IP
# (single-label hosts like "prod-db-01" evade the patterns below).
_HOST_KEY = re.compile(
    r"^(?:.*_)?(?:host|hostname|fqdn|node|server|peer|address|addr|ip|ipv4|ipv6|"
    r"cluster_name|domain)(?:es|s)?(?:_.*)?$", re.I)

# Order matters: most specific first.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+", re.I), "<url>"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*"), "<user@host>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<ipv4>"),
    (re.compile(r"\b(?:[0-9a-f]{1,4}:){2,7}[0-9a-f]{0,4}\b", re.I), "<ipv6>"),
    (re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}\b", re.I), "<host>"),
    (re.compile(r"\b[A-Za-z0-9+/_=-]{32,}\b"), "<token>"),
]


def redact_text(text: str) -> str:
    for pattern, placeholder in _PATTERNS:
        text = pattern.sub(placeholder, text)
    return text


def _walk(value: Any, notes: list[str], path: str) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if _SECRET_KEY.search(k):
                out[k] = "<redacted>"
            elif _HOST_KEY.match(k) and isinstance(v, str):
                out[k] = "<host>"
            elif _HOST_KEY.match(k) and isinstance(v, list) and all(isinstance(i, str) for i in v):
                out[k] = ["<host>"] * min(len(v), MAX_LIST_ITEMS)
            else:
                out[k] = _walk(v, notes, f"{path}.{k}")
        return out
    if isinstance(value, list):
        if len(value) > MAX_LIST_ITEMS:
            notes.append(f"{path or '$'}: {len(value)} items, first {MAX_LIST_ITEMS} shown")
        return [_walk(v, notes, f"{path}[]") for v in value[:MAX_LIST_ITEMS]]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_body(body: str) -> str:
    """Redact one response body; JSON stays valid JSON."""
    try:
        data = json.loads(body)
    except ValueError:
        return redact_text(body)
    notes: list[str] = []
    out = json.dumps(_walk(data, notes, ""), indent=1)
    if notes:
        out = "(trimmed: " + "; ".join(notes) + ")\n" + out
    return out
