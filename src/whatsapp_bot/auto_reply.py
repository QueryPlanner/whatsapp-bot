"""Auto-reply webhook handler for incoming WhatsApp DMs.

Receives webhook POSTs from the Go WhatsApp bridge when a new direct message
arrives and uses the ADK agent to generate and send a reply via the MCP
send_message tool.

Features:
- Debouncing: waits for a quiet period before replying so rapid messages
  are batched into one agent invocation.
- Per-contact sessions: each sender gets their own ADK session so the
  agent retains conversation context.
- Persistent storage: uses DatabaseSessionService to keep history across restarts.
- Ignore list: configurable JIDs to never auto-reply to.
- Rate limiting: max one reply per contact per cooldown window.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from google.adk.runners import Runner
from google.adk.sessions.database_session_service import DatabaseSessionService
from google.adk.errors.already_exists_error import AlreadyExistsError
from google.genai import types

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Seconds to wait after the last message before invoking the agent.
# Groups rapid-fire messages into a single agent turn.
DEBOUNCE_SECONDS = float(os.getenv("AUTO_REPLY_DEBOUNCE_SECONDS", "5"))

# Minimum seconds between replies to the same contact.
COOLDOWN_SECONDS = float(os.getenv("AUTO_REPLY_COOLDOWN_SECONDS", "0.5"))

# Comma-separated JIDs to never auto-reply to.
_raw_ignore = os.getenv("AUTO_REPLY_IGNORE_JIDS", "")
IGNORE_JIDS: set[str] = {
    jid.strip() for jid in _raw_ignore.split(",") if jid.strip()
}

# Feature flag to enable/disable auto-reply globally.
AUTO_REPLY_ENABLED = os.getenv("AUTO_REPLY_ENABLED", "true").lower() == "true"

# App name — must match the ADK App name (which matches the module dir name).
APP_NAME = "whatsapp_bot"
USER_ID = "whatsapp_auto_reply"


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------
@dataclass
class ContactState:
    """Tracks debounce/cooldown state for a single contact."""

    pending_messages: list[dict[str, Any]] = field(default_factory=list)
    debounce_task: asyncio.Task[None] | None = None
    last_reply_at: float = 0.0
    session_id: str | None = None


# phone_number -> ContactState
_contacts: dict[str, ContactState] = {}
_runner: Runner | None = None


def _get_runner() -> Runner:
    """Lazy-init the Runner with persistent session storage."""
    global _runner  # noqa: PLW0603
    if _runner is None:
        from .agent import app as adk_app

        # Use the same database URL logic as server.py
        # Default to a local SQLite file if not set
        db_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///store/sessions.db")
        
        # Handle postgres fix like in server.py
        if db_url.startswith("postgresql://"):
            db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
            
        # Fix for asyncpg which doesn't support sslmode/channel_binding in query
        if db_url:
            db_url = db_url.replace("sslmode=require", "ssl=require").replace(
                "&channel_binding=require", ""
            )

        logger.info("Initializing Runner with storage: %s", db_url)
        
        # Create persistent session service
        session_service = DatabaseSessionService(db_url=db_url)
        
        # Initialize Runner with the persistent session service
        _runner = Runner(app=adk_app, session_service=session_service)
        
    return _runner


def _get_contact_state(sender: str) -> ContactState:
    if sender not in _contacts:
        _contacts[sender] = ContactState()
    return _contacts[sender]


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------
@router.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    """Receive incoming DM notifications from the Go bridge.

    Expected JSON body::

        {
            "chat_jid": "918408878186@s.whatsapp.net",
            "sender": "918408878186",
            "sender_name": "...",
            "content": "Hello!",
            "timestamp": "2026-02-14T17:10:00+05:30"
        }
    """
    if not AUTO_REPLY_ENABLED:
        return JSONResponse({"status": "disabled"}, status_code=200)

    body: dict[str, Any] = await request.json()
    chat_jid: str = body.get("chat_jid", "")
    sender: str = body.get("sender", "")
    sender_name: str = body.get("sender_name", sender)
    content: str = body.get("content", "")
    timestamp: str = body.get("timestamp", "")
    is_from_me: bool = body.get("is_from_me", False)

    # --- Guard clauses ---
    # Must be a personal chat (not group)
    # Allow @s.whatsapp.net (phone numbers) and @lid (companion devices/usernames)
    if not (chat_jid.endswith("@s.whatsapp.net") or chat_jid.endswith("@lid")):
        logger.debug("Skipping non-DM: %s", chat_jid)
        return JSONResponse({"status": "skipped", "reason": "not_dm"})

    # Self-sent messages logic (Note to Self / Manual invocation)
    if is_from_me:
        # Only reply if explicitly triggered with "@agent"
        if not content.lower().strip().startswith("@agent"):
            return JSONResponse({"status": "skipped", "reason": "self_ignored"})
        
        # Optional: verify it's a self-chat (sender == chat_jid roughly)
        # But letting it work in any chat where I type "@agent" is also a useful feature
        logger.info("Universal agent invocation by user: %s", content[:50])

    # Ignore list check (skip if the contact is ignored, UNLESS I explicitly invoked it)
    if not is_from_me and (chat_jid in IGNORE_JIDS or sender in IGNORE_JIDS):
        logger.debug("Skipping ignored JID: %s", chat_jid)
        return JSONResponse({"status": "skipped", "reason": "ignored"})

    # Empty message
    if not content.strip():
        return JSONResponse({"status": "skipped", "reason": "empty"})

    logger.info(
        "Webhook received DM from %s (%s): %s",
        sender_name,
        sender,
        content[:80],
    )

    # --- Debounce logic ---
    state = _get_contact_state(sender)
    state.pending_messages.append(
        {
            "sender": sender,
            "sender_name": sender_name,
            "chat_jid": chat_jid,
            "content": content,
            "timestamp": timestamp,
        }
    )

    # Cancel existing debounce timer and restart
    if state.debounce_task and not state.debounce_task.done():
        state.debounce_task.cancel()

    state.debounce_task = asyncio.create_task(
        _debounced_reply(sender, DEBOUNCE_SECONDS)
    )

    return JSONResponse({"status": "queued"})


# ---------------------------------------------------------------------------
# Debounced reply logic
# ---------------------------------------------------------------------------
async def _debounced_reply(sender: str, delay: float) -> None:
    """Wait for quiet period, then invoke the agent."""
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        # A newer message arrived — this timer is superseded.
        return

    state = _get_contact_state(sender)

    # --- Cooldown check ---
    now = time.time()
    if now - state.last_reply_at < COOLDOWN_SECONDS:
        remaining = COOLDOWN_SECONDS - (now - state.last_reply_at)
        logger.info(
            "Cooldown active for %s, re-scheduling (%.0fs remaining)",
            sender,
            remaining,
        )
        # Don't drop messages — re-schedule after cooldown expires
        state.debounce_task = asyncio.create_task(
            _debounced_reply(sender, remaining + 0.5)
        )
        return

    # Drain pending messages
    messages = list(state.pending_messages)
    state.pending_messages.clear()

    if not messages:
        return

    # Build a consolidated user message for the agent
    sender_name = messages[0].get("sender_name", sender)
    chat_jid = messages[0]["chat_jid"]

    if len(messages) == 1:
        user_prompt = (
            f"You received a WhatsApp message from {sender_name} "
            f"(phone: {sender}, JID: {chat_jid}).\n\n"
            f'Their message: "{messages[0]["content"]}"\n\n'
            f"Reply to them using the send_message tool with recipient "
            f'"{chat_jid}".'
        )
    else:
        combined = "\n".join(
            f"  [{m['timestamp']}] {m['content']}" for m in messages
        )
        user_prompt = (
            f"You received {len(messages)} WhatsApp messages from "
            f"{sender_name} (phone: {sender}, JID: {chat_jid}).\n\n"
            f"Their messages (oldest first):\n{combined}\n\n"
            f"Reply to them using the send_message tool with recipient "
            f'"{chat_jid}". Consider all the messages together '
            f"when crafting your reply."
        )

    logger.info("Invoking agent for %s with %d message(s)", sender, len(messages))

    try:
        await _invoke_agent(sender, chat_jid, user_prompt)
        state.last_reply_at = time.time()
        logger.info("Auto-reply sent to %s", sender)
    except Exception:
        logger.exception("Failed to auto-reply to %s", sender)


# ---------------------------------------------------------------------------
# Agent invocation
# ---------------------------------------------------------------------------
async def _invoke_agent(sender: str, chat_jid: str, user_prompt: str) -> None:
    """Run the ADK agent with the given prompt and let it reply via tools."""
    runner = _get_runner()
    
    # Use a consistent session ID for this sender to maintain history
    session_id = f"auto_reply_{sender}"

    # Ensure we initialize the session if needed
    try:
        # Check if session exists by trying to get it
        session = await runner.session_service.get_session(
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=session_id,
        )
        
        if session:
            logger.info("Using existing session %s for %s", session_id, sender)
        else:
            # Create new session if it doesn't exist
            # Note: create_session is what actually persists the initial state record
            session = await runner.session_service.create_session(
                app_name=APP_NAME,
                user_id=USER_ID,
                session_id=session_id,
            )
            logger.info("Created new persistent session %s for %s", session_id, sender)
        
        # We don't need to manually store session_id in state anymore since 
        # we deterministically derive it from the sender.
        
    except AlreadyExistsError:
        # Race/edge case: just proceed, usage of the ID in run_async will work
        logger.info("Session %s already exists (caught race), proceeding", session_id)
    except Exception as e:
        logger.error("Error managing session for %s: %s", sender, e)
        # We might fail here if db is down, but let run_async try or bubble up
        raise

    new_message = types.Content(
        role="user",
        parts=[types.Part.from_text(text=user_prompt)],
    )

    final_text: list[str] = []
    
    # Execute the agent turnover with the persistent session ID
    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session_id,
        new_message=new_message,
    ):
        # Log all events for debugging
        if event.content and event.content.parts:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    final_text.append(part.text)

    if final_text:
        logger.info(
            "Agent response for %s: %s", sender, " ".join(final_text)[:200]
        )
    else:
        logger.warning("Agent produced no text output for %s", sender)
