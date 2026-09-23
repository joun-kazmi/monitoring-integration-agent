"""Command-line entry points.

    miagent validate --url http://localhost:9464/metrics --spec spec.json
    miagent validate --file scrape.txt --spec spec.json
    miagent llm-smoke [--stage extract] [--prompt "..."]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from miagent.ir import IntegrationSpec
from miagent.llm.router import Stage


def _add_target_auth_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--username", default=os.environ.get("MIAGENT_TARGET_USERNAME", ""),
                   help="target basic-auth user (default $MIAGENT_TARGET_USERNAME)")
    p.add_argument("--password", default=os.environ.get("MIAGENT_TARGET_PASSWORD", ""),
                   help="target basic-auth password (default $MIAGENT_TARGET_PASSWORD; "
                        "prefer the env var — argv is visible in ps)")
    p.add_argument("--token", default=os.environ.get("MIAGENT_TARGET_TOKEN", ""),
                   help="target API token for bearer/header/query auth "
                        "(default $MIAGENT_TARGET_TOKEN; prefer the env var)")
    p.add_argument("--live-samples", default="redacted", choices=["redacted", "raw", "off"],
                   help="what repair may send to the LLM from live target responses: "
                        "redacted (default; hosts/IPs/emails/tokens masked), raw, or off")


def _cmd_validate(args: argparse.Namespace) -> int:
    from miagent.validate import validate_scrape, validate_text

    spec = IntegrationSpec.model_validate_json(Path(args.spec).read_text())
    if args.url:
        report = validate_scrape(args.url, spec)
    else:
        report = validate_text(Path(args.file).read_text(), spec)
    print(report.summary())
    if args.json:
        print(report.model_dump_json(indent=2))
    return 0 if report.ok else 1


def _cmd_llm_smoke(args: argparse.Namespace) -> int:
    from miagent.llm import LLMRouter

    router = LLMRouter()
    stage = Stage(args.stage)
    resp = router.complete(stage, args.prompt, max_tokens=256)
    print(f"[backend={resp.backend} model={resp.model} "
          f"in={resp.input_tokens} out={resp.output_tokens}]")
    print(resp.text)
    return 0


def _cmd_generate(args: argparse.Namespace) -> int:
    from miagent.orchestrate import run_pipeline

    result = run_pipeline(
        service=args.service,
        docs_sources=args.docs,
        target=args.target,
        workdir=Path(args.workdir),
        kind=args.kind,
        port=args.port,
        username=args.username,
        password=args.password,
        token=args.token,
        live_samples=args.live_samples,
    )
    print(f"\n{'SUCCESS' if result.ok else 'FAILED'} in {result.elapsed_s:.0f}s, "
          f"{result.iterations} repair iteration(s)")
    print(f"usage: {result.usage}")
    if result.artifact_path:
        print(f"artifact: {result.artifact_path}")
    return 0 if result.ok else 1


def _cmd_report(args: argparse.Namespace) -> int:
    from miagent.report import build_index, build_report

    if args.manifest:
        index = build_index(Path(args.manifest), repo_url=args.repo_url,
                            theme=args.theme)
        print(f"wrote {index} and its reports")
        return 0
    if not args.workdir:
        print("error: give a workdir, or --manifest to rebuild a report set")
        return 2
    out = build_report(
        Path(args.workdir),
        out=Path(args.output) if args.output else None,
        title=args.title,
        theme=args.theme,
    )
    print(f"wrote {out} ({out.stat().st_size // 1024}KB)")
    return 0


def _cmd_generate_snmp(args: argparse.Namespace) -> int:
    from miagent.orchestrate import run_snmp_pipeline

    result = run_snmp_pipeline(
        service=args.service,
        mib_modules=args.mib,
        target=args.target,
        workdir=Path(args.workdir),
        mib_sources=args.mib_source or None,
        hint=args.hint,
        port=args.port,
        community=args.community,
    )
    print(f"\n{'SUCCESS' if result.ok else 'FAILED'} in {result.elapsed_s:.0f}s, "
          f"{result.iterations} repair iteration(s)")
    print(f"usage: {result.usage}")
    if result.artifact_path:
        print(f"artifact: {result.artifact_path} (+ generator.yml, spec.json)")
    return 0 if result.ok else 1


def _cmd_ingest(args: argparse.Namespace) -> int:
    from miagent.ingest import ingest

    text, docs = ingest(args.source)
    for d in docs:
        note = " (truncated)" if d.truncated else ""
        print(f"[{d.kind.value}] {d.source}: {len(d.text)} chars{note}")
    if args.output:
        Path(args.output).write_text(text)
        print(f"wrote {args.output}")
    else:
        print(text)
    return 0


def _cmd_repair(args: argparse.Namespace) -> int:
    from miagent.orchestrate import run_repair

    result = run_repair(
        spec_path=Path(args.spec),
        code_path=Path(args.code),
        target=args.target,
        workdir=Path(args.workdir),
        port=args.port,
        username=args.username,
        password=args.password,
        token=args.token,
        live_samples=args.live_samples,
    )
    print(f"\n{'SUCCESS' if result.ok else 'FAILED'} in {result.elapsed_s:.0f}s, "
          f"{result.iterations} repair iteration(s)")
    print(f"usage: {result.usage}")
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="miagent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_val = sub.add_parser("validate", help="validate a scrape against an IR spec")
    src = p_val.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="live /metrics endpoint to scrape")
    src.add_argument("--file", help="file with Prometheus exposition text")
    p_val.add_argument("--spec", required=True, help="IntegrationSpec JSON file")
    p_val.add_argument("--json", action="store_true", help="also print report JSON")
    p_val.set_defaults(func=_cmd_validate)

    p_smoke = sub.add_parser("llm-smoke", help="one cheap LLM call to verify the backend")
    p_smoke.add_argument("--stage", default="classify",
                         choices=[s.value for s in Stage])
    p_smoke.add_argument("--prompt", default="Reply with exactly: OK")
    p_smoke.set_defaults(func=_cmd_llm_smoke)

    p_gen = sub.add_parser("generate", help="run the full generation pipeline")
    p_gen.add_argument("--service", required=True)
    p_gen.add_argument("--docs", required=True, nargs="+",
                       help="documentation source(s): URLs or local files "
                            "(OpenAPI, HTML, markdown, plain text)")
    p_gen.add_argument("--target", required=True, help="base URL of the live API")
    p_gen.add_argument("--workdir", default="./build")
    p_gen.add_argument("--kind", default="python_exporter",
                       choices=["python_exporter", "otel", "snmp_generator"])
    p_gen.add_argument("--port", type=int, default=9464)
    _add_target_auth_args(p_gen)
    p_gen.set_defaults(func=_cmd_generate)

    p_rep_html = sub.add_parser("report",
                                help="render a run workdir into a self-contained HTML report")
    p_rep_html.add_argument("workdir", nargs="?", help="a run workdir, e.g. build/rabbitmq")
    p_rep_html.add_argument("-o", "--output", help="output .html path (default <workdir>/report.html)")
    p_rep_html.add_argument("--title", default=None, help="override the page heading")
    p_rep_html.add_argument("--manifest", default=None,
                            help="rebuild a whole report set + index from a manifest JSON")
    p_rep_html.add_argument("--repo-url", default="", help="repo link for the index footer")
    p_rep_html.add_argument("--theme", default=None, choices=["light", "dark"],
                            help="pin the palette instead of following the viewer's "
                                 "preference (for reproducible screenshots)")
    p_rep_html.set_defaults(func=_cmd_report)

    p_snmp = sub.add_parser("generate-snmp",
                            help="SNMP path: MIB -> snmp_exporter config, validated")
    p_snmp.add_argument("--service", required=True, help="device/service name")
    p_snmp.add_argument("--mib", required=True, nargs="+",
                        help="MIB module name(s), e.g. IF-MIB")
    p_snmp.add_argument("--target", required=True,
                        help="SNMP target host:port, e.g. 10.0.0.1:161")
    p_snmp.add_argument("--mib-source", nargs="*",
                        help="MIB source URL(s) or local directory containing MIB files")
    p_snmp.add_argument("--hint", default="",
                        help="operator guidance on what to monitor")
    p_snmp.add_argument("--community", default=None, help="SNMP community (default public)")
    p_snmp.add_argument("--port", type=int, default=None, help="snmp_exporter listen port")
    p_snmp.add_argument("--workdir", default="./build")
    p_snmp.set_defaults(func=_cmd_generate_snmp)

    p_ing = sub.add_parser("ingest", help="fetch and normalize doc sources (no LLM)")
    p_ing.add_argument("--source", required=True, nargs="+", help="URL(s) or file path(s)")
    p_ing.add_argument("-o", "--output", help="write combined corpus to this file")
    p_ing.set_defaults(func=_cmd_ingest)

    p_rep = sub.add_parser("repair", help="re-validate an existing artifact, repair on failure")
    p_rep.add_argument("--spec", required=True, help="IntegrationSpec JSON file")
    p_rep.add_argument("--code", required=True, help="existing exporter .py file")
    p_rep.add_argument("--target", required=True, help="base URL of the live API")
    p_rep.add_argument("--workdir", default="./build")
    p_rep.add_argument("--port", type=int, default=9464)
    _add_target_auth_args(p_rep)
    p_rep.set_defaults(func=_cmd_repair)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
