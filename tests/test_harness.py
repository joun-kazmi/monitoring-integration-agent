from miagent.ir import (
    IntegrationSpec,
    LabelSpec,
    MetricSpec,
    MetricType,
    ValueExpectation,
)
from miagent.validate import FailureKind, validate_text

SPEC = IntegrationSpec(
    service="demo",
    metrics=[
        MetricSpec(
            name="demo_requests_total",
            type=MetricType.counter,
            labels=[
                LabelSpec(name="method", allowed_values=["get", "post"]),
                LabelSpec(name="code"),
            ],
        ),
        MetricSpec(
            name="demo_queue_depth",
            type=MetricType.gauge,
            value=ValueExpectation(min=0, max=10000),
        ),
        MetricSpec(
            name="demo_optional_thing",
            type=MetricType.gauge,
            required=False,
        ),
    ],
)

GOOD = """\
# HELP demo_requests_total Requests.
# TYPE demo_requests_total counter
demo_requests_total{method="get",code="200"} 42
demo_requests_total{method="post",code="500"} 3
# HELP demo_queue_depth Queue depth.
# TYPE demo_queue_depth gauge
demo_queue_depth 7
"""


def test_good_scrape_passes():
    report = validate_text(GOOD, SPEC)
    assert report.ok, report.summary()
    assert report.metrics_found == 2
    # optional metric absent -> warning, not failure
    assert any(w.kind == FailureKind.missing_metric for w in report.warnings)


def test_missing_required_metric_fails():
    text = GOOD.replace("demo_queue_depth 7\n", "")
    text = text.replace("# HELP demo_queue_depth Queue depth.\n", "")
    text = text.replace("# TYPE demo_queue_depth gauge\n", "")
    report = validate_text(text, SPEC)
    assert not report.ok
    assert any(
        f.kind == FailureKind.missing_metric and f.metric == "demo_queue_depth"
        for f in report.failures
    )


def test_type_mismatch_detected():
    text = GOOD.replace("# TYPE demo_queue_depth gauge", "# TYPE demo_queue_depth counter")
    report = validate_text(text, SPEC)
    assert any(f.kind == FailureKind.unexpected_type for f in report.failures)


def test_bad_label_value_detected():
    text = GOOD.replace('method="post"', 'method="delete"')
    report = validate_text(text, SPEC)
    assert any(f.kind == FailureKind.label_value_invalid for f in report.failures)


def test_missing_required_label_detected():
    text = GOOD.replace('{method="get",code="200"}', '{method="get"}')
    report = validate_text(text, SPEC)
    assert any(f.kind == FailureKind.missing_label for f in report.failures)


def test_negative_counter_detected():
    text = GOOD.replace(
        'demo_requests_total{method="get",code="200"} 42',
        'demo_requests_total{method="get",code="200"} -1',
    )
    report = validate_text(text, SPEC)
    assert any(f.kind == FailureKind.value_implausible for f in report.failures)


def test_value_out_of_bounds_is_warning():
    text = GOOD.replace("demo_queue_depth 7", "demo_queue_depth 999999")
    report = validate_text(text, SPEC)
    assert report.ok  # bounds violations warn, don't fail
    assert any(w.kind == FailureKind.value_implausible for w in report.warnings)


def test_parse_error_reported():
    report = validate_text('this is { not metrics', SPEC)
    assert not report.ok
    assert any(f.kind == FailureKind.parse_error for f in report.failures)


def test_unexpected_metrics_listed():
    text = GOOD + "# TYPE surprise_gauge gauge\nsurprise_gauge 1\n"
    report = validate_text(text, SPEC)
    assert "surprise_gauge" in report.unexpected_metrics


def test_metric_outcomes_recorded():
    """Aggregate counts say 23/28; outcomes say which and why."""
    report = validate_text(GOOD, SPEC)
    by_name = {m.name: m for m in report.metrics}
    assert set(by_name) == {
        "demo_requests_total", "demo_queue_depth", "demo_optional_thing",
    }

    ok = by_name["demo_requests_total"]
    assert ok.status == "ok"
    assert ok.expected_type == "counter" and ok.observed_type == "counter"
    assert ok.series_count == 2  # two label combinations
    assert ok.observed_labels == ["code", "method"]
    assert ok.sample_value == 42

    absent = by_name["demo_optional_thing"]
    assert absent.status == "missing"
    assert absent.required is False


def test_metric_outcome_flags_type_mismatch():
    text = GOOD.replace("# TYPE demo_queue_depth gauge", "# TYPE demo_queue_depth counter")
    report = validate_text(text, SPEC)
    outcome = next(m for m in report.metrics if m.name == "demo_queue_depth")
    assert outcome.status == "type_mismatch"
    assert outcome.observed_type == "counter"
    assert "expected gauge" in outcome.detail


def test_metric_outcome_flags_label_mismatch():
    text = GOOD.replace('{method="get",code="200"}', '{method="get"}')
    report = validate_text(text, SPEC)
    outcome = next(m for m in report.metrics if m.name == "demo_requests_total")
    assert outcome.status == "label_mismatch"
