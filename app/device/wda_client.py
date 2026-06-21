"""WebDriverAgent HTTP クライアント（usbmux トランスポート経由）。

WDA は iPhone 上で localhost:8100 で動く HTTP サーバ。PC からは usbmux リレー
（ServiceConnection.create_using_usbmux(udid, 8100)）で到達する。tunnel(RSD) は
WDA 起動用で、HTTP アクセス自体は usbmux 経由（RSD 不要・device 固定ポート）。

タッチ操作は W3C Actions（標準・安定）で実装。ホーム/キー入力は WDA 固有エンドポイント。
座標は常にデバイスの論理座標（points）。呼び出し側で scale.py で変換済みの座標を渡すこと。

※ このトランスポート実装は pymobiledevice3 同梱の WdaServiceClient._request_json と
  同等方式。WdaServiceClient は要素ベース click しかなく座標タップがないため、
  座標系操作を維持しつつ同一トランスポートを使う。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from pymobiledevice3.service_connection import ServiceConnection

logger = logging.getLogger(__name__)

# WDA HTTP レスポンスは JSON。Content-Length で完結する。
_HTTP_TIMEOUT = 15.0


class WdaError(RuntimeError):
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class WdaClient:
    """usbmux 経由で WDA の HTTP API を叩くクライアント（座標系操作対応）。"""

    def __init__(self, udid: str, http_port: int = 8100, ready_timeout: float = 30.0,
                 usbmux_address: str | None = None):
        self.udid = udid
        self.http_port = http_port
        self._ready_timeout = ready_timeout
        self.usbmux_address = usbmux_address
        self._session_id: str | None = None
        self._screen: dict[str, Any] = {}

    # ------------------------------------------------------------ transport
    async def _request(self, method: str, path: str, payload: dict[str, Any] | None = None
                       ) -> tuple[int, dict[str, Any]]:
        """HTTP リクエストを usbmux 経由で送り (status_code, parsed_json) を返す。"""
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        headers = [
            f"{method} {path} HTTP/1.1",
            "Host: localhost",
            "Connection: close",
        ]
        if payload is not None:
            headers.append("Content-Type: application/json")
        headers.append(f"Content-Length: {len(body)}")
        request_bytes = ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8") + body

        conn = await ServiceConnection.create_using_usbmux(
            self.udid, self.http_port, usbmux_address=self.usbmux_address
        )
        try:
            await conn.sendall(request_bytes)
            header_bytes, body_prefix = await self._read_until(conn, b"\r\n\r\n", _HTTP_TIMEOUT)
            header_text = header_bytes.decode("iso-8859-1")
            lines = header_text.split("\r\n")
            status_code = int(lines[0].split(" ", 2)[1])
            resp_headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    resp_headers[k.strip().lower()] = v.strip()
            content_length = resp_headers.get("content-length")
            if content_length is not None:
                length = int(content_length)
                if length <= len(body_prefix):
                    body_bytes = body_prefix[:length]
                else:
                    body_bytes = body_prefix + await conn.recvall(length - len(body_prefix))
            else:
                chunks = [body_prefix] if body_prefix else []
                while True:
                    chunk = await conn.recv_any(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                body_bytes = b"".join(chunks)
        finally:
            try:
                await conn.close()
            except Exception:
                pass

        try:
            data = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except ValueError as exc:
            raise WdaError(f"WDA returned non-JSON (status={status_code})", status_code) from exc
        return status_code, data

    @staticmethod
    async def _read_until(conn: ServiceConnection, marker: bytes, timeout: float) -> tuple[bytes, bytes]:
        """marker が現れるまで読み、(marker前, marker後) を返す。"""
        buf = b""
        while marker not in buf:
            chunk = await asyncio.wait_for(conn.recv_any(65536), timeout=timeout)
            if not chunk:
                break
            buf += chunk
        if marker not in buf:
            raise WdaError("WDA response did not contain header terminator")
        return buf.split(marker, 1)[0], buf.split(marker, 1)[1]

    async def _request_ok(self, method: str, path: str, payload: dict[str, Any] | None = None
                           ) -> dict[str, Any]:
        status_code, data = await self._request(method, path, payload)
        if status_code >= 400:
            raise WdaError(f"WDA error status={status_code}: {data}", status_code)
        status = data.get("status")
        if status not in (None, 0, "0"):
            raise WdaError(f"WDA error: {data}", status_code)
        return data

    # ---------------------------------------------------------------- status
    async def status(self) -> dict[str, Any]:
        _, data = await self._request("GET", "/status")
        return data

    async def wait_until_ready(self) -> dict[str, Any]:
        last: Exception | None = None
        deadline = asyncio.get_event_loop().time() + self._ready_timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                data = await self.status()
                value = data.get("value", {})
                if value.get("ready") or value.get("sessionId") is not None:
                    return data
            except Exception as e:
                last = e
            await asyncio.sleep(0.5)
        raise WdaError(f"WDA did not become ready within {self._ready_timeout}s: {last}")

    # -------------------------------------------------------------- session
    async def create_session(self, bundle_id: str = "com.apple.springboard") -> str:
        capabilities = {
            "alwaysMatch": {
                "bundleId": bundle_id,
                "arguments": [],
                "environment": {},
                "shouldWaitForQuiescence": False,
                "shouldUseTestManagerForVisibilityDetection": False,
                "maxTypingFrequency": 60,
                "simpleIsVisibleCheck": True,
            }
        }
        _, data = await self._request("POST", "/session", {"capabilities": capabilities})
        value = data.get("value", {})
        sid = value.get("sessionId")
        if not sid:
            raise WdaError(f"WDA session creation returned no sessionId: {value}")
        self._session_id = sid
        self._screen = value.get("capabilities", {}).get("screenInfo", {}) or {}
        if not self._screen:
            self._screen = await self._fetch_window_size()
        return sid

    async def ensure_session(self) -> str:
        if self._session_id:
            return self._session_id
        return await self.create_session()

    async def delete_session(self) -> None:
        if not self._session_id:
            return
        try:
            await self._request("DELETE", f"/session/{self._session_id}")
        except Exception:
            pass
        self._session_id = None

    async def aclose(self) -> None:
        """No persistent transport to close (each _request opens/closes its own
        usbmux ServiceConnection). Provided for symmetry with teardown callers.
        """
        return

    async def _fetch_window_size(self) -> dict[str, Any]:
        if not self._session_id:
            return {}
        try:
            _, data = await self._request("GET", f"/session/{self._session_id}/window/size")
            value = data.get("value", {})
            return {"width": value.get("width"), "height": value.get("height"), "scale": 1.0}
        except Exception:
            return {}

    @property
    def screen_info(self) -> dict[str, Any]:
        return self._screen

    # ------------------------------------------------------------- touch (W3C)
    def _pointer_actions(self, steps: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "actions": [
                {
                    "type": "pointer",
                    "id": "finger1",
                    "parameters": {"pointerType": "touch"},
                    "actions": steps,
                }
            ]
        }

    @staticmethod
    def _move(x: int, y: int, duration: int = 0) -> dict[str, Any]:
        return {"type": "pointerMove", "duration": duration, "x": x, "y": y}

    @staticmethod
    def _down() -> dict[str, Any]:
        return {"type": "pointerDown", "button": 0}

    @staticmethod
    def _pause(ms: int) -> dict[str, Any]:
        return {"type": "pause", "duration": ms}

    @staticmethod
    def _up() -> dict[str, Any]:
        return {"type": "pointerUp", "button": 0}

    async def _perform(self, steps: list[dict[str, Any]]) -> None:
        sid = await self.ensure_session()
        await self._request_ok("POST", f"/session/{sid}/actions", self._pointer_actions(steps))

    async def tap(self, x: int, y: int, hold_ms: int = 50) -> None:
        await self._perform([self._move(x, y), self._down(), self._pause(hold_ms), self._up()])

    async def long_press(self, x: int, y: int, duration_ms: int = 1500) -> None:
        await self._perform([self._move(x, y), self._down(), self._pause(duration_ms), self._up()])

    async def swipe(self, from_x: int, from_y: int, to_x: int, to_y: int, duration_ms: int = 400) -> None:
        await self._perform([
            self._move(from_x, from_y), self._down(), self._pause(50),
            self._move(to_x, to_y, duration_ms), self._pause(50), self._up(),
        ])

    # ----------------------------------------------- WDA 固有エンドポイント
    async def press_button(self, button_name: str) -> None:
        """buttonName: home | volumeUp | volumeDown | snapshot | siri 等。"""
        sid = await self.ensure_session()
        await self._request_ok("POST", f"/session/{sid}/wda/pressButton",
                               {"buttonName": button_name})

    async def keys(self, text: str, typing_speed: int = 60) -> None:
        sid = await self.ensure_session()
        await self._request_ok("POST", f"/session/{sid}/wda/keys",
                               {"value": list(text), "typingSpeed": typing_speed})