"""Small Graph API client used by the Facebook Page adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class FacebookApiError(RuntimeError):
    pass


def verify_signature(app_secret: str, body: bytes, signature: str | None) -> bool:
    if not app_secret or not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        app_secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


class FacebookGraphClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.token = str(config.get("page_access_token", ""))
        self.version = str(config.get("graph_version", "v24.0"))
        self.base_url = f"https://graph.facebook.com/{self.version}"

    def _request(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.token:
            raise FacebookApiError("Facebook Page access token chưa được cấu hình.")
        url = f"{self.base_url}/{path.lstrip('/')}"
        data = None
        headers = {"Accept": "application/json"}
        if payload is None:
            url += ("&" if "?" in url else "?") + urlencode({"access_token": self.token})
        else:
            body = {**payload, "access_token": self.token}
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            with urlopen(Request(url, data=data, headers=headers), timeout=20) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise FacebookApiError(f"Graph API trả về HTTP {exc.code}: {detail}") from exc
        except (URLError, OSError, json.JSONDecodeError) as exc:
            raise FacebookApiError("Không thể kết nối Graph API.") from exc
        if not isinstance(decoded, dict):
            raise FacebookApiError("Graph API trả về dữ liệu không hợp lệ.")
        if decoded.get("error"):
            raise FacebookApiError(str(decoded["error"]))
        return decoded

    def page_identity(self) -> dict[str, Any]:
        return self._request("me?fields=id,name")

    def typing_on(self, recipient_id: str) -> None:
        self._request("me/messages", {"recipient": {"id": recipient_id}, "sender_action": "typing_on"})

    def send_text(self, recipient_id: str, text: str) -> None:
        self._request("me/messages", {"recipient": {"id": recipient_id}, "message": {"text": text}})
