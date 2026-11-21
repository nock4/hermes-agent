"""Local POSIX content/file search runs rg as a direct argv subprocess.

The shell path pays two bash spawns per search (``test -e`` probe + ``set -o
pipefail; rg ... | head``). On a ``LocalEnvironment`` the same rg argv can run
natively with a bounded stdout read; the parser and every argument builder are
shared, so the two transports must agree on results.
"""

import json
import os

import pytest

from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations

pytestmark = pytest.mark.platforms("posix")  # native rg lane is POSIX-only


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "a.py").write_text("needle one\nplain\nneedle two\n")
    (tmp_path / "b.txt").write_text("needle three\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.py").write_text("needle four\n")
    return tmp_path


@pytest.fixture(scope="module")
def _local_env(tmp_path_factory):
    """One real LocalEnvironment per module (constructing one costs ~0.8 s)."""
    return LocalEnvironment(cwd=str(tmp_path_factory.mktemp("native-rg")))


@pytest.fixture
def ops_factory(_local_env):
    """``make(tree, spy)`` → ShellFileOperations over the shared env, every execute recorded in ``spy``."""
    real = type(_local_env).execute.__get__(_local_env, type(_local_env))

    def make(tree, spy):
        _local_env.cwd = str(tree)

        def recording(command, *a, **kw):
            spy.append(command)
            return real(command, *a, **kw)

        _local_env.execute = recording
        return ShellFileOperations(_local_env, cwd=str(tree))

    yield make
    _local_env.__dict__.pop("execute", None)


def _normalized(result):
    d = result.to_dict()
    for key in ("matches", "files"):
        if key in d:
            d[key] = sorted(json.dumps(item, sort_keys=True) for item in d[key])
    return d


def test_native_search_never_touches_the_shell_and_matches_shell_results(tree, ops_factory, monkeypatch):
    if not ops_factory(tree, [])._has_command("rg"):
        pytest.skip("native search parity requires ripgrep in the search environment")
    cases = [
        dict(pattern="needle", path=str(tree)),
        # (no offset/limit slicing here: rg's parallel walk orders files
        # nondeterministically, so a page differs run-to-run on either transport)
        dict(pattern="needle", path=str(tree), output_mode="count"),
        dict(pattern="needle", path=str(tree), output_mode="files_only", file_glob="*.py"),
        dict(pattern="NEEDLE_NOPE", path=str(tree)),  # zero-match probes
        dict(pattern="*.py", path=str(tree), target="files"),
        dict(pattern="needle", path=str(tree / "missing")),
    ]
    for case in cases:
        monkeypatch.setenv("HERMES_NATIVE_FILE_READ", "0")
        shell = _normalized(ops_factory(tree, []).search(**case))
        monkeypatch.setenv("HERMES_NATIVE_FILE_READ", "1")
        calls = []
        native = _normalized(ops_factory(tree, calls).search(**case))
        assert native == shell, case
        # rg resolution (``command -v rg``) still goes through the shell once; the
        # existence probe and the rg pipeline itself must not.
        assert not [c for c in calls if "pipefail" in c or c.startswith("test -e")], case


def test_native_runner_honours_deadline_and_interrupt_while_rg_is_silent(tree, ops_factory, monkeypatch):
    """A search producing no output must still stop at the deadline / on /stop
    (the shell path gets this from ``_wait_for_process``)."""
    import threading
    import time

    from tools import interrupt

    ops = ops_factory(tree, [])
    started = time.monotonic()
    result = ops._run_rg_native(["sh", "-c", "'sleep 30'"], 5, timeout=1)
    assert result.exit_code == 124 and time.monotonic() - started < 5

    tid = threading.get_ident()
    threading.Timer(0.3, lambda: interrupt.set_interrupt(True, tid)).start()
    try:
        started = time.monotonic()
        result = ops._run_rg_native(["sh", "-c", "'sleep 30'"], 5, timeout=30)
    finally:
        interrupt.set_interrupt(False, tid)
    assert result.exit_code == 130 and time.monotonic() - started < 5


def test_kill_switch_routes_search_back_to_the_shell(tree, ops_factory, monkeypatch):
    monkeypatch.setenv("HERMES_NATIVE_FILE_READ", "0")
    calls = []
    result = ops_factory(tree, calls).search(pattern="needle", path=str(tree))
    assert result.total_count == 4
    assert any(c.startswith("test -e") for c in calls)
    assert any(c.startswith("set -o pipefail; ") for c in calls)


def test_limit_hit_keeps_drained_matches_when_group_kill_is_refused(tree, ops_factory, monkeypatch):
    """Reaching ``limit`` takes the early-stop branch that TERMs rg's group. macOS answers
    ``killpg`` with EPERM (not ESRCH) for a zombie-only group; that must not surface as a tool
    error nor discard the matches already drained (#116855)."""
    import os

    monkeypatch.setenv("HERMES_NATIVE_FILE_READ", "1")
    ops = ops_factory(tree, [])
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: (_ for _ in ()).throw(PermissionError(1, "Operation not permitted")))
    result = ops.search(pattern="needle", path=str(tree), limit=2)
    assert not result.error and len(result.matches) == 2, result.to_dict()


def test_local_search_runs_the_pinned_ripgrep_not_the_first_on_path(_local_env, tmp_path, monkeypatch):
    """The terminal PATH puts the user's tools first; Hermes's own search still runs PM's rg."""
    import pm
    from pm.install import InstalledPackage

    pinned = tmp_path / "store" / "ripgrep" / "rg"
    pinned.parent.mkdir(parents=True)
    pinned.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(pm, "installed_package",
                        lambda name: InstalledPackage(pinned.parent, "15", pinned) if name == "ripgrep" else None)
    ops = ShellFileOperations(_local_env, cwd=str(tmp_path))
    assert ops._resolve_command("rg") == str(pinned).replace("\\", "/")


def test_rg_exiting_between_poll_and_group_kill_is_not_a_tool_error(tree, ops_factory, monkeypatch):
    """rg closes stdout as it exits, so the drainer can finish while rg is mid-exit:
    ``poll()`` still reports it running, but ``getpgid`` already answers ESRCH (macOS does
    this for exiting/zombie processes). Under load this made single-file searches fail with
    ``[Errno 3] No such process`` almost every time; the search must still succeed."""
    import subprocess

    from tools.environments import local

    monkeypatch.setenv("HERMES_NATIVE_FILE_READ", "1")
    ops = ops_factory(tree, [])
    real_poll = subprocess.Popen.poll
    polls = {"n": 0}

    def poll_reports_running_once(self):
        # Only rg's own process: the shell lane's ``command -v rg`` probe polls too.
        if "rg" in str(self.args) and "command -v" not in str(self.args):
            polls["n"] += 1
            if polls["n"] == 1:
                return None
        return real_poll(self)

    def group_already_gone(proc):
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(subprocess.Popen, "poll", poll_reports_running_once)
    monkeypatch.setattr(local, "_kill_process_group_posix", group_already_gone)
    result = ops.search(pattern="needle", path=str(tree / "a.py"))
    assert not result.error, result.to_dict()
    assert polls["n"] >= 1
    assert result.total_count == 2, result.to_dict()


def test_search_and_terminal_survive_a_deleted_session_cwd(tree, ops_factory, _local_env, tmp_path, monkeypatch):
    """A prior command ``rm -rf``'d the session cwd. The native rg lane used to Popen with
    the dead cwd (ENOENT on every search), and the shell wrapper still ``cd``'d into it
    (exit 126 on every command) because the cwd was healed only after the wrapper was
    built. Both must fall back to a surviving parent and keep working."""
    import shutil

    monkeypatch.setenv("HERMES_NATIVE_FILE_READ", "1")
    ops = ops_factory(tree, [])
    # Warm the ``command -v rg`` cache first: that probe runs through the shell lane,
    # which would heal the cwd before the native lane ever saw it.
    assert not ops.search(pattern="needle", path=str(tree / "a.py")).error
    doomed = tmp_path / "doomed"
    doomed.mkdir()
    _local_env.cwd = str(doomed)
    shutil.rmtree(doomed)

    result = ops.search(pattern="needle", path=str(tree / "a.py"))
    assert not result.error and result.total_count == 2, result.to_dict()

    _local_env.cwd = str(doomed)  # dead again; this time the shell lane goes first
    out = type(_local_env).execute(_local_env, "echo alive")
    assert out["returncode"] == 0 and "alive" in out["output"], out
    assert os.path.isdir(_local_env.cwd)
