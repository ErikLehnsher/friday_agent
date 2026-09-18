"""Private, local configuration for the Facebook Page transport."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


_VERSION_RE = re.compile(r"^v\d+(?:\.\d+)?$")


class FacebookPageConfigStore:
    """Stores Page credentials outside git and never exposes them in reads."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "facebook_page.json"
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def configured(self) -> bool:
        value = self._load()
        return bool(
            value.get("enabled")
            and value.get("page_access_token")
            and value.get("app_secret")
            and value.get("verify_token")
        )

    def private(self) -> dict[str, Any]:
        """For the local webhook process only; never return this from an HTTP API."""
        return self._load()

    def public(self) -> dict[str, Any]:
        value = self._load()
        return {
            "enabled": bool(value.get("enabled")),
            "configured": self.configured(),
            "page_id": value.get("page_id", ""),
            "page_name": value.get("page_name", ""),
            "app_id": value.get("app_id", ""),
            "graph_version": value.get("graph_version", "v24.0"),
            "webhook_url": value.get(
                "webhook_url", "https://snakersdoo.io.vn/facebook/webhook"
            ),
            "has_app_secret": bool(value.get("app_secret")),
            "has_verify_token": bool(value.get("verify_token")),
            "has_page_access_token": bool(value.get("page_access_token")),
        }

    def save(self, incoming: dict[str, Any]) -> dict[str, Any]:
        existing = self._load()
        page_id = str(incoming.get("page_id", existing.get("page_id", ""))).strip()
        page_name = str(incoming.get("page_name", existing.get("page_name", ""))).strip()
        app_id = str(incoming.get("app_id", existing.get("app_id", ""))).strip()
        version = str(incoming.get("graph_version", existing.get("graph_version", "v24.0"))).strip()
        webhook_url = str(
            incoming.get(
                "webhook_url",
                existing.get("webhook_url", "https://snakersdoo.io.vn/facebook/webhook"),
            )
        ).strip()
        if page_id and not page_id.isdigit():
            raise ValueError("Facebook Page ID chỉ được chứa chữ số.")
        if not _VERSION_RE.fullmatch(version):
            raise ValueError("Graph API version phải có dạng v24.0.")
        parsed_url = urlparse(webhook_url)
        if parsed_url.scheme != "https" or not parsed_url.netloc or parsed_url.username or parsed_url.password:
            raise ValueError("Webhook URL phải là HTTPS public hợp lệ.")
        value: dict[str, Any] = {
            "enabled": bool(incoming.get("enabled", existing.get("enabled", False))),
            "page_id": page_id,
            "page_name": page_name,
            "app_id": app_id,
            "graph_version": version,
            "webhook_url": webhook_url,
        }
        for secret in ("app_secret", "verify_token", "page_access_token"):
            supplied = incoming.get(secret)
            # Blank password inputs mean "preserve the currently saved secret".
            value[secret] = str(supplied).strip() if supplied else existing.get(secret, "")
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return self.public()
