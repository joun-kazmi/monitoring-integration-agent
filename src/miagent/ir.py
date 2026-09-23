"""Metric intermediate representation (IR).

This is the load-bearing artifact of the whole pipeline: stage 3 (schema
design) emits it, stage 4 (generation) consumes it as the target contract,
and stage 5 (validation) diffs live scrape output against it. One artifact,
machine-checkable, no LLM judgment involved in validation.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator

METRIC_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
LABEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class MetricType(str, Enum):
    counter = "counter"
    gauge = "gauge"
    histogram = "histogram"
    summary = "summary"
    untyped = "untyped"


class LabelSpec(BaseModel):
    name: str
    description: str = ""
    # If set, every observed value for this label must be one of these.
    allowed_values: Optional[list[str]] = None
    # Labels marked optional may be absent from the scraped series.
    required: bool = True

    @field_validator("name")
    @classmethod
    def _valid_label_name(cls, v: str) -> str:
        if not LABEL_NAME_RE.match(v):
            raise ValueError(f"invalid Prometheus label name: {v!r}")
        return v


class ValueExpectation(BaseModel):
    """Plausibility bounds for scraped sample values."""

    min: Optional[float] = None
    max: Optional[float] = None
    # Counters must be monotonically non-negative; enforced automatically
    # for MetricType.counter regardless of this field.


class MetricSpec(BaseModel):
    name: str
    type: MetricType
    help: str = ""
    unit: str = ""  # e.g. "seconds", "bytes"; informational
    labels: list[LabelSpec] = Field(default_factory=list)
    value: ValueExpectation = Field(default_factory=ValueExpectation)
    # Source hint: where this metric comes from (JSON path, OID, column...).
    # Used by generation, ignored by validation.
    source: str = ""
    # If False, the metric may legitimately be absent (e.g. only appears
    # after certain events). Validation warns instead of failing.
    required: bool = True

    @field_validator("name")
    @classmethod
    def _valid_metric_name(cls, v: str) -> str:
        if not METRIC_NAME_RE.match(v):
            raise ValueError(f"invalid Prometheus metric name: {v!r}")
        return v


class AuthScheme(str, Enum):
    none = "none"
    basic = "basic"
    bearer = "bearer"
    header = "header"  # arbitrary header, e.g. X-Api-Key
    query = "query"  # token in query param (discouraged, some APIs require it)


class EndpointSpec(BaseModel):
    """One upstream data source the integration reads from."""

    url: str
    method: str = "GET"
    auth: AuthScheme = AuthScheme.none
    auth_detail: str = Field(
        default="",
        description="For auth=header: the exact header name (e.g. X-Api-Key). "
        "For auth=query: the exact query-parameter name (e.g. api_key). "
        "Only the bare name, no prose. Empty for other schemes.",
    )
    response_format: str = "json"  # json | prometheus | xml | snmp
    notes: str = ""


class IntegrationSpec(BaseModel):
    """Top-level IR: everything needed to generate and validate one integration."""

    service: str
    kind: str = "otel"  # otel | snmp_generator | python_exporter
    endpoints: list[EndpointSpec] = Field(default_factory=list)
    metrics: list[MetricSpec]
    # Scrape address of the *generated* artifact under test,
    # e.g. http://localhost:9464/metrics (filled by the harness).
    notes: str = ""

    def metric(self, name: str) -> Optional[MetricSpec]:
        for m in self.metrics:
            if m.name == name:
                return m
        return None
