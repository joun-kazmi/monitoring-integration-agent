"""Stage → tier → model routing, with escalation.

This encodes the cost strategy from the design doc: each pipeline stage is
assigned a tier (fast / standard / strong); tiers map to per-backend model
names; the repair loop escalates standard → strong after N failed attempts.
Model names per tier are overridable via MIAGENT_MODEL_{FAST,STANDARD,STRONG},
which is how non-Claude providers plug in.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional, Type, TypeVar

from pydantic import BaseModel

from miagent.config import Settings, settings as default_settings
from miagent.llm.base import LLMBackend, LLMError, Usage

T = TypeVar("T", bound=BaseModel)


class Tier(str, Enum):
    fast = "fast"
    standard = "standard"
    strong = "strong"


class Stage(str, Enum):
    classify = "classify"          # doc-type routing            -> fast
    extract = "extract"            # metric-surface extraction   -> standard
    schema_design = "schema"       # metric IR design            -> standard
    generate_config = "gen_config" # OTel / snmp generator YAML  -> standard
    generate_code = "gen_code"     # python exporter fallback    -> strong
    repair = "repair"              # diagnose & fix              -> standard, escalates
    snmp_select = "snmp_select"    # OID/table selection         -> standard


STAGE_TIERS: dict[Stage, Tier] = {
    Stage.classify: Tier.fast,
    Stage.extract: Tier.standard,
    Stage.schema_design: Tier.standard,
    Stage.generate_config: Tier.standard,
    Stage.generate_code: Tier.strong,
    Stage.repair: Tier.standard,
    Stage.snmp_select: Tier.standard,
}

# Per-backend default model names for each tier.
DEFAULT_MODELS: dict[str, dict[Tier, str]] = {
    "claude_cli": {
        Tier.fast: "haiku",
        Tier.standard: "sonnet",
        Tier.strong: "opus",
    },
    "anthropic": {
        Tier.fast: "claude-haiku-4-5",
        Tier.standard: "claude-sonnet-5",
        Tier.strong: "claude-opus-5",
    },
    "openai": {
        # No sane universal defaults for arbitrary OpenAI-compatible hosts;
        # users set MIAGENT_MODEL_* explicitly. These work for openai.com.
        Tier.fast: "gpt-4o-mini",
        Tier.standard: "gpt-4o",
        Tier.strong: "gpt-4o",
    },
}


def make_backend(cfg: Optional[Settings] = None) -> LLMBackend:
    cfg = cfg or default_settings
    name = cfg.llm_backend
    if name == "claude_cli":
        from miagent.llm.claude_cli import ClaudeCliBackend

        return ClaudeCliBackend()
    if name == "anthropic":
        from miagent.llm.anthropic_api import AnthropicBackend

        return AnthropicBackend(api_key=cfg.api_key, base_url=cfg.base_url)
    if name == "openai":
        from miagent.llm.openai_compat import OpenAICompatBackend

        return OpenAICompatBackend(api_key=cfg.api_key, base_url=cfg.base_url)
    raise LLMError(f"unknown MIAGENT_LLM_BACKEND: {name!r}")


class LLMRouter:
    """Resolves (stage, escalation state) -> (backend, model) and tracks usage."""

    def __init__(
        self, backend: Optional[LLMBackend] = None, cfg: Optional[Settings] = None
    ):
        self.cfg = cfg or default_settings
        self.backend = backend or make_backend(self.cfg)
        self.usage = Usage()

    def model_for(self, tier: Tier) -> str:
        override = {
            Tier.fast: self.cfg.model_fast,
            Tier.standard: self.cfg.model_standard,
            Tier.strong: self.cfg.model_strong,
        }[tier]
        if override:
            return override
        defaults = DEFAULT_MODELS.get(self.backend.name)
        if not defaults:
            raise LLMError(
                f"no default models for backend {self.backend.name!r}; "
                "set MIAGENT_MODEL_FAST/STANDARD/STRONG"
            )
        return defaults[tier]

    def tier_for(self, stage: Stage, repair_iteration: int = 0) -> Tier:
        tier = STAGE_TIERS[stage]
        if stage is Stage.repair and repair_iteration >= self.cfg.escalate_after:
            return Tier.strong
        return tier

    def complete(
        self,
        stage: Stage,
        prompt: str,
        *,
        system: str = "",
        repair_iteration: int = 0,
        max_tokens: int = 8192,
    ):
        tier = self.tier_for(stage, repair_iteration)
        model = self.model_for(tier)
        resp = self.backend.complete(
            prompt, model=model, system=system, max_tokens=max_tokens
        )
        self.usage.add(resp, stage=stage.value, tier=tier.value)
        return resp

    def structured(
        self,
        stage: Stage,
        prompt: str,
        schema: Type[T],
        *,
        system: str = "",
        repair_iteration: int = 0,
        max_tokens: int = 8192,
    ) -> T:
        tier = self.tier_for(stage, repair_iteration)
        model = self.model_for(tier)
        before = self.usage.snapshot()
        try:
            return self.backend.structured(
                prompt,
                schema,
                model=model,
                system=system,
                max_tokens=max_tokens,
                usage=self.usage,
            )
        finally:
            # Attribute even on failure: a stage that burned tokens and then
            # gave up is exactly what a cost review needs to see.
            self.usage.attribute_stage(stage.value, tier.value, model, before)
