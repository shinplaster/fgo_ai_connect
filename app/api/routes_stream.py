"""Screen stream delivery.

Primary path: WDA's MJPEG (usbmux -> device:9100), parsed into JPEG frames and
relayed to the browser <img> as multipart/x-mixed-replace.

Fallback: when MJPEG cannot be opened (WDA killed, MJPEG server not up), switch
to ScreenshotStreamer (lockdown ScreenshotService over usbmux, PNG frames). Set
`settings.streamer = "screenshot"` to force the fallback from the start.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.config import settings
from app.state import state
from app.stream.mjpeg import MjpegStreamer
from app.stream.screenshot import ScreenshotStreamer

logger = logging.getLogger(__name__)
router = APIRouter()


def _udid() -> str:
    sp = state.deployer.service_provider if state.deployer is not None else None
    return sp.udid if sp is not None else settings.device_udid


@router.get("/stream")
async def stream():
    if state.wda is None or not state.ready or state.deployer is None:
        raise HTTPException(status_code=503, detail="WDA not ready")
    udid = _udid()

    async def gen():
        if settings.streamer == "screenshot":
            # Forced fallback.
            async for chunk in _screenshot_frames(udid):
                yield chunk
            return
        # MJPEG with automatic fallback to screenshots on open failure.
        try:
            streamer = MjpegStreamer(
                udid=udid, mjpeg_port=settings.wda_mjpeg_port, scale=settings.mjpeg_scale
            )
            mime = "image/jpeg"
            got_frame = False
            try:
                async for frame in streamer.frames():
                    got_frame = True
                    yield _frame_chunk(frame, mime)
            finally:
                await streamer.close()
            if got_frame:
                return
            # Stream ended without a single frame -> MJPEG never came up.
            logger.warning("MJPEG produced no frames, falling back to screenshots")
        except Exception as e:
            logger.warning("MJPEG open failed (%s), falling back to screenshots", e)
        async for chunk in _screenshot_frames(udid):
            yield chunk

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


def _frame_chunk(frame: bytes, mime: str) -> bytes:
    return b"--frame\r\nContent-Type: " + mime.encode() + b"\r\n\r\n" + frame + b"\r\n"


async def _screenshot_frames(udid: str):
    streamer = ScreenshotStreamer(udid=udid, target_fps=settings.target_fps)
    try:
        async for frame in streamer.frames():
            yield _frame_chunk(frame, ScreenshotStreamer.MIME)
    finally:
        await streamer.close()