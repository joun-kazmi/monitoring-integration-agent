"""Pipeline orchestrator — a deterministic state machine that calls the
LLM at fixed points. Stages: extract → schema → generate → run/validate →
repair loop (bounded, escalating standard → strong per the router config).

The validate+repair loop is factored out so it can also run standalone
against an existing artifact (`miagent repair`) — that's the fleet-refresh
flow: re-validate a previously passing integration, repair only on failure.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from miagent.config import settings
from miagent.ingest import ingest
from miagent.ir import IntegrationSpec
from miagent.llm.router import LLMRouter, Stage
from miagent.runner import probe_endpoints, run_and_validate
from miagent.stages.extract import extract_surface
from miagent.stages.gen_python import generate_python_exporter, repair_python_exporter
from miagent.stages.schema import design_schema
from miagent.validate.report import Failure, FailureKind, ValidationReport


@dataclass
class PipelineResult:
    ok: bool
    spec: Optional[IntegrationSpec] = None
    artifact_path: Optional[Path] = None
    report: Optional[ValidationReport] = None
    iterations: int = 0
    usage: dict = field(default_factory=dict)
    elapsed_s: float = 0.0


def _log(msg: str) -> None:
    print(f"[miagent] {msg}", flush=True)


def _usage_dict(router: LLMRouter) -> dict:
    return {
        "calls": router.usage.calls,
        "input_tokens": router.usage.input_tokens,
        "output_tokens": router.usage.output_tokens,
        "by_model": router.usage.by_model,
        "by_stage": router.usage.by_stage,
    }


def validate_and_repair(
    router: LLMRouter,
    spec: IntegrationSpec,
    code_path: Path,
    target: str,
    workdir: Path,
    port: int = 9464,
    username: str = "",
    password: str = "",
    live_samples: str = "redacted",
) -> tuple[Optional[ValidationReport], int]:
    """Run/validate the artifact; on failure, repair with escalation until
    pass or budget exhaustion. Returns (final report, repair iterations)."""
    code = code_path.read_text()
    report: Optional[ValidationReport] = None
    iteration = 0
    while iteration <= settings.max_repair_iterations:
        _log(f"stage 5: run + validate (iteration {iteration})")
        result = run_and_validate(
            code_path, spec, target, port=port, username=username, password=password
        )
        report = result.report
        (workdir / f"report_{iteration}.json").write_text(report.model_dump_json(indent=2))
        if report.ok:
            _log(f"  -> PASS ({report.metrics_found}/{report.metrics_expected} metrics)")
            break
        _log("  -> FAIL:\n" + "\n".join(f"     {line}" for line in report.summary().splitlines()))
        iteration += 1
        if iteration > settings.max_repair_iterations:
            _log("giving up: repair budget exhausted")
            break
        tier = router.tier_for(Stage.repair, repair_iteration=iteration - 1)
        _log(f"stage 6: probing live endpoints + repair (iteration {iteration}, tier={tier.value})")
        samples = (
            ""
            if live_samples == "off"
            else probe_endpoints(
                spec, target, username=username, password=password,
                redact=live_samples != "raw",
            )
        )
        code = repair_python_exporter(
            router, spec, code, report, result.process_log, iteration - 1,
            endpoint_samples=samples,
        )
        code_path.write_text(code)
    return report, iteration


def run_pipeline(
    service: str,
    docs_sources: list[str],
    target: str,
    workdir: Path,
    kind: str = "python_exporter",
    port: int = 9464,
    username: str = "",
    password: str = "",
    router: Optional[LLMRouter] = None,
    live_samples: str = "redacted",
) -> PipelineResult:
    t0 = time.monotonic()
    router = router or LLMRouter()
    workdir.mkdir(parents=True, exist_ok=True)

    _log(f"stage 1: ingesting {len(docs_sources)} doc source(s)")
    docs, ingested = ingest(docs_sources, router=router)
    for d in ingested:
        note = ", truncated to budget" if d.truncated else ""
        _log(f"  -> {d.source}: kind={d.kind.value}, {len(d.text)} chars{note}")
    (workdir / "docs.md").write_text(docs)

    _log(f"stage 2: extracting metric surface for {service!r} "
         f"(model={router.model_for(router.tier_for(Stage.extract))})")
    surface = extract_surface(router, docs, service)
    (workdir / "surface.json").write_text(surface.model_dump_json(indent=2))
    _log(f"  -> {len(surface.endpoints)} endpoint(s), {len(surface.fields)} field candidate(s)")

    _log("stage 3: designing metric schema (IR)")
    spec = design_schema(router, surface, service, kind)
    (workdir / "spec.json").write_text(spec.model_dump_json(indent=2))
    _log(f"  -> {len(spec.metrics)} metrics designed")

    if kind != "python_exporter":
        raise NotImplementedError(f"artifact kind {kind!r} not implemented yet")

    _log("stage 4: generating python exporter (strong tier)")
    code = generate_python_exporter(router, spec, docs)
    code_path = workdir / "exporter.py"
    code_path.write_text(code)

    report, iterations = validate_and_repair(
        router, spec, code_path, target, workdir,
        port=port, username=username, password=password,
        live_samples=live_samples,
    )

    usage = _usage_dict(router)
    (workdir / "usage.json").write_text(json.dumps(usage, indent=2))
    return PipelineResult(
        ok=bool(report and report.ok),
        spec=spec,
        artifact_path=code_path,
        report=report,
        iterations=iterations,
        usage=usage,
        elapsed_s=time.monotonic() - t0,
    )


def run_snmp_pipeline(
    service: str,
    mib_modules: list[str],
    target: str,
    workdir: Path,
    mib_sources: Optional[list[str]] = None,
    hint: str = "",
    port: Optional[int] = None,
    community: Optional[str] = None,
    router: Optional[LLMRouter] = None,
) -> PipelineResult:
    """SNMP path: MIB -> selection (1 LLM call) -> snmp.yml -> validated.

    Only the selection comes from a model; snmp.yml, generator.yml and the
    validation IR are all compiled from it deterministically, so the repair
    loop targets the selection rather than generated text.
    """
    from miagent.snmp.compile import (
        CompileError,
        compile_generator_yaml,
        compile_snmp_yaml,
        selection_to_ir,
    )
    from miagent.snmp.mib import compile_mibs, load_mib_json
    from miagent.snmp.runner import repair_walk_oids, run_snmp_and_validate, walk_target
    from miagent.stages.snmp_select import repair_snmp_selection, select_snmp_objects

    t0 = time.monotonic()
    router = router or LLMRouter()
    workdir.mkdir(parents=True, exist_ok=True)
    port = port or settings.snmp_exporter_port
    community = community or settings.snmp_community

    _log(f"stage 1: compiling MIB module(s) {mib_modules} (deterministic)")
    json_paths = compile_mibs(
        mib_modules, workdir / "mibjson", mib_sources=mib_sources or settings.mib_sources
    )
    inv = load_mib_json(json_paths)
    _log(
        f"  -> {len(inv.objects)} objects, {len(inv.metric_candidates())} metric "
        f"candidates, {len(inv.tables())} table(s)"
    )

    _log(f"stage 2: selecting objects (model={router.model_for(router.tier_for(Stage.snmp_select))})")
    selection = select_snmp_objects(router, inv, service, hint=hint)
    (workdir / "selection.json").write_text(selection.model_dump_json(indent=2))
    _log(f"  -> {len(selection.metrics)} metrics, {len(selection.lookups)} lookup(s)")

    report: Optional[ValidationReport] = None
    spec: Optional[IntegrationSpec] = None
    config_path = workdir / "snmp.yml"
    iteration = 0

    while iteration <= settings.max_repair_iterations:
        compile_error = ""
        try:
            _log(f"stage 3: compiling snmp.yml + IR (iteration {iteration}, deterministic)")
            config_path.write_text(
                compile_snmp_yaml(inv, selection, community=community, version=settings.snmp_version)
            )
            (workdir / "generator.yml").write_text(compile_generator_yaml(inv, selection))
            spec = selection_to_ir(inv, selection, service, target=target)
            (workdir / "spec.json").write_text(spec.model_dump_json(indent=2))
        except CompileError as e:
            compile_error = str(e)
            _log(f"  -> compile rejected the selection: {e}")
            report = ValidationReport(
                ok=False,
                metrics_expected=len(selection.metrics),
                failures=[Failure(kind=FailureKind.lint_error, detail=compile_error)],
            )

        if not compile_error and spec is not None:
            _log(f"stage 5: run snmp_exporter + validate (iteration {iteration})")
            result = run_snmp_and_validate(
                config_path, spec, target, selection.module_name, workdir, port=port
            )
            report = result.report
            (workdir / f"report_{iteration}.json").write_text(report.model_dump_json(indent=2))
            if report.ok:
                _log(f"  -> PASS ({report.metrics_found}/{report.metrics_expected} metrics)")
                break
            _log("  -> FAIL:\n" + "\n".join(f"     {l}" for l in report.summary().splitlines()))

        iteration += 1
        if iteration > settings.max_repair_iterations:
            _log("giving up: repair budget exhausted")
            break

        tier = router.tier_for(Stage.snmp_select, repair_iteration=iteration - 1)
        _log(f"stage 6: walking device + repairing selection (iteration {iteration}, tier={tier.value})")
        walk_oids = repair_walk_oids(inv, selection)
        _log(f"  walking {len(walk_oids)} subtree(s): {', '.join(walk_oids)}")
        walk = walk_target(target, walk_oids, community=community)
        (workdir / f"walk_{iteration}.txt").write_text(walk)
        selection = repair_snmp_selection(
            router, inv, selection, report, walk, iteration - 1, compile_error=compile_error
        )
        (workdir / "selection.json").write_text(selection.model_dump_json(indent=2))

    usage = _usage_dict(router)
    (workdir / "usage.json").write_text(json.dumps(usage, indent=2))
    return PipelineResult(
        ok=bool(report and report.ok),
        spec=spec,
        artifact_path=config_path,
        report=report,
        iterations=iteration,
        usage=usage,
        elapsed_s=time.monotonic() - t0,
    )


def run_repair(
    spec_path: Path,
    code_path: Path,
    target: str,
    workdir: Path,
    port: int = 9464,
    username: str = "",
    password: str = "",
    router: Optional[LLMRouter] = None,
    live_samples: str = "redacted",
) -> PipelineResult:
    """Fleet-refresh entry: re-validate an existing artifact, repair on failure.

    The caller's file is never edited in place: repair iterates on a copy in
    ``workdir`` and the original is replaced (atomically) only on PASS. On
    failure the original is untouched and the last attempt stays in workdir.
    """
    t0 = time.monotonic()
    router = router or LLMRouter()
    workdir.mkdir(parents=True, exist_ok=True)
    spec = IntegrationSpec.model_validate_json(spec_path.read_text())

    work_path = workdir / "exporter.py"
    if work_path.resolve() != code_path.resolve():
        shutil.copyfile(code_path, work_path)

    report, iterations = validate_and_repair(
        router, spec, work_path, target, workdir,
        port=port, username=username, password=password,
        live_samples=live_samples,
    )

    ok = bool(report and report.ok)
    if ok and iterations > 0 and work_path.resolve() != code_path.resolve():
        tmp = code_path.with_name(code_path.name + ".miagent-tmp")
        shutil.copyfile(work_path, tmp)
        shutil.copymode(code_path, tmp)
        os.replace(tmp, code_path)

    usage = _usage_dict(router)
    (workdir / "usage.json").write_text(json.dumps(usage, indent=2))
    return PipelineResult(
        ok=ok,
        spec=spec,
        artifact_path=code_path if ok else work_path,
        report=report,
        iterations=iterations,
        usage=usage,
        elapsed_s=time.monotonic() - t0,
    )
