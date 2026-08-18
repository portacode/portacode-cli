import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from portacode.connection import terminal
from portacode.connection.handlers.system_handlers import SystemInfoHandler


def _manager_with_interested_client():
    manager = object.__new__(terminal.TerminalManager)
    manager._client_session_manager = SimpleNamespace(
        has_interested_clients=lambda: True
    )
    manager._control_channel = object()
    manager._context = {}
    manager._send_session_aware = AsyncMock()
    return manager


@pytest.mark.asyncio
async def test_slow_system_info_does_not_block_loop_or_start_duplicate_collection(
    monkeypatch,
):
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def blocking_execute(_self, _message):
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=1)
        return {"event": "system_info", "info": {}}

    monkeypatch.setattr(SystemInfoHandler, "execute", blocking_execute)
    monkeypatch.setattr(terminal, "SYSTEM_INFO_INTERVAL_S", 0.001)
    monkeypatch.setattr(terminal, "SYSTEM_INFO_COLLECTION_TIMEOUT_S", 0.005)
    manager = _manager_with_interested_client()
    periodic = asyncio.create_task(manager._periodic_system_info())

    try:
        assert await asyncio.to_thread(started.wait, 0.2)
        # If collection ran on the event loop, this timer could not fire while
        # blocking_execute waits. Several timeout cycles must still reuse the
        # same in-flight worker.
        await asyncio.wait_for(asyncio.sleep(0.02), timeout=0.05)
        assert calls == 1
        manager._send_session_aware.assert_not_awaited()
    finally:
        release.set()
        periodic.cancel()
        with pytest.raises(asyncio.CancelledError):
            await periodic


@pytest.mark.asyncio
async def test_completed_system_info_is_sent(monkeypatch):
    payload = {"event": "system_info", "info": {"proxmox": {}}}
    monkeypatch.setattr(SystemInfoHandler, "execute", lambda _self, _message: payload)
    monkeypatch.setattr(terminal, "SYSTEM_INFO_INTERVAL_S", 0.001)
    monkeypatch.setattr(terminal, "SYSTEM_INFO_COLLECTION_TIMEOUT_S", 0.1)
    manager = _manager_with_interested_client()
    periodic = asyncio.create_task(manager._periodic_system_info())

    try:
        async def wait_for_send():
            while not manager._send_session_aware.await_count:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(wait_for_send(), timeout=0.2)
        manager._send_session_aware.assert_awaited_with(payload)
    finally:
        periodic.cancel()
        with pytest.raises(asyncio.CancelledError):
            await periodic
