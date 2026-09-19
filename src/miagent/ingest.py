"""Stage 1 — doc ingestion & normalization. Deterministic-first.

Takes URLs or local paths, sniffs the content type by inspection (no LLM),
and normalizes everything to markdown-ish text for the extraction stage:

- OpenAPI/Swagger (JSON or YAML): parsed and condensed to the parts that
  matter for metrics — GET endpoints, auth schemes, response field names.
- HTML: stripped to readable text (scripts/nav/style dropped, headings and
  tables preserved).
- MIB files: detected and passed through (consumed by the SNMP path).
- Markdown/plain text: passed through.

The optional LLM fallback (haiku tier) only fires when sniffing is
genuinely ambiguous — which for real docs is rare.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx

from miagent.config import settings


class DocKind(str, Enum):
    openapi = "openapi"
    html = "html"
    mib = "mib"
    text = "text"  # markdown or plain text
    unknown = "unknown"


@dataclass
class IngestedDoc:
    source: str
    kind: DocKind
    text: str
    truncated: bool = False


_MIB_RE = re.compile(r"^\s*[\w-]+\s+DEFINITIONS\s*::=\s*BEGIN", re.MULTILINE)


def sniff(content: str, source: str = "") -> DocKind:
    """Deterministic content-type detection."""
    head = content[:4000]
    stripped = head.lstrip()
    lower_src = source.lower()

    if _MIB_RE.search(head) or lower_src.endswith((".mib", ".my")):
        return DocKind.mib

    # JSON OpenAPI
    if stripped.startswith("{"):
        try:
            data = json.loads(content)
            if isinstance(data, dict) and ("openapi" in data or "swagger" in data):
                return DocKind.openapi
        except json.JSONDecodeError:
            pass

    # YAML OpenAPI (cheap key check before a full parse)
    if re.search(r"^(openapi|swagger)\s*:", head, re.MULTILINE):
        return DocKind.openapi

    if stripped[:200].lower().startswith(("<!doctype html", "<html")) or (
        "<body" in head.lower() and "</" in head
    ):
        return DocKind.html

    if lower_src.endswith((".md", ".markdown", ".txt", ".rst")) or head.strip():
        return DocKind.text
    return DocKind.unknown


def fetch(source: str) -> str:
    """Fetch a URL or read a local file."""
    if source.startswith(("http://", "https://")):
        r = httpx.get(
            source,
            timeout=settings.scrape_timeout_s,
            follow_redirects=True,
            headers={"User-Agent": "miagent-doc-ingest/0.1"},
        )
        r.raise_for_status()
        return r.text
    return Path(source).read_text()


# --- HTML -----------------------------------------------------------------

_DROP_TAGS = ("script", "style", "noscript", "svg", "iframe", "form")


def html_to_text(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_DROP_TAGS):
        tag.decompose()
    # Prefer the main content region when the page declares one.
    main = soup.find("main") or soup.find("article") or soup.body or soup

    lines: list[str] = []
    for el in main.descendants:
        name = getattr(el, "name", None)
        if name and re.fullmatch(r"h[1-6]", name):
            level = int(name[1])
            text = el.get_text(" ", strip=True)
            if text:
                lines.append("\n" + "#" * level + " " + text)
        elif name == "tr":
            cells = [c.get_text(" ", strip=True) for c in el.find_all(["td", "th"])]
            if any(cells):
                lines.append("| " + " | ".join(cells) + " |")
        elif name in ("p", "li", "pre", "dt", "dd"):
            text = el.get_text(" ", strip=True)
            if text:
                prefix = "- " if name == "li" else ""
                lines.append(prefix + text)

    out: list[str] = []
    for line in lines:  # drop consecutive duplicates from nested matches
        if not out or out[-1] != line:
            out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


# --- OpenAPI --------------------------------------------------------------


def _resolve_ref(spec: dict, node):
    if isinstance(node, dict) and "$ref" in node:
        parts = node["$ref"].lstrip("#/").split("/")
        cur = spec
        for p in parts:
            if not isinstance(cur, dict) or p not in cur:
                return {}
            cur = cur[p]
        return cur if isinstance(cur, dict) else {}
    return node if isinstance(node, dict) else {}


def _schema_fields(spec: dict, schema, prefix: str = "", depth: int = 0) -> list[str]:
    """Flatten a response schema to 'path (type) — description' lines."""
    if depth > 3:
        return []
    schema = _resolve_ref(spec, schema)
    out: list[str] = []
    if schema.get("type") == "array" or "items" in schema:
        return _schema_fields(spec, schema.get("items", {}), prefix + "[]", depth + 1)
    for name, sub in (schema.get("properties") or {}).items():
        sub = _resolve_ref(spec, sub)
        path = f"{prefix}.{name}" if prefix else name
        typ = sub.get("type", "object" if sub.get("properties") else "any")
        desc = (sub.get("description") or "").split("\n")[0][:120]
        out.append(f"- `{path}` ({typ})" + (f" — {desc}" if desc else ""))
        if sub.get("properties") or sub.get("items"):
            out.extend(_schema_fields(spec, sub, path, depth + 1))
    return out


def condense_openapi(content: str) -> str:
    """Reduce an OpenAPI spec to the metric-relevant parts, deterministically."""
    try:
        spec = json.loads(content)
    except json.JSONDecodeError:
        import yaml

        spec = yaml.safe_load(content)
    if not isinstance(spec, dict):
        return content

    info = spec.get("info", {})
    lines = [f"# {info.get('title', 'API')} (OpenAPI {spec.get('openapi') or spec.get('swagger')})"]
    if info.get("description"):
        lines.append(info["description"].split("\n\n")[0][:500])
    for server in spec.get("servers", [])[:3]:
        lines.append(f"- server: {server.get('url')}")

    schemes = (spec.get("components", {}) or {}).get("securitySchemes") or spec.get(
        "securityDefinitions"
    ) or {}
    for name, sch in schemes.items():
        lines.append(
            f"- auth `{name}`: type={sch.get('type')} scheme={sch.get('scheme', '')} "
            f"in={sch.get('in', '')} name={sch.get('name', '')}".rstrip()
        )

    for path, ops in (spec.get("paths") or {}).items():
        get = (ops or {}).get("get")
        if not get:
            continue
        lines.append(f"\n## GET {path}")
        summary = get.get("summary") or get.get("description") or ""
        if summary:
            lines.append(summary.split("\n")[0][:300])
        resp = (get.get("responses") or {}).get("200") or {}
        content_types = resp.get("content") or {}
        schema = (
            content_types.get("application/json", {}).get("schema")
            or resp.get("schema")  # swagger 2
            or {}
        )
        fields = _schema_fields(spec, schema)
        if fields:
            lines.append("Response fields:")
            lines.extend(fields[:60])
    return "\n".join(lines)


# --- section filter for oversized docs ------------------------------------

_METRIC_KEYWORDS = re.compile(
    r"metric|stat|monitor|count|rate|gauge|total|memory|cpu|disk|queue|"
    r"connection|latency|duration|bytes|health|status|usage",
    re.IGNORECASE,
)


def _budget(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    # Keep heading-delimited sections that mention metric-ish things.
    sections = re.split(r"(?=^#{1,6} )", text, flags=re.MULTILINE)
    kept = [s for s in sections if _METRIC_KEYWORDS.search(s)]
    filtered = "\n".join(kept) if kept else text
    if len(filtered) > max_chars:
        filtered = filtered[:max_chars] + "\n\n[... truncated by ingestion budget ...]"
    return filtered, True


# --- entry points ----------------------------------------------------------


def ingest_source(source: str, router=None) -> IngestedDoc:
    content = fetch(source)
    kind = sniff(content, source)

    if kind is DocKind.unknown and router is not None:
        from miagent.llm.router import Stage

        resp = router.complete(
            Stage.classify,
            "Classify this document as one of: openapi, html, mib, text. "
            "Reply with the single word only.\n\n" + content[:3000],
            max_tokens=16,
        )
        try:
            kind = DocKind(resp.text.strip().lower())
        except ValueError:
            kind = DocKind.text

    if kind is DocKind.openapi:
        text = condense_openapi(content)
    elif kind is DocKind.html:
        text = html_to_text(content)
    else:
        text = content

    text, truncated = _budget(text, settings.docs_max_chars)
    return IngestedDoc(source=source, kind=kind, text=text, truncated=truncated)


def ingest(sources: list[str], router=None) -> tuple[str, list[IngestedDoc]]:
    """Ingest one or more sources into a single normalized doc corpus."""
    docs = [ingest_source(s, router=router) for s in sources]
    parts = []
    for d in docs:
        header = f"<!-- source: {d.source} (kind={d.kind.value}"
        header += ", truncated) -->" if d.truncated else ") -->"
        parts.append(f"{header}\n{d.text}")
    return "\n\n---\n\n".join(parts), docs
