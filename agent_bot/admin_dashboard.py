"""Local-only monitoring dashboard for Friday administrators."""

from __future__ import annotations

import base64
import hmac
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .facebook_api import FacebookApiError, FacebookGraphClient
from .facebook_config import FacebookPageConfigStore


def _read_json(path: Path, fallback: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def _read_events(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def snapshot(data_dir: Path, engine_settings: dict[str, dict[str, str]] | None = None) -> dict:
    engine_settings = engine_settings or {
        "claude": {"model": "sonnet", "effort": "medium"},
        "codex": {"model": "gpt-5.6-luna", "effort": "medium"},
    }
    users = _read_json(data_dir / "users.json", {})
    users = users if isinstance(users, dict) else {}
    events = _read_events(data_dir / "audit.jsonl")
    # Legacy model rows predate the structured run schema and cannot truthfully
    # show model, effort or duration. Keep all other operator activity visible.
    dashboard_events = [
        item
        for item in events
        if item.get("event")
        not in {"claude_request", "claude_response", "codex_response"}
    ]
    completed = [item for item in events if item.get("event") == "engine_run_completed"]
    failed = [item for item in events if item.get("event") == "engine_run_failed"]
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=5)
    observed_runtime: dict[str, int] = defaultdict(int)
    for item in completed:
        timestamp = _parse_time(item.get("timestamp"))
        if timestamp and timestamp >= window_start:
            observed_runtime[str(item.get("engine", "unknown"))] += int(item.get("duration_ms", 0) or 0)

    user_rows = []
    for key, user in users.items():
        if not isinstance(user, dict):
            continue
        user_id = int(user.get("user_id", key))
        user_events = [event for event in completed if event.get("user_id") == user_id]
        user_failures = [event for event in failed if event.get("user_id") == user_id]
        engines = Counter(str(event.get("engine", "unknown")) for event in user_events)
        stored_engine = str(user.get("engine", "claude"))
        current_engine = "codex" if stored_engine == "chatgpt" else stored_engine
        current_settings = engine_settings.get(current_engine, {})
        user_rows.append(
            {
                "user_id": user_id,
                "name": user.get("display_name") or user.get("username") or str(user_id),
                "role": user.get("role", "user"),
                "status": user.get("status", "unknown"),
                "engine": current_engine,
                "model": current_settings.get("model", "—"),
                "effort": current_settings.get("effort", "—"),
                "permissions": user.get("permissions", []),
                "last_seen_at": user.get("last_seen_at"),
                "runs": len(user_events),
                "failures": len(user_failures),
                "engines": dict(engines),
            }
        )
    user_rows.sort(key=lambda item: (item["role"] != "admin", item["name"].lower()))
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "summary": {
            "users": len(user_rows),
            "active_users": sum(item["status"] == "active" for item in user_rows),
            "completed_runs": len(completed),
            "failed_runs": len(failed),
            "by_engine": dict(Counter(str(item.get("engine", "unknown")) for item in completed)),
            "observed_runtime_5h_ms": dict(observed_runtime),
        },
        "users": user_rows,
        "events": list(reversed(dashboard_events[-500:])),
    }


PAGE = """<!doctype html><html><head><meta charset='utf-8'><title>Friday Admin</title>
<style>body{font:14px -apple-system,BlinkMacSystemFont,sans-serif;background:#10131b;color:#eef2ff;margin:0;padding:28px}h1{margin-top:0}.muted{color:#a6b0c8}.cards{display:flex;gap:12px;flex-wrap:wrap}.card{background:#1a2030;border:1px solid #303a54;border-radius:12px;padding:14px;min-width:150px}.n{font-size:25px;font-weight:700}table{width:100%;border-collapse:collapse;background:#161c29;margin-top:14px}th,td{padding:9px;border-bottom:1px solid #303a54;text-align:left;vertical-align:top}input,select{background:#161c29;color:#fff;border:1px solid #4a5878;border-radius:7px;padding:7px;margin-right:8px}.ok{color:#72e6a5}.bad{color:#ff9090}code{color:#b9ceff}</style></head><body>
<h1>Friday Admin <span class='muted'>local-only</span></h1><p class='muted'>Provider quota is intentionally not estimated. “5h observed runtime” is bot execution time, not Claude/Codex account credit remaining.</p>
<div id='cards' class='cards'></div><h2>Facebook Page</h2><p class='muted'>Credentials are stored only in the private runtime data volume. Existing secret values are never rendered here.</p><form id='facebook'><label><input type='checkbox' name='enabled'> Enable Facebook Page agent</label><p><input name='page_name' placeholder='Page name'><input name='page_id' placeholder='Facebook Page ID'><input name='app_id' placeholder='Meta App ID'><input name='graph_version' placeholder='Graph API version (v24.0)'></p><p><input name='app_secret' type='password' placeholder='App secret (leave blank to keep)'><input name='verify_token' type='password' placeholder='Webhook verify token (leave blank to keep)'><input name='page_access_token' type='password' placeholder='Page access token (leave blank to keep)'></p><button>Save Facebook configuration</button> <button type='button' id='test-facebook'>Test Graph API</button> <span id='facebook-status' class='muted'></span></form><h2>Users</h2><div><input id='user' placeholder='Filter user'><select id='engine'><option value=''>All engines</option><option value='claude'>Claude</option><option value='codex'>Codex</option></select></div><table><thead><tr><th>User</th><th>Status</th><th>Current engine</th><th>Runs</th><th>Failed</th><th>Permissions</th><th>Last seen</th></tr></thead><tbody id='users'></tbody></table><h2>Activity</h2><table><thead><tr><th>Time</th><th>User</th><th>Event</th><th>Engine / model</th><th>Session</th><th>Duration</th></tr></thead><tbody id='events'></tbody></table>
<script>let data;const ms=x=>x?`${(x/60000).toFixed(1)} min`:'0 min',label=x=>x==='chatgpt'?'Codex':x,time=x=>x?new Intl.DateTimeFormat('vi-VN',{timeZone:'Asia/Ho_Chi_Minh',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(x)):'—';const note=t=>document.querySelector('#facebook-status').textContent=t;async function loadFacebook(){let c=await fetch('/api/facebook/config').then(r=>r.json());let f=document.querySelector('#facebook');for(let k of ['page_name','page_id','app_id','graph_version'])f.elements[k].value=c[k]||'';f.elements.enabled.checked=!!c.enabled;note(c.configured?'Configured (secrets hidden)':'Not fully configured')}async function load(){data=await fetch('/api/dashboard').then(r=>r.json());render()}function render(){let s=data.summary;document.querySelector('#cards').innerHTML=`<div class=card><div class=n>${s.active_users}/${s.users}</div>Active users</div><div class=card><div class=n>${s.completed_runs}</div>Completed runs</div><div class=card><div class=n>${s.failed_runs}</div>Failed runs</div><div class=card><div class=n>${ms(s.observed_runtime_5h_ms.claude)}</div>Claude: 5h observed</div><div class=card><div class=n>${ms(s.observed_runtime_5h_ms.codex)}</div>Codex: 5h observed</div>`;let q=document.querySelector('#user').value.toLowerCase(),e=document.querySelector('#engine').value;document.querySelector('#users').innerHTML=data.users.filter(x=>!q||`${x.name} ${x.user_id}`.toLowerCase().includes(q)).filter(x=>!e||x.engine===e||x.engines[e]).map(x=>`<tr><td><b>${x.name}</b><br><code>${x.user_id}</code> · ${x.role}</td><td class='${x.status==='active'?'ok':'bad'}'>${x.status}</td><td><b>${label(x.engine)}</b><br>${x.model} · ${x.effort}</td><td>${JSON.stringify(x.engines)}</td><td>${x.failures}</td><td>${x.permissions.join(', ')||'—'}</td><td>${time(x.last_seen_at)}</td></tr>`).join('');document.querySelector('#events').innerHTML=data.events.filter(x=>!e||x.engine===e).map(x=>`<tr><td>${time(x.timestamp)}</td><td>${x.user_id||'—'}</td><td>${x.event||'—'}</td><td>${label(x.engine||'—')} ${x.model||''}<br>${x.effort||''}</td><td>${x.session||'—'}</td><td>${x.duration_ms?ms(x.duration_ms):'—'}</td></tr>`).join('')}document.querySelector('#user').oninput=render;document.querySelector('#engine').onchange=render;document.querySelector('#facebook').onsubmit=async e=>{e.preventDefault();let f=e.currentTarget,p=Object.fromEntries(new FormData(f));p.enabled=f.elements.enabled.checked;let r=await fetch('/api/facebook/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});let x=await r.json();note(x.error||'Saved. The running Facebook adapter reads this configuration immediately.');f.elements.app_secret.value=f.elements.verify_token.value=f.elements.page_access_token.value=''};document.querySelector('#test-facebook').onclick=async()=>{note('Testing…');let r=await fetch('/api/facebook/test',{method:'POST'}),x=await r.json();note(x.error||`Connected: ${x.name||x.id||'OK'}`)};load();loadFacebook();setInterval(load,10000);</script></body></html>"""


def serve(
    data_dir: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    engine_settings: dict[str, dict[str, str]] | None = None,
    auth_user: str | None = None,
    auth_password: str | None = None,
) -> None:
    # When this dashboard is reachable beyond the host (e.g. via a tunnel),
    # auth_user/auth_password gate every request with HTTP Basic Auth.
    # Unset (the local-only default) leaves it open, matching prior behavior.
    require_auth = bool(auth_user and auth_password)
    facebook_config = FacebookPageConfigStore(data_dir)

    class Handler(BaseHTTPRequestHandler):
        def _authorized(self) -> bool:
            if not require_auth:
                return True
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return False
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return False
            user, _, password = decoded.partition(":")
            return hmac.compare_digest(user, auth_user or "") and hmac.compare_digest(
                password, auth_password or ""
            )

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                body = b"Unauthorized"
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Friday Admin"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            path = urlparse(self.path).path
            if path == "/api/dashboard":
                body = json.dumps(
                    snapshot(data_dir, engine_settings), ensure_ascii=False
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
            elif path == "/api/facebook/config":
                body = json.dumps(facebook_config.public(), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
            elif path == "/":
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            else:
                self.send_response(404)
                body = b"Not found"
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                self.send_response(401)
                self.end_headers()
                return
            path = urlparse(self.path).path
            try:
                if path == "/api/facebook/config":
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("Payload không hợp lệ.")
                    body = json.dumps(facebook_config.save(payload), ensure_ascii=False).encode("utf-8")
                elif path == "/api/facebook/test":
                    body = json.dumps(FacebookGraphClient(facebook_config.private()).page_identity(), ensure_ascii=False).encode("utf-8")
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
            except (ValueError, FacebookApiError, json.JSONDecodeError) as exc:
                body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                self.send_response(400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    ThreadingHTTPServer((host, port), Handler).serve_forever()
