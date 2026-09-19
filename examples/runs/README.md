# Committed run records

Each directory is the on-disk output of one real pipeline run, kept as
evidence and as the input to the published reports. Nothing here is
hand-written — these are the files the pipeline wrote.

| Run | What it demonstrates |
|---|---|
| `rabbitmq-full` | Clean generate from local docs: per-stage cost attribution across tiers |
| `rabbitmq` | The same integration after an upstream API change, repaired in one iteration |
| `rabbitmq-url` | Documentation fetched over HTTP and normalized before extraction |
| `snmp-ifmib` | SNMP path on a full-featured device — one model call total |
| `snmp-oldswitch2` | SNMP path on a device lacking `ifXTable`, forcing a selection fallback |

Files you'll find in each:

- `spec.json` — the metric IR: the machine-checkable contract validation enforces
- `report_N.json` — validation result per iteration, with per-metric expected-vs-observed outcomes
- `usage.json` — token ledger, by model and by pipeline stage
- `exporter.py` / `snmp.yml` + `generator.yml` — the generated artifact
- `selection.json` — SNMP only: the one model-produced artifact in that path
- `surface.json` — REST only: the extracted metric surface
- `walk_N.txt` — SNMP only: the live device walk fed to repair as ground truth
- `docs.md` — the normalized doc corpus, when docs were ingested

Process logs, compiled MIB JSON, and `__pycache__` are omitted as noise.

Rendered pages live in [`../../docs/reports/`](../../docs/reports). Rebuild
them from these records with:

```bash
PYTHONPATH=src python3 -m miagent.cli report --manifest docs/reports.manifest.json
```
