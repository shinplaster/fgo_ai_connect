"""Unit tests for WdaDeployer.kill_runner (explicit runner pid kill).

The real pymobiledevice3 DvtProvider/ProcessControl need a live RSD tunnel, so
we monkeypatch the attributes kill_runner imports lazily. kill_runner must:
  - kill when a pid is resolved,
  - no-op when the runner is already dead (pid 0),
  - never raise (best-effort; falls back to task-cancel in stop()).
"""
from __future__ import annotations

import asyncio

import pymobiledevice3.services.dvt.instruments.dvt_provider as dvt_mod
import pymobiledevice3.services.dvt.instruments.process_control as pc_mod

from app.device.wda import WdaDeployer


class _FakeDvt:
    def __init__(self, _sp, *, raise_on_open: bool = False) -> None:
        self.raise_on_open = raise_on_open

    async def __aenter__(self):
        if self.raise_on_open:
            raise ConnectionError("tunnel dead")
        return self

    async def __aexit__(self, *a):
        return False


class _FakeProcessControl:
    def __init__(self, _dvt, *, pid: int = 0, raise_on_kill: bool = False) -> None:
        self._pid = pid
        self.raise_on_kill = raise_on_kill
        self.killed: int | None = None
        self.queries: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def process_identifier_for_bundle_identifier(self, bid: str) -> int:
        self.queries.append(bid)
        return self._pid

    async def kill(self, pid: int) -> None:
        if self.raise_on_kill:
            raise RuntimeError("kill boom")
        self.killed = pid


def _patch(monkeypatch, *, pid: int = 0, raise_on_open: bool = False,
           raise_on_kill: bool = False) -> dict:
    captured: dict = {}

    def _dvt_factory(sp):
        d = _FakeDvt(sp, raise_on_open=raise_on_open)
        captured["dvt"] = d
        return d

    def _pc_factory(dvt):
        pc = _FakeProcessControl(dvt, pid=pid, raise_on_kill=raise_on_kill)
        captured["pc"] = pc
        return pc

    monkeypatch.setattr(dvt_mod, "DvtProvider", _dvt_factory)
    monkeypatch.setattr(pc_mod, "ProcessControl", _pc_factory)
    return captured


def _deployer() -> WdaDeployer:
    d = WdaDeployer(
        app_path="./vendor/does-not-exist.app",
        runner_bundle_id="com.test.xctrunner",
    )
    # service_provider truthy + a non-done task -> kill_runner proceeds.
    d.service_provider = object()
    d._xctrunner_task = asyncio.Future()
    return d


async def test_kill_runner_kills_when_pid_present(monkeypatch):
    cap = _patch(monkeypatch, pid=12345)
    await _deployer().kill_runner()
    assert cap["pc"].queries == ["com.test.xctrunner"]
    assert cap["pc"].killed == 12345


async def test_kill_runner_noop_when_pid_zero(monkeypatch):
    cap = _patch(monkeypatch, pid=0)
    await _deployer().kill_runner()  # must not raise
    assert cap["pc"].killed is None


async def test_kill_runner_fallback_on_open_failure(monkeypatch):
    cap = _patch(monkeypatch, pid=123, raise_on_open=True)
    await _deployer().kill_runner()  # must not raise
    # ProcessControl is never instantiated because DvtProvider.__aenter__ raised.
    assert "pc" not in cap


async def test_kill_runner_fallback_on_kill_failure(monkeypatch):
    cap = _patch(monkeypatch, pid=123, raise_on_kill=True)
    await _deployer().kill_runner()  # must not raise
    assert cap["pc"].killed is None


async def test_kill_runner_skips_when_no_service_provider():
    d = WdaDeployer(
        app_path="./vendor/x.app",
        runner_bundle_id="com.test.xctrunner",
    )
    d._xctrunner_task = asyncio.Future()
    assert d.service_provider is None
    await d.kill_runner()  # no-op, must not raise