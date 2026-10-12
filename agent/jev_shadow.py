"""Opt-in metadata-only shadow sidecar; never owns dispatch or runtime objects."""
import hashlib
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from hermes_constants import get_hermes_home

_slot = threading.BoundedSemaphore(1)
_cooldown_until = 0.0


def _record(home, event):
    directory = home / "jev-shadow" / "events"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    import uuid
    with (directory / (str(uuid.uuid4()) + ".json")).open("x") as stream:
        json.dump(event, stream)


def _work(home, cfg, event):
    global _cooldown_until
    try:
        import os
        import signal
        with subprocess.Popen(
            [sys.executable, "-B", str(Path(cfg["bridge"]).resolve()), "event", "--home", str(home)],
            stdin=subprocess.PIPE, text=True, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        ) as child:
            try:
                child.communicate(json.dumps(event), timeout=min(30, max(1, float(cfg.get("timeout_seconds", 20)))))
            except BaseException:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
                raise
            if child.returncode:
                raise subprocess.CalledProcessError(child.returncode, "jev-shadow")
    except Exception as exc:
        _cooldown_until = time.monotonic() + 60
        event.update(status="hook_timeout" if isinstance(exc, subprocess.TimeoutExpired) else "hook_error")
        try:
            _record(home, event)
        except Exception:
            pass  # Logging must not own task lifecycle either.
    finally:
        _slot.release()


def observe(task, *, kind, actual_model, actual_provider=None, locked=False):
    """Hash task in memory; only metadata crosses the sidecar boundary. No context argument."""
    try:
        home = Path(get_hermes_home()).resolve()
        if home.parent.name == "profiles":
            return
        import yaml
        cfg = (yaml.safe_load((home / "config.yaml").read_text()) or {}).get("extra", {}).get("jev_shadow", {})
        if cfg.get("enabled") is not True:
            return
        if not isinstance(task, str) or kind not in ("delegate", "background"):
            return
        def identifier(value):
            return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_./:-]{1,120}", value) else None
        event = {"task_hash": hashlib.sha256(task.encode()).hexdigest(), "kind": kind,
                 "actual_model": identifier(actual_model), "actual_provider": identifier(actual_provider),
                 "locked": bool(locked), "mode": "SHADOW_ONLY", "executed": False, "timestamp": time.time()}
        if time.monotonic() < _cooldown_until:
            event["status"] = "circuit_open"
            _record(home, event)
            return
        if not _slot.acquire(blocking=False):
            event["status"] = "busy_skipped"
            _record(home, event)
            return
        try:
            threading.Thread(target=_work, args=(home, cfg, event), daemon=True, name="jev-shadow").start()
        except Exception:
            _slot.release()
    except Exception:
        return  # Opt-in sidecar failures can never abort or reroute dispatch.
