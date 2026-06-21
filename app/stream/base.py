"""画面ストリームの抽象。"""
from __future__ import annotations

from typing import AsyncIterator


class Streamer:
    """JPEG フレームのバイト列を非同期に生成する。"""

    async def frames(self) -> AsyncIterator[bytes]:
        raise NotImplementedError
        yield b""  # pragma: no cover

    async def close(self) -> None:
        pass