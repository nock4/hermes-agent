"""A loop-agnostic auxiliary client (``HERMES_SKIP_ASYNC_WRAP``) is shared across event loops.

The DirectSDK provider's client runs each request in its own subprocess and declares that it owns
no loop-bound transport. Its ``close()`` cancels every in-flight request, so evicting it when a
caller arrives from another loop (each vision tool call runs on its own loop) cancelled a
concurrent session's request: "Claude request cancelled" whenever two sessions used vision at once.
"""

import asyncio
import threading
from unittest.mock import patch

import pytest

import agent.auxiliary_client as aux
from tests.waiting import wait_until


class _LoopAgnosticClient:
    """Mirrors the DirectSDK contract: no async wrapper, close() cancels in-flight requests."""

    HERMES_SKIP_ASYNC_WRAP = True

    def __init__(self):
        self.api_key = "external-process"
        self.base_url = "process://loop-agnostic"
        self.closed = threading.Event()
        self.started = threading.Barrier(2, timeout=5)

    def close(self):
        self.closed.set()

    def request(self):
        """One in-flight call: waits for the sibling to start, then fails if the client was closed."""
        self.started.wait()
        return "cancelled" if self.closed.is_set() else "ok"


@pytest.fixture(autouse=True)
def _clean_client_cache():
    aux._client_cache.clear()
    yield
    aux._client_cache.clear()


def _call_on_own_loop(results, name):
    async def run():
        client, _ = aux._get_cached_client("loop-agnostic", "m1", async_mode=True)
        results[name] = (client, await asyncio.to_thread(client.request))

    asyncio.run(run())


def test_concurrent_callers_on_different_loops_share_and_do_not_close_the_client():
    built = []

    def resolve(*_args, **_kwargs):
        built.append(_LoopAgnosticClient())
        return built[-1], "m1"

    results = {}
    with patch("agent.auxiliary_client.resolve_provider_client", side_effect=resolve):
        first = threading.Thread(target=_call_on_own_loop, args=(results, "a"))
        first.start()
        # The second caller arrives on another loop while the first request is in flight.
        wait_until(lambda: aux._client_cache, what="first client cached")
        second = threading.Thread(target=_call_on_own_loop, args=(results, "b"))
        second.start()
        first.join(10)
        second.join(10)

    assert len(built) == 1
    assert results["a"] == (built[0], "ok")
    assert results["b"] == (built[0], "ok")
    assert not built[0].closed.is_set()
