"""Cloud-browser session names double as the local agent-browser session, which names both the
socket dir and the socket file. A gateway-length task id must still fit macOS's AF_UNIX limit."""

from __future__ import annotations

import os
import shutil
import sys

import pytest

from plugins.browser._common import CloudBrowserProvider

DARWIN_SUN_PATH_MAX = 103  # agent-browser's own check: "max 103"
GATEWAY_TASK_ID = "20261120_093651_4ee677a4"  # the id from the failing Telegram session


def _socket_path(socket_dir: str, session_name: str) -> str:
    return os.path.join(socket_dir, f"{session_name}.sock")


def test_gateway_task_id_socket_path_fits_on_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import browser_tool_session as session

    monkeypatch.setattr(sys, "platform", "darwin")
    name = CloudBrowserProvider._session_name(GATEWAY_TASK_ID)
    socket_dir = session._prepare_session_socket_dir(name)
    try:
        assert socket_dir == f"/tmp/agent-browser-{name}"
        path = _socket_path(socket_dir, name)
        assert len(path.encode()) <= DARWIN_SUN_PATH_MAX, path
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)


def test_session_name_keeps_reaper_prefix_and_is_unique_per_task() -> None:
    # browser_tool_lifecycle reaps ``agent-browser-hermes_*`` dirs; keep that prefix.
    a = CloudBrowserProvider._session_name(GATEWAY_TASK_ID)
    b = CloudBrowserProvider._session_name(GATEWAY_TASK_ID)
    other = CloudBrowserProvider._session_name("20261120_093651_ffffffff")
    assert a.startswith("hermes_") and a != b
    assert a.split("_")[1] == b.split("_")[1] != other.split("_")[1]


@pytest.mark.parametrize("task_id", ["", "x" * 200, "agent:main:telegram:dm:2142213564:223193"])
def test_session_name_length_is_bounded(task_id: str) -> None:
    name = CloudBrowserProvider._session_name(task_id)
    worst = _socket_path(f"/tmp/agent-browser-{name}", name)
    assert len(worst.encode()) <= DARWIN_SUN_PATH_MAX
