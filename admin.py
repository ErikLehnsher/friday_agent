"""Run Friday's local-only admin dashboard."""

from agent_bot.admin_dashboard import serve
from agent_bot.config import load_settings


if __name__ == "__main__":
    settings = load_settings()
    print("Friday Admin: http://127.0.0.1:8765")
    serve(
        settings.bot_data_dir,
        engine_settings={
            "claude": {"model": settings.claude_model, "effort": settings.claude_effort},
            "codex": {"model": settings.codex_model, "effort": settings.codex_effort},
        },
    )
