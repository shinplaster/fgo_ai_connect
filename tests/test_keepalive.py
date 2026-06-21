import pytest

from app import main as app_main
from app.state import state


class _MockWda:
    def __init__(self):
        self.swipe_args = None

    async def swipe(self, from_x, from_y, to_x, to_y, duration_ms=400):
        self.swipe_args = (from_x, from_y, to_x, to_y, duration_ms)


@pytest.mark.asyncio
async def test_keepalive_sends_center_1px_swipe(monkeypatch):
    wda = _MockWda()
    monkeypatch.setattr(state, "wda", wda)
    monkeypatch.setattr(state, "screen", {"width": 390, "height": 844})
    await app_main._keepalive()
    # center (195, 422) -> (195, 423), 1px down, 50ms
    assert wda.swipe_args == (195, 422, 195, 423, 50)


@pytest.mark.asyncio
async def test_keepalive_noop_without_screen(monkeypatch):
    wda = _MockWda()
    monkeypatch.setattr(state, "wda", wda)
    monkeypatch.setattr(state, "screen", {})
    await app_main._keepalive()
    assert wda.swipe_args is None


@pytest.mark.asyncio
async def test_keepalive_noop_without_wda(monkeypatch):
    monkeypatch.setattr(state, "wda", None)
    monkeypatch.setattr(state, "screen", {"width": 390, "height": 844})
    # should not raise
    await app_main._keepalive()