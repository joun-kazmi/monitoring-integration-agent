"""Machine-readable validation report.

Failures carry a category so the repair stage can route cheaply: an
AUTH_ERROR needs a different (and simpler) fix than a TYPE_MISMATCH, and
classifying it here — deterministically — lets the standard-tier model fix
things that would otherwise need escalation.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class FailureKind(str, Enum):
    scrape_error = "scrape_error"        # endpoint unreachable / timeout / 5xx
    auth_error = "auth_error"            # 401 / 403
    parse_error = "parse_error"          # not valid Prometheus text format
    lint_error = "lint_error"            # promtool check metrics failed
    missing_metric = "missing_metric"
    unexpected_type = "unexpected_type"
    missing_label = "missing_label"
    label_value_invalid = "label_value_invalid"
    value_implausible = "value_implausible"


class Failure(BaseModel):
    kind: FailureKind
    metric: str = ""
    detail: str = ""
    severity: str = "error"  # error | warning

    def __str__(self) -> str:
        loc = f" [{self.metric}]" if self.metric else ""
        return f"{self.severity.upper()} {self.kind.value}{loc}: {self.detail}"


class MetricOutcome(BaseModel):
    """Per-metric expected-vs-observed record.

    The aggregate counts say "23/28 found"; this says *which* and *why*,
    which is what both the repair stage and a human reading the report
    actually need.
    """

    name: str
    expected_type: str = ""
    observed_type: str = ""
    status: str = "ok"  # ok | missing | type_mismatch | label_mismatch | value_suspect
    series_count: int = 0
    sample_value: Optional[float] = None
    expected_labels: list[str] = Field(default_factory=list)
    observed_labels: list[str] = Field(default_factory=list)
    required: bool = True
    detail: str = ""


class ValidationReport(BaseModel):
    ok: bool
    failures: list[Failure] = Field(default_factory=list)
    warnings: list[Failure] = Field(default_factory=list)
    metrics_expected: int = 0
    metrics_found: int = 0
    unexpected_metrics: list[str] = Field(default_factory=list)
    scrape_url: str = ""
    promtool_ran: bool = False
    metrics: list[MetricOutcome] = Field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"{'PASS' if self.ok else 'FAIL'}: "
            f"{self.metrics_found}/{self.metrics_expected} expected metrics found"
        ]
        lines += [str(f) for f in self.failures]
        lines += [str(w) for w in self.warnings]
        if self.unexpected_metrics:
            lines.append(
                "note: unexpected metrics present: "
                + ", ".join(sorted(self.unexpected_metrics)[:20])
            )
        return "\n".join(lines)
