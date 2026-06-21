"""WDA 内蔵 MJPEG サーバからストリームを読み出す（usbmux 経由）。

WDA の MJPEG サーバは iPhone 上で localhost:9100 で動く。PC からは usbmux リレー
（ServiceConnection.create_using_usbmux(udid, 9100)）で到達し、GET /mjpeg を送って
multipart/x-mixed-replace ストリームを読み、JPEG フレームを切り出す。

scale クエリ（例: /mjpeg?scale=0.5）で帯域削減。実機で最適値を確認して config.mjpeg_scale 設定。
"""
from __future__ import annotations

import asyncio
import logging
import re

from pymobiledevice3.service_connection import ServiceConnection

from app.stream.base import Streamer

logger = logging.getLogger(__name__)

# multipart/x-mixed-replace の境界を Content-Type ヘッダから抽出
_BOUNDARY_RE = re.compile(rb"boundary=(\S+)")
# JPEG SOI/EOI
_JPEG_RE = re.compile(rb"\xff\xd8.*?\xff\xd9", re.S)


class MjpegStreamer(Streamer):
    def __init__(self, udid: str, mjpeg_port: int = 9100, scale: float | None = None,
                 usbmux_address: str | None = None):
        self.udid = udid
        self.mjpeg_port = mjpeg_port
        self.scale = scale
        self.usbmux_address = usbmux_address
        self._conn: ServiceConnection | None = None

    async def _open(self) -> None:
        """usbmux 経由で WDA の MJPEG ポートへ接続し、データを1つ送ってブロードキャストを開始させる。

        WDA (FBMjpegServer) はクライアントからデータ受信後に
        HTTP/1.0 200 OK + multipart/x-mixed-replace; boundary=--BoundaryString を返し、
        --BoundaryString\\r\\nContent-Length:N\\r\\n\\r\\n<JPEG> フレームを送り始める。
        """
        self._conn = await ServiceConnection.create_using_usbmux(
            self.udid, self.mjpeg_port, usbmux_address=self.usbmux_address
        )
        # 任意のデータを送ればブロードキャスト開始。GET リクエスト形式で送る。
        await self._conn.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n")

    async def frames(self):
        await self._open()
        assert self._conn is not None
        # ヘッダ(\r\n\r\n まで)を読み飛ばす
        header_buf = b""
        while b"\r\n\r\n" not in header_buf:
            chunk = await self._conn.recv_any(65536)
            if not chunk:
                raise RuntimeError("MJPEG 接続がヘッダ前に切断")
            header_buf += chunk
        _, body = header_buf.split(b"\r\n\r\n", 1)

        buf = bytearray(body)
        while True:
            frame = self._extract_frame(buf)
            if frame is not None:
                yield frame
                continue
            chunk = await self._conn.recv_any(65536)
            if not chunk:
                frame = self._extract_frame(buf)
                if frame is not None:
                    yield frame
                break
            buf.extend(chunk)
            if len(buf) > 2_000_000:
                del buf[: len(buf) - 500_000]

    @staticmethod
    def _extract_frame(buf: bytearray) -> bytes | None:
        """buf 先頭近くの JPEG フレームを1つ取り出して buf から削除。無ければ None。"""
        start = buf.find(b"\xff\xd8")
        if start < 0:
            if len(buf) > 200_000:
                del buf[: len(buf) - 100_000]
            return None
        end = buf.find(b"\xff\xd9", start + 2)
        if end < 0:
            # フレーム未完。start 以前のゴミを捨てて待つ
            if start > 0:
                del buf[:start]
            return None
        frame = bytes(buf[start : end + 2])
        del buf[: end + 2]
        return frame

    async def close(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:
                pass
            self._conn = None