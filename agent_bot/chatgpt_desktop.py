"""Experimental local macOS driver for the ChatGPT Desktop application."""

from __future__ import annotations

import subprocess
import time
import re


POLL_SECONDS = 2


class ChatGPTDesktopAutomationError(RuntimeError):
    """A safe, user-actionable error from macOS UI automation."""


def _safe_error_detail(stderr: str) -> str:
    detail = stderr.lower()
    code_match = re.search(r"\((-?\d+)\)", stderr)
    code = f" (macOS code {code_match.group(1)})" if code_match else ""
    if any(token in detail for token in ("-1743", "-1719", "-10004", "not authorized", "not permitted", "accessibility", "-25211")):
        return "macOS chưa cấp Accessibility/Automation cho osascript hoặc app đang chạy bot" + code
    if any(token in detail for token in ("application \"chatgpt\"", "process \"chatgpt\"", "-600", "-1728")):
        return "không tìm thấy hoặc không thể điều khiển giao diện ChatGPT Desktop" + code
    return "AppleScript điều khiển ChatGPT Desktop bị từ chối hoặc không tương thích" + code

_SUBMIT_SCRIPT = r'''
on run argv
    set promptText to item 1 of argv
    set startNewChat to item 2 of argv
    set previousClipboard to the clipboard
    tell application "ChatGPT" to activate
    delay 1
    tell application "System Events"
        tell process "ChatGPT"
            set frontmost to true
            if startNewChat is "true" then
                keystroke "n" using command down
                delay 2
            end if
            set the clipboard to promptText
            keystroke "v" using command down
            delay 0.5
            try
                set sendButton to first button of entire contents of window 1 whose description contains "Send"
                click sendButton
            on error
                try
                    set sendButton to first button of entire contents of window 1 whose name contains "Send"
                    click sendButton
                on error
                    keystroke return
                end try
            end try
            delay 0.2
            set the clipboard to previousClipboard
        end tell
    end tell
end run
'''

_VISIBLE_TEXT_SCRIPT = r'''
tell application "System Events"
    tell process "ChatGPT"
        if not (exists window 1) then return ""
        set textItems to value of every static text of entire contents of window 1
        set AppleScript's text item delimiters to linefeed
        return textItems as text
    end tell
end tell
'''

_COPY_BUTTON_COUNT_SCRIPT = r'''
tell application "System Events"
    tell process "ChatGPT"
        if not (exists window 1) then return "0"
        try
            set copyButtons to every button of entire contents of window 1 whose description contains "Copy"
            return (count of copyButtons) as text
        on error
            try
                set copyButtons to every button of entire contents of window 1 whose name contains "Copy"
                return (count of copyButtons) as text
            on error
                return "0"
            end try
        end try
    end tell
end tell
'''

_COPY_LATEST_RESPONSE_SCRIPT = r'''
set previousClipboard to the clipboard
tell application "System Events"
    tell process "ChatGPT"
        try
            set copyButtons to every button of entire contents of window 1 whose description contains "Copy"
        on error
            set copyButtons to every button of entire contents of window 1 whose name contains "Copy"
        end try
        if (count of copyButtons) is 0 then return ""
        click item (count of copyButtons) of copyButtons
    end tell
end tell
delay 0.3
set responseText to the clipboard as text
set the clipboard to previousClipboard
return responseText
'''


def _osascript(script: str, *arguments: str, timeout: int = 20) -> str:
    try:
        completed = subprocess.run(
            ["osascript", "-e", script, *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except subprocess.CalledProcessError as exc:
        raise ChatGPTDesktopAutomationError(
            _safe_error_detail(exc.stderr or "")
        ) from exc
    return completed.stdout.strip()


def _visible_text() -> str:
    try:
        return _osascript(_VISIBLE_TEXT_SCRIPT)
    except (ChatGPTDesktopAutomationError, subprocess.SubprocessError, OSError):
        return ""


def _copy_button_count() -> int:
    try:
        return int(_osascript(_COPY_BUTTON_COUNT_SCRIPT) or "0")
    except (ValueError, ChatGPTDesktopAutomationError, subprocess.SubprocessError, OSError):
        return 0


def _copy_latest_response() -> str:
    try:
        return _osascript(_COPY_LATEST_RESPONSE_SCRIPT)
    except (ChatGPTDesktopAutomationError, subprocess.SubprocessError, OSError):
        return ""


def ask_chatgpt_desktop(
    prompt: str, timeout_seconds: int = 180, start_new_chat: bool = False
) -> str:
    """Run through the local app; no Telegram-to-ChatGPT connection exists."""
    if not prompt.strip():
        raise ValueError("Prompt ChatGPT không được để trống.")
    before = _visible_text()
    # Recent ChatGPT builds do not consistently expose the Copy icon as an
    # accessibility button.  Snapshot the copied latest answer itself instead
    # of relying only on the number of visible Copy buttons.
    copied_before = _copy_latest_response()
    copy_buttons_before = _copy_button_count()
    _osascript(
        _SUBMIT_SCRIPT, prompt, "true" if start_new_chat else "false", timeout=20
    )
    deadline = time.monotonic() + timeout_seconds
    latest = ""
    unchanged_rounds = 0
    last_copied = copied_before
    while time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        visible = _visible_text()
        copied_response = _copy_latest_response()
        if (
            copied_response
            and copied_response != copied_before
            and copied_response != last_copied
        ):
            return copied_response
        last_copied = copied_response or last_copied

        # Keep the count as a fast path for app versions that do expose it, but
        # do not make it a prerequisite for collecting the answer.
        if _copy_button_count() > copy_buttons_before:
            copied_response = _copy_latest_response()
            if copied_response and copied_response != copied_before:
                return copied_response
        if visible and visible != before:
            if visible == latest:
                unchanged_rounds += 1
            else:
                latest = visible
                unchanged_rounds = 0
            if unchanged_rounds >= 2:
                break
    if not latest:
        raise RuntimeError(
            "Không đọc được phản hồi từ ChatGPT Desktop. Kiểm tra app đang đăng nhập "
            "và quyền Accessibility cho Terminal/Python."
        )
    _, separator, response = latest.rpartition(prompt)
    result = response.strip() if separator else latest.strip()
    return result or "ChatGPT Desktop chưa trả về nội dung có thể đọc được."
