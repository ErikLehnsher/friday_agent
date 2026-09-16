"""Telegram command handlers."""

from __future__ import annotations

import logging
import re
import secrets
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from logging.handlers import RotatingFileHandler

import telebot
from requests.exceptions import RequestException
from dotenv import set_key

from .actions import open_chrome, open_claude, unread_mail
from .claude_runner import (
    append_session_transcript,
    get_user_session,
    list_user_sessions,
    run_claude,
    switch_user_session,
)
from .codex_runner import run_codex
from .config import Settings, load_settings
from .permissions import PERMISSIONS, PermissionRequests
from .user_registry import UserRegistry


MAX_MESSAGE_LENGTH = 4000
PERMISSION_REQUEST_PATTERN = re.compile(
    r"^\s*\[\[PERMISSION_REQUEST:([a-z_]+)\]\]\s*(.*)$", re.DOTALL
)
HELP_TEXT = """Lệnh hỗ trợ:
/start, /help - xem hướng dẫn
/pair <mã> - ghép tài khoản lần đầu
/invite - admin tạo mã mời user mới (10 phút)
/users - admin xem danh sách user
/request <quyền> <lý do> - xin admin cấp quyền
/permissions - xem quyền hiện tại
/requests - admin xem yêu cầu đang chờ
/research <nội dung> - tra cứu web hoặc xin quyền nếu chưa có
/mail - xem tối đa 20 email chưa đọc
/open_claude - mở ứng dụng Claude
/open_chrome - mở Google Chrome
/claude <prompt> - gửi yêu cầu tới Friday
/chatgpt <prompt> - dùng Codex CLI (tài khoản ChatGPT) trên máy Mac này
/engine - xem hoặc đổi engine mặc định của bạn
/new <tên> - tạo một Claude session sạch
/sessions - xem các session của bạn
/use <tên> - chuyển session đang dùng
/status - kiểm tra trạng thái bot

Quyền riêng tư: nội dung hội thoại được lưu cục bộ theo từng session để phát triển
memory. Không gửi token, mật khẩu hoặc dữ liệu nhạy cảm vào bot."""


def truncate(text: str, limit: int = MAX_MESSAGE_LENGTH) -> str:
    if len(text) <= limit:
        return text
    suffix = "\n… (đã rút gọn)"
    return text[: limit - len(suffix)] + suffix


@contextmanager
def typing_indicator(bot: telebot.TeleBot, chat_id: int):
    """Keep Telegram's typing animation alive during a blocking Claude call."""
    stopped = threading.Event()

    def refresh() -> None:
        while not stopped.is_set():
            try:
                bot.send_chat_action(chat_id, "typing")
            except Exception as exc:
                logging.debug(
                    "Could not refresh typing indicator: %s", type(exc).__name__
                )
            stopped.wait(4)

    worker = threading.Thread(target=refresh, name="telegram-typing", daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join(timeout=1)


def create_bot(settings: Settings) -> telebot.TeleBot:
    bot = telebot.TeleBot(settings.telegram_bot_token, threaded=False)
    allowed_user_ids = set(settings.allowed_user_ids)
    admin_user_ids = set(settings.admin_user_ids)
    registry = UserRegistry(settings.bot_data_dir)
    permission_requests = PermissionRequests(settings.bot_data_dir)
    registry.sync_configured_users(allowed_user_ids, admin_user_ids)
    pairing_code = secrets.token_urlsafe(8) if not allowed_user_ids else None
    pairing_expires_at = time.monotonic() + 600 if pairing_code else None
    pairing_invited_by: int | None = None

    if pairing_code:
        logging.warning("No allowed Telegram user is configured.")
        logging.warning("Send this command to the bot: /pair %s", pairing_code)

    def authorized(message: telebot.types.Message) -> bool:
        user = message.from_user
        if user is None or user.id not in allowed_user_ids:
            logging.warning("Rejected an unauthorized Telegram request")
            return False
        if not registry.is_active(user.id):
            logging.warning("Rejected a blocked Telegram user")
            return False
        registry.touch(user)
        return True

    @bot.message_handler(commands=["pair"])
    def pair_handler(message: telebot.types.Message) -> None:
        nonlocal pairing_code, pairing_expires_at, pairing_invited_by
        user = message.from_user
        supplied_code = (message.text or "").partition(" ")[2].strip()
        if (
            pairing_code is None
            or pairing_expires_at is None
            or time.monotonic() > pairing_expires_at
            or user is None
            or not secrets.compare_digest(supplied_code, pairing_code)
        ):
            return
        first_user = not allowed_user_ids
        allowed_user_ids.add(user.id)
        if first_user:
            admin_user_ids.add(user.id)
        set_key(
            str(settings.env_path),
            "TELEGRAM_ALLOWED_USER_IDS",
            ",".join(str(item) for item in sorted(allowed_user_ids)),
            quote_mode="never",
        )
        if first_user:
            set_key(
                str(settings.env_path),
                "TELEGRAM_ADMIN_USER_IDS",
                str(user.id),
                quote_mode="never",
            )
        role = "admin" if first_user else "user"
        registry.record_user(user, role=role, invited_by=pairing_invited_by)
        registry.audit("user_paired", user.id, role=role)
        pairing_code = None
        pairing_expires_at = None
        pairing_invited_by = None
        logging.info("Telegram account paired successfully")
        bot.reply_to(message, "Ghép tài khoản thành công. Gửi /start để tiếp tục.")

    @bot.message_handler(commands=["invite"])
    def invite_handler(message: telebot.types.Message) -> None:
        nonlocal pairing_code, pairing_expires_at, pairing_invited_by
        user = message.from_user
        if (
            not authorized(message)
            or user is None
            or user.id not in admin_user_ids
        ):
            return
        pairing_code = secrets.token_urlsafe(8)
        pairing_expires_at = time.monotonic() + 600
        pairing_invited_by = user.id
        set_key(
            str(settings.env_path),
            "TELEGRAM_ADMIN_USER_IDS",
            ",".join(str(item) for item in sorted(admin_user_ids)),
            quote_mode="never",
        )
        registry.audit("invite_created", user.id, expires_in_seconds=600)
        reply(
            message,
            "Mã mời dùng một lần, hết hạn sau 10 phút:\n"
            f"/pair {pairing_code}\n\n"
            "Gửi riêng lệnh này cho user cần mời.",
        )

    @bot.message_handler(commands=["users"])
    def users_handler(message: telebot.types.Message) -> None:
        user = message.from_user
        if (
            not authorized(message)
            or user is None
            or user.id not in admin_user_ids
        ):
            return
        rows = registry.list_users()
        lines = [f"Tổng user đã ghép: {len(rows)}"]
        for item in rows:
            username = f"@{item['username']}" if item.get("username") else "(chưa có username)"
            approved = [PERMISSIONS.get(name, name) for name in item.get("permissions", [])]
            permissions = "; ".join(approved) or "Chỉ workspace riêng"
            lines.append(
                f"- {item['user_id']} | {username} | {item.get('role', 'user')} | "
                f"{item.get('status', 'active')}\n  Đã duyệt: {permissions}"
            )
            if item["user_id"] not in admin_user_ids:
                keyboard = telebot.types.InlineKeyboardMarkup()
                state_action = (
                    "unblock" if item.get("status") == "blocked" else "block"
                )
                state_label = (
                    "✅ Cho phép dùng bot" if state_action == "unblock" else "⛔ Chặn dùng bot"
                )
                keyboard.row(
                    telebot.types.InlineKeyboardButton(
                        state_label,
                        callback_data=f"user:{state_action}:{item['user_id']}",
                    ),
                    telebot.types.InlineKeyboardButton(
                        "Quản lý quyền",
                        callback_data=f"user:perms:{item['user_id']}",
                    ),
                )
                bot.send_message(
                    message.chat.id,
                    f"Quản lý {username} ({item['user_id']})",
                    reply_markup=keyboard,
                )
        registry.audit("users_list_viewed", user.id, count=len(rows))
        reply(message, "\n".join(lines))

    def reply(
        message: telebot.types.Message, text: str, markdown: bool = False
    ) -> None:
        is_admin = (
            message.from_user is not None
            and message.from_user.id in admin_user_ids
        )
        if settings.agent_mode == "test" and not is_admin:
            text = text.replace(str(settings.agent_project_dir), "[workspace]")
            text = re.sub(
                r"(?<!\w)/(?:Users|Volumes|private|Applications|System|Library|opt|usr|var|etc|tmp)(?:/[^\s`]+)*",
                "[private path]",
                text,
            )
        text = truncate(text)
        if markdown:
            try:
                bot.reply_to(message, text, parse_mode="Markdown")
                return
            except telebot.apihelper.ApiTelegramException:
                logging.warning("Telegram rejected Markdown; using plain text")
        bot.reply_to(message, text)

    def send_to_user(
        user_id: int, chat_id: int, text: str, markdown: bool = False
    ) -> None:
        """Send a safe response when an admin resumes another user's request."""
        is_admin = user_id in admin_user_ids
        if settings.agent_mode == "test" and not is_admin:
            text = text.replace(str(settings.agent_project_dir), "[workspace]")
            text = re.sub(
                r"(?<!\w)/(?:Users|Volumes|private|Applications|System|Library|opt|usr|var|etc|tmp)(?:/[^\s`]+)*",
                "[private path]",
                text,
            )
        text = truncate(text)
        if markdown:
            try:
                bot.send_message(chat_id, text, parse_mode="Markdown")
                return
            except telebot.apihelper.ApiTelegramException:
                logging.warning("Telegram rejected Markdown; using plain text")
        bot.send_message(chat_id, text)

    def has_permission(user_id: int, permission: str) -> bool:
        return (
            user_id in admin_user_ids
            or permission in registry.get_permissions(user_id)
        )

    def request_permission(
        message: telebot.types.Message,
        permission: str,
        reason: str,
        deferred_action: dict | None = None,
    ) -> None:
        user = message.from_user
        if user is None or not authorized(message):
            return
        item = permission_requests.create(
            user.id, user.username, permission, reason, deferred_action
        )
        if not item["_created"]:
            reply(message, "Yêu cầu quyền này đang chờ admin duyệt.")
            return
        keyboard = telebot.types.InlineKeyboardMarkup()
        keyboard.row(
            telebot.types.InlineKeyboardButton(
                "✅ Approve", callback_data=f"perm:a:{item['request_id']}"
            ),
            telebot.types.InlineKeyboardButton(
                "❌ Deny", callback_data=f"perm:d:{item['request_id']}"
            ),
        )
        username = f"@{user.username}" if user.username else "(không username)"
        admin_text = (
            "Yêu cầu phê duyệt mới\n"
            f"User: {user.id} | {username}\n"
            f"Khả năng cần dùng: {PERMISSIONS[permission]}\n"
            f"Lý do: {item['reason'] or '(không nêu)'}"
        )
        for admin_id in admin_user_ids:
            bot.send_message(admin_id, admin_text, reply_markup=keyboard)
        request_kind = deferred_action.get("kind") if isinstance(deferred_action, dict) else None
        engine_metadata: dict[str, str] = {}
        if request_kind == "chatgpt":
            engine_metadata = {
                "engine": "codex",
                "model": settings.codex_model,
                "effort": settings.codex_effort,
            }
        elif request_kind == "claude":
            engine_metadata = {
                "engine": "claude",
                "model": settings.claude_model,
                "effort": settings.claude_effort,
            }
        registry.audit(
            "permission_requested", user.id, permission=permission, **engine_metadata
        )
        reply(
            message,
            f"Yêu cầu này cần {PERMISSIONS[permission].lower()}, nên Friday đang xin admin phê duyệt."
        )

    @bot.callback_query_handler(
        func=lambda call: bool(call.data and call.data.startswith("perm:"))
    )
    def permission_callback(call: telebot.types.CallbackQuery) -> None:
        admin = call.from_user
        if admin is None or admin.id not in admin_user_ids:
            bot.answer_callback_query(call.id, "Bạn không phải admin.", show_alert=True)
            return
        try:
            _, decision, request_id = call.data.split(":", 2)
        except ValueError:
            bot.answer_callback_query(call.id, "Request không hợp lệ.")
            return
        if decision not in {"a", "d"}:
            bot.answer_callback_query(call.id, "Request không hợp lệ.")
            return
        approved = decision == "a"
        item = permission_requests.resolve(request_id, approved, admin.id)
        if item is None:
            bot.answer_callback_query(call.id, "Request đã được xử lý hoặc không tồn tại.")
            return
        if approved:
            registry.grant_permission(item["user_id"], item["permission"])
        result_text = "được cấp" if approved else "bị từ chối"
        bot.answer_callback_query(call.id, f"Đã xử lý: {result_text}.")
        if call.message is not None:
            bot.edit_message_reply_markup(
                call.message.chat.id, call.message.message_id, reply_markup=None
            )
        bot.send_message(
            item["user_id"],
            (
                "Admin đã phê duyệt. Friday đang tiếp tục yêu cầu của bạn."
                if approved
                else "Admin chưa phê duyệt yêu cầu này."
            ),
        )
        deferred = item.get("deferred_action")
        resolved_kind = deferred.get("kind") if isinstance(deferred, dict) else None
        resolved_engine_metadata: dict[str, str] = {}
        if resolved_kind == "chatgpt":
            resolved_engine_metadata = {
                "engine": "codex",
                "model": settings.codex_model,
                "effort": settings.codex_effort,
            }
        elif resolved_kind == "claude":
            resolved_engine_metadata = {
                "engine": "claude",
                "model": settings.claude_model,
                "effort": settings.claude_effort,
            }
        registry.audit(
            "permission_resolved",
            item["user_id"],
            permission=item["permission"],
            approved=approved,
            admin_id=admin.id,
            **resolved_engine_metadata,
        )
        if approved:
            continue_approved_request(item)

    @bot.callback_query_handler(
        func=lambda call: bool(call.data and call.data.startswith("user:"))
    )
    def user_management_callback(call: telebot.types.CallbackQuery) -> None:
        admin = call.from_user
        if admin is None or admin.id not in admin_user_ids:
            bot.answer_callback_query(call.id, "Bạn không phải admin.", show_alert=True)
            return
        parts = (call.data or "").split(":")
        if len(parts) not in {3, 4}:
            bot.answer_callback_query(call.id, "Lệnh quản trị không hợp lệ.")
            return
        _, action, raw_user_id, *remainder = parts
        try:
            target_id = int(raw_user_id)
        except ValueError:
            bot.answer_callback_query(call.id, "User không hợp lệ.")
            return
        if target_id in admin_user_ids:
            bot.answer_callback_query(call.id, "Không thể chặn hoặc đổi quyền admin.")
            return
        try:
            if action in {"block", "unblock"}:
                registry.set_status(
                    target_id, "blocked" if action == "block" else "active"
                )
                label = "đã chặn" if action == "block" else "đã cho phép dùng lại"
                registry.audit("user_status_changed", target_id, status=action, admin_id=admin.id)
                bot.answer_callback_query(call.id, label.capitalize())
                if call.message is not None:
                    bot.edit_message_reply_markup(
                        call.message.chat.id, call.message.message_id, reply_markup=None
                    )
                bot.send_message(target_id, f"Admin {label} bot cho tài khoản này.")
                return
            if action == "perms" and not remainder:
                granted = sorted(registry.get_permissions(target_id))
                if not granted:
                    bot.answer_callback_query(call.id, "User này chưa có quyền bổ sung.")
                    return
                keyboard = telebot.types.InlineKeyboardMarkup()
                for permission in granted:
                    keyboard.add(
                        telebot.types.InlineKeyboardButton(
                            f"Thu hồi: {PERMISSIONS.get(permission, permission)}",
                            callback_data=f"user:revoke:{target_id}:{permission}",
                        )
                    )
                bot.answer_callback_query(call.id)
                bot.send_message(
                    admin.id,
                    f"Các quyền đã duyệt của user {target_id}:",
                    reply_markup=keyboard,
                )
                return
            if action == "revoke" and len(remainder) == 1:
                permission = remainder[0]
                if permission not in PERMISSIONS:
                    raise ValueError("Quyền không hợp lệ.")
                registry.revoke_permission(target_id, permission)
                registry.audit(
                    "permission_revoked", target_id, permission=permission, admin_id=admin.id
                )
                bot.answer_callback_query(call.id, "Đã thu hồi quyền.")
                if call.message is not None:
                    bot.edit_message_reply_markup(
                        call.message.chat.id, call.message.message_id, reply_markup=None
                    )
                bot.send_message(target_id, "Admin đã thu hồi một quyền đã cấp cho bạn.")
                return
        except ValueError as exc:
            bot.answer_callback_query(call.id, str(exc), show_alert=True)
            return
        bot.answer_callback_query(call.id, "Lệnh quản trị không hợp lệ.")

    @bot.message_handler(commands=["request"])
    def request_handler(message: telebot.types.Message) -> None:
        if not authorized(message) or message.from_user is None:
            return
        arguments = (message.text or "").partition(" ")[2].strip()
        permission, _, reason = arguments.partition(" ")
        permission = permission.strip().lower()
        if permission not in PERMISSIONS:
            reply(
                message,
                "Cách dùng: /request <quyền> <lý do>\nQuyền: "
                + ", ".join(sorted(PERMISSIONS)),
            )
            return
        if has_permission(message.from_user.id, permission):
            reply(message, f"Admin đã duyệt: {PERMISSIONS[permission]}.")
            return
        request_permission(message, permission, reason.strip())

    @bot.message_handler(commands=["permissions"])
    def permissions_handler(message: telebot.types.Message) -> None:
        if not authorized(message) or message.from_user is None:
            return
        if message.from_user.id in admin_user_ids:
            reply(message, "Bạn là admin và có toàn bộ quyền trong vùng được quản lý.")
            return
        granted = registry.get_permissions(message.from_user.id)
        lines = ["Các khả năng admin đã duyệt:"]
        for permission, description in PERMISSIONS.items():
            marker = "✅" if permission in granted else "○"
            lines.append(f"{marker} {description}")
        reply(message, "\n".join(lines))

    @bot.message_handler(commands=["requests"])
    def requests_handler(message: telebot.types.Message) -> None:
        user = message.from_user
        if not authorized(message) or user is None or user.id not in admin_user_ids:
            return
        pending = permission_requests.pending()
        if not pending:
            reply(message, "Không có yêu cầu quyền đang chờ.")
            return
        lines = ["Yêu cầu đang chờ:"]
        for item in pending:
            lines.append(
                f"- user {item['user_id']} | {PERMISSIONS.get(item['permission'], item['permission'])}"
            )
        reply(message, "\n".join(lines))

    @bot.message_handler(commands=["start", "help"])
    def help_handler(message: telebot.types.Message) -> None:
        if authorized(message):
            reply(message, HELP_TEXT)

    @bot.message_handler(commands=["status"])
    def status_handler(message: telebot.types.Message) -> None:
        if not authorized(message):
            return
        claude_path = shutil.which(settings.claude_bin)
        codex_path = shutil.which(settings.codex_bin)
        current_engine = registry.get_engine(message.from_user.id)
        model_status = (
            f"{settings.codex_model} / effort {settings.codex_effort}"
            if current_engine == "chatgpt"
            else f"{settings.claude_model} / effort {settings.claude_effort}"
        )
        reply(
            message,
            "Bot: đang chạy\n"
            f"Workspace: {'riêng theo user (đã ẩn đường dẫn)' if settings.agent_mode == 'test' and message.from_user.id not in admin_user_ids else settings.agent_project_dir}\n"
            f"Mode: {'admin' if message.from_user.id in admin_user_ids else settings.agent_mode}\n"
            f"Engine hiện tại: {'Codex' if current_engine == 'chatgpt' else 'Claude'}\n"
            f"Model: {model_status}\n"
            f"Web access: {settings.web_access}\n"
            f"Claude CLI: {'có (' + claude_path + ')' if claude_path else 'không tìm thấy'}\n"
            f"Codex CLI: {'có (' + codex_path + ')' if codex_path else 'không tìm thấy'}\n"
            f"Thời gian: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        )

    def run_action(message: telebot.types.Message, action) -> None:
        if not authorized(message):
            return
        try:
            reply(message, action())
        except subprocess.TimeoutExpired:
            reply(message, "Action quá thời gian chờ.")
        except (subprocess.SubprocessError, OSError) as exc:
            logging.warning("Allowlisted action failed: %s", type(exc).__name__)
            reply(message, "Không thể thực hiện action. Hãy kiểm tra quyền macOS và log cục bộ.")

    def handle_chatgpt_prompt(message: telebot.types.Message, prompt: str) -> None:
        if not authorized(message) or message.from_user is None:
            return
        if not settings.codex_enabled:
            reply(
                message,
                "Engine Codex đang tắt trên máy local. Admin cần bật CODEX_ENABLED=true rồi khởi động lại bot.",
            )
            return
        if not has_permission(message.from_user.id, "chatgpt_desktop"):
            request_permission(
                message,
                "chatgpt_desktop",
                "Yêu cầu cần dùng Codex CLI bằng tài khoản ChatGPT trên máy local.",
                deferred_action={
                    "kind": "chatgpt", "prompt": prompt, "chat_id": message.chat.id
                },
            )
            return
        active_session = get_user_session(
            settings.agent_project_dir, message.from_user.id
        )
        started_at = time.monotonic()
        registry.audit(
            "engine_run_started",
            message.from_user.id,
            engine="codex",
            model=settings.codex_model,
            effort=settings.codex_effort,
            session=active_session.workspace.name,
        )
        try:
            with typing_indicator(bot, message.chat.id):
                result, session = run_codex(
                    settings.codex_bin,
                    settings.agent_project_dir,
                    message.from_user.id,
                    prompt,
                    settings.codex_model,
                    settings.codex_effort,
                    settings.codex_timeout_seconds,
                    settings.store_raw_transcripts,
                )
            reply(
                message,
                result,
                markdown=True,
            )
            registry.audit(
                "engine_run_completed",
                message.from_user.id,
                engine="codex",
                model=settings.codex_model,
                effort=settings.codex_effort,
                session=session.workspace.name,
                duration_ms=round((time.monotonic() - started_at) * 1000),
            )
        except (RuntimeError, OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
            logging.warning("Codex invocation failed: %s", exc)
            registry.audit(
                "engine_run_failed",
                message.from_user.id,
                engine="codex",
                model=settings.codex_model,
                effort=settings.codex_effort,
                session=active_session.workspace.name,
                duration_ms=round((time.monotonic() - started_at) * 1000),
                error_type=type(exc).__name__,
            )
            reply(
                message,
                "Codex chưa chạy được. Kiểm tra Codex CLI đã đăng nhập và log cục bộ.",
            )

    @bot.message_handler(commands=["mail"])
    def mail_handler(message: telebot.types.Message) -> None:
        if not authorized(message) or message.from_user is None:
            return
        if not has_permission(message.from_user.id, "mail"):
            request_permission(
                message,
                "mail",
                "Yêu cầu cần đọc danh sách email chưa đọc.",
                deferred_action={"kind": "mail", "chat_id": message.chat.id},
            )
            return
        run_action(message, unread_mail)

    @bot.message_handler(commands=["open_claude"])
    def open_claude_handler(message: telebot.types.Message) -> None:
        if not authorized(message) or message.from_user is None:
            return
        if not has_permission(message.from_user.id, "claude_app"):
            request_permission(
                message,
                "claude_app",
                "Yêu cầu cần mở ứng dụng Claude trên máy host.",
                deferred_action={"kind": "claude_app", "chat_id": message.chat.id},
            )
            return
        run_action(message, open_claude)

    @bot.message_handler(commands=["open_chrome"])
    def open_chrome_handler(message: telebot.types.Message) -> None:
        if not authorized(message) or message.from_user is None:
            return
        if not has_permission(message.from_user.id, "chrome"):
            request_permission(
                message,
                "chrome",
                "Yêu cầu cần mở Chrome bằng profile riêng.",
                deferred_action={"kind": "chrome", "chat_id": message.chat.id},
            )
            return
        profile_dir = (
            settings.agent_project_dir
            / f"user-{message.from_user.id}"
            / "chrome-profile"
        )
        registry.audit("chrome_open_requested", message.from_user.id)
        run_action(message, lambda: open_chrome(profile_dir))

    @bot.message_handler(commands=["claude"])
    def claude_handler(message: telebot.types.Message) -> None:
        if not authorized(message):
            return
        prompt = (message.text or "").partition(" ")[2].strip()
        if not prompt:
            reply(message, "Cách dùng: /claude <prompt>")
            return
        # An explicit engine command also selects the engine for subsequent
        # ordinary messages in this user's active Telegram session.
        registry.set_engine(message.from_user.id, "claude")
        registry.audit(
            "engine_switched",
            message.from_user.id,
            engine="claude",
            model=settings.claude_model,
            effort=settings.claude_effort,
        )
        handle_claude_prompt(message, prompt)

    @bot.message_handler(commands=["chatgpt"])
    def chatgpt_handler(message: telebot.types.Message) -> None:
        if not authorized(message):
            return
        prompt = (message.text or "").partition(" ")[2].strip()
        if not prompt:
            reply(message, "Cách dùng: /chatgpt <nội dung cần hỏi>")
            return
        # Mirror /claude: the most recently chosen explicit engine becomes the
        # default for normal text until the user switches again.
        registry.set_engine(message.from_user.id, "chatgpt")
        registry.audit(
            "engine_switched",
            message.from_user.id,
            engine="codex",
            model=settings.codex_model,
            effort=settings.codex_effort,
        )
        handle_chatgpt_prompt(message, prompt)

    @bot.message_handler(commands=["engine"])
    def engine_handler(message: telebot.types.Message) -> None:
        if not authorized(message) or message.from_user is None:
            return
        selected = (message.text or "").partition(" ")[2].strip().lower()
        if selected:
            if selected not in {"claude", "chatgpt"}:
                reply(message, "Cách dùng: /engine claude hoặc /engine chatgpt")
                return
            registry.set_engine(message.from_user.id, selected)
            registry.audit(
                "engine_switched",
                message.from_user.id,
                engine="codex" if selected == "chatgpt" else selected,
                model=(settings.codex_model if selected == "chatgpt" else settings.claude_model),
                effort=(settings.codex_effort if selected == "chatgpt" else settings.claude_effort),
            )
            reply(message, f"Đã chuyển engine mặc định sang {selected.title()}.")
            return
        current = registry.get_engine(message.from_user.id)
        keyboard = telebot.types.InlineKeyboardMarkup()
        keyboard.row(
            telebot.types.InlineKeyboardButton("Claude", callback_data="engine:claude"),
            telebot.types.InlineKeyboardButton("ChatGPT", callback_data="engine:chatgpt"),
        )
        reply(message, f"Engine hiện tại: {current.title()}\nChọn engine cho tin nhắn thường:")
        bot.send_message(message.chat.id, "Chuyển engine:", reply_markup=keyboard)

    @bot.callback_query_handler(
        func=lambda call: bool(call.data and call.data.startswith("engine:"))
    )
    def engine_callback(call: telebot.types.CallbackQuery) -> None:
        user = call.from_user
        engine = (call.data or "").partition(":")[2]
        if user is None or user.id not in allowed_user_ids or not registry.is_active(user.id):
            bot.answer_callback_query(call.id, "Bạn không có quyền dùng bot.", show_alert=True)
            return
        if engine not in {"claude", "chatgpt"}:
            bot.answer_callback_query(call.id, "Engine không hợp lệ.")
            return
        registry.set_engine(user.id, engine)
        registry.audit(
            "engine_switched",
            user.id,
            engine="codex" if engine == "chatgpt" else engine,
            model=(settings.codex_model if engine == "chatgpt" else settings.claude_model),
            effort=(settings.codex_effort if engine == "chatgpt" else settings.claude_effort),
        )
        bot.answer_callback_query(call.id, f"Đã chuyển sang {engine.title()}.")
        if call.message is not None:
            bot.edit_message_reply_markup(
                call.message.chat.id, call.message.message_id, reply_markup=None
            )
            bot.send_message(call.message.chat.id, f"Engine mặc định: {engine.title()}.")

    def handle_claude_prompt(
        message: telebot.types.Message,
        prompt: str,
        force_new: bool = False,
        session_name: str | None = None,
        retried_after_grant: bool = False,
    ) -> None:
        if not authorized(message) or message.from_user is None:
            return
        try:
            is_admin = message.from_user.id in admin_user_ids
            effective_mode = "personal" if is_admin else settings.agent_mode
            permissions = registry.get_permissions(message.from_user.id)
            if is_admin:
                effective_web_access = settings.web_access
            elif (
                "web_fetch" in permissions
                and settings.web_access == "full"
            ):
                effective_web_access = "full"
            elif (
                {"web_search", "web_fetch"} & permissions
                and settings.web_access != "none"
            ):
                effective_web_access = "search"
            else:
                effective_web_access = "none"
            allow_shell = (
                is_admin
                or settings.agent_mode == "personal"
                or "shell" in permissions
            )
            granted_permissions = (
                tuple(sorted(PERMISSIONS)) if is_admin else tuple(sorted(permissions))
            )
            registry.audit(
                "claude_request",
                message.from_user.id,
                new_session=force_new,
            )
            active_session = None if force_new else get_user_session(
                settings.agent_project_dir, message.from_user.id
            )
            started_at = time.monotonic()
            registry.audit(
                "engine_run_started",
                message.from_user.id,
                engine="claude",
                model=settings.claude_model,
                effort=settings.claude_effort,
                session=(active_session.workspace.name if active_session else None),
            )
            with typing_indicator(bot, message.chat.id):
                result, session = run_claude(
                    settings.claude_bin,
                    settings.agent_project_dir,
                    message.from_user.id,
                    prompt,
                    settings.claude_model,
                    settings.claude_effort,
                    effective_mode,
                    force_new=force_new,
                    session_name=session_name,
                    admin_workspace_root=(
                        settings.agent_project_dir if is_admin else None
                    ),
                    store_raw_transcripts=settings.store_raw_transcripts,
                    web_access=effective_web_access,
                    allow_shell=allow_shell,
                    granted_permissions=granted_permissions,
                )
            approval_match = PERMISSION_REQUEST_PATTERN.match(result)
            if approval_match:
                permission, reason = approval_match.groups()
                if permission in PERMISSIONS and not has_permission(
                    message.from_user.id, permission
                ):
                    registry.audit(
                        "permission_auto_raised",
                        message.from_user.id,
                        permission=permission,
                        session=session.workspace.name,
                    )
                    request_permission(
                        message,
                        permission,
                        reason.strip(),
                        deferred_action={
                            "kind": {
                                "chrome": "chrome",
                                "mail": "mail",
                                "claude_app": "claude_app",
                            }.get(permission, "claude"),
                            "prompt": prompt,
                            "chat_id": message.chat.id,
                        },
                    )
                    return
                if permission in PERMISSIONS and not retried_after_grant:
                    # The permission is already active, so this is an obsolete
                    # model marker, not a new user approval request. Retry once
                    # with the current, granted tool configuration.
                    registry.audit(
                        "permission_marker_retried",
                        message.from_user.id,
                        permission=permission,
                    )
                    handle_claude_prompt(
                        message,
                        (
                            f"Admin has already approved {permission}. Use it now "
                            "and complete the original request; do not request it again.\n\n"
                            f"Original user request: {prompt}"
                        ),
                        force_new=force_new,
                        session_name=session_name,
                        retried_after_grant=True,
                    )
                    return
                if permission in PERMISSIONS:
                    registry.audit(
                        "permission_marker_suppressed",
                        message.from_user.id,
                        permission=permission,
                    )
                    reply(
                        message,
                        "Quyền này đã được cấp, nhưng Friday chưa kích hoạt được công cụ cho lượt này. Mình đã ghi log để admin kiểm tra.",
                    )
                    return
            reply(
                message,
                result,
                markdown=True,
            )
            registry.audit(
                "engine_run_completed",
                message.from_user.id,
                engine="claude",
                model=settings.claude_model,
                effort=settings.claude_effort,
                session=session.workspace.name,
                duration_ms=round((time.monotonic() - started_at) * 1000),
            )
        except subprocess.TimeoutExpired:
            registry.audit(
                "engine_run_failed",
                message.from_user.id,
                engine="claude",
                model=settings.claude_model,
                effort=settings.claude_effort,
                duration_ms=round((time.monotonic() - started_at) * 1000),
                error_type="TimeoutExpired",
            )
            reply(message, "Claude quá thời gian chờ 180 giây.")
        except (RuntimeError, OSError) as exc:
            # Claude CLI already returns a bounded stderr excerpt. Keep it in the
            # local operator log so configuration/auth failures are diagnosable,
            # but do not expose it to the Telegram user.
            logging.warning("Claude invocation failed: %s", exc)
            registry.audit(
                "engine_run_failed",
                message.from_user.id,
                engine="claude",
                model=settings.claude_model,
                effort=settings.claude_effort,
                duration_ms=round((time.monotonic() - started_at) * 1000),
                error_type=type(exc).__name__,
            )
            reply(message, "Claude không chạy được. Hãy kiểm tra CLI/auth và log cục bộ.")
        except ValueError as exc:
            reply(message, str(exc))

    def continue_approved_request(item: dict) -> None:
        """Resume the exact request after an admin grants its missing capability."""
        deferred = item.get("deferred_action")
        if not isinstance(deferred, dict):
            return
        user_id = item["user_id"]
        chat_id = deferred.get("chat_id", user_id)
        kind = deferred.get("kind")
        if not registry.is_active(user_id):
            logging.info("Skipped approved request for blocked user")
            return
        try:
            if kind == "claude":
                prompt = deferred.get("prompt")
                if not isinstance(prompt, str) or not prompt.strip():
                    return
                is_admin = user_id in admin_user_ids
                permissions = registry.get_permissions(user_id)
                effective_mode = "personal" if is_admin else settings.agent_mode
                if is_admin:
                    effective_web_access = settings.web_access
                elif "web_fetch" in permissions and settings.web_access == "full":
                    effective_web_access = "full"
                elif {"web_search", "web_fetch"} & permissions and settings.web_access != "none":
                    effective_web_access = "search"
                else:
                    effective_web_access = "none"
                allow_shell = (
                    is_admin
                    or settings.agent_mode == "personal"
                    or "shell" in permissions
                )
                granted_permissions = (
                    tuple(sorted(PERMISSIONS))
                    if is_admin
                    else tuple(sorted(permissions))
                )
                active_session = get_user_session(settings.agent_project_dir, user_id)
                started_at = time.monotonic()
                registry.audit(
                    "engine_run_started",
                    user_id,
                    engine="claude",
                    model=settings.claude_model,
                    effort=settings.claude_effort,
                    session=active_session.workspace.name,
                    approved_permission=item["permission"],
                )
                approved_prompt = (
                    f"Admin has approved {item['permission']} for this request. "
                    "Use that capability now and complete the original request. "
                    "Do not ask for the same permission again.\n\n"
                    f"Original user request: {prompt}"
                )
                with typing_indicator(bot, chat_id):
                    result, session = run_claude(
                        settings.claude_bin,
                        settings.agent_project_dir,
                        user_id,
                        approved_prompt,
                        settings.claude_model,
                        settings.claude_effort,
                        effective_mode,
                        admin_workspace_root=(
                            settings.agent_project_dir if is_admin else None
                        ),
                        store_raw_transcripts=settings.store_raw_transcripts,
                        web_access=effective_web_access,
                        allow_shell=allow_shell,
                        granted_permissions=granted_permissions,
                    )
                if PERMISSION_REQUEST_PATTERN.match(result):
                    registry.audit(
                        "permission_marker_suppressed",
                        user_id,
                        permission=item["permission"],
                        session=session.workspace.name,
                    )
                    send_to_user(
                        user_id,
                        chat_id,
                        "Quyền đã được cấp, nhưng Friday chưa kích hoạt được công cụ cho lượt này. Admin đã có log để kiểm tra.",
                    )
                    return
                send_to_user(user_id, chat_id, result, markdown=True)
                registry.audit(
                    "engine_run_completed",
                    user_id,
                    engine="claude",
                    model=settings.claude_model,
                    effort=settings.claude_effort,
                    session=session.workspace.name,
                    duration_ms=round((time.monotonic() - started_at) * 1000),
                    approved_permission=item["permission"],
                )
                registry.audit(
                    "approved_request_completed",
                    user_id,
                    permission=item["permission"],
                    session=session.workspace.name,
                )
                return
            if kind == "mail":
                send_to_user(user_id, chat_id, unread_mail())
            elif kind == "claude_app":
                send_to_user(user_id, chat_id, open_claude())
            elif kind == "chrome":
                profile_dir = settings.agent_project_dir / f"user-{user_id}" / "chrome-profile"
                send_to_user(user_id, chat_id, open_chrome(profile_dir))
            elif kind == "chatgpt":
                if not settings.codex_enabled:
                    send_to_user(
                        user_id,
                        chat_id,
                        "Admin đã duyệt, nhưng engine Codex hiện đang tắt trên máy local.",
                    )
                    return
                prompt = deferred.get("prompt")
                if not isinstance(prompt, str) or not prompt.strip():
                    return
                active_session = get_user_session(settings.agent_project_dir, user_id)
                started_at = time.monotonic()
                registry.audit(
                    "engine_run_started",
                    user_id,
                    engine="codex",
                    model=settings.codex_model,
                    effort=settings.codex_effort,
                    session=active_session.workspace.name,
                    approved_permission=item["permission"],
                )
                with typing_indicator(bot, chat_id):
                    result, session = run_codex(
                        settings.codex_bin,
                        settings.agent_project_dir,
                        user_id,
                        prompt,
                        settings.codex_model,
                        settings.codex_effort,
                        settings.codex_timeout_seconds,
                        settings.store_raw_transcripts,
                )
                send_to_user(user_id, chat_id, result, markdown=True)
                registry.audit(
                    "engine_run_completed",
                    user_id,
                    engine="codex",
                    model=settings.codex_model,
                    effort=settings.codex_effort,
                    session=session.workspace.name,
                    duration_ms=round((time.monotonic() - started_at) * 1000),
                    approved_permission=item["permission"],
                )
            else:
                return
            registry.audit(
                "approved_request_completed", user_id, permission=item["permission"]
            )
        except (RuntimeError, OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
            logging.warning("Approved request could not continue: %s", exc)
            send_to_user(user_id, chat_id, "Đã được cấp quyền, nhưng Friday chưa thể hoàn tất yêu cầu. Hãy kiểm tra log cục bộ.")

    @bot.message_handler(commands=["research"])
    def research_handler(message: telebot.types.Message) -> None:
        if not authorized(message) or message.from_user is None:
            return
        prompt = (message.text or "").partition(" ")[2].strip()
        if not prompt:
            reply(message, "Cách dùng: /research <nội dung cần tra cứu>")
            return
        if not has_permission(message.from_user.id, "web_search"):
            request_permission(
                message,
                "web_search",
                "Yêu cầu cần tra cứu Internet.",
                deferred_action={
                    "kind": "claude", "prompt": prompt, "chat_id": message.chat.id
                },
            )
            return
        handle_claude_prompt(message, prompt)

    @bot.message_handler(commands=["new"])
    def new_session_handler(message: telebot.types.Message) -> None:
        if not authorized(message) or message.from_user is None:
            return
        name = (message.text or "").partition(" ")[2].strip()
        if not name:
            reply(message, "Cách dùng: /new <tên>, ví dụ /new coding")
            return
        try:
            get_user_session(
                settings.agent_project_dir,
                message.from_user.id,
                force_new=True,
                session_name=name,
            )
            reply(
                message,
                f"Đã tạo session mới '{name}'. Engine hiện tại: {registry.get_engine(message.from_user.id).title()}.",
            )
        except ValueError as exc:
            reply(message, str(exc))

    @bot.message_handler(commands=["sessions"])
    def sessions_handler(message: telebot.types.Message) -> None:
        if not authorized(message) or message.from_user is None:
            return
        current, names = list_user_sessions(
            settings.agent_project_dir, message.from_user.id
        )
        if not names:
            reply(message, "Bạn chưa có session. Tạo bằng /new daily")
            return
        lines = ["Sessions của bạn:"]
        for name in names:
            lines.append(f"{'●' if name == current else '○'} {name}")
        reply(message, "\n".join(lines))

    @bot.message_handler(commands=["use"])
    def use_session_handler(message: telebot.types.Message) -> None:
        if not authorized(message) or message.from_user is None:
            return
        name = (message.text or "").partition(" ")[2].strip()
        if not name:
            reply(message, "Cách dùng: /use <tên>, ví dụ /use coding")
            return
        try:
            session = switch_user_session(
                settings.agent_project_dir, message.from_user.id, name
            )
            registry.audit(
                "session_switched",
                message.from_user.id,
                session=session.name,
            )
            reply(message, f"Đã chuyển sang session: {session.name}")
        except ValueError as exc:
            reply(message, str(exc))

    @bot.message_handler(func=lambda message: True, content_types=["text"])
    def natural_message_handler(message: telebot.types.Message) -> None:
        text = (message.text or "").strip()
        # Commands are handled above. Do not dispatch a command a second time as
        # normal text to the user's currently selected engine.
        if text and not text.startswith("/"):
            if message.from_user is not None and registry.get_engine(message.from_user.id) == "chatgpt":
                handle_chatgpt_prompt(message, text)
            else:
                handle_claude_prompt(message, text)

    @bot.message_handler(content_types=["photo"])
    def photo_handler(message: telebot.types.Message) -> None:
        """Keep a Telegram image inside its owner's active private workspace."""
        if not authorized(message) or message.from_user is None or not message.photo:
            return
        try:
            session = get_user_session(settings.agent_project_dir, message.from_user.id)
            uploads_dir = session.workspace / "uploads"
            uploads_dir.mkdir(mode=0o700, exist_ok=True)
            file_info = bot.get_file(message.photo[-1].file_id)
            image_bytes = bot.download_file(file_info.file_path)
            filename = f"telegram-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}.jpg"
            image_path = uploads_dir / filename
            image_path.write_bytes(image_bytes)
            image_path.chmod(0o600)
            relative_path = f"uploads/{filename}"
            append_session_transcript(
                session,
                "user",
                f"[User uploaded an image available at {relative_path}]",
                settings.codex_model,
                settings.codex_effort,
                engine="telegram",
            )
            registry.audit(
                "image_received",
                message.from_user.id,
                session=session.workspace.name,
                file_name=filename,
            )
        except Exception as exc:
            logging.warning("Could not save Telegram image: %s", type(exc).__name__)
            reply(message, "Friday chưa lưu được ảnh này. Bạn thử gửi lại giúp mình nhé.")
            return

        caption = (message.caption or "").strip()
        if not caption:
            reply(message, "Đã nhận ảnh. Bạn muốn mình xem hay chỉnh gì với ảnh này?")
            return
        prompt = f"Người dùng vừa gửi ảnh `{relative_path}`. Yêu cầu của họ: {caption}"
        if registry.get_engine(message.from_user.id) == "chatgpt":
            handle_chatgpt_prompt(message, prompt)
        else:
            handle_claude_prompt(message, prompt)

    return bot


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        settings = load_settings()
    except ValueError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    settings.bot_data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        settings.bot_data_dir / "friday.log",
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    logging.getLogger().addHandler(file_handler)
    logging.info("Starting Telegram local agent")
    # A transient TLS/network reset must not take Friday offline.  Keep retries
    # narrow: programming errors are still allowed to surface normally.
    while True:
        try:
            create_bot(settings).infinity_polling(skip_pending=True, timeout=30)
        except RequestException as exc:
            logging.warning(
                "Telegram connection interrupted (%s); retrying in 5 seconds.", exc
            )
            time.sleep(5)


if __name__ == "__main__":
    main()
