import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from portacode.connection import terminal
from portacode.connection.handlers.registry import CommandRegistry


@pytest.mark.asyncio
async def test_registry_coalesces_concurrent_system_info_dispatches():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    class BlockingHandler:
        async def handle(self, _message, _reply_channel):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()

    registry = CommandRegistry(AsyncMock(), {})
    registry._handlers["system_info"] = BlockingHandler()

    assert await registry.dispatch("system_info", {}, None)
    await asyncio.wait_for(started.wait(), timeout=0.1)
    assert await registry.dispatch("system_info", {}, None)
    assert calls == 1

    inflight = registry._inflight_dispatches["system_info"]
    release.set()
    await asyncio.wait_for(inflight, timeout=0.1)


@pytest.mark.asyncio
async def test_periodic_system_info_uses_shared_registry(monkeypatch):
    manager = object.__new__(terminal.TerminalManager)
    manager._client_session_manager = SimpleNamespace(
        has_interested_clients=lambda: True
    )
    manager._command_registry = SimpleNamespace(dispatch=AsyncMock(return_value=True))
    monkeypatch.setattr(terminal, "SYSTEM_INFO_INTERVAL_S", 0.001)
    periodic = asyncio.create_task(manager._periodic_system_info())

    try:
        async def wait_for_dispatch():
            while not manager._command_registry.dispatch.await_count:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(wait_for_dispatch(), timeout=0.1)
        manager._command_registry.dispatch.assert_awaited_with(
            "system_info", {}, None
        )
    finally:
        periodic.cancel()
        with pytest.raises(asyncio.CancelledError):
            await periodic
