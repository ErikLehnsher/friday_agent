"""Non-interactive Codex CLI runner for Telegram's ChatGPT engine."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .claude_runner import (
    UserSession,
    append_session_transcript,
    get_session_context,
    get_user_session,
)


FRIDAY_CODEX_INSTRUCTIONS = """You are Friday, a concise personal assistant.
Work only inside the current directory, which is this Telegram user's private
workspace. Never access parent directories or unrelated host data. Do not install
software, delete data, or use network access unless the bot explicitly authorizes
that capability. If the conversation context identifies an uploaded image, it is
available inside the current workspace. Inspect that image when the user refers to
it; do not claim it was not received. Format multi-line code in fenced Markdown
blocks for Telegram."""


def _build_prompt(prompt: str, session: UserSession) -> str:
    context = get_session_context(session)
    if not context:
        return f"{FRIDAY_CODEX_INSTRUCTIONS}\n\nUser request:\n{prompt}"
    return (
        f"{FRIDAY_CODEX_INSTRUCTIONS}\n\n"
        "Recent local conversation context from this same Telegram session:\n"
        f"{context}\n\nUser request:\n{prompt}"
    )


def run_codex(
    codex_bin: str,
    sessions_root: Path,
    user_id: int,
    prompt: str,
    model: str = "gpt-5.6-luna",
    effort: str = "medium",
    timeout_seconds: int = 300,
    store_raw_transcripts: bool = False,
) -> tuple[str, UserSession]:
    """Run Codex in the user's isolated workspace and return its final text."""
    session = get_user_session(sessions_root, user_id)
    prepared_prompt = _build_prompt(prompt, session)
    if store_raw_transcripts:
        append_session_transcript(session, "user", prompt, model, effort, engine="codex")
    command = [
        codex_bin,
        "exec",
        "--model",
        model,
        "--config",
        f'model_reasoning_effort="{effort}"',
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
        prepared_prompt,
    ]
    completed = subprocess.run(
        command,
        cwd=session.workspace,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        shell=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip()[:500]
        raise RuntimeError(detail or f"Codex exited with code {completed.returncode}")
    result = completed.stdout.strip() or "Codex không trả về nội dung."
    if store_raw_transcripts:
        append_session_transcript(session, "assistant", result, model, effort, engine="codex")
    return result, session
