from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB
from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _mock_response(content: str = "Done"):
    message = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _make_agent(session_db: SessionDB, session_id: str = "continuity-session") -> AIAgent:
    hermes_home = session_db.db_path.parent / "hermes_home"
    (hermes_home / "logs").mkdir(parents=True, exist_ok=True)
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("run_agent._hermes_home", hermes_home),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            session_id=session_id,
            session_db=session_db,
            platform="cli",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.client = MagicMock()
        return agent


@pytest.fixture()
def session_db(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield db
    finally:
        db.close()


def test_proceed_injects_prior_assistant_plan_without_persisting_note(session_db):
    agent = _make_agent(session_db)
    agent.client.chat.completions.create.return_value = _mock_response("Implemented.")
    history = [
        {"role": "user", "content": "Come up with a plan."},
        {
            "role": "assistant",
            "content": "Plan:\n1. Update the parser.\n2. Add tests.\n3. Run pytest.",
        },
    ]

    result = agent.run_conversation("ok, let's proceed", conversation_history=history)

    sent_messages = agent.client.chat.completions.create.call_args.kwargs["messages"]
    sent_user = [m for m in sent_messages if m.get("role") == "user"][-1]
    assert "Continuity note:" in sent_user["content"]
    assert "Update the parser" in sent_user["content"]
    assert "ok, let's proceed" in sent_user["content"]

    assert result["messages"][2]["role"] == "user"
    assert result["messages"][2]["content"] == "ok, let's proceed"

    persisted = session_db.get_messages_as_conversation(agent.session_id)
    assert any(
        m["role"] == "user" and m["content"] == "ok, let's proceed"
        for m in persisted
    )
    assert not any(
        isinstance(m.get("content"), str) and "Continuity note:" in m["content"]
        for m in persisted
    )


def test_pending_plan_survives_fresh_agent_instance(session_db):
    first = _make_agent(session_db)
    plan = (
        "Implementation plan:\n"
        "1. Edit run_agent.py to preserve the pending plan.\n"
        "2. Add regression tests.\n"
        "3. Run targeted pytest.\n\n"
        "Say proceed when you want me to implement it."
    )
    first.client.chat.completions.create.return_value = _mock_response(plan)

    first.run_conversation("come up with a plan")

    pending = session_db.get_pending_action(first.session_id)
    assert pending
    assert "Edit run_agent.py" in pending["assistant_content"]

    second = _make_agent(session_db, session_id=first.session_id)
    second.client.chat.completions.create.return_value = _mock_response("Implemented.")

    second.run_conversation("proceed", conversation_history=[])

    sent_messages = second.client.chat.completions.create.call_args.kwargs["messages"]
    sent_user = [m for m in sent_messages if m.get("role") == "user"][-1]
    assert "stored pending assistant plan" in sent_user["content"]
    assert "Edit run_agent.py" in sent_user["content"]
    assert "User reply:\nproceed" in sent_user["content"]


def test_reply_to_plan_overrides_newer_pending_plan(session_db):
    agent = _make_agent(session_db)
    agent.client.chat.completions.create.return_value = _mock_response("Implemented.")
    session_db.create_session(agent.session_id, "cli")
    session_db.set_pending_action(
        agent.session_id,
        {
            "version": 1,
            "source": "assistant_final_response",
            "assistant_content": (
                "Implementation plan:\n"
                "1. Newer pending plan.\n"
                "2. Do something else.\n\n"
                "Say proceed when ready."
            ),
        },
    )

    replied_to = (
        "Implementation plan:\n"
        "1. Replied-to Telegram plan.\n"
        "2. Preserve this anchor.\n\n"
        "Say proceed when ready."
    )
    agent.run_conversation(f'[Replying to: "{replied_to}"]\n\nproceed')

    sent_messages = agent.client.chat.completions.create.call_args.kwargs["messages"]
    sent_user = [m for m in sent_messages if m.get("role") == "user"][-1]
    assert "Telegram replied-to assistant message" in sent_user["content"]
    assert "Replied-to Telegram plan" in sent_user["content"]
    assert "Newer pending plan" not in sent_user["content"]
