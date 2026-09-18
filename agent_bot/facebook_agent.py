"""Facebook Messenger transport for Friday, isolated from the Telegram adapter."""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .claude_runner import run_claude
from .config import Settings
from .facebook_api import FacebookApiError, FacebookGraphClient, verify_signature
from .facebook_config import FacebookPageConfigStore
from .user_registry import UserRegistry

LOG = logging.getLogger(__name__)
MAX_MESSAGE_CHARS = 1800

FACEBOOK_PROMPT = """You are Friday replying through a Facebook Page inbox. Be warm,
brief, capable, and helpful. Do not mention internal systems, credentials, host paths,
or tool configuration. This is a public messaging channel: do not undertake privileged
actions, run shell commands, or browse the web. If the task needs a file or private
system action, explain what information or approved channel is needed."""


def _chunks(text: str) -> list[str]:
    text = text.strip() or "Mình chưa có phản hồi hoàn chỉnh. Bạn thử nhắn lại giúp mình nhé."
    return [text[index:index + MAX_MESSAGE_CHARS] for index in range(0, len(text), MAX_MESSAGE_CHARS)]


def _message_events(payload: dict[str, Any]) -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []
    for entry in payload.get("entry", []):
        if not isinstance(entry, dict):
            continue
        for item in entry.get("messaging", []):
            if not isinstance(item, dict) or item.get("message", {}).get("is_echo"):
                continue
            sender = item.get("sender", {}).get("id")
            message = item.get("message", {})
            text = message.get("text") if isinstance(message, dict) else None
            mid = message.get("mid", "") if isinstance(message, dict) else ""
            if isinstance(sender, str) and isinstance(text, str) and text.strip():
                results.append((sender, text.strip(), str(mid)))
    return results


class FacebookAgentServer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.config_store = FacebookPageConfigStore(settings.bot_data_dir)
        self.registry = UserRegistry(settings.bot_data_dir)
        self._seen: set[str] = set()
        self._seen_lock = threading.Lock()

    def _already_seen(self, message_id: str) -> bool:
        if not message_id:
            return False
        with self._seen_lock:
            if message_id in self._seen:
                return True
            self._seen.add(message_id)
            if len(self._seen) > 1000:
                self._seen.clear()
            return False

    def process_message(self, sender_id: str, text: str, message_id: str) -> None:
        config = self.config_store.private()
        identity = f"facebook-{config.get('page_id') or 'page'}-{sender_id}"
        self.registry.audit("facebook_message_received", identity, channel="facebook", page_id=config.get("page_id", ""))
        client = FacebookGraphClient(config)
        try:
            client.typing_on(sender_id)
            reply, session = run_claude(
                self.settings.claude_bin,
                self.settings.agent_project_dir,
                identity,
                FACEBOOK_PROMPT + "\n\nCustomer message:\n" + text,
                model=self.settings.claude_model,
                effort=self.settings.claude_effort,
                agent_mode="test",
                max_turns=self.settings.claude_max_turns,
                timeout_seconds=self.settings.claude_timeout_seconds,
                store_raw_transcripts=self.settings.store_raw_transcripts,
                web_access="none",
                allow_shell=False,
                granted_permissions=(),
            )
            for part in _chunks(reply):
                client.send_text(sender_id, part)
            self.registry.audit("facebook_agent_replied", identity, channel="facebook", session=session.session_id, message_id=message_id)
        except (FacebookApiError, RuntimeError, OSError) as exc:
            LOG.warning("Facebook agent invocation failed: %s", exc)
            self.registry.audit("facebook_agent_failed", identity, channel="facebook", reason=type(exc).__name__)
            try:
                client.send_text(sender_id, "Friday đang gặp sự cố tạm thời. Bạn thử lại sau ít phút nhé.")
            except FacebookApiError:
                pass

    def serve(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status: int, body: bytes, content_type: str = "text/plain; charset=utf-8") -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                url = urlparse(self.path)
                if url.path == "/health":
                    self._send(200, b"ok")
                    return
                if url.path != "/facebook/webhook":
                    self._send(404, b"Not found")
                    return
                query = parse_qs(url.query)
                config = server.config_store.private()
                if query.get("hub.mode", [""])[0] == "subscribe" and query.get("hub.verify_token", [""])[0] == config.get("verify_token"):
                    self._send(200, query.get("hub.challenge", [""])[0].encode("utf-8"))
                    return
                self._send(403, b"Verification failed")

            def do_POST(self) -> None:  # noqa: N802
                if urlparse(self.path).path != "/facebook/webhook":
                    self._send(404, b"Not found")
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 1_000_000:
                    self._send(400, b"Invalid body")
                    return
                body = self.rfile.read(length)
                config = server.config_store.private()
                if not server.config_store.configured() or not verify_signature(config.get("app_secret", ""), body, self.headers.get("X-Hub-Signature-256")):
                    self._send(403, b"Invalid signature")
                    return
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._send(400, b"Invalid JSON")
                    return
                if not isinstance(payload, dict) or payload.get("object") != "page":
                    self._send(400, b"Unsupported webhook")
                    return
                self._send(200, b"EVENT_RECEIVED")
                for sender, text, message_id in _message_events(payload):
                    if not server._already_seen(message_id):
                        threading.Thread(target=server.process_message, args=(sender, text, message_id), daemon=True).start()

            def log_message(self, _format: str, *_args: object) -> None:
                return

        ThreadingHTTPServer((self.settings.facebook_host, self.settings.facebook_port), Handler).serve_forever()
