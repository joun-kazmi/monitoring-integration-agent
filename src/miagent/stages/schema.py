"""Stage 3 — metric-IR design (standard tier).

Turn the extracted surface into a Prometheus-convention IntegrationSpec.
The IR doubles as the expectation spec the validator diffs against, so
whatever the model designs here is exactly what the generated artifact
will be held to.
"""

from __future__ import annotations

from miagent.ir import IntegrationSpec
from miagent.llm.router import LLMRouter, Stage
from miagent.stages.extract import SurfaceSpec

SYSTEM = """\
You design Prometheus metric schemas for a monitoring-integration
generator. Follow Prometheus naming conventions strictly:
- snake_case names prefixed with the service name (e.g. rabbitmq_)
- cumulative counters end in _total; gauges do not
- include a unit suffix where applicable (_bytes, _seconds, _milliseconds)
- labels for dimensions (queue name, vhost, node), never for values
- keep label cardinality bounded: no timestamps, IDs, or unbounded values
  as labels
- every metric gets a clear `help` string and a `source` noting the
  endpoint + JSON path it comes from
- set value expectations (min/max) only where genuinely known
- per-object metrics (e.g. per-queue) must carry the identifying labels
  from that object
Mark a metric required=false only if the docs say the field can be absent."""


def design_schema(
    router: LLMRouter, surface: SurfaceSpec, service: str, kind: str
) -> IntegrationSpec:
    prompt = (
        f"Service: {service}\n"
        f"Artifact kind: {kind}\n\n"
        "Extracted metric surface (endpoints + candidate fields):\n"
        f"{surface.model_dump_json(indent=2)}\n\n"
        f"Design the complete metric schema. Use service={service!r} and "
        f"kind={kind!r} in the output. Include the endpoints from the "
        "surface in the output's endpoints list."
    )
    spec = router.structured(
        Stage.schema_design, prompt, IntegrationSpec, system=SYSTEM
    )
    # Don't trust the model for these two fields — they're ours.
    spec.service = service
    spec.kind = kind
    return spec
