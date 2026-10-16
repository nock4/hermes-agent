"""Ephemeral private-topic presence driven by the existing work/typing lifecycle.

Not a timer of its own: approval waits, finished turns and stopped background work
stop calling refresh. Telegram expires an unsent draft after about 30 seconds.
Use the same lane for streamed text so a blank activity frame cannot erase it.
"""
import asyncio
import secrets
import time
from dataclasses import dataclass, field


@dataclass
class _Preview:
    draft_id: int
    content: str = ""
    last_sent: float = float("-inf")
    last_used: float = 0.0
    retry_after: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class TopicActivity:
    interval = 10.0

    def __init__(self):
        self.previews = {}

    @staticmethod
    def key(adapter, chat_id, metadata):
        if not adapter.config.typing_indicator:
            return None
        if not adapter._coerce_bool_extra("topic_activity_preview", True):
            return None
        if not adapter._dm_topic_fallback(metadata):
            return None
        if not adapter.supports_draft_streaming(chat_type="dm", metadata=metadata):
            return None
        try:
            thread = adapter._message_thread_id_for_send(adapter._metadata_thread_id(metadata))
            # Never infer a group or a missing topic from a user/chat label.
            if int(chat_id) <= 0 or thread is None or thread <= 0:
                return None
        except (ValueError, TypeError):
            return None
        return str(chat_id), str(thread)

    def _state(self, key, draft_id=None):
        now = time.monotonic()
        # Bound history even for background-only topics that have no stop hook.
        for old_key, old in list(self.previews.items()):
            if now - old.last_used > 120 and not old.lock.locked():
                self.previews.pop(old_key, None)
        if key not in self.previews:
            self.previews[key] = _Preview(
                draft_id=draft_id if draft_id is not None else secrets.randbelow(2**31 - 1) + 1)
        state = self.previews[key]
        state.last_used = now
        return state

    def forget(self, chat_id, metadata=None):
        thread = (metadata or {}).get("thread_id")
        for key in list(self.previews):
            if key[0] == str(chat_id) and (thread is None or key[1] == str(thread)):
                self.previews.pop(key, None)

    async def refresh(self, adapter, chat_id, metadata):
        key = self.key(adapter, chat_id, metadata)
        if key is None:
            return
        state = self._state(key)
        async with state.lock:
            paused = getattr(adapter, "_typing_paused", set())
            if (self.previews.get(key) is not state or key in paused
                    or str(chat_id) in paused):
                return
            now = time.monotonic()
            if now < state.retry_after or now - state.last_sent < self.interval:
                return
            # Set before await: timeout/cancellation cannot create a retry storm.
            state.retry_after = now + self.interval
            result = await adapter._send_draft_frame(chat_id, state.draft_id, state.content, metadata)
            if result.success:
                state.last_sent = time.monotonic()
            else:
                state.retry_after = time.monotonic() + 60.0

    async def stream(self, adapter, chat_id, draft_id, content, metadata):
        key = self.key(adapter, chat_id, metadata)
        if key is None:
            return await adapter._send_draft_frame(chat_id, draft_id, content, metadata)
        state = self._state(key, draft_id)
        async with state.lock:
            # A stop/final send may have invalidated a frame waiting for the lock.
            if self.previews.get(key) is not state:
                return await adapter._send_draft_frame(chat_id, draft_id, content, metadata)
            result = await adapter._send_draft_frame(chat_id, state.draft_id, content, metadata)
            if result.success:
                state.content = content
                state.last_sent = time.monotonic()
                state.retry_after = 0.0
            else:
                state.retry_after = time.monotonic() + 60.0
            return result
