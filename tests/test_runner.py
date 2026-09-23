import socket
import textwrap
from types import SimpleNamespace

import miagent.orchestrate as orch
from miagent.ir import IntegrationSpec, MetricSpec, MetricType, ValueExpectation
from miagent.runner import RunResult, run_and_validate, scrubbed_env
from miagent.validate import FailureKind
from miagent.validate.report import Failure, ValidationReport

SPEC = IntegrationSpec(
    service="demo",
    metrics=[
        # Must be 0 (else a value warning): the child must not see the secret.
        MetricSpec(
            name="demo_leaked_secret",
            type=MetricType.gauge,
            value=ValueExpectation(min=0, max=0),
        )
    ],
)

# Exposes 1 if the parent's secret leaked into the child's environment.
EXPORTER = textwrap.dedent("""\
    import argparse, os, time
    from prometheus_client import Gauge, start_http_server
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int); p.add_argument("--target")
    a = p.parse_args()
    Gauge("demo_leaked_secret", "x").set(1 if "MIAGENT_TEST_SECRET" in os.environ else 0)
    start_http_server(a.port)
    while True:
        time.sleep(1)
""")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_scrubbed_env_drops_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-xxx")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = scrubbed_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert env["PATH"] == "/usr/bin"


def test_generated_exporter_does_not_inherit_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MIAGENT_TEST_SECRET", "hunter2")
    code = tmp_path / "exporter.py"
    code.write_text(EXPORTER)
    result = run_and_validate(
        code, SPEC, "http://127.0.0.1:1", port=_free_port(), settle_s=0
    )
    assert result.report.ok, result.report.summary() + result.process_log
    # Out-of-range values are warnings, not failures: a leak shows up here.
    assert not result.report.warnings, result.report.warnings


def _fail_report() -> ValidationReport:
    return ValidationReport(
        ok=False, metrics_expected=1,
        failures=[Failure(kind=FailureKind.scrape_error, detail="boom")],
    )


def _stub_loop(monkeypatch, passes_on: int):
    """run_and_validate passes from iteration `passes_on` on (-1 = never)."""
    calls = {"n": 0}

    def fake_run(code_path, spec, target, **kw):
        n = calls["n"]
        calls["n"] += 1
        ok = passes_on >= 0 and n >= passes_on
        report = ValidationReport(ok=True, metrics_expected=1) if ok else _fail_report()
        return RunResult(report=report, process_log="")

    monkeypatch.setattr(orch, "run_and_validate", fake_run)
    monkeypatch.setattr(orch, "probe_endpoints", lambda *a, **k: "")
    monkeypatch.setattr(
        orch, "repair_python_exporter",
        lambda router, spec, code, *a, **k: "# repaired\n" + code,
    )


ROUTER = SimpleNamespace(
    tier_for=lambda *a, **k: SimpleNamespace(value="standard"),
    usage=SimpleNamespace(calls=0, input_tokens=0, output_tokens=0, by_model={}, by_stage={}),
)


def _setup(tmp_path):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(SPEC.model_dump_json())
    code = tmp_path / "prod_exporter.py"
    code.write_text("# original\n")
    return spec_path, code


def test_failed_repair_leaves_original_untouched(tmp_path, monkeypatch):
    _stub_loop(monkeypatch, passes_on=-1)
    spec_path, code = _setup(tmp_path)
    res = orch.run_repair(spec_path, code, "http://x", tmp_path / "work", router=ROUTER)
    assert not res.ok
    assert code.read_text() == "# original\n"
    assert res.artifact_path == tmp_path / "work" / "exporter.py"
    assert res.artifact_path.read_text().startswith("# repaired")


def test_successful_repair_replaces_original(tmp_path, monkeypatch):
    _stub_loop(monkeypatch, passes_on=1)
    spec_path, code = _setup(tmp_path)
    res = orch.run_repair(spec_path, code, "http://x", tmp_path / "work", router=ROUTER)
    assert res.ok and res.artifact_path == code
    assert code.read_text() == "# repaired\n# original\n"
    assert not list(tmp_path.glob("*.miagent-tmp"))
