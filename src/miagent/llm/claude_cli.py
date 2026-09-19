"""Backend that shells out to the local `claude` CLI (Claude Code).

Uses whatever login the CLI already has — no API key required. This is the
default backend for local development. Model names are the CLI aliases
("haiku", "sonnet", "opus") or full model IDs.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from miagent.llm.base import LLMBackend, LLMError, LLMResponse


class ClaudeCliBackend(LLMBackend):
    name = "claude_cli"

    def __init__(self, binary: str = "claude", timeout: int = 600):
        self.binary = binary
        self.timeout = timeout
        if shutil.which(binary) is None:
            raise LLMError(
                f"`{binary}` CLI not found on PATH — install Claude Code or "
                "switch backends (MIAGENT_LLM_BACKEND=anthropic|openai)"
            )

    @staticmethod
    def _explain_failure(proc: subprocess.CompletedProcess) -> str:
        """Turn a CLI failure into one actionable line.

        The CLI reports errors as a ~1.5KB JSON blob on stdout; dumping it
        raw buries the one field that matters. `terminal_reason: api_error`
        with zero tokens is the usage-limit signature, which a long
        pipeline run needs to distinguish from a prompt problem.
        """
        detail = ""
        try:
            payload = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            payload = {}
        if payload:
            reason = payload.get("terminal_reason") or payload.get("stop_reason") or ""
            usage = payload.get("usage") or {}
            spent = int(usage.get("output_tokens", 0) or 0)
            if reason == "api_error" and spent == 0:
                return (
                    "claude CLI made no progress (terminal_reason=api_error, 0 tokens) — "
                    "usually a usage limit or upstream API error. Retry later, or switch "
                    "backends: MIAGENT_LLM_BACKEND=anthropic (ANTHROPIC_API_KEY) / "
                    "openai (MIAGENT_API_KEY + MIAGENT_BASE_URL)."
                )
            detail = f"terminal_reason={reason!r} result={str(payload.get('result'))[:300]!r}"
        return (
            f"claude CLI exited {proc.returncode}: "
            + (detail or f"stderr={proc.stderr.strip()[:300]!r} stdout={proc.stdout.strip()[:300]!r}")
        )

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        system: str = "",
        max_tokens: int = 8192,  # not enforceable via CLI; kept for interface parity
    ) -> LLMResponse:
        cmd = [
            self.binary,
            "-p",  # print mode (non-interactive)
            "--output-format", "json",
            "--model", model,
            "--max-turns", "1",
            # Pure text-in/text-out calls: without these, prompts like "fix
            # this exporter" make the model reach for Edit/Bash or MCP tools
            # and the single-turn run dies with stop_reason=tool_use.
            # --tools "" strips built-ins; --strict-mcp-config ignores the
            # user's configured MCP servers.
            "--tools", "",
            "--strict-mcp-config",
        ]
        if system:
            cmd += ["--system-prompt", system]
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"claude CLI timed out after {self.timeout}s") from e
        if proc.returncode != 0:
            raise LLMError(self._explain_failure(proc))
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise LLMError(
                f"unparseable claude CLI output: {proc.stdout[:500]!r}"
            ) from e
        if payload.get("is_error"):
            raise LLMError(f"claude CLI error result: {payload.get('result')}")
        usage = payload.get("usage") or {}
        # Cache-served tokens are reported separately; fold them in so the
        # ledger reflects real prompt size (cache reads are ~10x cheaper,
        # but they're still input).
        input_tokens = sum(
            int(usage.get(k, 0) or 0)
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        )
        return LLMResponse(
            text=payload.get("result", ""),
            model=payload.get("model", model),
            backend=self.name,
            input_tokens=input_tokens,
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            raw=payload,
        )
