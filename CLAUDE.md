# monitoring-integration-agent — project context for Claude sessions

Monitoring-integration generator agent: docs/MIB in → working exporter or
collector config out, validated by **executing against the real endpoint**
and diffing scraped metrics against a machine-checkable IR spec.

**Read `docs/HANDOFF.md` before starting new work** — it has the remaining
roadmap with per-item implementation guidance and the environment gotchas.

## Core design principles (do not violate)

1. **Validation is 100% deterministic.** No LLM ever judges correctness.
   The harness (`src/miagent/validate/`) scrapes, lints, parses, and diffs
   against the IR. LLMs only: understand docs, generate artifacts, repair.
2. **Deterministic code before LLM calls.** OpenAPI parsing, HTML
   stripping, MIB compilation, failure classification — all code. An LLM
   call needs justification; a regex doesn't.
3. **Cost tiering via the router** (`src/miagent/llm/router.py`): stages map
   to fast (haiku) / standard (sonnet) / strong (opus) tiers; the repair
   loop escalates standard→strong after `MIAGENT_ESCALATE_AFTER` (2) failed
   iterations, hard cap `MIAGENT_MAX_REPAIR_ITERATIONS` (6).
4. **The IR (`src/miagent/ir.py`) is the contract.** Schema design emits it,
   generation targets it, validation enforces it. Never let generation and
   validation drift from a shared spec.
5. **Provider-agnostic LLM layer.** Backends: `claude_cli` (default; local
   Claude Code login, no API key), `anthropic` (API key), `openai` (any
   OpenAI-compatible: OpenRouter, vLLM, Ollama). Never hardcode a backend
   or model name in stage code — always go through `LLMRouter`.

## Running things

```bash
PYTHONPATH=src python3 -m pytest tests/ -q          # tests (all must pass)
PYTHONPATH=src python3 -m miagent.cli llm-smoke        # verify LLM backend
python3 examples/rabbitmq/mock_server.py 15672 &     # mock target (add --break to break it)
MIAGENT_TARGET_USERNAME=guest MIAGENT_TARGET_PASSWORD=guest \
PYTHONPATH=src python3 -m miagent.cli generate \
  --service rabbitmq --docs examples/rabbitmq/docs.md \
  --target http://127.0.0.1:15672 \
  --workdir build/rabbitmq
```

Use `PYTHONPATH=src` — `pip install -e` fails on this box (old setuptools,
no PEP 660). Long pipeline runs: use background execution and tail the
output; a full generate run is ~2–4 minutes.

SNMP path (needs the pinned tools below, all already installed):

```bash
snmpsim-command-responder --data-dir=examples/snmp \
    --agent-udpv4-endpoint=127.0.0.1:11162 --quiet &   # community = filename
PYTHONPATH=src python3 -m miagent.cli generate-snmp \
  --service oldswitch --mib IF-MIB --target 127.0.0.1:11162 \
  --community oldswitch --port 9119 --workdir build/snmp-oldswitch
```

## Environment constraints (this machine)

- **No sudo, no docker.** Can't run real RabbitMQ or containerized otelcol.
  Static binary downloads to `~/bin` or `./bin` work fine (network is open).
- `bwrap` works here (unprivileged userns allowed), so generated exporters
  run sandboxed; `MIAGENT_SANDBOX=off` to debug outside it.
- `promtool` / `otelcol` not installed — harness skips promtool lint
  gracefully. Installing them (static binaries) is a welcome improvement.
- Python 3.10 — no 3.11+ syntax. Installed: pydantic v2, httpx, requests,
  prometheus_client, bs4, yaml, anthropic, openai, pytest, and for the
  SNMP path pysmi 2.0 (`mibdump` in `~/.local/bin`), pysnmp 7, snmpsim 1.2.
- `./bin/snmp_exporter` (v0.30.1) is committed-adjacent but gitignored-
  sized; re-download from the prometheus/snmp_exporter release tarball if
  missing. The Go `generator` binary is NOT in the tarball and can't be
  built here (no Go) — `snmp/compile.py` replaces it deliberately.

## Hard-won gotchas

- **claude CLI backend must pass `--tools "" --strict-mcp-config`**
  (already in `src/miagent/llm/claude_cli.py`). Without both, repair-style
  prompts make the model call Claude Code tools / user MCP servers and the
  single-turn run dies with `stop_reason: tool_use`. Don't remove them.
- Don't `pkill -f <pattern>` where the pattern appears in your own command
  line — it kills your shell (exit 144). Use `pgrep | grep -v $$ | xargs kill`
  in a separate call from any command that mentions the pattern.
- claude CLI JSON reports cache tokens separately; the backend folds
  `cache_read/creation_input_tokens` into `input_tokens` for the ledger.
- The counter-family name convention: the Prometheus parser strips `_total`
  from counter family names; `validate/harness.py::_find_family` handles
  both spellings. IR counter names should end in `_total`.
