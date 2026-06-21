"""Unit tests for the desired-state reconciler (_lifecycle_loop).

Drives the loop with faked _connect_once / _teardown_connection / _maintain
and asserts: idle startup (no auto-connect), connect on desired=True, teardown
(keep_tunnel=True) on desired=False, and reconnect after a drop while desired.
"""
from __future__ import annotations

import asyncio

from app import main as appmain
from app.state import state


async def _wait_for(pred, timeout: float = 2.0) -> None:
    async def _spin():
        while not pred():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(_spin(), timeout)


def _reset_state() -> None:
    state.desired_connected = False
    state.ready = False
    state.busy = False
    state.error = None
    state.user_action = None
    state.tunnel_died = False
    # asyncio primitives are loop-bound; recreate so each test binds to its own
    # loop (state is a module-level singleton shared across tests).
    state.wakeup = asyncio.Event()
    state.lifecycle_lock = asyncio.Lock()


async def test_idle_startup_does_not_connect(monkeypatch):
    _reset_state()

    async def fake_connect():
        raise AssertionError("must not connect while idle")
    monkeypatch.setattr(appmain, "_connect_once", fake_connect)

    task = asyncio.create_task(appmain._lifecycle_loop())
    try:
        # Idle for a moment; the loop should be blocked on wakeup.wait().
        await asyncio.sleep(0.1)
        assert state.ready is False
        assert state.desired_connected is False
    finally:
        state.desired_connected = False
        state.wakeup.set()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def test_connect_then_disconnect(monkeypatch):
    _reset_state()
    calls = {"connect": 0, "teardown": []}

    async def fake_connect():
        calls["connect"] += 1
        state.ready = True
    async def fake_teardown(keep_tunnel: bool = True):
        calls["teardown"].append(keep_tunnel)
        state.ready = False
    async def fake_maintain():
        # Block until something wakes us (desired flip).
        try:
            await asyncio.wait_for(state.wakeup.wait(), timeout=10)
            state.wakeup.clear()
        except asyncio.TimeoutError:
            pass

    monkeypatch.setattr(appmain, "_connect_once", fake_connect)
    monkeypatch.setattr(appmain, "_teardown_connection", fake_teardown)
    monkeypatch.setattr(appmain, "_maintain_until_drop", fake_maintain)

    # Set desired=True BEFORE starting the loop so it skips the idle wait
    # (the idle branch clears wakeup unconditionally, which would race a
    # pre-set event). With desired already True the loop goes straight to
    # connect.
    state.desired_connected = True
    task = asyncio.create_task(appmain._lifecycle_loop())
    try:
        await _wait_for(lambda: calls["connect"] == 1)
        assert state.ready is True

        # Disconnect -> teardown with keep_tunnel=True (tunnel preserved).
        state.desired_connected = False
        state.wakeup.set()
        await _wait_for(lambda: state.ready is False)
        assert True in calls["teardown"]  # keep_tunnel=True
    finally:
        state.desired_connected = False
        state.wakeup.set()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def test_drop_while_desired_reconnects(monkeypatch):
    _reset_state()
    connect_count = {"n": 0}

    async def fake_connect():
        connect_count["n"] += 1
        state.ready = True
    async def fake_teardown(keep_tunnel: bool = True):
        state.ready = False
    # First maintain call "drops" immediately (tunnel died); subsequent ones
    # block like the normal maintain phase.
    maintain_calls = {"n": 0}

    async def fake_maintain():
        maintain_calls["n"] += 1
        if maintain_calls["n"] == 1:
            state.tunnel_died = True
            return  # drop -> caller reconnects
        try:
            await asyncio.wait_for(state.wakeup.wait(), timeout=10)
            state.wakeup.clear()
        except asyncio.TimeoutError:
            pass

    monkeypatch.setattr(appmain, "_connect_once", fake_connect)
    monkeypatch.setattr(appmain, "_teardown_connection", fake_teardown)
    monkeypatch.setattr(appmain, "_maintain_until_drop", fake_maintain)

    state.desired_connected = True
    task = asyncio.create_task(appmain._lifecycle_loop())
    try:
        # First connect, then a drop, then a reconnect -> connect_count == 2.
        await _wait_for(lambda: connect_count["n"] == 2, timeout=5.0)
        assert state.ready is True
        assert state.desired_connected is True
    finally:
        state.desired_connected = False
        state.wakeup.set()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def test_connect_failure_sets_error_and_retries(monkeypatch):
    _reset_state()
    attempts = {"n": 0}

    async def fake_connect():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("boom")
        state.ready = True
    async def fake_teardown(keep_tunnel: bool = True):
        state.ready = False

    monkeypatch.setattr(appmain, "_connect_once", fake_connect)
    monkeypatch.setattr(appmain, "_teardown_connection", fake_teardown)
    async def fake_maintain():
        await asyncio.sleep(10)

    monkeypatch.setattr(appmain, "_maintain_until_drop", fake_maintain)

    state.desired_connected = True
    task = asyncio.create_task(appmain._lifecycle_loop())
    try:
        await _wait_for(lambda: attempts["n"] == 1)
        await _wait_for(lambda: state.error == "boom")
        # Wake the backoff wait early so it retries immediately.
        state.wakeup.set()
        await _wait_for(lambda: attempts["n"] == 2 and state.ready is True, timeout=5.0)
    finally:
        state.desired_connected = False
        state.wakeup.set()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass