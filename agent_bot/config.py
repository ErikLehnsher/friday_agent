"""Environment-only configuration for the bot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    allowed_user_ids: frozenset[int]
    admin_user_ids: frozenset[int]
    agent_project_dir: Path
    bot_data_dir: Path
    claude_bin: str = "claude"
    claude_model: str = "sonnet"
    claude_effort: str = "medium"
    claude_max_turns: int = 16
    claude_timeout_seconds: int = 300
    codex_bin: str = "codex"
    codex_model: str = "gpt-5.6-luna"
    codex_effort: str = "medium"
    codex_enabled: bool = True
    codex_timeout_seconds: int = 300
    agent_mode: str = "test"
    store_raw_transcripts: bool = True
    web_access: str = "search"
    chatgpt_desktop_enabled: bool = False
    chatgpt_desktop_timeout_seconds: int = 180
    env_path: Path = ENV_PATH


def _parse_user_ids(raw: str) -> frozenset[int]:
    values: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            value = int(item)
        except ValueError as exc:
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS must contain integers") from exc
        if value <= 0:
            raise ValueError("Telegram user IDs must be positive integers")
        values.add(value)
    return frozenset(values)


def load_settings() -> Settings:
    load_dotenv(ENV_PATH)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required")

    project_dir_raw = os.getenv("AGENT_PROJECT_DIR", "").strip()
    if not project_dir_raw:
        raise ValueError("AGENT_PROJECT_DIR is required")
    project_dir = Path(project_dir_raw).expanduser().resolve()
    if not project_dir.is_dir():
        raise ValueError("AGENT_PROJECT_DIR must be an existing directory")

    claude_bin = os.getenv("CLAUDE_BIN", "claude").strip()
    if not claude_bin:
        raise ValueError("CLAUDE_BIN must not be empty")
    claude_model = os.getenv("CLAUDE_MODEL", "sonnet").strip() or "sonnet"
    claude_effort = os.getenv("CLAUDE_EFFORT", "medium").strip() or "medium"
    if claude_effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError("CLAUDE_EFFORT must be low, medium, high, xhigh, or max")
    claude_max_turns_raw = os.getenv("CLAUDE_MAX_TURNS", "16").strip()
    try:
        claude_max_turns = int(claude_max_turns_raw)
    except ValueError as exc:
        raise ValueError("CLAUDE_MAX_TURNS must be an integer") from exc
    if not 1 <= claude_max_turns <= 40:
        raise ValueError("CLAUDE_MAX_TURNS must be 1-40")
    claude_timeout_raw = os.getenv("CLAUDE_TIMEOUT_SECONDS", "300").strip()
    try:
        claude_timeout_seconds = int(claude_timeout_raw)
    except ValueError as exc:
        raise ValueError("CLAUDE_TIMEOUT_SECONDS must be an integer") from exc
    if not 15 <= claude_timeout_seconds <= 900:
        raise ValueError("CLAUDE_TIMEOUT_SECONDS must be 15-900")
    codex_bin = os.getenv("CODEX_BIN", "codex").strip()
    if not codex_bin:
        raise ValueError("CODEX_BIN must not be empty")
    codex_model = os.getenv("CODEX_MODEL", "gpt-5.6-luna").strip() or "gpt-5.6-luna"
    codex_effort = os.getenv("CODEX_EFFORT", "medium").strip() or "medium"
    if codex_effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
        raise ValueError("CODEX_EFFORT must be none, low, medium, high, xhigh, or max")
    codex_enabled_raw = os.getenv("CODEX_ENABLED", "true").strip().lower()
    if codex_enabled_raw not in {"true", "false"}:
        raise ValueError("CODEX_ENABLED must be true or false")
    codex_timeout_raw = os.getenv("CODEX_TIMEOUT_SECONDS", "300").strip()
    try:
        codex_timeout_seconds = int(codex_timeout_raw)
    except ValueError as exc:
        raise ValueError("CODEX_TIMEOUT_SECONDS must be an integer") from exc
    if not 15 <= codex_timeout_seconds <= 900:
        raise ValueError("CODEX_TIMEOUT_SECONDS must be 15-900")
    agent_mode = os.getenv("AGENT_MODE", "test").strip().lower() or "test"
    if agent_mode not in {"test", "personal"}:
        raise ValueError("AGENT_MODE must be test or personal")
    raw_transcripts_value = os.getenv("STORE_RAW_TRANSCRIPTS", "true").strip().lower()
    if raw_transcripts_value not in {"true", "false"}:
        raise ValueError("STORE_RAW_TRANSCRIPTS must be true or false")
    store_raw_transcripts = raw_transcripts_value == "true"
    web_access = os.getenv("WEB_ACCESS", "search").strip().lower() or "search"
    if web_access not in {"none", "search", "full"}:
        raise ValueError("WEB_ACCESS must be none, search, or full")
    chatgpt_enabled_raw = os.getenv("CHATGPT_DESKTOP_ENABLED", "false").strip().lower()
    if chatgpt_enabled_raw not in {"true", "false"}:
        raise ValueError("CHATGPT_DESKTOP_ENABLED must be true or false")
    chatgpt_timeout_raw = os.getenv("CHATGPT_DESKTOP_TIMEOUT_SECONDS", "180").strip()
    try:
        chatgpt_desktop_timeout_seconds = int(chatgpt_timeout_raw)
    except ValueError as exc:
        raise ValueError("CHATGPT_DESKTOP_TIMEOUT_SECONDS must be an integer") from exc
    if not 15 <= chatgpt_desktop_timeout_seconds <= 600:
        raise ValueError("CHATGPT_DESKTOP_TIMEOUT_SECONDS must be 15-600")
    bot_data_dir_raw = os.getenv("BOT_DATA_DIR", "").strip()
    if not bot_data_dir_raw:
        raise ValueError("BOT_DATA_DIR is required")
    bot_data_dir = Path(bot_data_dir_raw).expanduser().resolve()

    allowed_user_ids = _parse_user_ids(os.getenv("TELEGRAM_ALLOWED_USER_IDS", ""))
    admin_user_ids = _parse_user_ids(os.getenv("TELEGRAM_ADMIN_USER_IDS", ""))
    if not admin_user_ids and allowed_user_ids:
        admin_user_ids = frozenset({min(allowed_user_ids)})

    return Settings(
        telegram_bot_token=token,
        allowed_user_ids=allowed_user_ids,
        admin_user_ids=admin_user_ids,
        agent_project_dir=project_dir,
        bot_data_dir=bot_data_dir,
        claude_bin=claude_bin,
        claude_model=claude_model,
        claude_effort=claude_effort,
        claude_max_turns=claude_max_turns,
        claude_timeout_seconds=claude_timeout_seconds,
        codex_bin=codex_bin,
        codex_model=codex_model,
        codex_effort=codex_effort,
        codex_enabled=codex_enabled_raw == "true",
        codex_timeout_seconds=codex_timeout_seconds,
        agent_mode=agent_mode,
        store_raw_transcripts=store_raw_transcripts,
        web_access=web_access,
        chatgpt_desktop_enabled=chatgpt_enabled_raw == "true",
        chatgpt_desktop_timeout_seconds=chatgpt_desktop_timeout_seconds,
    )
