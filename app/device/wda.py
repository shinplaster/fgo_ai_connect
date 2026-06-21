"""WDA のデバイスへのインストール・起動・停止と、署名期限の検知。

Phase 0（verify_smoke.py）で確認した手順を Python API で実装:
1. tunnel が確立した RSD (host, port) から RemoteServiceDiscoveryService を生成・connect。
2. WDA .app を InstallationProxyService.install_from_local(developer=True) でインストール。
3. XCUITestService.run(TestConfig) を常駐 asyncio タスクで起動（WDA 本体を立ち上げる）。
   ※ developer wda launch CLI はワンショット（終了時に WDA を kill する）なので不適。
   ※ WDA の MJPEG サーバは env MJPEG_SERVER_PORT で起動（既定 9100）。runner_app_env に注入。
4. usbmux で device の 8100 ポートに接続できるまで待つ（WDA HTTP 起動完了の目安）。

WDA 起動後の HTTP(8100)/MJPEG(9100) は WdaClient / MjpegStreamer が usbmux 経由で直接叩く。
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from pymobiledevice3 import usbmux
from pymobiledevice3.exceptions import ConnectionFailedError
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.services.dvt.testmanaged.dtx_services import XCUITestListener
from pymobiledevice3.services.dvt.testmanaged.xcuitest import TestConfig, XCUITestService
from pymobiledevice3.services.installation_proxy import InstallationProxyService

logger = logging.getLogger(__name__)


class _WdaRunnerLogListener(XCUITestListener):
    """Capture the WDA runner's NSLog output streamed over testmanagerd.

    The runner's idle keepalive timer logs ``WDA_RUNNER_IDLE_KEEPALIVE`` here,
    which lets us verify the heartbeat is actually firing (and observe the
    runner's own crash/retire messages when the 5s-kill hits).
    """

    async def log_message(self, message: str) -> None:
        logger.info("WDA: %s", message.rstrip())

    async def log_debug_message(self, message: str) -> None:
        logger.debug("WDA dbg: %s", message.rstrip())

# .app がインストール済みか確認するための apps list（XCUITest TestConfig が内部で使うものと同系）


class WdaDeployer:
    """WDA .app のインストール・起動（常駐）・停止。"""

    def __init__(self, app_path: str, udid: str | None = None,
                 runner_bundle_id: str = "com.facebook.WebDriverAgentRunner.xctrunner",
                 target_bundle_id: str = "com.apple.springboard",
                 http_port: int = 8100, mjpeg_port: int = 9100,
                 build_epoch: float | None = None,
                 wda_ready_timeout: float = 30.0, skip_install: bool = False):
        self.app_path = Path(app_path)
        self.udid = udid
        self.runner_bundle_id = runner_bundle_id
        self.target_bundle_id = target_bundle_id
        self.http_port = http_port
        self.mjpeg_port = mjpeg_port
        self.build_epoch = build_epoch
        self.wda_ready_timeout = wda_ready_timeout
        self.skip_install = skip_install
        self.service_provider: RemoteServiceDiscoveryService | None = None
        self._xctrunner_task: asyncio.Task | None = None

    def sign_remaining_days(self, now_epoch: float | None = None) -> float | None:
        if self.build_epoch is None:
            return None
        now = now_epoch if now_epoch is not None else time.time()
        return max(0.0, 7.0 - (now - self.build_epoch) / 86400.0)

    async def install_and_launch(self, rsd_host: str, rsd_port: int) -> RemoteServiceDiscoveryService:
        """tunnel の RSD に接続し、WDA をインストール＋常駐起動。service_provider を返す。"""
        self.service_provider = RemoteServiceDiscoveryService((rsd_host, rsd_port))
        await self.service_provider.connect()
        logger.info("RSD connected: udid=%s", self.service_provider.udid)

        # 1. インストール（skip でなければ）
        if not self.skip_install:
            await self._install()

        # 2. XCUITest で WDA を常駐起動
        await self._launch_xctest()

        # 3. WDA HTTP(8100) が usbmux で到達可能になるまで待つ
        await self._wait_for_wda_port()
        return self.service_provider

    async def _install(self) -> None:
        if not self.app_path.exists():
            raise FileNotFoundError(f"WDA .app が見つかりません: {self.app_path}")
        logger.info("Installing WDA .app: %s", self.app_path)
        svc = InstallationProxyService(lockdown=self.service_provider)
        await svc.install_from_local(str(self.app_path), developer=True)
        logger.info("WDA .app installed")

    async def _launch_xctest(self) -> None:
        udid = self.service_provider.udid
        cfg = await TestConfig.create_for(
            self.service_provider, runner_bundle_id=self.runner_bundle_id
        )
        # WDA の MJPEG サーバを起動（env MJPEG_SERVER_PORT でポート指定。既定 9100）。
        # これにより /stream で画面ストリームが取れる。
        cfg.runner_app_env = dict(cfg.runner_app_env or {})
        cfg.runner_app_env["MJPEG_SERVER_PORT"] = str(self.mjpeg_port)
        ts = XCUITestService(self.service_provider)
        self._listener = _WdaRunnerLogListener()
        self._xctrunner_task = asyncio.create_task(
            ts.run(cfg, listener=self._listener), name="wda-xctrunner"
        )
        logger.info("WDA xctest launched (runner=%s udid=%s mjpeg=%d)",
                    self.runner_bundle_id, udid, self.mjpeg_port)

    async def _wait_for_wda_port(self) -> None:
        udid = self.service_provider.udid
        deadline = asyncio.get_event_loop().time() + self.wda_ready_timeout
        last_err: Any = None
        while asyncio.get_event_loop().time() < deadline:
            if self._xctrunner_task and self._xctrunner_task.done():
                # タスクが早期終了した場合はエラーを上る
                exc = self._xctrunner_task.exception()
                raise RuntimeError(f"WDA xctest exited early: {exc}")
            device = await usbmux.select_device(udid)
            if device is None:
                last_err = "device not found in usbmux"
                await asyncio.sleep(0.3)
                continue
            try:
                sock = await device.connect(self.http_port)
                sock.close()
                logger.info("WDA HTTP port %d reachable", self.http_port)
                return
            except ConnectionFailedError as e:
                last_err = e
                await asyncio.sleep(0.3)
        raise TimeoutError(f"WDA did not become reachable on port {self.http_port}: {last_err}")

    async def is_alive(self) -> bool:
        if self._xctrunner_task is None:
            return False
        return not self._xctrunner_task.done()

    async def stop(self) -> None:
        if self._xctrunner_task is not None and not self._xctrunner_task.done():
            self._xctrunner_task.cancel()
            try:
                await self._xctrunner_task
            except (asyncio.CancelledError, Exception):
                pass
        self._xctrunner_task = None
        if self.service_provider is not None:
            try:
                await self.service_provider.close()
            except Exception:
                pass
            self.service_provider = None