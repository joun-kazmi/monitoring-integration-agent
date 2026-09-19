import json

import pytest
from pydantic import BaseModel

from miagent.config import Settings
from miagent.llm.base import LLMBackend, LLMError, LLMResponse, extract_json
from miagent.llm.router import LLMRouter, Stage, Tier


class FakeBackend(LLMBackend):
    name = "claude_cli"  # reuse claude_cli default model table

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, prompt, *, model, system="", max_tokens=8192):
        self.calls.append({"prompt": prompt, "model": model, "system": system})
        return LLMResponse(
            text=self.replies.pop(0), model=model, backend=self.name,
            input_tokens=10, output_tokens=5,
        )


class Point(BaseModel):
    x: int
    y: int


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Here you go:\n```json\n{"a": 1}\n```\nthanks') == {"a": 1}
    assert extract_json('prefix text {"a": [1, 2]}') == {"a": [1, 2]}
    with pytest.raises(LLMError):
        extract_json("no json here")


def test_structured_repair_loop():
    backend = FakeBackend(['{"x": "not-an-int", "y": 2}', '{"x": 1, "y": 2}'])
    p = backend.structured("give me a point", Point, model="sonnet")
    assert p == Point(x=1, y=2)
    assert len(backend.calls) == 2
    assert "failed validation" in backend.calls[1]["prompt"]


def test_structured_gives_up():
    backend = FakeBackend(["nope", "nope", "nope"])
    with pytest.raises(LLMError):
        backend.structured("point please", Point, model="sonnet", max_repairs=2)


def test_router_tiers_and_escalation():
    backend = FakeBackend([])
    router = LLMRouter(backend=backend, cfg=Settings(escalate_after=2))
    assert router.model_for(router.tier_for(Stage.classify)) == "haiku"
    assert router.model_for(router.tier_for(Stage.extract)) == "sonnet"
    assert router.model_for(router.tier_for(Stage.generate_code)) == "opus"
    # repair escalates standard -> strong after N iterations
    assert router.tier_for(Stage.repair, repair_iteration=0) is Tier.standard
    assert router.tier_for(Stage.repair, repair_iteration=2) is Tier.strong


def test_router_model_overrides():
    backend = FakeBackend([])
    cfg = Settings(model_standard="anthropic/claude-sonnet-5")
    router = LLMRouter(backend=backend, cfg=cfg)
    assert router.model_for(Tier.standard) == "anthropic/claude-sonnet-5"


def test_router_usage_tracking():
    backend = FakeBackend([json.dumps({"x": 1, "y": 2})])
    router = LLMRouter(backend=backend, cfg=Settings())
    router.structured(Stage.extract, "point", Point)
    assert router.usage.calls == 1
    assert router.usage.input_tokens == 10


def test_claude_cli_explains_usage_limit():
    """A usage-limit failure must be identifiable, not buried in JSON."""
    import subprocess

    from miagent.llm.claude_cli import ClaudeCliBackend

    payload = json.dumps({
        "terminal_reason": "api_error", "is_error": True,
        "usage": {"output_tokens": 0, "input_tokens": 0},
    })
    proc = subprocess.CompletedProcess(args=[], returncode=1, stdout=payload, stderr="")
    msg = ClaudeCliBackend._explain_failure(proc)
    assert "usage limit" in msg
    assert "MIAGENT_LLM_BACKEND" in msg


def test_claude_cli_explains_other_failures():
    import subprocess

    from miagent.llm.claude_cli import ClaudeCliBackend

    payload = json.dumps({
        "terminal_reason": "tool_use", "usage": {"output_tokens": 120},
        "result": "wanted to call a tool",
    })
    proc = subprocess.CompletedProcess(args=[], returncode=1, stdout=payload, stderr="")
    msg = ClaudeCliBackend._explain_failure(proc)
    assert "tool_use" in msg
    assert "usage limit" not in msg


def test_usage_tracks_by_stage_and_tier():
    backend = FakeBackend([json.dumps({"x": 1, "y": 2}), "plain text"])
    router = LLMRouter(backend=backend, cfg=Settings())
    router.structured(Stage.extract, "point", Point)
    router.complete(Stage.classify, "hi")

    assert set(router.usage.by_stage) == {"extract", "classify"}
    assert router.usage.by_stage["extract"]["tier"] == "standard"
    assert router.usage.by_stage["extract"]["model"] == "sonnet"
    assert router.usage.by_stage["classify"]["tier"] == "fast"
    assert router.usage.by_stage["classify"]["model"] == "haiku"
    assert router.usage.by_stage["extract"]["input_tokens"] == 10


def test_usage_attributes_stage_even_when_structured_retries():
    """A retried structured call must bill all attempts to its stage."""
    backend = FakeBackend(["not json", json.dumps({"x": 1, "y": 2})])
    router = LLMRouter(backend=backend, cfg=Settings())
    router.structured(Stage.schema_design, "point", Point)
    assert router.usage.by_stage["schema"]["calls"] == 2
    assert router.usage.by_stage["schema"]["input_tokens"] == 20


def test_usage_dict_serializes_every_ledger_field():
    """The serialized ledger must carry by_stage — it was silently dropped
    once, which lost the per-stage cost attribution for a whole run."""
    from dataclasses import fields

    from miagent.llm.base import Usage
    from miagent.orchestrate import _usage_dict

    backend = FakeBackend([json.dumps({"x": 1, "y": 2})])
    router = LLMRouter(backend=backend, cfg=Settings())
    router.structured(Stage.extract, "point", Point)

    d = _usage_dict(router)
    for f in fields(Usage):
        assert f.name in d, f"{f.name} missing from serialized usage ledger"
    assert d["by_stage"]["extract"]["model"] == "sonnet"
