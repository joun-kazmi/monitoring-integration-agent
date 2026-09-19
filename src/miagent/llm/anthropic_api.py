"""Anthropic API backend (requires ANTHROPIC_API_KEY or an `ant auth` profile).

Overrides ``structured()`` to use the API's native structured outputs
(`client.messages.parse` with a Pydantic model), which guarantees
schema-valid JSON without the extract-and-repair loop.
"""

from __future__ import annotations

from typing import Optional, Type, TypeVar

from pydantic import BaseModel

from miagent.llm.base import LLMBackend, LLMError, LLMResponse, Usage

T = TypeVar("T", bound=BaseModel)


class AnthropicBackend(LLMBackend):
    name = "anthropic"

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover
            raise LLMError("`anthropic` package not installed") from e
        kwargs = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        # With no explicit key the SDK resolves ANTHROPIC_API_KEY /
        # ANTHROPIC_AUTH_TOKEN / an `ant auth login` profile.
        self.client = anthropic.Anthropic(**kwargs)

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        system: str = "",
        max_tokens: int = 8192,
    ) -> LLMResponse:
        kwargs = {}
        if system:
            kwargs["system"] = system
        resp = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
        if resp.stop_reason == "refusal":
            raise LLMError(f"model refused the request (model={model})")
        text = "".join(b.text for b in resp.content if b.type == "text")
        return LLMResponse(
            text=text,
            model=resp.model,
            backend=self.name,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            raw=resp,
        )

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
        kwargs = {}
        if system:
            kwargs["system"] = system
        try:
            resp = self.client.messages.parse(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
                output_format=schema,
                **kwargs,
            )
        except Exception:
            # Native structured outputs can reject schemas with unsupported
            # constraints on some models; fall back to the generic path.
            return super().structured(
                prompt,
                schema,
                model=model,
                system=system,
                max_tokens=max_tokens,
                max_repairs=max_repairs,
                usage=usage,
            )
        if usage is not None:
            usage.add(
                LLMResponse(
                    text="",
                    model=resp.model,
                    backend=self.name,
                    input_tokens=resp.usage.input_tokens,
                    output_tokens=resp.usage.output_tokens,
                )
            )
        if resp.parsed_output is None:
            raise LLMError("structured output parse returned no result")
        return resp.parsed_output
