"""Tests for Telegram plan-confirmation inline buttons."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from gateway.config import PlatformConfig
from gateway.platforms.telegram import TelegramAdapter


def _make_adapter(extra=None):
    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    )
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _plan_text() -> str:
    return (
        "Implementation plan:\n"
        "1. Update the Telegram adapter.\n"
        "2. Add callback regression tests.\n"
        "3. Run targeted pytest.\n\n"
        "Say proceed when you want me to implement it."
    )


@pytest.mark.asyncio
async def test_final_actionable_plan_send_does_not_attach_buttons_by_default():
    adapter = _make_adapter()
    msg = MagicMock()
    msg.message_id = 42
    adapter._bot.send_message = AsyncMock(return_value=msg)

    result = await adapter.send("12345", _plan_text(), metadata={"notify": True})

    assert result.success is True
    assert len(adapter._plan_action_state) == 0
    kwargs = adapter._bot.send_message.call_args.kwargs
    assert kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_final_actionable_plan_buttons_are_explicit_opt_in():
    adapter = _make_adapter(extra={"plan_action_buttons": True})
    msg = MagicMock()
    msg.message_id = 42
    adapter._bot.send_message = AsyncMock(return_value=msg)

    result = await adapter.send("12345", _plan_text(), metadata={"notify": True})

    assert result.success is True
    assert len(adapter._plan_action_state) == 1
    stored = next(iter(adapter._plan_action_state.values()))
    assert "Update the Telegram adapter" in stored["content"]
    kwargs = adapter._bot.send_message.call_args.kwargs
    assert kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_plan_proceed_button_dispatches_clean_turn_with_reply_anchor():
    adapter = _make_adapter()
    plan = _plan_text()
    adapter._plan_action_state[7] = {"content": plan, "created_at": 1.0}
    captured = {}

    async def _handle_message(event):
        captured["event"] = event

    adapter.handle_message = AsyncMock(side_effect=_handle_message)

    query = AsyncMock()
    query.data = "pa:p:7"
    query.message = SimpleNamespace(
        chat=SimpleNamespace(id=12345, type="private", title=None, full_name="Tester"),
        chat_id=12345,
        message_id=88,
        message_thread_id=None,
        text=plan,
    )
    query.from_user = SimpleNamespace(id="777", first_name="Tester", full_name="Tester")
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
        await adapter._handle_callback_query(update, MagicMock())

    event = captured["event"]
    assert event.text == "proceed"
    assert event.reply_to_message_id == "88"
    assert event.reply_to_text == plan
    assert event.source.chat_id == "12345"
    assert event.source.user_id == "777"
    query.edit_message_reply_markup.assert_called_once_with(reply_markup=None)
