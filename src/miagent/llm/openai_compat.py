"""OpenAI-compatible backend: OpenAI, OpenRouter, vLLM, Ollama, etc.

Anything that speaks the /v1/chat/completions protocol works — point
``base_url`` at the provider (e.g. https://openrouter.ai/api/v1) and supply
its key. Structured output uses the generic extract-and-repair path from
the base class, with a JSON response-format hint where the server honors it.
"""

from __future__ import annotations

from typing import Optional

from miagent.llm.base import LLMBackend, LLMError, LLMResponse


class OpenAICompatBackend(LLMBackend):
    name = "openai"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        json_mode: bool = True,
    ):
        try:
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover
            raise LLMError("`openai` package not installed") from e
        kwargs = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self.client = OpenAI(**kwargs)
        self.json_mode = json_mode

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        system: str = "",
        max_tokens: int = 8192,
    ) -> LLMResponse:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        kwargs = {}
        # Ask for JSON mode when the system prompt demands JSON (our
        # structured() path always does); harmless hint elsewhere is skipped.
        if self.json_mode and "JSON Schema" in system:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = self.client.chat.completions.create(
                model=model,
                messages=messages,
                max_completion_tokens=max_tokens,
                **kwargs,
            )
        except Exception as e:
            # Some servers reject response_format or max_completion_tokens;
            # retry once with the plainest possible request.
            try:
                resp = self.client.chat.completions.create(
                    model=model, messages=messages
                )
            except Exception:
                raise LLMError(f"openai-compatible request failed: {e}") from e
        choice = resp.choices[0]
        u = getattr(resp, "usage", None)
        return LLMResponse(
            text=choice.message.content or "",
            model=getattr(resp, "model", model) or model,
            backend=self.name,
            input_tokens=getattr(u, "prompt_tokens", 0) or 0,
            output_tokens=getattr(u, "completion_tokens", 0) or 0,
            raw=resp,
        )
