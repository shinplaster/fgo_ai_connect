"""状態確認 API と ページ駆動の接続/切断 API。"""
from __future__ import annotations

from fastapi import APIRouter

from app.state import state

router = APIRouter()


@router.get("/api/status")
async def get_status() -> dict:
    alive = False
    if state.wda is not None:
        try:
            data = await state.wda.status()
            alive = bool(data.get("value", {}).get("ready"))
        except Exception:
            alive = False
    sign_days = None
    if state.deployer is not None:
        sign_days = state.deployer.sign_remaining_days()
    return {
        "ready": state.ready,
        "desired": state.desired_connected,
        "busy": state.busy,
        "user_action": state.user_action,
        "error": state.error,
        "wda_alive": alive,
        "screen": state.screen,
        "sign_remaining_days": sign_days,
    }


@router.post("/api/connect")
async def connect() -> dict:
    """Begin connecting (fire-and-forget). Flips desired_connected=True and wakes
    the lifecycle loop, which runs _connect_once under the lock. Poll /api/status
    to see busy/ready/user_action. Idempotent."""
    state.desired_connected = True
    state.error = None
    # Clear any stale actionable hint from a prior attempt; _connect_once will
    # re-set it (connect_usb / unlock) if the same condition recurs.
    state.user_action = None
    state.wakeup.set()
    return {"ok": True, "desired": True, "busy": state.busy, "ready": state.ready}


@router.post("/api/disconnect")
async def disconnect() -> dict:
    """Begin disconnecting (fire-and-forget). Flips desired_connected=False and
    wakes the lifecycle loop, which tears down WDA (explicit runner kill -> the
    on-device "Automation running" banner dismisses) while keeping the tunnel.
    Poll /api/status to see busy/ready. Clears user_action/error so the UI
    returns to the idle "未接続" state instead of lingering a stale hint."""
    state.desired_connected = False
    state.user_action = None
    state.error = None
    state.wakeup.set()
    return {"ok": True, "desired": False, "busy": state.busy, "ready": state.ready}