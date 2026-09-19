"""Settings, all overridable via environment (prefix MIAGENT_).

Examples:
    MIAGENT_LLM_BACKEND=claude_cli          # default; local claude login, no key
    MIAGENT_LLM_BACKEND=anthropic  ANTHROPIC_API_KEY=sk-ant-...
    MIAGENT_LLM_BACKEND=openai     MIAGENT_API_KEY=sk-or-... \
        MIAGENT_BASE_URL=https://openrouter.ai/api/v1 \
        MIAGENT_MODEL_FAST=openai/gpt-4o-mini \
        MIAGENT_MODEL_STANDARD=anthropic/claude-sonnet-5 \
        MIAGENT_MODEL_STRONG=anthropic/claude-opus-5
"""

from __future__ import annotations

from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MIAGENT_", env_file=".env", extra="ignore")

    llm_backend: str = "claude_cli"  # claude_cli | anthropic | openai

    # Generic credentials for the API backends. For the anthropic backend,
    # the standard ANTHROPIC_API_KEY env var also works.
    api_key: Optional[str] = None
    base_url: Optional[str] = None

    # Per-tier model overrides. Empty = per-backend defaults from the router.
    model_fast: Optional[str] = None
    model_standard: Optional[str] = None
    model_strong: Optional[str] = None

    # Repair loop bounds
    max_repair_iterations: int = 6
    escalate_after: int = 2  # repair iterations on standard tier before strong

    # Doc ingestion
    docs_max_chars: int = 80_000

    # Validation harness
    scrape_timeout_s: float = 10.0
    promtool_path: str = "promtool"
    otelcol_path: str = "otelcol"
    snmp_exporter_path: str = "./bin/snmp_exporter"

    # SNMP
    snmp_community: str = "public"
    snmp_version: int = 2
    snmp_exporter_port: int = 9116
    mib_sources: list[str] = ["https://mibs.pysnmp.com/asn1/"]


settings = Settings()
