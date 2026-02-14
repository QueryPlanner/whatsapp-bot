# WhatsApp Auto-Reply Bot

## Project Overview

This is **Chirag's WhatsApp auto-reply bot** — an AI agent that automatically replies to incoming WhatsApp direct messages on Chirag's behalf. It uses the Google Agent Development Kit (ADK) with a WhatsApp MCP integration.

The bot replies **as Chirag** — casual, friendly, first-person — not as a generic assistant.

### How It Works (End-to-End Flow)

```
Someone sends a WhatsApp DM to Chirag
        ↓
Go WhatsApp Bridge (port 8080) receives the message
        ↓
Bridge POSTs webhook to Python server (port 8888) at /webhook/whatsapp
        ↓
auto_reply.py debounces (waits 5s for more messages)
        ↓
ADK agent is invoked with InMemoryRunner
        ↓
Agent calls send_message tool via MCP → Go bridge → WhatsApp
        ↓
Reply delivered to sender's WhatsApp
```

### Key Technologies
*   **Language:** Python 3.13+ (agent), Go (WhatsApp bridge)
*   **Framework:** Google ADK (`google-adk`) with LlmAgent
*   **Model Interface:** LiteLLM via OpenRouter
*   **Server:** FastAPI (Uvicorn)
*   **WhatsApp:** `whatsmeow` (Go library) + custom MCP server
*   **Database:** PostgreSQL via `asyncpg` (session storage)
*   **Observability:** OpenTelemetry + LoggingPlugin + custom callbacks

---

## Architecture

The system has **three components** that must all be running:

### 1. Go WhatsApp Bridge (`whatsapp-mcp/whatsapp-bridge/`)
- Connects to WhatsApp using `go.mau.fi/whatsmeow`
- Stores messages in a local SQLite database (`store/messages.db`)
- Exposes a REST API on `:8080` for sending messages and querying data
- **Webhook**: On incoming DMs, POSTs to `WEBHOOK_URL` (default: `http://localhost:8888/webhook/whatsapp`)
- Filters: only DMs (`@s.whatsapp.net`), skips groups (`@g.us`), skips self-sent (`is_from_me`)
- Key file: `main.go` — the `handleMessage` function triggers the webhook, `notifyWebhook` sends the POST

### 2. WhatsApp MCP Server (`whatsapp-mcp/whatsapp-mcp-server/`)
- Python FastMCP server that wraps the Go bridge REST API as MCP tools
- Runs via stdio (spawned by the ADK agent on demand, NOT run separately)
- Key files: `main.py` (tool definitions), `whatsapp.py` (HTTP calls to Go bridge + SQLite queries)
- Available MCP tools: `search_contacts`, `list_messages`, `list_chats`, `get_chat`, `get_direct_chat_by_contact`, `get_last_interaction`, `send_message`

### 3. Python ADK Agent Server (`src/whatsapp_bot/`)
- FastAPI server running on `:8888`
- Contains the ADK agent + auto-reply webhook handler
- The agent uses `McpToolset` with `StdioConnectionParams` to spawn and connect to the MCP server

---

## Code Structure

### `src/whatsapp_bot/` — Core Agent Code

| File | Purpose |
|------|---------|
| `agent.py` | Defines `root_agent` (LlmAgent) and `app` (ADK App). Configures the WhatsApp MCP toolset, model, callbacks, and plugins. **App name MUST be `whatsapp_bot`** (matches directory name). |
| `auto_reply.py` | Webhook handler at `/webhook/whatsapp`. Receives DM notifications from the Go bridge, debounces messages, manages per-contact sessions, and invokes the agent via `InMemoryRunner`. |
| `prompt.py` | Agent prompts. The agent replies **as Chirag** — casual, friendly, first-person. Never reveals it's an AI. |
| `server.py` | FastAPI server entry point. Loads env vars, configures ADK services, mounts the auto-reply router. |
| `callbacks.py` | `LoggingCallbacks` class (before/after agent, model, tool) and `add_session_to_memory` callback. |
| `tools.py` | Placeholder for custom tools (currently unused — all tools come from MCP). |
| `utils/` | Utility modules (env loading, etc). |

### `whatsapp-mcp/` — Vendored WhatsApp Integration

| Directory | Purpose |
|-----------|---------|
| `whatsapp-bridge/` | Go binary. Connects to WhatsApp, stores messages, REST API, webhook. Must be built with `go build -o whatsapp-bridge main.go`. |
| `whatsapp-mcp-server/` | Python MCP server. Spawned by ADK agent via stdio. Not run independently. |

### Other Important Files

| File | Purpose |
|------|---------|
| `.env` | Runtime configuration (API keys, ports, auto-reply settings) |
| `.env.example` | Template for environment variables |
| `pyproject.toml` | Python dependencies and project config |
| `Dockerfile` | Multi-stage production build |
| `compose.yaml` | Docker Compose config |

---

## Environment Variables

### Core
| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_NAME` | `local-agent` | Agent identifier |
| `ROOT_AGENT_MODEL` | `gemini-2.5-flash` | LLM model (use `openrouter/` prefix for OpenRouter) |
| `OPENROUTER_API_KEY` | — | OpenRouter API key |
| `DATABASE_URL` | — | PostgreSQL connection string |
| `HOST` | `0.0.0.0` | Server bind host |
| `PORT` | `8888` | Server port (**not 8080** — that's the Go bridge) |

### Auto-Reply
| Variable | Default | Description |
|----------|---------|-------------|
| `AUTO_REPLY_ENABLED` | `true` | Global on/off switch |
| `AUTO_REPLY_DEBOUNCE_SECONDS` | `5` | Wait time after last message before replying (batches rapid messages) |
| `AUTO_REPLY_COOLDOWN_SECONDS` | `0.5` | Minimum gap between replies to same contact |
| `AUTO_REPLY_IGNORE_JIDS` | — | Comma-separated JIDs to never reply to |
| `WEBHOOK_URL` | `http://localhost:8888/webhook/whatsapp` | Set in Go bridge env to change webhook target |

---

## Running the Bot

### Prerequisites
*   Python 3.13+ with [`uv`](https://github.com/astral-sh/uv)
*   Go 1.21+ (for building the WhatsApp bridge)
*   WhatsApp account linked (QR code scan on first run)

### Start Everything (2 terminals)

**Terminal 1 — Go WhatsApp Bridge:**
```bash
cd whatsapp-mcp/whatsapp-bridge
go build -o whatsapp-bridge main.go && ./whatsapp-bridge
```
Wait for: `✓ Connected to WhatsApp!` and `REST server is running.`

On first run, scan the QR code displayed in terminal with your WhatsApp app.

**Terminal 2 — ADK Agent Server:**
```bash
cd /Users/lordpatil/Projects/whatsapp-bot
uv run python -m whatsapp_bot.server
```
Wait for: `Uvicorn running on http://0.0.0.0:8888`

### Testing
- Have someone send you a WhatsApp DM — the bot auto-replies within ~10 seconds
- Manual webhook test:
  ```bash
  curl -s -X POST http://localhost:8888/webhook/whatsapp \
    -H "Content-Type: application/json" \
    -d '{"chat_jid": "918408878186@s.whatsapp.net", "sender": "918408878186", "sender_name": "Suraj Gavali", "content": "Hey!", "timestamp": "2026-02-14T17:00:00+05:30"}'
  ```

### Docker
```bash
docker compose up --build -d
```

---

## Auto-Reply System Details (`auto_reply.py`)

### Message Flow
1. Go bridge receives incoming DM → POSTs to `/webhook/whatsapp`
2. Guard clauses filter out: groups, ignored JIDs, empty messages
3. Message is added to `ContactState.pending_messages`
4. Debounce timer starts (5s). If another message arrives, timer restarts
5. After quiet period, all pending messages are batched into one agent prompt
6. `InMemoryRunner` invokes the agent with `run_async()`
7. Agent calls `send_message` MCP tool → MCP server → Go bridge REST API → WhatsApp
8. Cooldown timer set to prevent rapid-fire replies

### Key Design Decisions
- **InMemoryRunner** (not the main server's Runner): The auto-reply system uses its own `InMemoryRunner` to avoid session conflicts with the ADK web UI
- **Per-contact sessions**: Each sender gets a unique session (`auto_reply_{phone}`) so the agent has conversation context
- **Debouncing**: Rapid messages (e.g., 3 messages in 2 seconds) are batched into a single agent invocation
- **Cooldown**: After replying, a 0.5s cooldown prevents infinite loops (bot's own sent message triggers a webhook → reply → webhook → ...)
- **The Go bridge already filters `is_from_me`**, so the bot's own outgoing messages don't trigger webhooks

### Guard Clauses (messages that are NOT replied to)
- Group messages (`chat_jid` ending in `@g.us`)
- Messages from self (`msg.Info.IsFromMe` in Go bridge)
- Empty or whitespace-only messages
- JIDs in the `AUTO_REPLY_IGNORE_JIDS` list
- `AUTO_REPLY_ENABLED=false`

---

## Agent Configuration (`agent.py`)

### MCP Toolset
The agent connects to the WhatsApp MCP server via `StdioConnectionParams`:
```python
McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="uv",
            args=["--directory", WHATSAPP_MCP_DIR, "run", "main.py"],
        ),
        timeout=30,
    ),
    tool_filter=[
        "search_contacts", "list_messages", "list_chats",
        "get_chat", "get_direct_chat_by_contact",
        "get_last_interaction", "send_message",
    ],
)
```

### Model
- Configured via `ROOT_AGENT_MODEL` env var
- Models with `/` in the name (e.g., `openrouter/...`) use `LiteLlm` wrapper
- Current model: `openrouter/nvidia/nemotron-3-nano-30b-a3b:free`

### Plugins
- `GlobalInstructionPlugin`: Injects current date + "You are Chirag's assistant" into system prompt
- `LoggingPlugin`: Rich emoji-formatted event logging

### Callbacks
- `LoggingCallbacks`: Logs before/after agent, model, and tool calls
- `add_session_to_memory`: Saves completed sessions to memory service

---

## Development Conventions

### Code Quality
```bash
uv run ruff format     # Format code
uv run ruff check      # Lint
uv run mypy .          # Type check
uv run pytest --cov=src  # Tests
```

### Port Assignments
| Port | Service |
|------|---------|
| `8080` | Go WhatsApp Bridge REST API |
| `8888` | Python ADK Agent Server (FastAPI) |

**IMPORTANT:** Never change the Go bridge port from 8080 — the MCP server hardcodes `http://localhost:8080/api` in `whatsapp.py`.

### Common Issues

1. **Port 8888 already in use**: Kill the old process: `lsof -ti :8888 | xargs kill -9`
2. **Port 8080 already in use**: Old Go bridge still running: `lsof -ti :8080 | xargs kill -9`
3. **Go bridge exits immediately**: The `client.IsConnected()` check can fail if connection is slow. There's a retry loop (10 attempts × 1s). If it still fails, check your WhatsApp session.
4. **QR code needed**: Delete `store/` directory in `whatsapp-bridge/` to force re-authentication
5. **MCP server timeout**: If agent can't connect to MCP, ensure Go bridge is running first (MCP server needs the REST API at `:8080`)
6. **Session mismatch error**: The `App` name in `agent.py` MUST be `whatsapp_bot` (matches the module directory name)
7. **"Client outdated (405)" error**: The `whatsmeow` Go dependency needs updating. Run `go get -u go.mau.fi/whatsmeow@latest` in `whatsapp-bridge/`

### Git Workflow
- Runtime data is gitignored: `whatsapp-mcp/whatsapp-bridge/store/`, `whatsapp-mcp/whatsapp-bridge/whatsapp-bridge` (binary), `whatsapp-mcp/whatsapp-mcp-server/.venv/`
- GitHub account: `QueryPlanner` (email: `chiragnpatil@gmail.com`)

### Testing Contacts
- Suraj Gavali: `{"phone": "918408878186", "jid": "918408878186@s.whatsapp.net"}`
- Chetan Jadhav: `{"phone": "919067732279", "jid": "919067732279@s.whatsapp.net"}`