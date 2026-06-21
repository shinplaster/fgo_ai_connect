"""Fallback screen streamer: poll the lockdown ScreenshotService over usbmux.

When WDA's MJPEG server is unavailable or drops (e.g. the 5s-kill on iOS 26),
this streamer keeps the browser view alive by taking periodic screenshots via
the lockdown `com.apple.mobile.screenshotr` service. It needs no RSD/tunnel:
lockdown is obtained from usbmux (`create_using_usbmux`) and returns PNG bytes.

This is a fallback, not a primary path: latency is bounded by `1/target_fps`
and each frame is a full screenshot, so it is heavier than WDA's MJPEG.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from app.stream.base import Streamer

logger = logging.getLogger(__name__)


class ScreenshotStreamer(Streamer):
    """Poll the lockdown ScreenshotService and yield PNG frames."""

    # MIME type for the frames this streamer produces (ScreenshotService returns PNG).
    MIME = "image/png"

    def __init__(self, udid: str | None = None, target_fps: int = 10,
                 usbmux_address: str | None = None):
        self.udid = udid
        self.interval = 1.0 / max(1, target_fps)
        self.usbmux_address = usbmux_address
        self._lockdown = None
        self._svc = None

    async def _ensure_service(self) -> None:
        """Lazily create (or recreate) the lockdown + ScreenshotService on demand."""
        if self._svc is not None:
            return
        # Lazy imports so an unused fallback does not load pymobiledevice3 paths.
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.screenshot import ScreenshotService

        self._lockdown = await create_using_usbmux(
            serial=self.udid, usbmux_address=self.usbmux_address
        )
        self._svc = ScreenshotService(self._lockdown)

    async def frames(self) -> AsyncIterator[bytes]:
        try:
            while True:
                try:
                    await self._ensure_service()
                    assert self._svc is not None
                    frame = await self._svc.take_screenshot()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # Connection dropped (e.g. screen locked, service gone): drop
                    # the service so the next iteration recreates it. Keep looping
                    # so the browser view recovers automatically.
                    logger.warning("screenshot frame failed, will retry: %s", e)
                    await self._drop_service()
                    await asyncio.sleep(self.interval)
                    continue
                if frame:
                    yield frame
                await asyncio.sleep(self.interval)
        finally:
            await self.close()

    async def _drop_service(self) -> None:
        if self._svc is not None:
            try:
                await self._svc.close()
            except Exception:
                pass
            self._svc = None
        if self._lockdown is not None:
            try:
                await self._lockdown.close()
            except Exception:
                pass
            self._lockdown = None

    async def close(self) -> None:
        await self._drop_service()