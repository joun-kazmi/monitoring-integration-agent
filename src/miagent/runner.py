"""Execution of a generated Python exporter (part of stage 5).

Deterministic: static-check the file, launch it, wait for /metrics to come
up, hand off to the validation harness, kill the process, and return the
report plus captured logs for the repair stage.

NOT a sandbox. The exporter is LLM-generated code and runs as the current
user. Mitigations only: the child gets a scrubbed environment (no API keys
or cloud credentials inherited), runs with the workdir as cwd, and is
bounded by CPU-time and memory rlimits. A real isolation boundary
(container or namespace sandbox) is roadmap work.
"""

from __future__ import annotations

import os
import py_compile
import resource
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from miagent.config import settings
from miagent.ir import IntegrationSpec
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


def scrubbed_env(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    env.update(extra or {})
    return env


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
) -> str:
    """Fetch a fresh sample of each upstream endpoint (deterministic).

    Given to the repair stage so it can *see* the target's current response
    shapes instead of guessing from logs — the key input when an upstream
    API has changed shape since generation.
    """
    auth = (username, password) if username or password else None
    chunks = []
    for ep in spec.endpoints:
        url = ep.url if ep.url.startswith("http") else target.rstrip("/") + "/" + ep.url.lstrip("/")
        try:
            r = httpx.get(url, auth=auth, timeout=settings.scrape_timeout_s)
            body = r.text[:max_bytes]
            chunks.append(f"### GET {url} -> HTTP {r.status_code}\n{body}")
        except httpx.HTTPError as e:
            chunks.append(f"### GET {url} -> {type(e).__name__}: {e}")
    return "\n\n".join(chunks)


def static_check(path: Path) -> Optional[str]:
    """Compile-check the generated file; returns error text or None."""
    try:
        py_compile.compile(str(path), doraise=True)
        return None
    except py_compile.PyCompileError as e:
        return str(e)


def _wait_for_metrics(url: str, proc: subprocess.Popen, timeout_s: float = 25.0) -> Optional[str]:
    """Poll until the metrics URL answers 200. Returns error text or None."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return f"process exited early with code {proc.returncode}"
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code == 200:
                return None
            return f"metrics endpoint returned HTTP {r.status_code}"
        except httpx.HTTPError:
            time.sleep(0.5)
    return f"metrics endpoint did not come up within {timeout_s}s"


def run_process_and_validate(
    cmd: list[str],
    metrics_url: str,
    spec: IntegrationSpec,
    log_path: Path,
    settle_s: float = 1.0,
    untrusted: bool = False,
) -> RunResult:
    """Launch a long-running artifact, scrape it once, tear it down.

    Shared by every artifact kind (python exporter, snmp_exporter, and any
    future collector): the only things that differ per kind are the command
    and the metrics URL. Every child gets a scrubbed environment;
    ``untrusted=True`` (LLM-written code) additionally runs it from the log's
    directory under rlimits.
    """
    with open(log_path, "w") as log_file:
        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            env=scrubbed_env(),
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
    )
