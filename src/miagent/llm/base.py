"""Provider-agnostic LLM backend interface.

Backends implement one method — ``complete()`` — returning plain text.
Structured output (the common case in this pipeline) is layered on top in
``structured()``: prompt for JSON, extract, validate against a Pydantic
model, and feed validation errors back for a bounded number of repair
attempts. Backends that have a native structured-output mode (Anthropic)
override ``structured()`` to use it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

# Fenced or bare JSON object/array anywhere in the text.
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class LLMError(RuntimeError):
    """Raised when a backend fails or structured output cannot be obtained."""


@dataclass
class LLMResponse:
    text: str
    model: str
    backend: str
    input_tokens: int = 0
    output_tokens: int = 0
    raw: Any = None


@dataclass
class Usage:
    """Accumulated usage for a run — the per-service cost ledger.

    Tracked by stage as well as by model, because the interesting question
    isn't "what did this cost" but "which stage spent it, on which tier" —
    that's what shows whether the cost tiering is actually working.
    """

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    by_model: dict = field(default_factory=dict)
    by_stage: dict = field(default_factory=dict)

    @staticmethod
    def _bump(bucket: dict, key: str, resp: LLMResponse, **extra) -> None:
        entry = bucket.setdefault(
            key, {"calls": 0, "input_tokens": 0, "output_tokens": 0, **extra}
        )
        entry["calls"] += 1
        entry["input_tokens"] += resp.input_tokens
        entry["output_tokens"] += resp.output_tokens
        entry.update(extra)

    def add(self, resp: LLMResponse, stage: str = "", tier: str = "") -> None:
        self.calls += 1
        self.input_tokens += resp.input_tokens
        self.output_tokens += resp.output_tokens
        self._bump(self.by_model, resp.model, resp)
        if stage:
            self._bump(self.by_stage, stage, resp, model=resp.model, tier=tier)

    def snapshot(self) -> tuple[int, int, int]:
        return (self.calls, self.input_tokens, self.output_tokens)

    def attribute_stage(
        self, stage: str, tier: str, model: str, since: tuple[int, int, int]
    ) -> None:
        """Assign usage accrued since `since` to a stage.

        Structured-output calls go through the backend (which may retry on
        schema-validation failures), so the router can't see each response;
        it attributes the whole delta to the stage instead.
        """
        entry = self.by_stage.setdefault(
            stage,
            {"calls": 0, "input_tokens": 0, "output_tokens": 0, "model": model, "tier": tier},
        )
        entry["calls"] += self.calls - since[0]
        entry["input_tokens"] += self.input_tokens - since[1]
        entry["output_tokens"] += self.output_tokens - since[2]
        entry["model"] = model
        entry["tier"] = tier


def extract_json(text: str) -> Any:
    """Pull the first parseable JSON value out of model output."""
    candidates: list[str] = []
    for m in _JSON_BLOCK_RE.finditer(text):
        candidates.append(m.group(1))
    candidates.append(text)
    # Also try from the first brace/bracket to the end.
    for opener in ("{", "["):
        idx = text.find(opener)
        if idx != -1:
            candidates.append(text[idx:])
    for cand in candidates:
        cand = cand.strip()
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    raise LLMError("no parseable JSON found in model output")


class LLMBackend:
    name = "base"

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        system: str = "",
        max_tokens: int = 8192,
    ) -> LLMResponse:
        raise NotImplementedError

    def structured(
        self,
        prompt: str,
        schema: Type[T],
        *,
        model: str,
        system: str = "",
        max_tokens: int = 8192,
        max_repairs: int = 2,
        usage: Optional[Usage] = None,
    ) -> T:
        """Get a validated instance of ``schema`` from the model."""
        schema_json = json.dumps(schema.model_json_schema(), indent=2)
        base_system = (system + "\n\n" if system else "") + (
            "Respond with a single JSON object that validates against this "
            "JSON Schema. Output only the JSON, no prose, no markdown fences.\n"
            f"{schema_json}"
        )
        attempt_prompt = prompt
        last_err: Exception = LLMError("no attempts made")
        for _ in range(1 + max_repairs):
            resp = self.complete(
                attempt_prompt, model=model, system=base_system, max_tokens=max_tokens
            )
            if usage is not None:
                usage.add(resp)
            try:
                data = extract_json(resp.text)
                return schema.model_validate(data)
            except (LLMError, ValidationError) as e:
                last_err = e
                attempt_prompt = (
                    prompt
                    + "\n\nYour previous response failed validation with this "
                    + f"error:\n{e}\n\nReturn corrected JSON only."
                )
        raise LLMError(
            f"structured output failed after {1 + max_repairs} attempts: {last_err}"
        )
