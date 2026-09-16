"""Persistent admin approval requests for privileged bot capabilities."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


PERMISSIONS = {
    "web_search": "Tìm kiếm thông tin công khai trên web",
    "web_fetch": "Đọc trực tiếp nội dung trang web công khai",
    "chrome": "Mở Chrome profile riêng của user",
    "shell": "Chạy công cụ terminal bên trong workspace riêng",
    "mail": "Đọc danh sách email chưa đọc trên máy host",
    "claude_app": "Mở ứng dụng Claude trên máy host",
    "chatgpt_desktop": "Dùng Codex CLI (tài khoản ChatGPT) trên máy host",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PermissionRequests:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "permission_requests.json"
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _load(self) -> dict[str, dict]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, requests: dict[str, dict]) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(requests, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def create(
        self,
        user_id: int,
        username: str | None,
        permission: str,
        reason: str,
        deferred_action: dict | None = None,
    ) -> dict:
        requests = self._load()
        for item in requests.values():
            if (
                item.get("status") == "pending"
                and item.get("user_id") == user_id
                and item.get("permission") == permission
            ):
                return {**item, "_created": False}
        request_id = uuid4().hex[:12]
        item = {
            "request_id": request_id,
            "user_id": user_id,
            "username": username,
            "permission": permission,
            "reason": reason[:500],
            "status": "pending",
            "created_at": _now(),
            "resolved_at": None,
            "resolved_by": None,
            "deferred_action": deferred_action,
        }
        requests[request_id] = item
        self._save(requests)
        return {**item, "_created": True}

    def resolve(self, request_id: str, approved: bool, admin_id: int) -> dict | None:
        requests = self._load()
        item = requests.get(request_id)
        if not item or item.get("status") != "pending":
            return None
        item["status"] = "approved" if approved else "denied"
        item["resolved_at"] = _now()
        item["resolved_by"] = admin_id
        self._save(requests)
        return item

    def pending(self) -> list[dict]:
        return [
            item
            for item in self._load().values()
            if item.get("status") == "pending"
        ]
