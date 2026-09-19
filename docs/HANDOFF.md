# monitoring-integration-agent — handoff: state, remaining work, and how to do it

Written 2026-09-19 for whichever model/session continues this project.
Read `CLAUDE.md` first for design principles and environment gotchas.

## What exists and works (all verified live)

| Piece | File(s) | Status |
|---|---|---|
| Metric IR (expectation contract) | `src/miagent/ir.py` | done |
| LLM backends: claude_cli / anthropic / openai-compat | `src/miagent/llm/` | done, all keyless-default |
| Tier router + escalation + usage ledger | `src/miagent/llm/router.py` | done |
| Stage 1: doc ingestion (URL/file, OpenAPI condense, HTML strip, MIB sniff, budget) | `src/miagent/ingest.py` | done |
| Stage 2: metric-surface extraction (sonnet, structured) | `src/miagent/stages/extract.py` | done |
| Stage 3: IR design (sonnet, structured) | `src/miagent/stages/schema.py` | done |
| Stage 4: python_exporter codegen (opus) | `src/miagent/stages/gen_python.py` | done |
| Stage 5: deterministic run+scrape+validate harness | `src/miagent/validate/`, `src/miagent/runner.py` | done |
| Stage 6: repair loop w/ live endpoint samples + escalation | `gen_python.py::repair_python_exporter`, `orchestrate.py::validate_and_repair` | done |
| SNMP path: MIB parse → select (1 LLM call) → snmp.yml/generator.yml/IR → validated | `src/miagent/snmp/`, `src/miagent/stages/snmp_select.py` | done |
| Shared process runner (used by python + snmp kinds) | `runner.py::run_process_and_validate` | done |
| CLI: generate / generate-snmp / repair / ingest / validate / llm-smoke | `src/miagent/cli.py` | done |
| Mock RabbitMQ mgmt API (+ `--break` mode) | `examples/rabbitmq/mock_server.py` | done |
| Tests (32) | `tests/` | green |

Proven scenarios:
1. Full generate against mock RabbitMQ: PASS 28/28 first try (~3 min, 2 sonnet + 1 opus calls).
2. Broke the upstream API shape (`message_stats`→`msg_stats`, `memory`→`memory_details.bytes`);
   `miagent repair` detected, sonnet fixed in 1 iteration using fresh endpoint
   samples, repaired code handles BOTH shapes. 45s, ~$0.12.
3. Docs ingested over HTTP end-to-end: PASS 27/27.

## Remaining work, in recommended order

### ~~1. SNMP path~~ — DONE (2026-09-19). Notes for maintainers:

Implemented as `miagent generate-snmp`. Key decisions that differ from the
original plan, and why:

- **We compile `snmp.yml` ourselves** (`snmp/compile.py`) instead of using
  the upstream Go `generator` binary — it isn't in the release tarball and
  needs Go + net-snmp to build (neither available here). pysmi exposes
  everything the generator needs, so this is a full substitute for our
  subset and it unlocks live validation. `generator.yml` is still emitted
  as the human-maintainable artifact.
- **One LLM call total.** The IR is *derived* from the selection
  (`selection_to_ir`) rather than designed separately, so artifact and
  expectations cannot drift — `test_ir_and_yaml_names_always_agree` locks
  this in. Don't add a second model call here.
- **Repair targets the selection, not the YAML** (the YAML is
  deterministic, so a failure means the selection was wrong). Repair is
  fed a real `pysnmp` walk of the device as ground truth — an OID absent
  from the walk is not implemented.
- **The walk must cover parent tables, not selected leaf OIDs**
  (`runner.repair_walk_oids`). This was measured, not guessed: walking
  only the selected leaves, repair on the oldswitch fixture dropped
  throughput entirely and "passed" with 5 metrics; walking the parent
  table subtrees, the same failure recovered 9 metrics by substituting
  32-bit `ifInOctets`/`ifSpeed` for the absent 64-bit objects. A green
  check on a degraded selection is the failure mode to watch for here —
  when changing repair, compare *metric counts and coverage*, not just
  pass/fail.
- **Deterministic convention enforcement** beats prompting for it:
  `normalize_metric_name` forces `_total` on counters and `_seconds` +
  `scale: 0.01` on TimeTicks/TimeStamp/TimeInterval (centiseconds). The
  model got the TimeTicks unit wrong in testing; the compiler fixes it.
- **Gotchas found the hard way**: `enum_values` keys must be YAML **ints**
  (string keys → snmp_exporter refuses to load the config); rows declared
  `AUGMENTS` (IF-MIB's `ifXTable`, home of the 64-bit counters) inherit
  their INDEX from the base row — miss that and every ifX metric loses
  its labels; lookup source OIDs must be in the `walk` list or labels come
  back empty.
- **Test/dev rig**: `snmpsim-command-responder --data-dir=<dir>
  --agent-udpv4-endpoint=127.0.0.1:11161`; the community name is the
  `.snmprec` filename. `~/.local/lib/python3.10/site-packages/snmpsim/data/public.snmprec`
  has real IF-MIB rows with simulated increasing counters.
  `examples/snmp/oldswitch.snmprec` is a device lacking `ifXTable`, used
  to exercise the repair loop.
- Enum handling is plain-gauge only. snmp_exporter also supports
  `EnumAsStateSet`/`EnumAsInfo`; if you add those, the IR derivation must
  change too (one series per state, `_info` label semantics).

**Known gaps in the SNMP path** (none block use):

- Rate-unit naming is prompt-guided, not enforced: `ifSpeed` (bits per
  second) came back as `network_interface_speed_bits` rather than
  `_bits_per_second`. The prompt now calls this out, but unlike the
  TimeTicks case it can't be fixed deterministically — "is this a rate?"
  isn't derivable from the SNMP type, only from the DESCRIPTION prose.
- Only SNMP v2c with a community string is wired up (`auths` emits one
  `public_v2` entry). v3 (auth/priv protocols, security levels) needs new
  CLI flags and an `auths` builder.
- Single module per run. Multi-MIB devices work (pass several `--mib`),
  but everything lands in one snmp.yml module.
- `miagent repair` (the fleet-refresh command) only handles
  `kind=python_exporter`. The SNMP equivalent is re-running
  `generate-snmp`, which re-does the selection call. A cheap win: teach
  `repair` to load `selection.json` and enter the SNMP loop at stage 3.

### 2. OTel collector path (`kind=otel`) — the design-preferred default for REST APIs

Goal: docs in → otelcol config YAML out → collector runs → its Prometheus
exporter endpoint validated against the IR.

- Get the binary: download **otelcol-contrib** static tarball from GitHub
  releases into `./bin/` (works without sudo; network is open). Pin the
  version in `config.py` (`settings.otelcol_path`).
- New stage module `stages/gen_otel.py`:
  - `Stage.generate_config` (standard tier — sonnet; escalate to opus via
    repair loop, mirroring the python path but starting cheaper).
  - Prompt contract: emit a single YAML doc with receivers (use the
    dedicated receiver when one exists — e.g. `rabbitmq`; else
    `httpcheck`/`prometheus` receivers), a `prometheus` exporter on
    `0.0.0.0:{port}`, and a metrics pipeline. Provide the IR + docs.
  - Extract YAML from fences (mirror `_extract_code`).
- Static gate replaces `py_compile`: `./bin/otelcol-contrib validate
  --config <file>` — free, catches most errors before any live run.
- Runner variant: launch `otelcol-contrib --config`, wait for the
  prometheus exporter port, then `validate_scrape` as usual.
- Caveat: receiver-emitted metric names are fixed by the receiver, so for
  `kind=otel` the schema stage must be told to *conform the IR to the
  receiver's documented metric names* (feed the receiver's documentation —
  ingest it from the otel-collector-contrib repo README for that receiver)
  rather than inventing names. Alternative simpler start: only support
  `prometheus`-scrape and `httpcheck` receivers plus transform processors.

### 3. Batch-API fleet mode — for hundreds of integrations on refresh

Goal: run stage calls for many services at 50% cost via Anthropic's
Message Batches (only meaningful for the `anthropic` backend; claude_cli
and openai backends fall back to sequential).

- The stages are already stateless functions keyed by explicit inputs —
  batch-friendly by construction.
- Add `AnthropicBackend.complete_batch(requests: list[...]) -> list[LLMResponse]`
  using `client.messages.batches.create` + poll + results (key by
  `custom_id`, never order). See the anthropic SDK batches API.
- Fleet flow (`miagent refresh --manifest fleet.yaml`): for each service in
  a manifest (spec path, code path, target, creds): run **deterministic
  validation first, in parallel** (free); collect only the failures; batch
  the repair calls for all failed services in one batch job; apply
  patches; re-validate; iterate. LLM spend scales with the number of
  *broken* integrations, not fleet size.
- Cost ledger: extend `Usage` with a `$` estimate table per model
  (pricing per 1M tokens; keep it in config so it can be updated).

### 4. Runner refactor — partially done

`runner.run_process_and_validate(cmd, metrics_url, spec, log_path)` is now
the shared launch/probe/scrape/teardown path, used by both the python
exporter and snmp_exporter. A third kind only needs to supply a command
and a metrics URL, so the full `ArtifactRunner` protocol is probably
unnecessary — add it only if OTel needs per-kind static checks and repair
prompts that don't fit the current shape (it likely will need a static
check: `otelcol validate --config`).

Note the two kinds have genuinely different repair targets: the python
path repairs generated *code*, the SNMP path repairs the *selection* that
the artifact is compiled from. Keep that distinction when adding kinds —
repair whatever the artifact is derived from, not the derived text.

### 5. Smaller items (any order)

- **Record-replay**: in `runner.probe_endpoints`, also persist the samples
  to `workdir/samples/` on every successful validation; on later refresh
  failures, diff current samples vs recorded to answer "did the API change
  or did we regress?" deterministically — feed that diff to repair.
- **promtool**: download prometheus release tarball to `./bin`, set
  `MIAGENT_PROMTOOL_PATH` — turns on the lint gate that exists but is
  currently skipped.
- **Escalated repair as a multi-turn subagent**: iterations ≥ escalate_after
  currently use one-shot opus calls. Better: a `claude_cli` session mode
  (drop `--max-turns 1`, allow Bash/Read tools scoped to the workdir via
  `--tools` and `--add-dir`) so opus can run the exporter and probe the
  API itself. Only for the strong tier; keep standard-tier repairs
  single-shot and cheap.
- **Anthropic backend prompt caching**: when using the API backend, add
  `cache_control` to the docs/IR prefix in stage prompts (structure the
  prompt as [docs+IR block][varying instruction]) — big savings on the
  repair loop. The claude_cli backend gets caching automatically.
- **Packaging**: `pip install -e` is broken on this box (setuptools too
  old for PEP 660). Either document `PYTHONPATH=src` (current approach) or
  add a `setup.py` shim.
- **Value-drift checks**: `ValueExpectation` supports min/max only.
  Consider counter monotonicity across two scrapes (scrape twice in the
  harness, assert counter_2 >= counter_1) — deterministic, cheap, catches
  gauge-as-counter bugs.

## Model guidance for whoever runs this next

The tier defaults live in `router.py::DEFAULT_MODELS`. If running as Opus
(or with an Opus-tier budget) rather than Fable:

- Nothing in this repo depends on the driving model. The *driving* session
  (you) mainly orchestrates code edits; the pipeline's own LLM calls go
  through the router and are already tiered.
- If claude_cli subscription limits bite, switch backends:
  `MIAGENT_LLM_BACKEND=anthropic` + `ANTHROPIC_API_KEY`, or
  `MIAGENT_LLM_BACKEND=openai` + `MIAGENT_BASE_URL=https://openrouter.ai/api/v1`
  + `MIAGENT_API_KEY` + `MIAGENT_MODEL_{FAST,STANDARD,STRONG}`.
- Keep the discipline: run the tests after every change
  (`PYTHONPATH=src python3 -m pytest tests/ -q`), run long pipeline
  commands in the background, and demo each new path against the mock
  (`examples/rabbitmq/mock_server.py`, `--break` for repair testing)
  before calling it done.
