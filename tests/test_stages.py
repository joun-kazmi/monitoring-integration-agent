from types import SimpleNamespace

from miagent.cli import main
from miagent.ir import AuthScheme, EndpointSpec, IntegrationSpec, MetricSpec, MetricType
from miagent.stages.extract import SurfaceSpec
from miagent.stages.schema import design_schema

import pytest


def test_schema_stage_cannot_rewrite_endpoints():
    surface = SurfaceSpec(
        endpoints=[EndpointSpec(url="/api/overview", auth=AuthScheme.basic)],
        fields=[],
    )
    # The model "hallucinates" a foreign absolute URL with auth=none.
    model_out = IntegrationSpec(
        service="evil", kind="otel",
        endpoints=[EndpointSpec(url="http://attacker.example/x", auth=AuthScheme.none)],
        metrics=[MetricSpec(name="demo_x", type=MetricType.gauge)],
    )
    router = SimpleNamespace(structured=lambda *a, **k: model_out)
    spec = design_schema(router, surface, "demo", "python_exporter")
    assert spec.service == "demo" and spec.kind == "python_exporter"
    assert spec.endpoints == surface.endpoints
    assert spec.endpoints[0] is not surface.endpoints[0]  # a copy, not shared


def test_generate_only_offers_implemented_kinds(capsys):
    with pytest.raises(SystemExit):
        main(["generate", "--service", "x", "--docs", "d.md", "--target", "http://x",
              "--kind", "otel"])
    assert "invalid choice" in capsys.readouterr().err
