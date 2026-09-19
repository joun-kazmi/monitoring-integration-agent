import json

from miagent.ingest import DocKind, condense_openapi, html_to_text, sniff

OPENAPI_JSON = json.dumps({
    "openapi": "3.0.0",
    "info": {"title": "Pet Store", "description": "A sample API."},
    "servers": [{"url": "https://api.example.com/v1"}],
    "components": {
        "securitySchemes": {"basicAuth": {"type": "http", "scheme": "basic"}},
        "schemas": {
            "Stats": {
                "type": "object",
                "properties": {
                    "requests_total": {"type": "integer", "description": "Total requests"},
                    "queue": {
                        "type": "object",
                        "properties": {"depth": {"type": "integer"}},
                    },
                },
            }
        },
    },
    "paths": {
        "/stats": {
            "get": {
                "summary": "Get statistics",
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Stats"}
                            }
                        }
                    }
                },
            }
        },
        "/pets": {"post": {"summary": "Create a pet"}},  # non-GET: excluded
    },
})

HTML = """<!DOCTYPE html>
<html><head><title>Docs</title><style>body{color:red}</style>
<script>alert(1)</script></head>
<body><nav><ul><li>Home</li></ul></nav>
<main><h1>API Reference</h1><p>Endpoints below.</p>
<h2>GET /api/overview</h2><p>Returns system stats.</p>
<table><tr><th>field</th><th>meaning</th></tr>
<tr><td>messages</td><td>total messages</td></tr></table>
</main></body></html>"""

MIB = """RABBITMQ-MIB DEFINITIONS ::= BEGIN
IMPORTS OBJECT-TYPE FROM SNMPv2-SMI;
END"""


def test_sniff_kinds():
    assert sniff(OPENAPI_JSON) is DocKind.openapi
    assert sniff("openapi: 3.0.0\ninfo:\n  title: X") is DocKind.openapi
    assert sniff(HTML) is DocKind.html
    assert sniff(MIB) is DocKind.mib
    assert sniff("# My docs\nplain markdown") is DocKind.text
    assert sniff("whatever", source="device.mib") is DocKind.mib


def test_condense_openapi():
    out = condense_openapi(OPENAPI_JSON)
    assert "Pet Store" in out
    assert "GET /stats" in out
    assert "`requests_total` (integer)" in out
    assert "`queue.depth` (integer)" in out  # nested + $ref resolved
    assert "basicAuth" in out
    assert "/pets" not in out  # non-GET dropped


def test_html_to_text():
    out = html_to_text(HTML)
    assert "# API Reference" in out
    assert "## GET /api/overview" in out
    assert "| messages | total messages |" in out
    assert "alert(1)" not in out
    assert "color:red" not in out
