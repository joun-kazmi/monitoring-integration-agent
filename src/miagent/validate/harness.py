"""Deterministic validation harness (stage 5).

No LLM anywhere in this module. It scrapes a metrics endpoint (or takes
raw exposition text), optionally lints with promtool, parses with the
official Prometheus client parser, and diffs the result against the
IntegrationSpec IR. Output is a categorized ValidationReport the repair
stage consumes.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Optional

import httpx
from prometheus_client.parser import text_string_to_metric_families

from miagent.config import settings
from miagent.ir import IntegrationSpec, MetricSpec, MetricType
from miagent.validate.report import (
    Failure,
    FailureKind,
    MetricOutcome,
    ValidationReport,
)

# prometheus_client family types -> our MetricType names
_TYPE_MAP = {
    "counter": MetricType.counter,
    "gauge": MetricType.gauge,
    "histogram": MetricType.histogram,
    "summary": MetricType.summary,
    "unknown": MetricType.untyped,
    "untyped": MetricType.untyped,
}

# Metrics emitted by the runtime/exporter itself rather than the integration:
# reported as informational noise, never as unexpected findings.
_INFRA_PREFIXES = (
    "python_", "process_", "promhttp_", "go_", "otelcol_", "target_", "scrape_",
    "snmp_scrape_", "snmp_exporter_",  # snmp_exporter per-scrape internals
)


_BUCKET_LABELS = ("le", "quantile")  # per-bucket/quantile, not per-entity


def _cardinality_warnings(found: dict[str, object], families: dict[str, object]) -> list[Failure]:
    """Series-count budgets (deterministic).

    Per metric: distinct label sets, ignoring histogram ``le`` / summary
    ``quantile``, i.e. how many entities the metric fans out over. Total:
    every series actually emitted (buckets, _sum, _count, _created
    included) across all non-infrastructure families in the scrape.
    """
    out: list[Failure] = []
    for metric_name, fam in found.items():
        labelsets = {
            tuple(sorted((k, v) for k, v in smp.labels.items() if k not in _BUCKET_LABELS))
            for smp in fam.samples
        }
        if len(labelsets) <= settings.max_series_per_metric:
            continue
        distinct: dict[str, set] = {}
        for ls in labelsets:
            for k, v in ls:
                distinct.setdefault(k, set()).add(v)
        worst = max(distinct.items(), key=lambda kv: len(kv[1]), default=None)
        driver = f"; most distinct values: {worst[0]!r} ({len(worst[1])})" if worst else ""
        out.append(Failure(
            kind=FailureKind.high_cardinality, metric=metric_name, severity="warning",
            detail=f"{len(labelsets)} label sets > budget "
                   f"{settings.max_series_per_metric}{driver}",
        ))
    total = sum(
        len({(smp.name, tuple(sorted(smp.labels.items()))) for smp in fam.samples})
        for name, fam in families.items()
        if not name.startswith(_INFRA_PREFIXES)
    )
    if total > settings.max_series_total:
        out.append(Failure(
            kind=FailureKind.high_cardinality, severity="warning",
            detail=f"{total} series emitted in total > budget {settings.max_series_total}",
        ))
    return out


def _promtool_lint(text: str) -> Optional[str]:
    """Run `promtool check metrics`; returns error output, None if clean,
    or None (skipped) when promtool isn't installed."""
    if shutil.which(settings.promtool_path) is None:
        return None
    proc = subprocess.run(
        [settings.promtool_path, "check", "metrics"],
        input=text,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return (proc.stdout + proc.stderr).strip()
    return None


def _family_index(text: str):
    """Parse exposition text into {family_name: family}."""
    families = {}
    for fam in text_string_to_metric_families(text):
        families[fam.name] = fam
    return families


def _find_family(families: dict, spec: MetricSpec):
    """Match a spec name to a parsed family, tolerating the _total suffix
    convention for counters (parser strips it from the family name)."""
    if spec.name in families:
        return families[spec.name]
    if spec.name.endswith("_total") and spec.name[: -len("_total")] in families:
        return families[spec.name[: -len("_total")]]
    return None


def _check_metric(
    spec: MetricSpec, fam, failures: list, warnings: list, outcome=None
) -> None:
    fam_type = _TYPE_MAP.get(fam.type, MetricType.untyped)
    if outcome is not None:
        outcome.observed_type = fam.type
        base_names = (spec.name, fam.name, fam.name + "_total")
        base = [s for s in fam.samples if s.name in base_names]
        outcome.series_count = len(base) or len(fam.samples)
        if base:
            outcome.observed_labels = sorted(base[0].labels)
            if isinstance(base[0].value, (int, float)):
                outcome.sample_value = float(base[0].value)
    if spec.type is not MetricType.untyped and fam_type is not spec.type:
        failures.append(
            Failure(
                kind=FailureKind.unexpected_type,
                metric=spec.name,
                detail=f"expected {spec.type.value}, exposed as {fam.type}",
            )
        )

    required_labels = {l.name for l in spec.labels if l.required}
    allowed = {
        l.name: set(l.allowed_values) for l in spec.labels if l.allowed_values
    }
    saw_samples = False
    for sample in fam.samples:
        saw_samples = True
        label_names = set(sample.labels)
        missing = required_labels - label_names
        # Synthetic series (_sum/_count/_bucket, quantile) inherit labels;
        # only check base-name samples for label completeness.
        base_sample = sample.name in (spec.name, fam.name, fam.name + "_total")
        if base_sample and missing:
            failures.append(
                Failure(
                    kind=FailureKind.missing_label,
                    metric=spec.name,
                    detail=f"series missing required label(s): {sorted(missing)}",
                )
            )
            required_labels -= missing  # report each label once
        for lname, values in allowed.items():
            v = sample.labels.get(lname)
            if v is not None and v not in values:
                failures.append(
                    Failure(
                        kind=FailureKind.label_value_invalid,
                        metric=spec.name,
                        detail=f"label {lname}={v!r} not in allowed set {sorted(values)}",
                    )
                )
        # Value plausibility (skip histogram/summary synthetics' semantics)
        if base_sample and isinstance(sample.value, (int, float)):
            v = float(sample.value)
            if spec.type is MetricType.counter and v < 0:
                failures.append(
                    Failure(
                        kind=FailureKind.value_implausible,
                        metric=spec.name,
                        detail=f"counter has negative value {v}",
                    )
                )
            if spec.value.min is not None and v < spec.value.min:
                warnings.append(
                    Failure(
                        kind=FailureKind.value_implausible,
                        metric=spec.name,
                        severity="warning",
                        detail=f"value {v} below expected min {spec.value.min}",
                    )
                )
            if spec.value.max is not None and v > spec.value.max:
                warnings.append(
                    Failure(
                        kind=FailureKind.value_implausible,
                        metric=spec.name,
                        severity="warning",
                        detail=f"value {v} above expected max {spec.value.max}",
                    )
                )
    if not saw_samples:
        (failures if spec.required else warnings).append(
            Failure(
                kind=FailureKind.missing_metric,
                metric=spec.name,
                severity="error" if spec.required else "warning",
                detail="family present but exposes no series",
            )
        )


def validate_text(text: str, spec: IntegrationSpec, scrape_url: str = "") -> ValidationReport:
    """Validate raw Prometheus exposition text against the IR."""
    failures: list[Failure] = []
    warnings: list[Failure] = []
    report = ValidationReport(
        ok=False,
        scrape_url=scrape_url,
        metrics_expected=len(spec.metrics),
    )

    lint_err = _promtool_lint(text)
    if lint_err is not None:
        report.promtool_ran = True
        failures.append(
            Failure(kind=FailureKind.lint_error, detail=lint_err[:2000])
        )
    elif shutil.which(settings.promtool_path) is not None:
        report.promtool_ran = True

    try:
        families = _family_index(text)
    except Exception as e:
        failures.append(
            Failure(kind=FailureKind.parse_error, detail=f"{type(e).__name__}: {e}")
        )
        report.failures = failures
        return report

    expected_family_names = set()
    found_families: dict[str, object] = {}
    for m in spec.metrics:
        outcome = MetricOutcome(
            name=m.name,
            expected_type=m.type.value,
            expected_labels=sorted(l.name for l in m.labels),
            required=m.required,
        )
        report.metrics.append(outcome)

        fam = _find_family(families, m)
        if fam is None:
            outcome.status = "missing"
            outcome.detail = "metric not found in scrape output"
            (failures if m.required else warnings).append(
                Failure(
                    kind=FailureKind.missing_metric,
                    metric=m.name,
                    severity="error" if m.required else "warning",
                    detail="metric not found in scrape output",
                )
            )
            continue
        expected_family_names.add(fam.name)
        found_families[m.name] = fam
        report.metrics_found += 1
        before = len(failures)
        _check_metric(m, fam, failures, warnings, outcome=outcome)
        if len(failures) > before:
            new = failures[before:]
            outcome.status = {
                FailureKind.unexpected_type: "type_mismatch",
                FailureKind.missing_label: "label_mismatch",
                FailureKind.label_value_invalid: "label_mismatch",
            }.get(new[0].kind, "value_suspect")
            outcome.detail = new[0].detail

    report.unexpected_metrics = sorted(
        name
        for name in families
        if name not in expected_family_names
        and not name.startswith(_INFRA_PREFIXES)
    )
    warnings.extend(_cardinality_warnings(found_families, families))
    report.failures = failures
    report.warnings = warnings
    report.ok = not failures
    return report


def validate_scrape(url: str, spec: IntegrationSpec) -> ValidationReport:
    """Scrape a live /metrics endpoint and validate against the IR."""
    try:
        resp = httpx.get(url, timeout=settings.scrape_timeout_s)
    except httpx.HTTPError as e:
        return ValidationReport(
            ok=False,
            scrape_url=url,
            metrics_expected=len(spec.metrics),
            failures=[
                Failure(
                    kind=FailureKind.scrape_error,
                    detail=f"{type(e).__name__}: {e}",
                )
            ],
        )
    if resp.status_code in (401, 403):
        return ValidationReport(
            ok=False,
            scrape_url=url,
            metrics_expected=len(spec.metrics),
            failures=[
                Failure(
                    kind=FailureKind.auth_error,
                    detail=f"HTTP {resp.status_code}: {resp.text[:500]}",
                )
            ],
        )
    if resp.status_code != 200:
        return ValidationReport(
            ok=False,
            scrape_url=url,
            metrics_expected=len(spec.metrics),
            failures=[
                Failure(
                    kind=FailureKind.scrape_error,
                    detail=f"HTTP {resp.status_code}: {resp.text[:500]}",
                )
            ],
        )
    return validate_text(resp.text, spec, scrape_url=url)
