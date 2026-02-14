"""ADK LlmAgent configuration with WhatsApp MCP tools."""

import logging
import os
from pathlib import Path
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.plugins.global_instruction_plugin import GlobalInstructionPlugin
from google.adk.plugins.logging_plugin import LoggingPlugin
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from mcp import StdioServerParameters

from .callbacks import LoggingCallbacks, add_session_to_memory
from .prompt import (
    return_description_root,
    return_global_instruction,
    return_instruction_root,
)

logger = logging.getLogger(__name__)

logging_callbacks = LoggingCallbacks()

# Determine model configuration
model_name = os.getenv("ROOT_AGENT_MODEL", "gemini-2.5-flash")
model: Any = model_name

# Explicitly use LiteLlm for OpenRouter or other provider-prefixed models
# that might not be auto-detected by ADK's registry.
if model_name.lower().startswith("openrouter/") or "/" in model_name:
    try:
        from google.adk.models import LiteLlm

        logger.info(f"Using LiteLlm for model: {model_name}")
        model = LiteLlm(model=model_name)
    except ImportError:
        logger.warning(
            "LiteLlm not available, falling back to string model name. "
            "OpenRouter models may not work."
        )

# Path to the WhatsApp MCP server
WHATSAPP_MCP_DIR = str(
    Path(__file__).resolve().parent.parent.parent / "whatsapp-mcp" / "whatsapp-mcp-server"
)

# Resolve the uv binary path
UV_PATH = os.getenv("UV_PATH", "uv")

# WhatsApp MCP Toolset — connects to the whatsapp-mcp-server via stdio
whatsapp_mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=UV_PATH,
            args=[
                "--directory",
                WHATSAPP_MCP_DIR,
                "run",
                "main.py",
            ],
        ),
        timeout=30,
    ),
    # Filter to only the tools we need for the WhatsApp bot
    tool_filter=[
        "search_contacts",
        "list_messages",
        "list_chats",
        "get_chat",
        "get_direct_chat_by_contact",
        "get_last_interaction",
        "send_message",
    ],
)

root_agent = LlmAgent(
    name="root_agent",
    description=return_description_root(),
    before_agent_callback=logging_callbacks.before_agent,
    after_agent_callback=[logging_callbacks.after_agent, add_session_to_memory],
    model=model,
    instruction=return_instruction_root(),
    tools=[PreloadMemoryTool(), whatsapp_mcp_toolset],
    before_model_callback=logging_callbacks.before_model,
    after_model_callback=logging_callbacks.after_model,
    before_tool_callback=logging_callbacks.before_tool,
    after_tool_callback=logging_callbacks.after_tool,
)

# Optional App configs explicitly set to None for template documentation
app = App(
    name="whatsapp_bot",
    root_agent=root_agent,
    plugins=[
        GlobalInstructionPlugin(return_global_instruction),
        LoggingPlugin(),
    ],
    events_compaction_config=None,
    context_cache_config=None,
    resumability_config=None,
)
