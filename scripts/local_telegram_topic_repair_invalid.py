#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from ruamel.yaml import YAML


ROOT = Path("/Users/nickgeorge-studio/.hermes")
FRONTPAGE = ROOT / "profiles" / "frontpage"
REPO = Path("/Users/nickgeorge-studio/Projects/hermes/hermes-agent")
CHAT_ID = "2142213564"

yaml = YAML()
yaml.preserve_quotes = True
yaml.default_flow_style = False


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.load(f) or {}


def dump_yaml(path: Path, data: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f)


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def replace_env_thread_ids(path: Path, replacements: dict[str, str]) -> bool:
    if not path.exists():
        return False
    changed = False
    out: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key, value = stripped.split("=", 1)
        raw_value = value.strip().strip('"').strip("'")
        replacement = replacements.get(raw_value)
        if replacement is None:
            out.append(line)
            continue
        out.append(f"{key.strip()}={replacement}")
        changed = True
    if changed:
        path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    return changed


def telegram_token() -> str:
    for env_path in (ROOT / ".env", FRONTPAGE / ".env"):
        token = load_env(env_path).get("TELEGRAM_BOT_TOKEN", "")
        if token:
            return token
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if token:
        return token
    raise RuntimeError("TELEGRAM_BOT_TOKEN was not found")


def telegram_api(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{telegram_token()}/{method}"
    response = requests.post(url, json=payload, timeout=20)
    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f"{method} returned non-JSON status {response.status_code}") from exc
    if not data.get("ok"):
        raise RuntimeError(f"{method} failed: {data.get('description') or data}")
    return data["result"]


def create_topic(name: str) -> int:
    result = telegram_api("createForumTopic", {"chat_id": int(CHAT_ID), "name": name})
    thread_id = result.get("message_thread_id")
    if not thread_id:
        raise RuntimeError(f"createForumTopic for {name!r} returned no message_thread_id")
    return int(thread_id)


def rename_topic(thread_id: int, name: str) -> tuple[bool, str | None]:
    try:
        telegram_api(
            "editForumTopic",
            {"chat_id": int(CHAT_ID), "message_thread_id": int(thread_id), "name": name},
        )
        return True, None
    except Exception as exc:
        return False, str(exc)


def configured_topics(config: dict[str, Any]) -> list[dict[str, Any]]:
    dm_topics = (
        config.get("platforms", {})
        .get("telegram", {})
        .get("extra", {})
        .get("dm_topics", [])
    )
    topics: list[dict[str, Any]] = []
    for chat_entry in dm_topics if isinstance(dm_topics, list) else []:
        if not isinstance(chat_entry, dict) or str(chat_entry.get("chat_id")) != CHAT_ID:
            continue
        for topic in chat_entry.get("topics", []) or []:
            if isinstance(topic, dict) and topic.get("name") and topic.get("thread_id") is not None:
                topics.append(topic)
    return topics


def update_config_topics(config: dict[str, Any], replacements: dict[str, str]) -> bool:
    changed = False
    for topic in configured_topics(config):
        old = str(topic.get("thread_id"))
        if old in replacements:
            topic["thread_id"] = int(replacements[old])
            changed = True
    return changed


def update_cron_threads(path: Path, replacements: dict[str, str]) -> bool:
    data = load_json(path)
    changed = False
    for job in data.get("jobs", []):
        origin = job.get("origin")
        if isinstance(origin, dict):
            old = str(origin.get("thread_id")) if origin.get("thread_id") is not None else None
            if old in replacements:
                origin["thread_id"] = replacements[old]
                changed = True
        deliver = job.get("deliver")
        if isinstance(deliver, str):
            updated = deliver
            for old, new in replacements.items():
                updated = updated.replace(f":{old}", f":{new}")
            if updated != deliver:
                job["deliver"] = updated
                changed = True
    if changed:
        data["updated_at"] = datetime.now().astimezone().isoformat()
        dump_json(path, data)
    return changed


def rebuild_channel_directory(home: Path) -> dict[str, Any]:
    code = (
        "import asyncio, json; "
        "from gateway.channel_directory import build_channel_directory; "
        "d = asyncio.run(build_channel_directory({})); "
        "print(json.dumps({'home': %r, 'updated_at': d.get('updated_at'), "
        "'telegram_entries': len(d.get('platforms', {}).get('telegram', []))}))"
    ) % str(home)
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def main() -> None:
    config_paths = [ROOT / "config.yaml", FRONTPAGE / "config.yaml"]
    configs = {path: load_yaml(path) for path in config_paths}
    seen: set[tuple[str, str]] = set()
    replacements: dict[str, str] = {}
    checked: dict[str, Any] = {}
    errors: dict[str, str] = {}

    for config in configs.values():
        for topic in configured_topics(config):
            name = str(topic["name"])
            old_thread = str(topic["thread_id"])
            key = (name, old_thread)
            if key in seen or old_thread in replacements:
                continue
            seen.add(key)
            ok, err = rename_topic(int(old_thread), name)
            if ok:
                checked[old_thread] = {"name": name, "status": "valid"}
                continue
            if err and "TOPIC_ID_INVALID" in err:
                new_thread = str(create_topic(name))
                replacements[old_thread] = new_thread
                checked[old_thread] = {
                    "name": name,
                    "status": "replaced",
                    "new_thread_id": new_thread,
                }
            else:
                errors[old_thread] = err or "unknown error"

    changed_files: list[str] = []
    if replacements:
        for path, config in configs.items():
            if update_config_topics(config, replacements):
                dump_yaml(path, config)
                changed_files.append(str(path))
        for env_path in (ROOT / ".env", FRONTPAGE / ".env"):
            if replace_env_thread_ids(env_path, replacements):
                changed_files.append(str(env_path))
        for cron_path in (ROOT / "cron" / "jobs.json", FRONTPAGE / "cron" / "jobs.json"):
            if update_cron_threads(cron_path, replacements):
                changed_files.append(str(cron_path))

    rebuilt = [rebuild_channel_directory(ROOT), rebuild_channel_directory(FRONTPAGE)]

    print(json.dumps(
        {
            "checked": checked,
            "errors": errors,
            "replacements": replacements,
            "changed_files": changed_files,
            "rebuilt_channel_directories": rebuilt,
        },
        indent=2,
        sort_keys=True,
    ))


if __name__ == "__main__":
    main()
