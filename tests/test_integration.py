"""Integration tests for whatsapp_bot configuration and component wiring.

This module validates the basic structure and wiring of ADK app components.
Tests are pattern-based and validate integration points regardless of specific
implementation choices (plugins, tools, etc.).

Future: Container-based smoke tests for CI/CD will be added here.
"""

from collections.abc import Sequence
from typing import Any, Protocol, cast

from whatsapp_bot import app


class AgentConfigLike(Protocol):
    """Minimal whatsapp_bot surface needed for integration assertions."""

    name: str
    model: Any
    instruction: str | None
    description: str | None
    tools: Sequence[object] | None


def as_whatsapp_bot_config(whatsapp_bot: object) -> AgentConfigLike:
    """Treat runtime whatsapp_bot instances as a typed config surface."""
    return cast(AgentConfigLike, whatsapp_bot)


class TestAppIntegration:
    """Pattern-based integration tests for App configuration and wiring."""

    def test_app_is_properly_instantiated(self) -> None:
        """Verify app container is properly instantiated."""
        assert app is not None
        assert app.name is not None
        assert isinstance(app.name, str)
        assert len(app.name) > 0

    def test_app_has_root_whatsapp_bot(self) -> None:
        """Verify app is wired to root whatsapp_bot."""
        assert app.root_whatsapp_bot is not None

    def test_app_plugins_are_valid_if_configured(self) -> None:
        """Verify plugins (if any) are properly initialized."""
        # Plugins are optional - if configured, they should be a list
        if app.plugins is not None:
            assert isinstance(app.plugins, list)
            # Each plugin should be an object instance
            for plugin in app.plugins:
                assert plugin is not None
                assert hasattr(plugin, "__class__")


class TestAgentIntegration:
    """Pattern-based integration tests for Agent configuration."""

    def test_whatsapp_bot_has_required_configuration(self) -> None:
        """Verify whatsapp_bot has required configuration fields."""
        whatsapp_bot = app.root_whatsapp_bot
        assert whatsapp_bot is not None
        typed_whatsapp_bot = as_whatsapp_bot_config(whatsapp_bot)

        # Required: whatsapp_bot name
        assert typed_whatsapp_bot.name is not None
        assert isinstance(typed_whatsapp_bot.name, str)
        assert len(typed_whatsapp_bot.name) > 0

        # Required: whatsapp_bot model
        assert typed_whatsapp_bot.model is not None
        # model can be a string name or a model object (e.g. LiteLlm)
        if isinstance(typed_whatsapp_bot.model, str):
            assert len(typed_whatsapp_bot.model) > 0
        else:
            # If it's an object, it should have a model attribute that is a string
            assert hasattr(typed_whatsapp_bot.model, "model")
            assert isinstance(typed_whatsapp_bot.model.model, str)
            assert len(typed_whatsapp_bot.model.model) > 0

    def test_whatsapp_bot_instructions_are_valid_if_configured(self) -> None:
        """Verify whatsapp_bot instructions (if configured) are valid strings."""
        whatsapp_bot = app.root_whatsapp_bot
        assert whatsapp_bot is not None
        typed_whatsapp_bot = as_whatsapp_bot_config(whatsapp_bot)

        # Instruction is optional - if configured, should be non-empty string
        if typed_whatsapp_bot.instruction is not None:
            assert isinstance(typed_whatsapp_bot.instruction, str)
            assert len(typed_whatsapp_bot.instruction) > 0

        # Description is optional - if configured, should be non-empty string
        if typed_whatsapp_bot.description is not None:
            assert isinstance(typed_whatsapp_bot.description, str)
            assert len(typed_whatsapp_bot.description) > 0

    def test_whatsapp_bot_tools_are_valid_if_configured(self) -> None:
        """Verify whatsapp_bot tools (if any) are properly initialized."""
        whatsapp_bot = app.root_whatsapp_bot
        assert whatsapp_bot is not None
        typed_whatsapp_bot = as_whatsapp_bot_config(whatsapp_bot)

        # Tools are optional - if configured, should be a list
        if typed_whatsapp_bot.tools is not None:
            assert isinstance(typed_whatsapp_bot.tools, list)
            # Each tool should be an object instance
            for tool in typed_whatsapp_bot.tools:
                assert tool is not None
                assert hasattr(tool, "__class__")
