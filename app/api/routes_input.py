"""操作入力 API。座標はデバイスの points（ブラウザ側で scale.py 相当の変換済み）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.state import state

router = APIRouter()


def _wda():
    if state.wda is None or not state.ready:
        raise HTTPException(status_code=503, detail="WDA not ready")
    return state.wda


class TapBody(BaseModel):
    x: int
    y: int
    hold_ms: int = 50


class HoldBody(BaseModel):
    x: int
    y: int
    duration_ms: int = 1500


class SwipeBody(BaseModel):
    from_x: int = Field(..., alias="fromX")
    from_y: int = Field(..., alias="fromY")
    to_x: int = Field(..., alias="toX")
    to_y: int = Field(..., alias="toY")
    duration_ms: int = 400

    model_config = {"populate_by_name": True}


class ButtonBody(BaseModel):
    button: str  # home | volumeUp | volumeDown | snapshot | siri


class KeysBody(BaseModel):
    text: str


@router.post("/api/tap")
async def tap(body: TapBody) -> dict:
    await _wda().tap(body.x, body.y, body.hold_ms)
    return {"ok": True}


@router.post("/api/hold")
async def hold(body: HoldBody) -> dict:
    await _wda().long_press(body.x, body.y, body.duration_ms)
    return {"ok": True}


@router.post("/api/swipe")
async def swipe(body: SwipeBody) -> dict:
    await _wda().swipe(body.from_x, body.from_y, body.to_x, body.to_y, body.duration_ms)
    return {"ok": True}


@router.post("/api/button")
async def button(body: ButtonBody) -> dict:
    await _wda().press_button(body.button)
    return {"ok": True}


@router.post("/api/keys")
async def keys(body: KeysBody) -> dict:
    await _wda().keys(body.text)
    return {"ok": True}