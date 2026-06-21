"""pymobiledevice3 による iOS 17+ トンネル管理（CoreDevice remote start-tunnel）。

Phase 0 RemoteXPC 調査で実機検証して確定した手順（scripts/verify_coredevice_tunnel.py）:
- iOS 17.4+ は `remote start-tunnel` 相当を **Python API** で起動する:
  `create_core_device_tunnel_service_using_rsd(rsd, autopair=True)` +
  `start_tunnel_over_core_device(service, protocol=TunnelProtocol.TCP)`。
  従来の `lockdown start-tunnel`（subprocess・USB lockdown pair）は RSD が untrusted で
  サービス7個のみで、iOS26 再起動後に RSD に testmanagerd が公開されない
  （testmanagerd 不在問題）根本原因だった。remote start-tunnel（CoreDevice pair）なら
  tunnel 経由 RSD でサービス74個・`com.apple.dt.testmanagerd.remote`/`testmanagerd.remote.automation`/
  `lockdown.remote.trusted`/`installation_proxy.shim.remote`/`mobile_image_mounter.shim.remote` が公開される。

前提（既に完了・本モジュールの前提条件）:
- WeTest/PerfDog NCM ドライバ導入済み（iPhone が NCM モード・`ncm._remoted._tcp.local` 広告）。
- CoreDevice pair record 既存（`~/.pymobiledevice3/remote_<id>.plist`・デバイス側信頼済み・
  scripts/verify_coredevice_pair.py で確立）。autopair=True は既存 record の validate を試みる。
- 起動には管理者権限が必要（TUN インターフェース作成・wintun のため）。
- Windows は asyncio に SelectorEventLoop 必須（ProactorEventLoop は UDP マルチキャストを受信できず
  browse_remoted が壊れる）。policy 設定は app/main.py で行う。

`start_tunnel_over_core_device` は @asynccontextmanager（`async with` ブロック内だけ TunnelResult
が有効・抜けると tunnel stop）。本番は常駐が必要なので、バックグラウンド task が `async with` を
握り続け、TunnelResult.address/port を self.info にセットして asyncio.Event で start() に通知、
stop() は別 Event で task を `async with` から抜けさせ tunnel stop + service.close を任せる常駐パターン。

WDA の HTTP(8100)/MJPEG(9100) はデバイス localhost に立ち上がり、usbmux リレー
（ServiceConnection.create_using_usbmux(udid, port)）で PC から到達する。tunnel (RSD) は WDA を
「起動」するための developer サービス（XCUITest）経路として必要。
"""
from __future__ import annotations

import asyncio
import logging
import socket
import sys
from contextlib import suppress
from dataclasses import dataclass

# Windows IPv6 link-local scope-id fix (regression from the Mac port).
# pymobiledevice3's bonjour sets Address.iface = socket.if_indextoname(scopeid),
# which on Windows yields a *name* like "ethernet_32777", and Address.full_ip
# returns "fe80::...%ethernet_32777". Windows getaddrinfo cannot resolve a name
# as an IPv6 scope id (gaierror 11001 "getaddrinfo failed"), so every RSD
# connect in get_rsds fails and tunnel discovery returns 0 RSDs on Windows.
# Mac resolves the name form fine; Windows needs the NUMERIC interface index
# ("fe80::...%60"). Convert the iface name back to its numeric index here.
if sys.platform == "win32":
    from pymobiledevice3.bonjour import Address as _BonjourAddress

    def _win_full_ip(self) -> str:
        if self.iface and self.ip.lower().startswith("fe80:"):
            try:
                return f"{self.ip}%{socket.if_nametoindex(self.iface)}"
            except OSError:
                # Fall back to the original name-form (will likely fail to
                # connect, but preserves upstream behavior if if_nametoindex
                # ever cannot resolve the name).
                return f"{self.ip}%{self.iface}"
        return self.ip

    _BonjourAddress.full_ip = property(_win_full_ip)

from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.remote.tunnel_service import (
    TunnelProtocol,
    create_core_device_tunnel_service_using_rsd,
    start_tunnel_over_core_device,
)
from pymobiledevice3.remote.utils import get_rsds

logger = logging.getLogger(__name__)


@dataclass
class TunnelInfo:
    # tunnel 確立後の RSD エンドポイント。WDA 起動（developer サービス）に使う。
    # remote start-tunnel の場合は tunnel_result.address/port（fd63:.. IPv6 + port）。
    rsd_host: str = ""
    rsd_port: int = 0
    # WDA の HTTP/MJPEG はデバイス側で固定ポートに立ち上がる（usbmux リレーで到達）。
    wda_http_port: int = 8100
    wda_mjpeg_port: int = 9100

    @property
    def ready(self) -> bool:
        return bool(self.rsd_host) and self.rsd_port > 0


class TunnelManager:
    """CoreDevice remote start-tunnel（Python API）の常駐 task によるライフサイクル管理。"""

    def __init__(
        self,
        udid: str | None = None,
        protocol: TunnelProtocol = TunnelProtocol.TCP,
        bonjour_timeout: float = 10.0,
        autopair: bool = True,
    ):
        self.udid = udid
        self._protocol = protocol
        self._bonjour_timeout = bonjour_timeout
        self._autopair = autopair
        self.info = TunnelInfo()
        # Resident task holding the `async with start_tunnel_over_core_device(...)` block open.
        self._tunnel_task: asyncio.Task | None = None
        # Set once the tunnel is up (info populated) OR the task failed (_start_exc set).
        self._ready_event = asyncio.Event()
        # Set by stop() to make the resident task exit the `async with` (tunnel teardown).
        self._stop_event = asyncio.Event()
        # First-failure exception surfaced from the resident task to start().
        self._start_exc: BaseException | None = None

    async def start(self, timeout: float = 30.0) -> TunnelInfo:
        # Reuse an already-running tunnel: every reconnect would change the RSD endpoint
        # (IPv6+port) and the new RSD's service list may lack testmanagerd etc. Keep the
        # tunnel alive and only restart WDA (see app/main.py _connect_loop design).
        if self._tunnel_task is not None and not self._tunnel_task.done() and self.info.ready:
            logger.info("tunnel reused: RSD=%s:%d", self.info.rsd_host, self.info.rsd_port)
            return self.info
        # Fresh start: reset state for a new resident task.
        self._ready_event.clear()
        self._stop_event.clear()
        self._start_exc = None
        self.info = TunnelInfo()
        self._tunnel_task = asyncio.create_task(self._run_tunnel(), name="tunnel-resident")
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("tunnel ready timeout (%ss); cancelling resident task", timeout)
            await self._join_task()
            raise RuntimeError("tunnel がタイムアウト内に ready になりませんでした")
        if self._start_exc is not None:
            # _run_tunnel already logged; surface a RuntimeError to _connect_loop for backoff.
            raise RuntimeError(f"tunnel 起動失敗: {self._start_exc}") from self._start_exc
        if not self.info.ready:
            raise RuntimeError("tunnel が ready になりましたが RSD host/port が未設定です")
        logger.info("tunnel ready: RSD=%s:%d", self.info.rsd_host, self.info.rsd_port)
        return self.info

    async def is_alive(self) -> bool:
        # Async (not sync) to stay compatible with existing `await state.tunnel.is_alive()`
        # call sites in app/main.py. The resident task being alive + info populated == up.
        if self._tunnel_task is None:
            return False
        return not self._tunnel_task.done() and self.info.ready

    async def stop(self) -> None:
        if self._tunnel_task is None:
            return
        self._stop_event.set()
        await self._join_task()

    async def _run_tunnel(self) -> None:
        """Resident task body: discover NCM RSD, open CoreDevice tunnel, park until stop()."""
        try:
            rsd = await self._discover_rsd()
            try:
                service = await create_core_device_tunnel_service_using_rsd(rsd, autopair=self._autopair)
                try:
                    async with start_tunnel_over_core_device(service, protocol=self._protocol) as tr:
                        self.info.rsd_host = tr.address
                        self.info.rsd_port = tr.port
                        logger.info("tunnel UP: interface=%s RSD=%s:%d protocol=%s",
                                    getattr(tr, "interface", None), tr.address, tr.port, tr.protocol)
                        self._ready_event.set()
                        # Park here until stop() sets _stop_event. Exiting this block tears
                        # down the tunnel (start_tunnel_over_core_device is an asynccontextmanager).
                        await self._stop_event.wait()
                finally:
                    with suppress(Exception):
                        await service.close()
            finally:
                with suppress(Exception):
                    await rsd.close()
        except BaseException as e:  # noqa: BLE001 - surface to start() / log + re-raise
            self._start_exc = e
            logger.exception("tunnel task failed")
            self._ready_event.set()  # unblock start()'s wait_for
            raise
        finally:
            # Clear info once the resident task ends (tunnel is gone).
            self.info = TunnelInfo()

    async def _discover_rsd(self) -> RemoteServiceDiscoveryService:
        """Discover the device's RSD via pymobiledevice3's standard ``get_rsds``.

        ``get_rsds`` handles platform differences correctly and supersedes the old
        Windows-specific bonjour + if_nameindex workaround:

        - On Darwin it suspends the native ``remoted`` daemon (SIGSTOP via psutil,
          SIP-safe unlike launchctl bootout) during the bonjour browse so the
          contending native daemon does not reset our RemoteXPC handshake
          (``ConnectionResetError: Connection lost``).
        - It uses iface-name scope ids (``address.full_ip``) which Mac getaddrinfo
          resolves; the numeric-scope-id workaround (``fe80::...%22``) broke Mac's
          IPv6 link-local connect (``EINVAL`` on ``TCP_NODELAY``).
        - It filters RSDs by udid and tries every advertised endpoint, tolerating
          per-endpoint failures (e.g. when multiple advertisements surface after
          an unpair: ``count=2``).

        Run on Python 3.13+ so the TCP tunnel uses the native
        ``set_psk_client_callback`` (OpenSSL 3.x); on 3.12 the sslpsk-pmd3 fallback
        fails against recent OpenSSL with ``NO_CIPHERS_AVAILABLE``.
        """
        rsds = await get_rsds(udid=self.udid)
        if not rsds:
            raise RuntimeError(
                "no RSD discovered via get_rsds (device not paired / not connected?)"
            )
        # Multiple RSDs (e.g. usb + wifi) may be returned; use the first match.
        rsd = rsds[0]
        logger.info("RSD discovered via get_rsds: udid=%s product=%s ios=%s",
                    rsd.udid, rsd.product_type, rsd.product_version)
        return rsd

    async def _join_task(self) -> None:
        """Await the resident task (let it exit via _stop_event first), then reset state.

        Prefer voluntary exit: when stop() set _stop_event, the task unwinds its
        `async with` and closes service/rsd cleanly in its own finally blocks. Only
        fall back to cancel() if it does not finish within a short grace period
        (e.g. start() timeout while still discovering).
        """
        task = self._tunnel_task
        if task is None:
            return
        if not task.done():
            with suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._tunnel_task = None
        self._stop_event.clear()
        self._ready_event.clear()
        self.info = TunnelInfo()