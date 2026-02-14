"""Auto-reply webhook handler for incoming WhatsApp DMs.

Receives webhook POSTs from the Go WhatsApp bridge when a new direct message
arrives and uses the ADK agent to generate and send a reply via the MCP
send_message tool.

Features:
- Debouncing: waits for a quiet period before replying so rapid messages
  are batched into one agent invocation.
- Per-contact sessions: each sender gets their own ADK session so the
  agent retains conversation context.
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
from google.adk.runners import InMemoryRunner
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
COOLDOWN_SECONDS = float(os.getenv("AUTO_REPLY_COOLDOWN_SECONDS", "15"))

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
_runner: InMemoryRunner | None = None


def _get_runner() -> InMemoryRunner:
    """Lazy-init the InMemoryRunner using the root_agent from agent.py."""
    global _runner  # noqa: PLW0603
    if _runner is None:
        from .agent import app as adk_app

        _runner = InMemoryRunner(app=adk_app)
        logger.info("Auto-reply InMemoryRunner initialized")
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

    # --- Guard clauses ---
    # Must be a personal chat (not group)
    if not chat_jid.endswith("@s.whatsapp.net"):
        logger.debug("Skipping non-DM: %s", chat_jid)
        return JSONResponse({"status": "skipped", "reason": "not_dm"})

    # Ignore list
    if chat_jid in IGNORE_JIDS or sender in IGNORE_JIDS:
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
            "Cooldown active for %s, skipping (%.0fs remaining)", sender, remaining
        )
        state.pending_messages.clear()
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
    state = _get_contact_state(sender)

    # Create or reuse a session per contact
    if state.session_id is None:
        session = await runner.session_service.create_session(
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=f"auto_reply_{sender}",
        )
        state.session_id = session.id
        logger.info("Created session %s for %s", state.session_id, sender)

    new_message = types.Content(
        role="user",
        parts=[types.Part.from_text(text=user_prompt)],
    )

    final_text: list[str] = []
    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=state.session_id,
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
