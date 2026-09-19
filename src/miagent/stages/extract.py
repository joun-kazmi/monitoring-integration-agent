"""Stage 2 — metric-surface extraction (standard tier).

From documentation text, extract which endpoints expose metric data, how to
authenticate, and which fields are candidate metrics. Output is structured
and gets sanity-probed deterministically before anything downstream spends
tokens on it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from miagent.ir import EndpointSpec
from miagent.llm.router import LLMRouter, Stage

SYSTEM = """\
You analyze API documentation for a monitoring-integration generator.
Identify every HTTP endpoint that exposes numeric operational data usable
as metrics. Be precise: only endpoints and fields that the documentation
actually describes. Relative paths are fine for endpoint URLs when the
docs don't give a full base URL."""


class FieldCandidate(BaseModel):
    endpoint_url: str
    json_path: str  # e.g. "message_stats.publish" or "[].messages"
    description: str = ""
    suggested_kind: str = "gauge"  # counter | gauge
    labels_from: list[str] = Field(
        default_factory=list,
        description="JSON fields on the same object that should become labels, e.g. ['name', 'vhost']",
    )


class SurfaceSpec(BaseModel):
    endpoints: list[EndpointSpec]
    fields: list[FieldCandidate]
    notes: str = ""


def extract_surface(router: LLMRouter, docs: str, service: str) -> SurfaceSpec:
    prompt = (
        f"Service: {service}\n\n"
        "Documentation:\n"
        "<<<DOCS\n"
        f"{docs}\n"
        "DOCS>>>\n\n"
        "Extract the metric surface: the endpoints to poll, their auth "
        "scheme, and every documented numeric field that is a useful metric "
        "candidate, with its JSON path and whether it behaves as a "
        "cumulative counter or a point-in-time gauge."
    )
    return router.structured(Stage.extract, prompt, SurfaceSpec, system=SYSTEM)
