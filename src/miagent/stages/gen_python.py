"""Stage 4 (python_exporter kind) — exporter code generation (strong tier)
and stage 6 — repair.

The generated file must honor a fixed CLI contract so the runner can launch
it deterministically:

    python exporter.py --port PORT --target BASE_URL

Credentials come from MIAGENT_TARGET_USERNAME / MIAGENT_TARGET_PASSWORD (basic)
and MIAGENT_TARGET_TOKEN (bearer / header / query), per each endpoint's `auth`.
"""

from __future__ import annotations

import re

from miagent.ir import IntegrationSpec
from miagent.llm.base import LLMError
from miagent.llm.router import LLMRouter, Stage
from miagent.validate.report import ValidationReport

_CODE_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)

CONTRACT = """\
The exporter is a single Python file with this exact CLI contract:
  python exporter.py --port PORT --target BASE_URL

Hard requirements:
- Credentials come ONLY from environment variables, never CLI flags (argv
  is visible to other users on the host): MIAGENT_TARGET_USERNAME /
  MIAGENT_TARGET_PASSWORD and MIAGENT_TARGET_TOKEN. Authenticate each
  endpoint per its `auth` field in the metric schema:
    basic  -> HTTP basic auth with username/password
    bearer -> header `Authorization: Bearer <token>`
    header -> header named exactly `auth_detail`, value = token
    query  -> query parameter named exactly `auth_detail`, value = token
    none   -> no auth, unless username/password are set (then basic)
  Never log the token or full request URLs that contain it.
- Python 3.10, only stdlib + `prometheus_client` + `httpx` (both installed).
  Do NOT import `requests` — it is not installed.
- Implement a custom prometheus_client Collector class (registered on a
  fresh CollectorRegistry or the default REGISTRY) that polls the target
  API on every collect() call. Use CounterMetricFamily for counters and
  GaugeMetricFamily for gauges — do NOT use Counter()/Gauge() objects,
  since the upstream values are already totals.
- Metric names, types, labels must match the provided metric schema
  EXACTLY (counters are exposed with their _total name).
- Serve /metrics with prometheus_client.start_http_server(port) and block
  forever (e.g. while True: time.sleep(...)).
- HTTP errors from the target must not crash the process: log to stderr
  and skip that scrape (expose what you can).
- Use an httpx timeout of 5 seconds.
- No placeholder code, no TODOs — complete and runnable."""

SYSTEM_GEN = (
    "You write production-quality Prometheus exporters in Python.\n" + CONTRACT
)

SYSTEM_REPAIR = (
    "You fix Prometheus exporters written in Python. Return the complete "
    "corrected file, not a diff.\n" + CONTRACT
)


def _extract_code(text: str) -> str:
    blocks = _CODE_RE.findall(text)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    stripped = text.strip()
    # Model may return bare code without fences.
    if stripped.startswith(("#!", "import ", "from ", '"""', "#")):
        return stripped + "\n"
    raise LLMError("no python code block found in model output")


def generate_python_exporter(
    router: LLMRouter, spec: IntegrationSpec, docs: str
) -> str:
    prompt = (
        f"Write the exporter for service {spec.service!r}.\n\n"
        "Metric schema (authoritative — match names/types/labels exactly):\n"
        f"{spec.model_dump_json(indent=2)}\n\n"
        "Upstream API documentation:\n<<<DOCS\n"
        f"{docs}\nDOCS>>>\n\n"
        "Each metric's `source` field tells you which endpoint and JSON "
        "path it comes from. Return only the complete Python file."
    )
    resp = router.complete(
        Stage.generate_code, prompt, system=SYSTEM_GEN, max_tokens=16000
    )
    return _extract_code(resp.text)


def repair_python_exporter(
    router: LLMRouter,
    spec: IntegrationSpec,
    code: str,
    report: ValidationReport,
    process_log: str,
    iteration: int,
    endpoint_samples: str = "",
) -> str:
    samples_block = (
        "Fresh raw responses from the live target endpoints (ground truth — "
        "if these differ from what the code expects, the API has changed "
        "and the code must adapt; the metric schema stays fixed):\n"
        f"<<<SAMPLES\n{endpoint_samples}\nSAMPLES>>>\n\n"
        if endpoint_samples
        else ""
    )
    prompt = (
        f"The exporter for {spec.service!r} failed validation "
        f"(repair iteration {iteration}).\n\n"
        "Validation report:\n"
        f"{report.summary()}\n\n"
        "Exporter stderr/stdout (tail):\n<<<LOG\n"
        f"{process_log[-4000:]}\nLOG>>>\n\n"
        + samples_block +
        "Metric schema (authoritative):\n"
        f"{spec.model_dump_json(indent=2)}\n\n"
        "Current code:\n```python\n"
        f"{code}\n```\n\n"
        "Fix the root cause of the failures. Return the complete corrected "
        "Python file."
    )
    resp = router.complete(
        Stage.repair,
        prompt,
        system=SYSTEM_REPAIR,
        repair_iteration=iteration,
        max_tokens=16000,
    )
    return _extract_code(resp.text)
