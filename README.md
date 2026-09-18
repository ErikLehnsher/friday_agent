# Friday Agent

Friday Agent is a **local-first personal agent platform**. It connects people to
approved AI engines and local capabilities while keeping execution, audit data,
and private workspaces on a trusted host machine.

The included Telegram bot is an early transport adapter for testing the platform
from a phone. It is not the product boundary: the same core is intended to later
support native macOS, desktop, web, voice, and other messaging interfaces.

## What exists today

- A Telegram adapter for a private, allowlisted group of users.
- Per-user, named workspaces and local transcript storage.
- Claude Code CLI and Codex CLI engines, selectable per user/session.
- An admin approval flow for privileged capabilities.
- A local-only admin dashboard for users, permissions, engine activity, sessions,
  runtime, and audit events.
- Local macOS actions for Chrome, Mail, and opening Claude, behind approval.
- Raw session records designed to become a future retrieval/vector-memory input.

## Platform shape

```text
Telegram (current test adapter)        Future adapters
             |                         macOS / Web / Voice / API
             +-------------+-------------------+
                           |
                    Friday Agent Core
     identity · sessions · policy · audit · routing · artifacts
                           |
          +----------------+----------------+
          |                                 |
 Claude Code CLI                       Codex CLI
          |                                 |
   managed user workspace      managed user workspace
          |
 local artifacts, transcripts, uploads, future memory index
```

Telegram is only responsible for transport: receiving a message and returning a
reply. Identity, user isolation, capability policy, session continuity, and audit
belong to the core so future adapters do not need to reimplement them.

## Security model

- The host machine is trusted; unapproved remote users are not.
- Every user has isolated session workspaces under one configured managed root.
- In test mode, users cannot discover real host paths or access sibling/parent
  workspaces.
- Admins are configured explicitly through `TELEGRAM_ADMIN_USER_IDS`; they are
  trusted within the managed root, not across the rest of the Mac.
- Privileged capabilities are granted per user and can be approved, denied,
  revoked, or blocked by an admin.
- Secrets stay in `.env`; runtime data stays outside the repository.
- The dashboard binds to `127.0.0.1` only.

This is a practical isolation layer for a personal trusted host, not a hardened
multi-tenant sandbox. Do not use it as a boundary for hostile code or secrets.

## Engines

### Claude

Claude Code CLI executes inside the active user workspace. Friday keeps the durable
conversation record locally in `transcript.jsonl`; it does not depend on a remote
Claude resume ID surviving a host restart.

### Codex

`/chatgpt` is retained as a familiar Telegram alias, but it executes the local
Codex CLI with a signed-in ChatGPT account. The default configuration is
`gpt-5.6-luna` at `medium` reasoning effort.

The current engine is selected per user. An explicit `/claude` or `/chatgpt`
selects the engine for later normal messages in the same user session.

## Sessions and artifacts

Each user can maintain multiple named sessions:

```text
managed-sessions/
  user-<telegram-id>/
    sessions.json
    session-daily-.../
      session.json
      transcript.jsonl
      uploads/
      artifacts/
    session-coding-.../
      ...
```

Use `/new <name>`, `/sessions`, and `/use <name>` in Telegram. Future adapters
should use the same session service rather than inventing their own storage.

## Capabilities and approval

Friday can request capability approval when a task genuinely needs it:

| Capability | Purpose |
| --- | --- |
| `web_search` / `web_fetch` | Public internet research |
| `shell` | Commands inside the user workspace |
| `chrome` | Open the isolated Chrome profile |
| `mail` | Read the configured host mailbox |
| `claude_app` | Open the Claude desktop application |
| `chatgpt_desktop` | Compatibility permission name for the Codex CLI engine |

For non-admin users, Friday sends the request to the configured admin account with
Approve/Deny controls and resumes the exact deferred task after approval. Admins
already have these capabilities within the managed root and should not need to
approve their own shell/file requests.

## Quick start: Telegram adapter

Requirements:

- macOS or another trusted local host.
- Python 3.9+.
- A Telegram bot token from BotFather.
- Claude Code CLI and/or Codex CLI, authenticated on the host.

```bash
git clone https://github.com/ErikLehnsher/friday_agent.git
cd friday_agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Create the managed runtime directories outside the cloned repository:

```bash
mkdir -p "$HOME/FridayAgentData/sessions" "$HOME/FridayAgentData/control"
```

Then fill `.env` with your own bot token, user IDs, and absolute local paths. Never
commit this file.

```dotenv
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USER_IDS=
TELEGRAM_ADMIN_USER_IDS=
AGENT_PROJECT_DIR=/absolute/path/to/FridayAgentData/sessions
BOT_DATA_DIR=/absolute/path/to/FridayAgentData/control

CLAUDE_BIN=claude
CLAUDE_MODEL=sonnet
CLAUDE_EFFORT=medium

CODEX_BIN=codex
CODEX_MODEL=gpt-5.6-luna
CODEX_EFFORT=medium
CODEX_ENABLED=true
CODEX_TIMEOUT_SECONDS=300

AGENT_MODE=test
STORE_RAW_TRANSCRIPTS=true
WEB_ACCESS=search
```

Run the adapter:

```bash
source .venv/bin/activate
python bot.py
```

Run the local dashboard in a separate terminal:

```bash
source .venv/bin/activate
python admin.py
```

Open `http://127.0.0.1:8765` on the host. The dashboard's observed runtime is bot
execution time, not a provider account quota estimate.

## Telegram commands

| Command | Function |
| --- | --- |
| `/start`, `/help` | Start and command help |
| `/pair <code>` | One-time first pairing |
| `/invite` | Admin creates an invite code |
| `/users`, `/requests` | Admin user and pending-request controls |
| `/permissions` | See granted capabilities |
| `/new`, `/sessions`, `/use` | Manage named sessions |
| `/claude <prompt>` | Use Claude and make it the default engine |
| `/chatgpt <prompt>` | Use local Codex CLI and make it the default engine |
| `/engine` | View or switch the default engine |
| `/research <prompt>` | Request research with the required approval flow |

Users can also write naturally. Friday routes normal messages through their current
engine. Do not send passwords, API keys, or other sensitive data through a chat
adapter.

## Facebook Page adapter

Facebook Messenger is a separate transport adapter. It uses the same Friday core,
but gives each sender a separate identity and workspace under the managed session
root. A Facebook customer is never mapped onto a Telegram user or administrator.

The Page credentials are entered through the local Friday Admin dashboard, not in
`.env` and never in git:

1. Start the dashboard and open `http://127.0.0.1:8765` locally. Set
   `ADMIN_USER` and `ADMIN_PASSWORD` before making it reachable through a tunnel.
2. In **Facebook Page**, enter the Page ID, Meta App ID, Graph API version, App
   Secret, webhook Verify Token, and long-lived Page Access Token. Secret fields
   are write-only: leaving one blank preserves the saved value.
3. Click **Test Graph API** to verify that the Page token can read the Page
   identity. This is the only dashboard action that calls Meta.
4. Start the `friday-facebook` service once. It reads saved credential changes on
   each incoming webhook, so restarting is not needed just to change credentials.
5. Put a reverse proxy in front of `friday-facebook` and expose only
   `https://your-domain.example/facebook/webhook`. Do not publish port 8781 to the
   host directly. Configure that callback URL and the same Verify Token in the
   Meta app webhook settings, then subscribe the Page to incoming messages.

The adapter validates `X-Hub-Signature-256` before accepting a POST. It sends a
typing indicator while Friday works, splits long replies into Messenger-sized
messages, and records metadata-only Facebook events in the common audit log. It
does not grant web, shell, browser, or host-file access to Page users. Meta's app
review, privacy-policy, and data-deletion requirements remain the Page owner's
responsibility and should be completed before a public launch.

## Development notes

The repository deliberately excludes:

- `.env`, tokens, and local credentials.
- Python environments and editor state.
- User workspaces, transcripts, uploads, artifacts, Chrome profiles, audit files,
  and logs.

Before modifying a runner or security policy, review the boundary between a user
workspace and its parent managed root. Avoid a design that exposes arbitrary host
paths to test users.

## Roadmap

- [ ] Extract a transport-neutral core package and adapter interface.
- [ ] Add a native macOS control surface.
- [ ] Add a web/API adapter with the same policy and session services.
- [ ] Add first-class artifact delivery for documents, spreadsheets, and media.
- [ ] Add image/vision generation and editing behind explicit policy controls.
- [ ] Build retrieval/vector memory from opted-in local transcript data.
- [ ] Add durable process supervision and operational health checks.

## Current limitations

- The Telegram adapter uses long polling and runs only while the host process is
  alive.
- The host must have the chosen CLI installed and authenticated.
- Provider quotas are not inferred by Friday.
- Capability approvals are a policy layer, not a substitute for OS-level sandboxing.
- Image receipt is stored per session, but full image generation/editing needs a
  dedicated image provider and is not enabled by default.
