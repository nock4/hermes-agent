"""Small helpers for short user continuations like ``proceed``."""

from __future__ import annotations

import re
import string
import time
from typing import Any, Dict, Iterable, Optional


_EXPLICIT_CONTINUATION_PATTERNS = (
    re.compile(r"^(?:ok(?:ay)?[,\s]+)?(?:please\s+)?proceed$", re.IGNORECASE),
    re.compile(r"^(?:ok(?:ay)?[,\s]+)?(?:please\s+)?continue$", re.IGNORECASE),
    re.compile(r"^(?:ok(?:ay)?[,\s]+)?(?:let'?s\s+)?(?:proceed|continue)$", re.IGNORECASE),
    re.compile(r"^(?:ok(?:ay)?[,\s]+)?go\s+ahead$", re.IGNORECASE),
    re.compile(r"^(?:ok(?:ay)?[,\s]+)?do\s+it$", re.IGNORECASE),
    re.compile(r"^(?:ok(?:ay)?[,\s]+)?carry\s+on$", re.IGNORECASE),
    re.compile(r"^(?:ok(?:ay)?[,\s]+)?make\s+it\s+happen$", re.IGNORECASE),
)

_GENERIC_AFFIRMATIONS = {
    "yes",
    "yep",
    "yup",
    "yeah",
    "sure",
    "ok",
    "okay",
    "approved",
    "sounds good",
    "looks good",
    "sgtm",
}

_PLAN_MARKERS = (
    "plan",
    "approach",
    "proposal",
    "proposed",
    "next steps",
    "implementation",
    "i will",
    "i'll",
    "i would",
    "i can",
    "going to",
)

_APPROVAL_MARKERS = (
    "proceed",
    "continue",
    "go ahead",
    "approve",
    "approval",
    "confirm",
    "ready",
)

_NUMBERED_LINE_RE = re.compile(r"^\s*\d+[\.)]\s+\S+", re.MULTILINE)
_BULLET_LINE_RE = re.compile(r"^\s*[-*]\s+\S+", re.MULTILINE)
_GATEWAY_REPLY_PREFIX_RE = re.compile(
    r'^\[Replying to:\s+"(?P<reply>(?:\\.|[^"\\])*)"\]\s*\n\n(?P<body>[\s\S]*)$'
)

MAX_PENDING_ACTION_CHARS = 12_000


def text_from_message_content(content: Any) -> str:
    """Extract text from a plain string or OpenAI-style content parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in {"text", "input_text"}:
                    parts.append(str(part.get("text", "")))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(p for p in parts if p)
    return ""


def _normalize_reply(text: str) -> str:
    normalized = text.strip().lower()
    normalized = normalized.replace("\u2019", "'")
    normalized = normalized.strip(string.whitespace + string.punctuation)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def classify_short_continuation_reply(content: Any) -> Optional[str]:
    """Return ``explicit`` or ``generic`` for short approval-like replies."""
    text = text_from_message_content(content)
    if not text:
        return None
    normalized = _normalize_reply(text)
    if not normalized:
        return None
    # Keep this intentionally narrow. "continue explaining" should remain a
    # normal user request, while "ok, let's proceed" should approve the prior
    # assistant plan.
    if len(normalized.split()) > 6:
        return None
    for pattern in _EXPLICIT_CONTINUATION_PATTERNS:
        if pattern.match(normalized):
            return "explicit"
    if normalized in _GENERIC_AFFIRMATIONS:
        return "generic"
    return None


def split_gateway_reply_context(content: Any) -> tuple[Optional[str], str]:
    """Return (quoted_text, body) for gateway-injected reply context.

    Messaging gateways prepend reply context as ``[Replying to: "..."]``.
    For continuity, the body may still be a short approval ("proceed") even
    though the full API-facing string is no longer short.
    """
    text = text_from_message_content(content)
    if not text:
        return None, ""
    match = _GATEWAY_REPLY_PREFIX_RE.match(text)
    if not match:
        return None, text
    reply = match.group("reply").replace(r"\"", '"').strip()
    return (reply or None), match.group("body")


def looks_like_actionable_plan(content: Any) -> bool:
    """Heuristic for assistant responses that likely await user approval."""
    text = text_from_message_content(content).strip()
    if len(text) < 40:
        return False
    lowered = text.lower()
    if "proceed with what" in lowered:
        return False

    marker_count = sum(1 for marker in _PLAN_MARKERS if marker in lowered)
    approval_count = sum(1 for marker in _APPROVAL_MARKERS if marker in lowered)
    list_count = len(_NUMBERED_LINE_RE.findall(text)) + len(_BULLET_LINE_RE.findall(text))

    if marker_count and list_count >= 2:
        return True
    if marker_count and approval_count:
        return True
    if approval_count and list_count >= 2:
        return True
    if re.search(r"\b(?:should|shall)\s+i\s+(?:proceed|continue|go ahead)\b", lowered):
        return True
    return False


def find_recent_assistant_anchor(messages: Iterable[Dict[str, Any]]) -> Optional[str]:
    """Return the latest visible assistant text from a message history."""
    for msg in reversed(list(messages or [])):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = text_from_message_content(msg.get("content")).strip()
        if content and content != "(empty)":
            return content
    return None


def build_pending_action_payload(
    assistant_content: Any,
    *,
    source: str = "assistant_response",
) -> Optional[Dict[str, Any]]:
    """Build a JSON-serializable pending action payload, if content qualifies."""
    content = text_from_message_content(assistant_content).strip()
    if not looks_like_actionable_plan(content):
        return None
    if len(content) > MAX_PENDING_ACTION_CHARS:
        content = content[-MAX_PENDING_ACTION_CHARS:]
    return {
        "version": 1,
        "source": source,
        "assistant_content": content,
        "created_at": time.time(),
    }


def pending_action_content(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    content = payload.get("assistant_content") or payload.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    return None


def build_continuity_user_message(
    user_text: str,
    assistant_anchor: str,
    *,
    source_label: str = "previous assistant message",
) -> str:
    """Create API-only context for a short continuation reply."""
    anchor = assistant_anchor.strip()
    if len(anchor) > MAX_PENDING_ACTION_CHARS:
        anchor = anchor[-MAX_PENDING_ACTION_CHARS:]
    return (
        "[Continuity note: The user's short reply below approves or continues "
        f"from the {source_label}. Treat that prior assistant message as the "
        "active plan/context and continue from it. Do not ask what to proceed "
        "with unless the prior message is genuinely unrelated.]\n\n"
        f"Prior assistant message:\n{anchor}\n\n"
        f"User reply:\n{user_text}"
    )
