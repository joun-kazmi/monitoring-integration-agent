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


# --- end-to-end: committed fixture exporter vs. the mock API (no LLM) -------

import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

import miagent.runner as runner  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "rabbitmq"


@pytest.fixture
def mock_api():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "examples" / "rabbitmq" / "mock_server.py"), str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            httpx.get(url + "/api/overview", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield url
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def popen_calls(monkeypatch):
    calls = []
    real = runner.subprocess.Popen

    def spy(cmd, **kw):
        calls.append((cmd, kw.get("env") or {}))
        return real(cmd, **kw)

    monkeypatch.setattr(runner.subprocess, "Popen", spy)
    return calls


def _run_fixture(tmp_path, target, password):
    spec = IntegrationSpec.model_validate_json((FIXTURE / "spec.json").read_text())
    code = tmp_path / "exporter.py"
    code.write_text((FIXTURE / "exporter.py").read_text())
    return run_and_validate(
        code, spec, target, port=_free_port(),
        username="guest", password=password, settle_s=0.2,
    )


def test_fixture_exporter_passes_against_mock(tmp_path, mock_api, popen_calls):
    result = _run_fixture(tmp_path, mock_api, "guest")
    assert result.report.ok, result.report.summary() + result.process_log
    cmd, env = popen_calls[0]
    assert "guest" not in cmd and "--password" not in cmd  # never in argv
    assert env["MIAGENT_TARGET_PASSWORD"] == "guest"


def test_fixture_exporter_fails_with_wrong_password(tmp_path, mock_api):
    # Proves the env credentials are actually used, not ignored.
    assert not _run_fixture(tmp_path, mock_api, "wrong").report.ok


def test_legacy_exporter_still_gets_argv_credentials(tmp_path, popen_calls):
    code = tmp_path / "exporter.py"
    code.write_text(EXPORTER)  # doesn't reference MIAGENT_TARGET_PASSWORD
    run_and_validate(code, SPEC, "http://x", port=_free_port(),
                     username="u", password="p", settle_s=0)
    cmd, env = popen_calls[0]
    assert cmd[-4:] == ["--username", "u", "--password", "p"]
    assert "MIAGENT_TARGET_PASSWORD" not in env


SLOW_START = textwrap.dedent("""\
    import argparse, time
    from http.server import BaseHTTPRequestHandler, HTTPServer
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int); p.add_argument("--target")
    a = p.parse_args()
    ready_at = time.monotonic() + 1.5
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if time.monotonic() < ready_at:
                self.send_response(503); self.end_headers(); return
            body = b"# TYPE demo_leaked_secret gauge\\ndemo_leaked_secret 0\\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.end_headers(); self.wfile.write(body)
        def log_message(self, *args):
            pass
    HTTPServer(("127.0.0.1", a.port), H).serve_forever()
""")


def test_readiness_retries_through_503(tmp_path):
    code = tmp_path / "exporter.py"
    code.write_text(SLOW_START)
    result = run_and_validate(code, SPEC, "http://x", port=_free_port(), settle_s=0)
    assert result.report.ok, result.report.summary() + result.process_log
