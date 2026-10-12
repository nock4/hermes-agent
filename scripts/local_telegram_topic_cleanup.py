#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from ruamel.yaml import YAML


ROOT = Path("/Users/nickgeorge-studio/.hermes")
FRONTPAGE = ROOT / "profiles" / "frontpage"
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


def set_env_values(path: Path, updates: dict[str, str]) -> None:
    lines = []
    seen: set[str] = set()
    if path.exists():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


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


def find_topic(config: dict[str, Any], name: str) -> dict[str, Any] | None:
    dm_topics = (
        config.get("platforms", {})
        .get("telegram", {})
        .get("extra", {})
        .get("dm_topics", [])
    )
    for chat_entry in dm_topics:
        if str(chat_entry.get("chat_id")) != CHAT_ID:
            continue
        for topic in chat_entry.get("topics", []) or []:
            if topic.get("name") == name:
                return topic
    return None


def configure_topics(config: dict[str, Any], topics: list[dict[str, Any]]) -> None:
    platforms = config.setdefault("platforms", {})
    telegram = platforms.setdefault("telegram", {})
    extra = telegram.setdefault("extra", {})
    extra["ignore_root_dm"] = True
    existing = extra.setdefault("dm_topics", [])
    chat_entry = None
    for entry in existing:
        if str(entry.get("chat_id")) == CHAT_ID:
            chat_entry = entry
            break
    if chat_entry is None:
        chat_entry = {"chat_id": int(CHAT_ID), "topics": []}
        existing.append(chat_entry)
    chat_entry["chat_id"] = int(CHAT_ID)
    chat_entry["topics"] = topics


def update_cron_jobs(path: Path, target_by_id: dict[str, tuple[str, str]], deliver_by_id: dict[str, str]) -> None:
    data = load_json(path)
    for job in data.get("jobs", []):
        job_id = job.get("id")
        if job_id in target_by_id:
            _, thread_id = target_by_id[job_id]
            origin = job.get("origin") or {}
            origin.update(
                {
                    "platform": "telegram",
                    "chat_id": CHAT_ID,
                    "chat_name": "Nick",
                    "thread_id": str(thread_id),
                }
            )
            job["origin"] = origin
        if job_id in deliver_by_id:
            job["deliver"] = deliver_by_id[job_id]
    data["updated_at"] = datetime.now().astimezone().isoformat()
    dump_json(path, data)


def update_sessions_topics(path: Path, topic_labels: dict[str, str]) -> None:
    data = load_json(path)
    for session in data.values():
        if not isinstance(session, dict):
            continue
        origin = session.get("origin") or {}
        thread_id = origin.get("thread_id")
        if thread_id is not None and str(thread_id) in topic_labels:
            origin["chat_topic"] = topic_labels[str(thread_id)]
            session["origin"] = origin
    dump_json(path, data)


def update_channel_directory(path: Path, topic_labels: dict[str, str]) -> None:
    data = load_json(path)
    data.setdefault("platforms", {}).setdefault("telegram", [])
    entries = data["platforms"]["telegram"]
    by_id = {entry.get("id"): entry for entry in entries if isinstance(entry, dict)}
    for thread_id, label in topic_labels.items():
        entry_id = f"{CHAT_ID}:{thread_id}"
        entry = by_id.get(entry_id)
        if entry is None:
            entry = {"id": entry_id, "type": "dm", "thread_id": str(thread_id)}
            entries.append(entry)
            by_id[entry_id] = entry
        entry["name"] = f"Nick / {label}"
        entry["type"] = "dm"
        entry["thread_id"] = str(thread_id)
    data["updated_at"] = datetime.now().isoformat()
    dump_json(path, data)


def main() -> None:
    root_config_path = ROOT / "config.yaml"
    frontpage_config_path = FRONTPAGE / "config.yaml"
    root_config = load_yaml(root_config_path)
    frontpage_config = load_yaml(frontpage_config_path)

    created: dict[str, int] = {}
    root_hermes_ops = find_topic(root_config, "Hermes Ops")
    hermes_ops_thread = (
        int(root_hermes_ops["thread_id"])
        if root_hermes_ops and root_hermes_ops.get("thread_id")
        else None
    )
    if hermes_ops_thread is None:
        hermes_ops_thread = create_topic("Hermes Ops")
        created["Hermes Ops"] = hermes_ops_thread

    root_frontpage = find_topic(root_config, "Frontpage")
    frontpage_thread = (
        int(root_frontpage["thread_id"])
        if root_frontpage and root_frontpage.get("thread_id")
        else None
    )
    if frontpage_thread is None:
        frontpage_thread = create_topic("Frontpage")
        created["Frontpage"] = frontpage_thread

    root_topics = [
        {"name": "General", "thread_id": 222709},
        {"name": "System / Cron", "thread_id": 222720},
        {"name": "Hermes Ops", "thread_id": hermes_ops_thread},
        {"name": "Research", "thread_id": 222712, "skill": "knowledge-base"},
        {"name": "Vault / Mind Map", "thread_id": 222733, "skill": "daily-inbox-filing"},
        {"name": "Email / Comms", "thread_id": 224122},
        {"name": "Images / Media", "thread_id": 222713},
        {"name": "Swagbound Ops", "thread_id": 223193},
        {"name": "Frontpage", "thread_id": frontpage_thread},
        {"name": "Dryad", "thread_id": 222710},
        {"name": "Chelmsford", "thread_id": 225730},
        {"name": "Source Ingest / Canon", "thread_id": 236632},
    ]
    configure_topics(root_config, root_topics)
    configure_topics(frontpage_config, [{"name": "Frontpage", "thread_id": frontpage_thread}])
    dump_yaml(root_config_path, root_config)
    dump_yaml(frontpage_config_path, frontpage_config)

    set_env_values(
        ROOT / ".env",
        {
            "TELEGRAM_HOME_CHANNEL_THREAD_ID": "222720",
            "TELEGRAM_CRON_THREAD_ID": "222720",
        },
    )
    set_env_values(
        FRONTPAGE / ".env",
        {
            "TELEGRAM_HOME_CHANNEL_THREAD_ID": str(frontpage_thread),
            "TELEGRAM_CRON_THREAD_ID": str(frontpage_thread),
        },
    )

    root_targets = {
        "9cc606e9a2c3": ("Vault / Mind Map", "222733"),
        "5b1472952d0a": ("Vault / Mind Map", "222733"),
        "c912ece044bc": ("Vault / Mind Map", "222733"),
        "93ac2f1b878a": ("Vault / Mind Map", "222733"),
        "dbda0b874deb": ("Email / Comms", "224122"),
        "5596a6091d0b": ("Research", "222712"),
        "2ef707d0a467": ("Chelmsford", "225730"),
        "3d1ca6f53bc9": ("Chelmsford", "225730"),
        "c267bcf9ba82": ("Swagbound Ops", "223193"),
        "32d6e64727ae": ("Swagbound Ops", "223193"),
        "aaf29fdf8b0e": ("Swagbound Ops", "223193"),
        "cea19b438114": ("Vault / Mind Map", "222733"),
        "776d7d424877": ("Hermes Ops", str(hermes_ops_thread)),
        "cf65e57af33b": ("Hermes Ops", str(hermes_ops_thread)),
        "fc6ea176fe0a": ("Hermes Ops", str(hermes_ops_thread)),
        "cc444cefed7e": ("Swagbound Ops", "223193"),
    }
    root_deliver = {
        "aaf29fdf8b0e": "telegram:2142213564:223193",
        "cea19b438114": "telegram:2142213564:222733",
        "776d7d424877": f"telegram:2142213564:{hermes_ops_thread}",
        "cf65e57af33b": f"telegram:2142213564:{hermes_ops_thread}",
    }
    update_cron_jobs(ROOT / "cron" / "jobs.json", root_targets, root_deliver)
    update_cron_jobs(
        FRONTPAGE / "cron" / "jobs.json",
        {"196820c9adb6": ("Frontpage", str(frontpage_thread))},
        {},
    )

    topic_labels = {
        "222709": "General",
        "222710": "Dryad",
        "222712": "Research",
        "222713": "Images / Media",
        "222720": "System / Cron",
        "222733": "Vault / Mind Map",
        "222705": "Driving Calls",
        "223193": "Swagbound Ops",
        "224122": "Email / Comms",
        "225730": "Chelmsford",
        "232903": "Swagbound Capture (legacy)",
        "236632": "Source Ingest / Canon",
        str(hermes_ops_thread): "Hermes Ops",
        str(frontpage_thread): "Frontpage",
    }
    update_sessions_topics(ROOT / "sessions" / "sessions.json", topic_labels)
    update_channel_directory(ROOT / "channel_directory.json", topic_labels)
    update_channel_directory(FRONTPAGE / "channel_directory.json", {str(frontpage_thread): "Frontpage"})

    renamed: dict[str, str] = {}
    rename_errors: dict[str, str] = {}
    for tid, label in topic_labels.items():
        ok, err = rename_topic(int(tid), label)
        if ok:
            renamed[tid] = label
        else:
            rename_errors[tid] = err or "unknown error"

    print(
        json.dumps(
            {
                "created": created,
                "renamed": renamed,
                "rename_errors": rename_errors,
                "frontpage_thread": frontpage_thread,
                "hermes_ops_thread": hermes_ops_thread,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
