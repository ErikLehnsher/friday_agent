"""Run Friday's local-only admin dashboard."""

import os

from agent_bot.admin_dashboard import serve
from agent_bot.config import load_settings

# Binds 127.0.0.1 by default (host-only). Inside Docker, the container's own
# loopback isn't reachable via the published port, so the compose file sets
# ADMIN_HOST=0.0.0.0 and relies on the "127.0.0.1:8765:8765" port mapping to
# keep the dashboard host-local-only instead.
ADMIN_HOST = os.getenv("ADMIN_HOST", "127.0.0.1").strip() or "127.0.0.1"
ADMIN_USER = os.getenv("ADMIN_USER", "").strip() or None
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip() or None


if __name__ == "__main__":
    settings = load_settings()
    print("Friday Admin: http://127.0.0.1:8765")
    if ADMIN_USER and ADMIN_PASSWORD:
        print("Basic Auth enabled for the dashboard.")
    else:
        print("WARNING: no ADMIN_USER/ADMIN_PASSWORD set - dashboard is open.")
    serve(
        settings.bot_data_dir,
        host=ADMIN_HOST,
        auth_user=ADMIN_USER,
        auth_password=ADMIN_PASSWORD,
        engine_settings={
            "claude": {"model": settings.claude_model, "effort": settings.claude_effort},
            "codex": {"model": settings.codex_model, "effort": settings.codex_effort},
        },
    )
