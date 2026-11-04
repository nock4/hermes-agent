"""Regression: a platform (Telegram) turn in a shared gateway must never route its
dangerous-command approval to the local CLI prompt.

A desktop/TUI session hosted by the same gateway process sets
``HERMES_INTERACTIVE=1`` process-wide (``tui_gateway.server._enable_gateway_prompts``).
Platform turns used to inherit it through the env fallback of
``_is_interactive_cli()``, so whenever the platform notifier was missing at tool
time the approval fell through to a thread-local CLI callback nobody could see,
and the command ended with "Command timed out without user response" and no
Telegram card. The turn runner now binds the interactive contextvar to False.

Also covers identity-checked notifier unregistration: an older run ending on the
same session key must not strip a newer run's notifier.
"""

from __future__ import annotations

import threading

import pytest

import tools.approval as approval_module
from tools import approval_context
from tools.approval import (
    check_all_command_guards,
    register_gateway_notify,
    unregister_gateway_notify,
)
from tools.approval_context import (
    reset_current_session_key,
    reset_hermes_interactive_context,
    set_current_session_key,
    set_hermes_interactive_context,
)
from tools.terminal_tool import set_approval_callback

SESSION_KEY = "agent:frontpage:telegram:dm:1"


@pytest.fixture(autouse=True)
def _shared_gateway_env(monkeypatch):
    for key in ("HERMES_CRON_SESSION", "HERMES_YOLO_MODE", "HERMES_SINGLE_QUERY_SESSION"):
        monkeypatch.delenv(key, raising=False)
    # Shared gateway after a desktop session started: all three set process-wide.
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    approval_module._session_approved.clear()
    approval_module._permanent_approved.clear()
    approval_module._pending.clear()
    approval_module._denial_tally.clear()
    approval_module._gateway_notify_cbs.clear()
    set_approval_callback(None)
    key_token = set_current_session_key(SESSION_KEY)
    yield
    reset_current_session_key(key_token)
    approval_module._gateway_notify_cbs.clear()
    approval_module._denial_tally.clear()
    set_approval_callback(None)


def _leaked_cli_callback(calls):
    def _cb(command, description, **kwargs):
        calls.append(command)
        return "timeout"  # what the invisible local prompt produced in production
    return _cb


class TestPlatformTurnNeverUsesCliPrompt:
    def test_leak_reaches_cli_prompt_without_binding(self):
        """Documents the failure: env fallback alone sends the approval to the CLI callback."""
        calls = []
        set_approval_callback(_leaked_cli_callback(calls))
        result = check_all_command_guards("rm -rf /tmp/testdir", "local")
        assert calls, "precondition: unbound context should reproduce the leak"
        assert "timed out without user response" in (result.get("message") or "")

    def test_bound_platform_turn_queues_instead_of_cli_prompt(self):
        calls = []
        set_approval_callback(_leaked_cli_callback(calls))
        token = set_hermes_interactive_context(False)
        try:
            result = check_all_command_guards("rm -rf /tmp/testdir", "local")
        finally:
            reset_hermes_interactive_context(token)
        assert not calls, "platform turn must not use the local CLI prompt"
        assert result.get("approved") is False  # safeguard intact: nothing ran
        assert result.get("status") == "pending_approval"
        assert approval_module._pending.get(SESSION_KEY), "approval must be queued for /approve"

    def test_bound_platform_turn_sends_card_to_its_notifier(self):
        cli_calls, sent = [], []
        set_approval_callback(_leaked_cli_callback(cli_calls))

        def _notify(data):
            sent.append(data)
            threading.Timer(0.05, approval_module.resolve_gateway_approval,
                            args=(SESSION_KEY, "deny")).start()

        register_gateway_notify(SESSION_KEY, _notify)
        token = set_hermes_interactive_context(False)
        try:
            result = check_all_command_guards("rm -rf /tmp/testdir", "local")
        finally:
            reset_hermes_interactive_context(token)
        assert sent and not cli_calls
        assert result.get("approved") is False
        assert "denied by user" in (result.get("message") or "")


class TestIdentityCheckedUnregister:
    def test_older_run_cannot_strip_newer_notifier(self):
        old_cb, new_cb = (lambda data: None), (lambda data: None)
        register_gateway_notify(SESSION_KEY, old_cb)
        register_gateway_notify(SESSION_KEY, new_cb)  # newer run on the same chat
        unregister_gateway_notify(SESSION_KEY, old_cb)  # older run's finally
        assert approval_module._gateway_notify_cb(SESSION_KEY) is new_cb
        unregister_gateway_notify(SESSION_KEY, new_cb)
        assert approval_module._gateway_notify_cb(SESSION_KEY) is None

    def test_unregister_without_cb_keeps_legacy_behavior(self):
        register_gateway_notify(SESSION_KEY, lambda data: None)
        unregister_gateway_notify(SESSION_KEY)
        assert approval_module._gateway_notify_cb(SESSION_KEY) is None
