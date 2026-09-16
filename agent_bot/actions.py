"""Fixed, allowlisted macOS actions. No user-provided command is executed."""

from __future__ import annotations

import subprocess
from pathlib import Path


SUBPROCESS_TIMEOUT_SECONDS = 20
MAIL_LIMIT = 20

_MAIL_SCRIPT = f'''tell application "Mail"
    set unreadMessages to (messages of inbox whose read status is false)
    set outputLines to {{}}
    set messageCount to count of unreadMessages
    if messageCount > {MAIL_LIMIT} then set messageCount to {MAIL_LIMIT}
    repeat with i from 1 to messageCount
        set m to item i of unreadMessages
        set end of outputLines to ((sender of m as text) & " | " & (subject of m as text) & " | " & (date received of m as text))
    end repeat
    set AppleScript's text item delimiters to linefeed
    return outputLines as text
end tell'''


def _run_fixed(args: list[str], timeout: int = SUBPROCESS_TIMEOUT_SECONDS) -> str:
    completed = subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
    )
    return completed.stdout.strip()


def unread_mail() -> str:
    result = _run_fixed(["osascript", "-e", _MAIL_SCRIPT])
    return result or "Không có email chưa đọc trong Inbox."


def open_claude() -> str:
    _run_fixed(["open", "-a", "Claude"])
    return "Đã gửi yêu cầu mở Claude."


def open_chrome(profile_dir: Path) -> str:
    profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _run_fixed(
        [
            "open",
            "-na",
            "Google Chrome",
            "--args",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
        ]
    )
    return "Đã mở Google Chrome bằng profile riêng, tách biệt với dữ liệu cá nhân."
