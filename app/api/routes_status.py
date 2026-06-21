"""状態確認 API。"""
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
        "error": state.error,
        "wda_alive": alive,
        "screen": state.screen,
        "sign_remaining_days": sign_days,
    }