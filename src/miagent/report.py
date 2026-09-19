"""Render a run workdir into one self-contained HTML report. No LLM.

Every input already exists on disk after a run — the IR, the per-iteration
validation reports, the usage ledger, the generated artifact, and (for the
SNMP path) the selection and device walks. This module is a deterministic
template over those files, so a report can be regenerated for any old run
without re-spending a single token.

    miagent report build/rabbitmq -o docs/reports/rabbitmq.html
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Artifact filenames per kind, in display order.
_ARTIFACT_FILES = (
    ("exporter.py", "python", "Generated exporter"),
    ("snmp.yml", "yaml", "Compiled snmp_exporter config"),
    ("generator.yml", "yaml", "generator.yml (human-maintained source)"),
    ("spec.json", "json", "Metric IR (validation contract)"),
    ("selection.json", "json", "SNMP selection (the one model-produced artifact)"),
    ("surface.json", "json", "Extracted metric surface"),
)

# Stage order and which are deterministic (no model call).
_STAGE_ORDER = (
    ("ingest", "Ingest docs", True),
    ("classify", "Classify", False),
    ("mib", "Parse MIB", True),
    ("extract", "Extract surface", False),
    ("schema", "Design IR", False),
    ("snmp_select", "Select objects", False),
    ("gen_config", "Generate config", False),
    ("gen_code", "Generate code", False),
    ("compile", "Compile artifacts", True),
    ("validate", "Run + validate", True),
    ("repair", "Repair", False),
)

_STATUS_CLASS = {
    "ok": "ok",
    "missing": "bad",
    "type_mismatch": "bad",
    "label_mismatch": "bad",
    "value_suspect": "warn",
}
_STATUS_GLYPH = {
    "ok": "✓",
    "missing": "✗",
    "type_mismatch": "✗",
    "label_mismatch": "✗",
    "value_suspect": "!",
}


@dataclass
class RunData:
    """Everything the renderer found in a workdir."""

    name: str
    spec: dict = field(default_factory=dict)
    reports: list[tuple[int, dict]] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    walks: dict[int, str] = field(default_factory=dict)
    artifacts: list[tuple[str, str, str, str]] = field(default_factory=list)
    docs_chars: int = 0

    @property
    def final(self) -> dict:
        return self.reports[-1][1] if self.reports else {}

    @property
    def ok(self) -> bool:
        return bool(self.final.get("ok"))

    @property
    def repair_iterations(self) -> int:
        return max(0, len(self.reports) - 1)

    @property
    def kind(self) -> str:
        return self.spec.get("kind", "unknown")

    @property
    def service(self) -> str:
        return self.spec.get("service", self.name)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def load_run(workdir: Path) -> RunData:
    """Collect a run's artifacts. Tolerates anything being absent —
    different artifact kinds emit different files."""
    run = RunData(name=workdir.name)
    run.spec = _read_json(workdir / "spec.json")
    run.usage = _read_json(workdir / "usage.json")

    for path in sorted(workdir.glob("report_*.json")):
        m = re.search(r"report_(\d+)\.json$", path.name)
        if m:
            run.reports.append((int(m.group(1)), _read_json(path)))
    run.reports.sort(key=lambda t: t[0])

    for path in sorted(workdir.glob("walk_*.txt")):
        m = re.search(r"walk_(\d+)\.txt$", path.name)
        if m:
            run.walks[int(m.group(1))] = path.read_text()

    docs = workdir / "docs.md"
    if docs.exists():
        run.docs_chars = len(docs.read_text())

    for fname, lang, label in _ARTIFACT_FILES:
        p = workdir / fname
        if p.exists():
            run.artifacts.append((fname, lang, label, p.read_text()))
    return run


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)


def _fmt_value(v: Any) -> str:
    if v is None:
        return "—"
    f = float(v)
    if f == int(f) and abs(f) < 1e15:
        return f"{int(f):,}"
    return f"{f:,.4g}"


def _e(s: Any) -> str:
    return html.escape(str(s), quote=True)


def _stage_rows(run: RunData) -> list[dict]:
    """Pipeline strip: the stages this run actually executed."""
    by_stage = run.usage.get("by_stage") or {}
    rows: list[dict] = []
    for key, label, deterministic in _STAGE_ORDER:
        if deterministic:
            # Infer which deterministic stages ran from the artifacts present.
            present = {
                "ingest": run.docs_chars > 0,
                "mib": any(a[0] == "selection.json" for a in run.artifacts),
                "compile": any(a[0] in ("snmp.yml", "generator.yml") for a in run.artifacts),
                "validate": bool(run.reports),
            }.get(key, False)
            if not present:
                continue
            detail = ""
            if key == "ingest":
                detail = f"{run.docs_chars:,} chars normalized"
            elif key == "validate":
                detail = f"{len(run.reports)} run(s), 0 tokens"
            rows.append({"label": label, "tier": "code", "model": "—", "detail": detail})
            continue
        entry = by_stage.get(key)
        if not entry:
            continue
        rows.append(
            {
                "label": label,
                "tier": entry.get("tier", ""),
                "model": entry.get("model", ""),
                "detail": f"{entry.get('calls', 0)} call(s) · "
                f"{_fmt_tokens(entry.get('input_tokens', 0))} in / "
                f"{_fmt_tokens(entry.get('output_tokens', 0))} out",
            }
        )
    return rows


def _metric_rows(run: RunData) -> list[dict]:
    """Expected-vs-observed table from the final validation report.

    Falls back to deriving presence from `failures` for reports written
    before per-metric outcomes were recorded.
    """
    final = run.final
    outcomes = final.get("metrics") or []
    if outcomes:
        return outcomes

    missing = {
        f.get("metric")
        for f in (final.get("failures", []) + final.get("warnings", []))
        if f.get("kind") == "missing_metric"
    }
    rows = []
    for m in run.spec.get("metrics", []):
        rows.append(
            {
                "name": m["name"],
                "expected_type": m.get("type", ""),
                "observed_type": "" if m["name"] in missing else m.get("type", ""),
                "status": "missing" if m["name"] in missing else "ok",
                "series_count": 0,
                "sample_value": None,
                "expected_labels": [l["name"] for l in m.get("labels", [])],
                "observed_labels": [],
                "required": m.get("required", True),
                "detail": "metric not found in scrape output" if m["name"] in missing else "",
                "_legacy": True,
            }
        )
    return rows


LIGHT_VARS = (
    "--bg:#fff;--fg:#14161a;--muted:#5c6470;--line:#e3e6ea;--panel:#f7f8fa;"
    "--ok:#0f7b3f;--okbg:#e7f5ec;--bad:#b3261e;--badbg:#fdecea;--warn:#8a5a00;"
    "--warnbg:#fff4e0;--code:#0b3a67;--accent:#1c4e80"
)
DARK_VARS = (
    "--bg:#0f1115;--fg:#e6e8ec;--muted:#9aa3b0;--line:#262b33;--panel:#161a20;"
    "--ok:#5ad68d;--okbg:#0e2a1b;--bad:#ff8f84;--badbg:#2c1513;--warn:#f0c26b;"
    "--warnbg:#2b2110;--code:#8fc4ff;--accent:#7fb2e8"
)


def theme_css(theme: Optional[str] = None) -> str:
    """Palette block. `theme` pins light/dark instead of following the
    viewer's preference — needed to render deterministic README images,
    since headless Chromium's color-scheme emulation is unreliable."""
    if theme == "light":
        return f":root{{{LIGHT_VARS}}}"
    if theme == "dark":
        return f":root{{{DARK_VARS}}}"
    return (
        f":root{{{LIGHT_VARS}}}"
        f"@media (prefers-color-scheme:dark){{:root{{{DARK_VARS}}}}}"
    )


CSS = """
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
code,.mono,pre,td.m,th.m{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 64px}
h1{font-size:19px;margin:0 0 2px;font-weight:650}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);
margin:34px 0 10px;font-weight:650}
.sub{color:var(--muted);font-size:13px}
.hero{border:1px solid var(--line);border-radius:8px;padding:16px 18px;background:var(--panel)}
.hero .top{display:flex;flex-wrap:wrap;gap:12px;align-items:baseline;justify-content:space-between}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;font-weight:700;font-size:12px;
letter-spacing:.04em}
.badge.ok{background:var(--okbg);color:var(--ok)} .badge.bad{background:var(--badbg);color:var(--bad)}
.kpis{display:flex;flex-wrap:wrap;gap:22px;margin-top:14px}
.kpi .v{font-size:19px;font-weight:650;font-family:ui-monospace,monospace}
.kpi .k{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.note{margin-top:12px;padding:9px 11px;border-left:3px solid var(--accent);
background:var(--bg);color:var(--muted);font-size:13px;border-radius:0 4px 4px 0}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:13px}
th{text-align:left;font-weight:650;font-size:11px;text-transform:uppercase;letter-spacing:.05em;
color:var(--muted);padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
td.m{font-size:12.5px}
.t-ok{color:var(--ok);font-weight:700} .t-bad{color:var(--bad);font-weight:700}
.t-warn{color:var(--warn);font-weight:700}
.pill{display:inline-block;padding:1px 7px;border-radius:4px;font-size:11px;font-weight:650;
background:var(--panel);border:1px solid var(--line);color:var(--muted)}
.pill.code{background:var(--okbg);color:var(--ok);border-color:transparent}
.pill.fast{color:var(--accent)} .pill.standard{color:var(--fg)}
.pill.strong{background:var(--warnbg);color:var(--warn);border-color:transparent}
.dim{color:var(--muted)}
.iter{border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin-bottom:10px}
.iter h3{margin:0 0 6px;font-size:13px;font-family:ui-monospace,monospace}
.flist{margin:6px 0 0;padding-left:18px}
.flist li{margin:1px 0;font-family:ui-monospace,monospace;font-size:12px}
details{border:1px solid var(--line);border-radius:8px;margin-bottom:8px;background:var(--panel)}
summary{cursor:pointer;padding:10px 14px;font-weight:600;font-size:13px}
summary .dim{font-weight:400}
details pre{margin:0;padding:12px 14px;overflow-x:auto;background:var(--bg);
border-top:1px solid var(--line);font-size:12px;line-height:1.5;
border-radius:0 0 7px 7px;max-height:520px;overflow-y:auto}
footer{margin-top:44px;padding-top:14px;border-top:1px solid var(--line);
color:var(--muted);font-size:12px}
a{color:var(--accent)}
"""


def render_html(run: RunData, title: Optional[str] = None,
                theme: Optional[str] = None) -> str:
    final = run.final
    metrics = _metric_rows(run)
    legacy = bool(metrics and metrics[0].get("_legacy"))
    found = final.get("metrics_found", 0)
    expected = final.get("metrics_expected", len(metrics))
    usage = run.usage
    total_series = sum(int(m.get("series_count") or 0) for m in metrics)

    status = "ok" if run.ok else "bad"
    heading = title or f"{run.service} · {run.kind}"

    p: list[str] = []
    p.append(f"<div class='wrap'><h1>{_e(heading)}</h1>")
    p.append(
        f"<div class='sub'>miagent run report · workdir <code>{_e(run.name)}</code></div>"
    )

    # ---- hero ----
    p.append("<div class='hero' style='margin-top:14px'><div class='top'>")
    p.append(
        f"<div><span class='badge {status}'>{'PASS' if run.ok else 'FAIL'}</span> "
        f"<span class='mono'>{found}/{expected} expected metrics verified</span></div>"
    )
    if final.get("scrape_url"):
        p.append(f"<div class='dim mono' style='font-size:12px'>{_e(final['scrape_url'])}</div>")
    p.append("</div><div class='kpis'>")
    for k, v in (
        ("repair iterations", run.repair_iterations),
        ("model calls", usage.get("calls", 0)),
        ("tokens in", _fmt_tokens(usage.get("input_tokens", 0))),
        ("tokens out", _fmt_tokens(usage.get("output_tokens", 0))),
        ("validation runs", len(run.reports)),
        ("series scraped", total_series or "—"),
    ):
        p.append(f"<div class='kpi'><div class='v'>{_e(v)}</div><div class='k'>{_e(k)}</div></div>")
    p.append("</div>")
    p.append(
        "<div class='note'>Every validation run above cost <strong>0 tokens</strong>: "
        "correctness is decided by scraping the artifact and diffing against the IR, "
        "never by a model. Models are used only to read docs, generate the artifact, "
        "and repair it.</div>"
    )
    p.append("</div>")

    # ---- pipeline ----
    rows = _stage_rows(run)
    if rows:
        p.append("<h2>Pipeline</h2><div class='scroll'><table>")
        p.append("<tr><th>Stage</th><th>Executed by</th><th>Model</th><th>Cost</th></tr>")
        for r in rows:
            tier = r["tier"]
            label = "deterministic code" if tier == "code" else f"{tier} tier"
            p.append(
                f"<tr><td>{_e(r['label'])}</td>"
                f"<td><span class='pill {_e(tier)}'>{_e(label)}</span></td>"
                f"<td class='m'>{_e(r['model'])}</td>"
                f"<td class='m dim'>{_e(r['detail'])}</td></tr>"
            )
        p.append("</table></div>")
        if not (usage.get("by_stage")):
            p.append(
                "<div class='note'>Per-stage attribution was added after this run; "
                "only totals are available here.</div>"
            )

    # ---- validation diff ----
    p.append("<h2>Validation diff · expected (IR) vs observed (scrape)</h2>")
    p.append("<div class='scroll'><table>")
    p.append(
        "<tr><th></th><th>Metric</th><th>Type</th><th>Observed</th><th>Series</th>"
        "<th>Sample</th><th>Labels</th><th>Note</th></tr>"
    )
    for m in metrics:
        st = m.get("status", "ok")
        cls = _STATUS_CLASS.get(st, "warn")
        exp_lbl = ", ".join(m.get("expected_labels") or []) or "—"
        obs_lbl = ", ".join(m.get("observed_labels") or [])
        lbl = exp_lbl if (not obs_lbl or obs_lbl == exp_lbl) else f"{exp_lbl} → {obs_lbl}"
        note = m.get("detail") or ""
        if st == "missing" and not m.get("required", True):
            note = note or "optional, absent"
        p.append(
            f"<tr><td class='t-{cls}'>{_STATUS_GLYPH.get(st, '?')}</td>"
            f"<td class='m'>{_e(m['name'])}</td>"
            f"<td class='m dim'>{_e(m.get('expected_type', ''))}</td>"
            f"<td class='m'>{_e(m.get('observed_type') or '—')}</td>"
            f"<td class='m'>{_e(m.get('series_count') or '—')}</td>"
            f"<td class='m dim'>{_e(_fmt_value(m.get('sample_value')))}</td>"
            f"<td class='m dim'>{_e(lbl)}</td>"
            f"<td class='dim'>{_e(note)}</td></tr>"
        )
    p.append("</table></div>")
    if legacy:
        p.append(
            "<div class='note'>This run predates per-metric outcome recording, so "
            "series counts and sampled values are unavailable; presence was derived "
            "from the failure list.</div>"
        )
    if final.get("unexpected_metrics"):
        p.append(
            "<div class='note'>Also present, not in the IR (informational): "
            f"<code>{_e(', '.join(final['unexpected_metrics'][:12]))}</code></div>"
        )

    # ---- repair history ----
    if len(run.reports) > 1 or (run.reports and not run.ok):
        p.append("<h2>Repair history</h2>")
        for i, rep in run.reports:
            ok = rep.get("ok")
            cls = "ok" if ok else "bad"
            p.append("<div class='iter'>")
            p.append(
                f"<h3>iteration {i} · <span class='t-{cls}'>"
                f"{'PASS' if ok else 'FAIL'}</span> "
                f"<span class='dim'>{rep.get('metrics_found', 0)}/"
                f"{rep.get('metrics_expected', 0)} metrics</span></h3>"
            )
            fails = rep.get("failures") or []
            if fails:
                kinds: dict[str, int] = {}
                for f in fails:
                    kinds[f.get("kind", "?")] = kinds.get(f.get("kind", "?"), 0) + 1
                p.append(
                    "<div class='dim'>failures: "
                    + ", ".join(f"<code>{_e(k)}</code> ×{v}" for k, v in sorted(kinds.items()))
                    + "</div>"
                )
                p.append("<ul class='flist'>")
                for f in fails[:6]:
                    loc = f" [{f['metric']}]" if f.get("metric") else ""
                    p.append(f"<li>{_e(f.get('kind', ''))}{_e(loc)}: {_e(f.get('detail', ''))[:180]}</li>")
                if len(fails) > 6:
                    p.append(f"<li class='dim'>… {len(fails) - 6} more</li>")
                p.append("</ul>")
            if not ok and (i + 1) in run.walks:
                walk = run.walks[i + 1]
                responding = len([l for l in walk.splitlines() if "=" in l])
                p.append(
                    "<div class='dim' style='margin-top:6px'>evidence fed to repair: "
                    f"live device walk — {responding} responding OID(s) "
                    "(an object absent here is not implemented, so repair must "
                    "substitute or drop it)</div>"
                )
            p.append("</div>")

    # ---- cost ledger ----
    if usage:
        p.append("<h2>Cost ledger</h2><div class='scroll'><table>")
        p.append("<tr><th>Model</th><th>Calls</th><th>Tokens in</th><th>Tokens out</th></tr>")
        for model, e in sorted((usage.get("by_model") or {}).items()):
            p.append(
                f"<tr><td class='m'>{_e(model)}</td><td class='m'>{_e(e.get('calls', 0))}</td>"
                f"<td class='m'>{_e(_fmt_tokens(e.get('input_tokens', 0)))}</td>"
                f"<td class='m'>{_e(_fmt_tokens(e.get('output_tokens', 0)))}</td></tr>"
            )
        p.append(
            f"<tr><td class='m'><strong>total</strong></td>"
            f"<td class='m'><strong>{_e(usage.get('calls', 0))}</strong></td>"
            f"<td class='m'><strong>{_e(_fmt_tokens(usage.get('input_tokens', 0)))}</strong></td>"
            f"<td class='m'><strong>{_e(_fmt_tokens(usage.get('output_tokens', 0)))}</strong></td></tr>"
        )
        p.append(
            f"<tr><td class='m'>validation ({len(run.reports)} run(s))</td>"
            "<td class='m t-ok'>0</td><td class='m t-ok'>0</td><td class='m t-ok'>0</td></tr>"
        )
        p.append("</table></div>")

    # ---- artifacts ----
    if run.artifacts:
        p.append("<h2>Artifacts</h2>")
        for fname, _lang, label, text in run.artifacts:
            lines = text.count("\n") + 1
            p.append(
                f"<details><summary>{_e(fname)} "
                f"<span class='dim'>— {_e(label)} · {lines} lines</span></summary>"
                f"<pre>{_e(text)}</pre></details>"
            )

    p.append(
        "<footer>Generated by <code>miagent report</code> from artifacts on disk — "
        "no model calls involved. Validation outcomes are reproducible by re-running "
        "<code>miagent validate</code> against the same target.</footer>"
    )
    p.append("</div>")

    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{_e(heading)} · miagent report</title>"
        f"<style>{theme_css(theme)}{CSS}</style></head>"
        f"<body>{''.join(p)}</body></html>"
    )


def build_report(workdir: Path, out: Optional[Path] = None,
                 title: Optional[str] = None, theme: Optional[str] = None) -> Path:
    run = load_run(workdir)
    if not run.reports and not run.spec:
        raise FileNotFoundError(
            f"{workdir} has no spec.json or report_*.json — not a miagent run workdir"
        )
    html_text = render_html(run, title=title, theme=theme)
    out = out or (workdir / "report.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_text)
    return out


INDEX_INTRO = (
    "Each page below is a full record of one pipeline run: the metric contract "
    "the model designed, the artifact it generated, what the artifact actually "
    "produced when executed against a live target, and the per-stage token cost. "
    "Correctness in every run was decided by scraping the artifact and diffing it "
    "against the contract — never by a model."
)


def build_index(manifest_path: Path, repo_url: str = "",
                theme: Optional[str] = None) -> Path:
    """Regenerate every report listed in a manifest, plus an index page.

    One command rebuilds the whole published set, so the samples can't drift
    from the code that produced them.

    Manifest: {"output_dir": "...", "reports": [{workdir, file, title, blurb}]}
    """
    manifest = json.loads(manifest_path.read_text())
    repo_url = repo_url or manifest.get("repo_url", "")
    out_dir = (manifest_path.parent / manifest.get("output_dir", ".")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    cards: list[str] = []
    for item in manifest["reports"]:
        wd = (manifest_path.parent / item["workdir"]).resolve()
        target = out_dir / item["file"]
        run = load_run(wd)
        build_report(wd, out=target, title=item.get("title"), theme=theme)

        final = run.final
        stats = [
            f"{'PASS' if run.ok else 'FAIL'} "
            f"{final.get('metrics_found', 0)}/{final.get('metrics_expected', 0)} metrics",
            f"{run.repair_iterations} repair iteration(s)",
            f"{run.usage.get('calls', 0)} model call(s)",
            f"{_fmt_tokens(run.usage.get('input_tokens', 0))} in / "
            f"{_fmt_tokens(run.usage.get('output_tokens', 0))} out",
        ]
        cards.append(
            f"<a class='card' href='{_e(item['file'])}'>"
            f"<div class='ct'>{_e(item.get('title', item['file']))}</div>"
            f"<div class='cb'>{_e(item.get('blurb', ''))}</div>"
            f"<div class='cs mono'>{' · '.join(_e(s) for s in stats)}</div></a>"
        )

    extra = (
        ".card{display:block;border:1px solid var(--line);border-radius:8px;padding:14px 16px;"
        "margin-bottom:10px;text-decoration:none;color:inherit;background:var(--panel)}"
        ".card:hover{border-color:var(--accent)}"
        ".ct{font-weight:650;margin-bottom:3px}"
        ".cb{color:var(--muted);font-size:13px;margin-bottom:7px}"
        ".cs{font-size:11.5px;color:var(--muted)}"
    )
    repo_link = (
        f" · <a href='{_e(repo_url)}'>source repository</a>" if repo_url else ""
    )
    body = (
        "<div class='wrap'><h1>miagent · run reports</h1>"
        "<div class='sub'>Monitoring integrations generated from docs or MIBs, "
        "verified by execution</div>"
        f"<div class='note' style='margin:16px 0 24px'>{INDEX_INTRO}</div>"
        f"{''.join(cards)}"
        f"<footer>Generated by <code>miagent report --manifest</code>{repo_link}</footer></div>"
    )
    index = out_dir / "index.html"
    index.write_text(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>miagent · run reports</title>"
        f"<style>{theme_css(theme)}{CSS}{extra}</style></head><body>{body}</body></html>"
    )
    return index
