"""Execution of a generated Python exporter (part of stage 5).

Deterministic: static-check the file, launch it, wait for /metrics to come
up, hand off to the validation harness, kill the process, and return the
report plus captured logs for the repair stage.

The exporter is LLM-generated code. Every child gets a scrubbed
environment (no API keys or cloud credentials inherited) and CPU-time /
memory rlimits. When bubblewrap works (``MIAGENT_SANDBOX=auto|bwrap``), it
also runs in a bwrap sandbox: filesystem read-only, home directories,
/tmp and /run/user hidden (SSH keys, cloud creds, agent sockets), only its
workdir writable, own PID/IPC/user namespaces, killed with the runner.
The network is NOT restricted: it can reach anything the host can.
"""

from __future__ import annotations

import os
import py_compile
import resource
import shutil
import site
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from miagent.config import settings
from miagent.ir import IntegrationSpec
from miagent.redact import redact_body, redact_text
from miagent.validate.harness import validate_scrape
from miagent.validate.report import Failure, FailureKind, ValidationReport


# Environment variables the child is allowed to inherit. Everything else
# (ANTHROPIC_API_KEY, AWS_*, GITHUB_TOKEN, ...) is dropped.
_ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "TZ", "PYTHONPATH", "SYSTEMROOT")

# Resource ceilings for generated exporters.
_RLIMIT_CPU_S = 60
_RLIMIT_AS_BYTES = 1024 * 1024 * 1024  # 1 GiB address space
# No RLIMIT_NPROC: it counts every process/thread the *user* owns, so any
# fixed ceiling breaks on a busy desktop. Fork bombs need a real sandbox.


# Target credentials reach generated exporters via env, never argv (argv is
# visible to every user via ps / /proc).
ENV_TARGET_USERNAME = "MIAGENT_TARGET_USERNAME"
ENV_TARGET_PASSWORD = "MIAGENT_TARGET_PASSWORD"


def scrubbed_env(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    env.update(extra or {})
    return env


_bwrap_ok: Optional[bool] = None


def _bwrap_works() -> bool:
    """bwrap installed and unprivileged user namespaces allowed (cached)."""
    global _bwrap_ok
    if _bwrap_ok is None:
        exe = shutil.which("bwrap")
        _bwrap_ok = bool(exe) and subprocess.run(
            [exe, "--ro-bind", "/", "/", "--unshare-all", "true"],
            capture_output=True,
        ).returncode == 0
    return _bwrap_ok


def _python_dirs_under(hidden: list[Path]) -> list[Path]:
    """Interpreter/site-packages dirs the exporter needs that live under a
    hidden tree (venv or ~/.local installs), to be re-exposed read-only."""
    cands = [Path(sys.prefix), Path(sys.base_prefix), Path(site.getusersitepackages())]
    cands += [Path(d) for d in site.getsitepackages()]
    return sorted({
        c for c in cands
        if c.exists() and any(c.resolve().is_relative_to(h) for h in hidden)
    })


def sandbox_prefix(workdir: Path) -> list[str]:
    """argv prefix that runs a command in a bwrap sandbox, or [] if off.

    Raises RuntimeError when MIAGENT_SANDBOX=bwrap but bwrap can't run.
    """
    mode = settings.sandbox
    if mode == "off":
        return []
    if not _bwrap_works():
        if mode == "bwrap":
            raise RuntimeError("MIAGENT_SANDBOX=bwrap but bubblewrap is unavailable "
                               "or user namespaces are disabled")
        print("[miagent] WARNING: bwrap unavailable; running generated code "
              "UNSANDBOXED (set MIAGENT_SANDBOX=off to silence)", file=sys.stderr)
        return []
    home = Path(os.path.expanduser("~")).resolve()
    hidden = [Path("/home"), Path("/tmp"), Path("/run/user")]
    if not any(home.is_relative_to(h) for h in hidden):
        hidden.append(home)  # e.g. /root
    hidden = [h for h in hidden if h.exists()]
    workdir = workdir.resolve()
    args = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    for h in hidden:
        args += ["--tmpfs", str(h)]
    for d in _python_dirs_under(hidden):
        args += ["--ro-bind", str(d), str(d)]
    args += [
        "--bind", str(workdir), str(workdir),
        "--chdir", str(workdir),
        "--unshare-all", "--share-net",
        "--die-with-parent", "--new-session",
        "--",
    ]
    return args


def _limit_resources() -> None:
    """preexec_fn for generated code: cap CPU time and address space."""
    resource.setrlimit(resource.RLIMIT_CPU, (_RLIMIT_CPU_S, _RLIMIT_CPU_S))
    resource.setrlimit(resource.RLIMIT_AS, (_RLIMIT_AS_BYTES, _RLIMIT_AS_BYTES))


@dataclass
class RunResult:
    report: ValidationReport
    process_log: str


def probe_endpoints(
    spec: IntegrationSpec,
    target: str,
    username: str = "",
    password: str = "",
    max_bytes: int = 3000,
    redact: bool = True,
) -> str:
    """Fetch a fresh sample of each upstream endpoint (deterministic).

    Given to the repair stage so it can *see* the target's current response
    shapes instead of guessing from logs — the key input when an upstream
    API has changed shape since generation. The result goes into an LLM
    prompt, so by default it is redacted (see ``miagent.redact``) and labeled
    by path only, never the target's host.
    """
    auth = (username, password) if username or password else None
    clean = redact_text if redact else (lambda t: t)
    chunks = []
    for ep in spec.endpoints:
        url = ep.url if ep.url.startswith("http") else target.rstrip("/") + "/" + ep.url.lstrip("/")
        label = httpx.URL(url).raw_path.decode() if redact else url
        try:
            r = httpx.get(url, auth=auth, timeout=settings.scrape_timeout_s)
            body = redact_body(r.text) if redact else r.text
            chunks.append(f"### GET {label} -> HTTP {r.status_code}\n{body[:max_bytes]}")
        except httpx.HTTPError as e:
            chunks.append(f"### GET {label} -> {type(e).__name__}: {clean(str(e))}")
    return "\n\n".join(chunks)


def static_check(path: Path) -> Optional[str]:
    """Compile-check the generated file; returns error text or None."""
    try:
        py_compile.compile(str(path), doraise=True)
        return None
    except py_compile.PyCompileError as e:
        return str(e)


def _wait_for_metrics(url: str, proc: subprocess.Popen, timeout_s: float = 25.0) -> Optional[str]:
    """Poll until the metrics URL answers 200. Returns error text or None.

    Connection errors and non-200s are both retried until the deadline: a
    server may bind its port before it's ready and answer 503/404 meanwhile.
    """
    deadline = time.monotonic() + timeout_s
    last = "no response"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return f"process exited early with code {proc.returncode}"
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code == 200:
                return None
            last = f"HTTP {r.status_code}"
        except httpx.HTTPError as e:
            last = type(e).__name__
        time.sleep(0.5)
    return f"metrics endpoint did not come up within {timeout_s}s (last: {last})"


def run_process_and_validate(
    cmd: list[str],
    metrics_url: str,
    spec: IntegrationSpec,
    log_path: Path,
    settle_s: float = 1.0,
    untrusted: bool = False,
    extra_env: Optional[dict[str, str]] = None,
) -> RunResult:
    """Launch a long-running artifact, scrape it once, tear it down.

    Shared by every artifact kind (python exporter, snmp_exporter, and any
    future collector): the only things that differ per kind are the command
    and the metrics URL. Every child gets a scrubbed environment;
    ``untrusted=True`` (LLM-written code) additionally runs it from the log's
    directory under rlimits, inside the bwrap sandbox when available.
    """
    if untrusted:
        cmd = sandbox_prefix(log_path.parent) + cmd
    with open(log_path, "w") as log_file:
        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            env=scrubbed_env(extra_env),
            cwd=str(log_path.parent) if untrusted else None,
            preexec_fn=_limit_resources if untrusted else None,
        )
        try:
            err = _wait_for_metrics(metrics_url, proc)
            if err:
                report = ValidationReport(
                    ok=False,
                    scrape_url=metrics_url,
                    metrics_expected=len(spec.metrics),
                    failures=[Failure(kind=FailureKind.scrape_error, detail=err)],
                )
            else:
                time.sleep(settle_s)  # let the first upstream poll land
                report = validate_scrape(metrics_url, spec)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    return RunResult(report=report, process_log=log_path.read_text())


def run_and_validate(
    code_path: Path,
    spec: IntegrationSpec,
    target: str,
    port: int = 9464,
    username: str = "",
    password: str = "",
    settle_s: float = 1.0,
) -> RunResult:
    log_path = code_path.with_suffix(".log")

    err = static_check(code_path)
    if err:
        return RunResult(
            report=ValidationReport(
                ok=False,
                metrics_expected=len(spec.metrics),
                failures=[Failure(kind=FailureKind.parse_error, detail=f"static check: {err}")],
            ),
            process_log="",
        )

    cmd = [sys.executable, str(code_path.resolve()), "--port", str(port), "--target", target]
    extra_env = {}
    if ENV_TARGET_PASSWORD in code_path.read_text():
        if username:
            extra_env[ENV_TARGET_USERNAME] = username
        if password:
            extra_env[ENV_TARGET_PASSWORD] = password
    else:
        # Legacy contract (pre env-credentials): credentials as argv.
        if username:
            cmd += ["--username", username]
        if password:
            cmd += ["--password", password]

    return run_process_and_validate(
        cmd,
        f"http://127.0.0.1:{port}/metrics",
        spec,
        log_path,
        settle_s=settle_s,
        untrusted=True,
        extra_env=extra_env,
    )
