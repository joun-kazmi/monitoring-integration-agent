import json

import pytest

from miagent.report import build_report, load_run, render_html

SPEC = {
    "service": "demo",
    "kind": "python_exporter",
    "endpoints": [],
    "metrics": [
        {"name": "demo_requests_total", "type": "counter", "help": "reqs",
         "labels": [{"name": "method", "required": True}], "value": {},
         "source": "", "required": True, "unit": ""},
        {"name": "demo_gone", "type": "gauge", "help": "", "labels": [],
         "value": {}, "source": "", "required": True, "unit": ""},
    ],
    "notes": "",
}

REPORT_FAIL = {
    "ok": False, "metrics_expected": 2, "metrics_found": 1,
    "failures": [{"kind": "missing_metric", "metric": "demo_gone",
                  "detail": "metric not found in scrape output", "severity": "error"}],
    "warnings": [], "unexpected_metrics": [], "scrape_url": "http://x/metrics",
    "promtool_ran": False, "metrics": [],
}

REPORT_PASS = {
    "ok": True, "metrics_expected": 2, "metrics_found": 2,
    "failures": [], "warnings": [], "unexpected_metrics": ["surprise_total"],
    "scrape_url": "http://x/metrics", "promtool_ran": True,
    "metrics": [
        {"name": "demo_requests_total", "expected_type": "counter",
         "observed_type": "counter", "status": "ok", "series_count": 3,
         "sample_value": 42.0, "expected_labels": ["method"],
         "observed_labels": ["method"], "required": True, "detail": ""},
        {"name": "demo_gone", "expected_type": "gauge", "observed_type": "gauge",
         "status": "ok", "series_count": 1, "sample_value": 7.0,
         "expected_labels": [], "observed_labels": [], "required": True, "detail": ""},
    ],
}

USAGE = {
    "calls": 3, "input_tokens": 15754, "output_tokens": 13436,
    "by_model": {"sonnet": {"calls": 2, "input_tokens": 8356, "output_tokens": 9632},
                 "opus": {"calls": 1, "input_tokens": 7398, "output_tokens": 3804}},
    "by_stage": {
        "extract": {"calls": 1, "input_tokens": 4000, "output_tokens": 5000,
                    "model": "sonnet", "tier": "standard"},
        "gen_code": {"calls": 1, "input_tokens": 7398, "output_tokens": 3804,
                     "model": "opus", "tier": "strong"},
    },
}


@pytest.fixture
def workdir(tmp_path):
    (tmp_path / "spec.json").write_text(json.dumps(SPEC))
    (tmp_path / "report_0.json").write_text(json.dumps(REPORT_FAIL))
    (tmp_path / "report_1.json").write_text(json.dumps(REPORT_PASS))
    (tmp_path / "usage.json").write_text(json.dumps(USAGE))
    (tmp_path / "exporter.py").write_text("print('hi')\n")
    (tmp_path / "docs.md").write_text("x" * 1234)
    (tmp_path / "walk_1.txt").write_text("1.3.6:\n  a = 1\n  b = 2\n")
    return tmp_path


def test_load_run_collects_artifacts(workdir):
    run = load_run(workdir)
    assert run.service == "demo"
    assert [i for i, _ in run.reports] == [0, 1]
    assert run.ok is True          # final report wins
    assert run.repair_iterations == 1
    assert run.docs_chars == 1234
    assert 1 in run.walks
    assert any(name == "exporter.py" for name, _, _, _ in run.artifacts)


def test_reports_sorted_numerically(tmp_path):
    """report_10 must not sort before report_2."""
    (tmp_path / "spec.json").write_text(json.dumps(SPEC))
    for i in (0, 2, 10):
        r = dict(REPORT_FAIL, metrics_found=i)
        (tmp_path / f"report_{i}.json").write_text(json.dumps(r))
    run = load_run(tmp_path)
    assert [i for i, _ in run.reports] == [0, 2, 10]


def test_render_contains_the_story(workdir):
    h = render_html(load_run(workdir))
    assert "PASS" in h and "2/2 expected metrics verified" in h
    # validation diff, with observed detail
    assert "demo_requests_total" in h and ">42<" in h
    # the free-validation claim is stated explicitly
    assert "0 tokens" in h
    # per-stage tiering is visible
    assert "strong tier" in h and "opus" in h
    assert "deterministic code" in h
    # repair history shows the earlier failure
    assert "Repair history" in h and "missing_metric" in h
    # walk evidence is summarised
    assert "responding OID" in h
    # artifacts embedded
    assert "exporter.py" in h


def test_render_escapes_untrusted_text(tmp_path):
    """Doc- and model-derived strings land in the page; they must be escaped."""
    spec = json.loads(json.dumps(SPEC))
    spec["service"] = "<script>alert(1)</script>"
    (tmp_path / "spec.json").write_text(json.dumps(spec))
    rep = json.loads(json.dumps(REPORT_FAIL))
    rep["failures"][0]["detail"] = "<img src=x onerror=alert(2)>"
    (tmp_path / "report_0.json").write_text(json.dumps(rep))
    h = render_html(load_run(tmp_path))
    assert "<script>alert(1)</script>" not in h
    assert "<img src=x" not in h
    assert "&lt;script&gt;" in h


def test_legacy_reports_degrade_gracefully(tmp_path):
    """Runs from before per-metric outcomes still render a usable diff."""
    (tmp_path / "spec.json").write_text(json.dumps(SPEC))
    (tmp_path / "report_0.json").write_text(json.dumps(REPORT_FAIL))
    h = render_html(load_run(tmp_path))
    assert "demo_requests_total" in h
    assert "demo_gone" in h
    assert "predates per-metric outcome recording" in h


def test_missing_usage_and_artifacts_ok(tmp_path):
    (tmp_path / "spec.json").write_text(json.dumps(SPEC))
    (tmp_path / "report_0.json").write_text(json.dumps(REPORT_PASS))
    h = render_html(load_run(tmp_path))
    assert "PASS" in h
    assert "Cost ledger" not in h  # nothing to show rather than an empty table


def test_build_report_writes_file(workdir, tmp_path):
    out = build_report(workdir, out=tmp_path / "sub" / "r.html")
    assert out.exists()
    text = out.read_text()
    assert text.startswith("<!doctype html>")
    assert "</html>" in text


def test_build_report_rejects_non_workdir(tmp_path):
    with pytest.raises(FileNotFoundError, match="not a miagent run workdir"):
        build_report(tmp_path)


def test_report_is_self_contained(workdir, tmp_path):
    """No external assets: it has to render from a file:// path or GitHub Pages."""
    h = build_report(workdir, out=tmp_path / "r.html").read_text()
    for bad in ("http://cdn", "https://cdn", "<script src", "@import", "<link "):
        assert bad not in h


def test_manifest_build_produces_index_and_reports(tmp_path):
    """The committed report set must be rebuildable from the manifest, so the
    published pages can't drift from the code that renders them."""
    import json as _json

    from miagent.report import build_index

    run = tmp_path / "runs" / "demo"
    run.mkdir(parents=True)
    (run / "spec.json").write_text(_json.dumps(SPEC))
    (run / "report_0.json").write_text(_json.dumps(REPORT_PASS))
    (run / "usage.json").write_text(_json.dumps(USAGE))

    manifest = tmp_path / "m.json"
    manifest.write_text(_json.dumps({
        "output_dir": "out",
        "reports": [{"workdir": "runs/demo", "file": "demo.html",
                     "title": "Demo run", "blurb": "what it shows"}],
    }))

    index = build_index(manifest, repo_url="https://example.com/repo")
    assert index.name == "index.html"
    assert (index.parent / "demo.html").exists()

    idx = index.read_text()
    assert "Demo run" in idx and "what it shows" in idx
    assert "PASS 2/2 metrics" in idx          # stats derived from the run
    assert "3 model call(s)" in idx
    assert "https://example.com/repo" in idx


def test_committed_reports_match_committed_runs():
    """Guards the real published set: every manifest entry must resolve."""
    import json as _json
    from pathlib import Path as _P

    manifest_path = _P("docs/reports.manifest.json")
    if not manifest_path.exists():          # tolerate running from elsewhere
        pytest.skip("manifest not present in cwd")
    manifest = _json.loads(manifest_path.read_text())
    out_dir = manifest_path.parent / manifest["output_dir"]
    for item in manifest["reports"]:
        wd = manifest_path.parent / item["workdir"]
        assert (wd / "spec.json").exists(), f"{wd} missing spec.json"
        assert list(wd.glob("report_*.json")), f"{wd} has no validation reports"
        assert (out_dir / item["file"]).exists(), f"{item['file']} not rendered"
    assert (out_dir / "index.html").exists()
