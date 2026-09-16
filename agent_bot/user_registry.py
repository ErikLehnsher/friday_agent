"""Local user registry and metadata-only audit log."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class UserRegistry:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.users_path = data_dir / "users.json"
        self.audit_path = data_dir / "audit.jsonl"
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.users_path.is_file():
            return {}
        try:
            payload = json.loads(self.users_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, users: dict[str, dict[str, Any]]) -> None:
        temporary = self.users_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.chmod(0o600)
        temporary.replace(self.users_path)

    def sync_configured_users(
        self, allowed_user_ids: set[int], admin_user_ids: set[int]
    ) -> None:
        users = self._load()
        changed = False
        for user_id in allowed_user_ids:
            key = str(user_id)
            if key not in users:
                users[key] = {
                    "user_id": user_id,
                    "username": None,
                    "display_name": None,
                    "role": "admin" if user_id in admin_user_ids else "user",
                    "joined_at": _now(),
                    "last_seen_at": None,
                    "status": "active",
                    "permissions": [],
                    "engine": "claude",
                    "active_chatgpt_session": None,
                }
                changed = True
            elif user_id in admin_user_ids and users[key].get("role") != "admin":
                users[key]["role"] = "admin"
                changed = True
        if changed:
            self._save(users)

    def record_user(
        self, telegram_user: Any, role: str = "user", invited_by: int | None = None
    ) -> None:
        users = self._load()
        key = str(telegram_user.id)
        existing = users.get(key, {})
        first_name = getattr(telegram_user, "first_name", None) or ""
        last_name = getattr(telegram_user, "last_name", None) or ""
        users[key] = {
            **existing,
            "user_id": telegram_user.id,
            "username": getattr(telegram_user, "username", None),
            "display_name": f"{first_name} {last_name}".strip() or None,
            "role": role,
            "joined_at": existing.get("joined_at") or _now(),
            "last_seen_at": _now(),
            "status": "active",
            "invited_by": invited_by,
            "permissions": existing.get("permissions", []),
            "engine": existing.get("engine", "claude"),
            "active_chatgpt_session": existing.get("active_chatgpt_session"),
        }
        self._save(users)

    def get_permissions(self, user_id: int) -> set[str]:
        user = self._load().get(str(user_id), {})
        return set(user.get("permissions", []))

    def get_engine(self, user_id: int) -> str:
        engine = self._load().get(str(user_id), {}).get("engine", "claude")
        return engine if engine in {"claude", "chatgpt"} else "claude"

    def set_engine(self, user_id: int, engine: str) -> None:
        if engine not in {"claude", "chatgpt"}:
            raise ValueError("Engine không hợp lệ.")
        users = self._load()
        key = str(user_id)
        if key not in users:
            raise ValueError("User không tồn tại trong registry.")
        users[key]["engine"] = engine
        self._save(users)

    def get_active_chatgpt_session(self, user_id: int) -> str | None:
        value = self._load().get(str(user_id), {}).get("active_chatgpt_session")
        return value if isinstance(value, str) else None

    def set_active_chatgpt_session(self, user_id: int, session_key: str) -> None:
        users = self._load()
        key = str(user_id)
        if key not in users:
            raise ValueError("User không tồn tại trong registry.")
        users[key]["active_chatgpt_session"] = session_key
        self._save(users)

    def is_active(self, user_id: int) -> bool:
        user = self._load().get(str(user_id))
        return bool(user and user.get("status") == "active")

    def set_status(self, user_id: int, status: str) -> None:
        if status not in {"active", "blocked"}:
            raise ValueError("Trạng thái user không hợp lệ.")
        users = self._load()
        key = str(user_id)
        if key not in users:
            raise ValueError("User không tồn tại trong registry.")
        users[key]["status"] = status
        self._save(users)

    def grant_permission(self, user_id: int, permission: str) -> None:
        users = self._load()
        key = str(user_id)
        if key not in users:
            raise ValueError("User không tồn tại trong registry.")
        permissions = set(users[key].get("permissions", []))
        permissions.add(permission)
        users[key]["permissions"] = sorted(permissions)
        self._save(users)

    def revoke_permission(self, user_id: int, permission: str) -> None:
        users = self._load()
        key = str(user_id)
        if key not in users:
            raise ValueError("User không tồn tại trong registry.")
        permissions = set(users[key].get("permissions", []))
        permissions.discard(permission)
        users[key]["permissions"] = sorted(permissions)
        self._save(users)

    def touch(self, telegram_user: Any) -> None:
        users = self._load()
        key = str(telegram_user.id)
        if key not in users:
            return
        users[key]["username"] = getattr(telegram_user, "username", None)
        first_name = getattr(telegram_user, "first_name", None) or ""
        last_name = getattr(telegram_user, "last_name", None) or ""
        users[key]["display_name"] = f"{first_name} {last_name}".strip() or None
        users[key]["last_seen_at"] = _now()
        self._save(users)

    def list_users(self, include_inactive: bool = True) -> list[dict[str, Any]]:
        users = self._load()
        values = users.values()
        if not include_inactive:
            values = (item for item in values if item.get("status") == "active")
        return sorted(
            values,
            key=lambda item: (item.get("role") != "admin", item.get("joined_at", "")),
        )

    def audit(self, event: str, user_id: int, **metadata: Any) -> None:
        entry = {
            "timestamp": _now(),
            "event": event,
            "user_id": user_id,
            **metadata,
        }
        with self.audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.audit_path.chmod(0o600)
