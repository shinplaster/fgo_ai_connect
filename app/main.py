"""FastAPI アプリ本体。

接続処理（tunnel → WDA 起動 → セッション作成）はバックグラウンドタスクで行い、
lifespan は即座に完了してサーバを常時応答可能にする。iPhone 未接続や WDA 未設定でも
サーバは起動し /api/status が状態を返す。

注意: device/wda.py の install_and_launch は Phase 0 で手順確定後に実装する。
現状は「WDA が既に起動済み（verify_smoke.py で手動起動）」前提で動く。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from pymobiledevice3.remote.tunnel_service import TunnelProtocol

from app.api import routes_input, routes_status, routes_stream
from app.config import resolve_build_epoch, resolve_runner_bundle_id, settings
from app.device.tunnel import TunnelManager
from app.device.wda import WdaDeployer
from app.device.wda_client import WdaClient
from app.stream.mjpeg import MjpegStreamer
from app.state import state

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).parent / "web"


async def _connect_once() -> None:
    """tunnel → WDA → セッション を順に確立し state.ready=True にする。

    tunnel は維持（一度確立した RSD を使い回す）。deployer は毎回再作成する
    （install_and_launch 内で service_provider(RSD) を connect/close するため）。
    WDA が死んでも tunnel は維持したまま WDA だけ再起動し、RSD のサービス一覧の
    安定性（testmanagerd 等）を保つ。
    """
    state.error = None
    state.user_action = None

    # Early device-presence check: skip the expensive tunnel bringup and surface
    # a friendly "connect USB" hint if no iPhone is reachable via usbmux.
    from pymobiledevice3 import usbmux
    dev = await usbmux.select_device(settings.device_udid) if settings.device_udid else None
    if dev is None:
        devs = await usbmux.list_devices()  # udid unset -> pick by list
        dev = devs[0] if devs else None
    if dev is None:
        state.user_action = "connect_usb"
        raise ConnectionError(
            "iPhone が USB 接続されていません。USB ケーブル・Developer Mode・信頼設定を確認してください。"
        )

    if state.tunnel is None:
        proto = TunnelProtocol.TCP if settings.tunnel_protocol == "tcp" else TunnelProtocol.QUIC
        state.tunnel = TunnelManager(
            settings.device_udid,
            protocol=proto,
            bonjour_timeout=settings.tunnel_bonjour_timeout,
            autopair=settings.tunnel_autopair,
        )
    # deployer は毎回再作成: RSD は install_and_launch で都度 connect/close する。
    state.deployer = WdaDeployer(
        app_path=settings.wda_app_path,
        udid=settings.device_udid,
        runner_bundle_id=resolve_runner_bundle_id(),
        target_bundle_id=settings.wda_target_bundle_id,
        http_port=settings.wda_http_port,
        mjpeg_port=settings.wda_mjpeg_port,
        build_epoch=resolve_build_epoch(),
        wda_ready_timeout=settings.wda_ready_timeout,
        skip_install=settings.wda_skip_install,
    )
    logger.info("Starting tunnel...")
    state.tunnel_info = await state.tunnel.start(timeout=settings.tunnel_ready_timeout)

    logger.info("Installing + launching WDA via RSD %s:%d ...",
                state.tunnel_info.rsd_host, state.tunnel_info.rsd_port)
    await state.deployer.install_and_launch(
        state.tunnel_info.rsd_host, state.tunnel_info.rsd_port
    )

    udid = state.deployer.service_provider.udid if state.deployer.service_provider else settings.device_udid
    logger.info("Connecting WDA HTTP over usbmux (udid=%s port=%d)", udid, settings.wda_http_port)

    state.wda = WdaClient(udid, settings.wda_http_port, settings.wda_ready_timeout)
    await state.wda.wait_until_ready()
    # iOS26 locks the device when WDA (an Xcode-unattached XCTest) launches. While
    # locked, create_session fails with "Unable to launch ... not, or could not be,
    # unlocked". The WDA process is already up and kept hot by MJPEG keepalive, so
    # do NOT restart it — restarting just makes iOS 26 lock again (infinite loop).
    # Instead, retry create_session while waiting for the user to manually unlock.
    deadline = asyncio.get_event_loop().time() + settings.session_unlock_wait_timeout
    while True:
        # Abort the unlock wait if the user clicked Disconnect mid-connect.
        if not state.desired_connected:
            raise RuntimeError("aborted by disconnect")
        try:
            await state.wda.create_session(settings.wda_target_bundle_id)
            break
        except Exception as e:
            msg = str(e)
            is_locked = (
                "not, or could not be, unlocked" in msg
                or "BSErrorCodeDescription=Locked" in msg
                or "FBSOpenApplicationErrorDomain Code=7" in msg
            )
            if not is_locked:
                raise
            if asyncio.get_event_loop().time() >= deadline:
                raise ConnectionError(
                    "iPhone のアンロック待ちがタイムアウトしました。"
                    "iPhone をアンロックして再度「接続」を押してください。"
                )
            # Surface an "unlock" hint so the UI prompts the user instead of
            # showing a generic "connecting..." while we wait up to 180s.
            state.user_action = "unlock"
            logger.warning("device locked, waiting for manual unlock to create session (retry in 2s)")
            await asyncio.sleep(2)
    state.user_action = None
    state.screen = state.wda.screen_info
    state.ready = True
    logger.info("WDA ready. screen=%s", state.screen)

    # MJPEG keepalive: start AFTER create_session succeeds (not before). iOS 26
    # locks the device within ~3s of WDA launch (Xcode-unattached XCTest security);
    # starting MJPEG keepalive before create_session delayed it past that window,
    # so create_session hit "device locked". verify_smoke.py creates the session
    # first (within the unlock window) and succeeds -- we mirror that. The
    # sustained MJPEG I/O then keeps the runner hot against the 5s idle-kill for
    # the maintain phase. See _mjpeg_keepalive for the rationale.
    if settings.mjpeg_keepalive_enabled:
        await _stop_mjpeg_keepalive()
        state.mjpeg_keepalive_task = asyncio.create_task(
            _mjpeg_keepalive(udid), name="mjpeg-keepalive"
        )
        logger.info("mjpeg keepalive started (udid=%s)", udid)


async def _teardown_connection(keep_tunnel: bool = True) -> None:
    """WDA と deployer を止める。keep_tunnel=True かつ tunnel が生きていれば
    tunnel は維持し、次の _connect_once で WDA だけ再起動する。

    deployer.stop(kill=True) が runner プロセスを明示 kill するので、端末側の
    "Automation running" banner が消える。
    """
    state.ready = False
    state.user_action = None
    await _stop_mjpeg_keepalive()
    if state.wda is not None:
        try:
            await state.wda.delete_session()
            await state.wda.aclose()
        except Exception:
            pass
        state.wda = None
    if state.deployer is not None:
        try:
            await state.deployer.stop(kill=True)
        except Exception:
            pass
        state.deployer = None
    if state.tunnel is not None:
        if keep_tunnel and await state.tunnel.is_alive():
            logger.info("keeping tunnel alive: RSD=%s:%d",
                        state.tunnel.info.rsd_host, state.tunnel.info.rsd_port)
            return
        try:
            await state.tunnel.stop()
        except Exception:
            pass
        state.tunnel = None


async def _mjpeg_keepalive(udid: str) -> None:
    """Continuously consume WDA's MJPEG stream to keep the runner I/O-hot.

    Hypothesis: iOS 26's 5s-kill is a testmanagerd idle retirement of the runner.
    A persistent MJPEG read (open socket + continuous frame delivery) keeps the
    runner process busy with I/O so it is not retired, unlike intermittent
    /status polling. Frames are discarded — the sustained read IS the keepalive.

    Reconnects automatically when the stream ends (WDA killed/restarted).
    """
    while True:
        streamer: MjpegStreamer | None = None
        try:
            streamer = MjpegStreamer(
                udid=udid, mjpeg_port=settings.wda_mjpeg_port, scale=settings.mjpeg_scale
            )
            count = 0
            async for _frame in streamer.frames():
                count += 1
                if count % 30 == 1:
                    logger.info("mjpeg keepalive consuming frames (n=%d)", count)
            # frames() returned normally -> stream ended (WDA killed). Reconnect.
            logger.info("mjpeg keepalive stream ended after %d frames, reconnecting", count)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("mjpeg keepalive error: %s", e)
        finally:
            if streamer is not None:
                try:
                    await streamer.close()
                except Exception:
                    pass
        try:
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise


async def _stop_mjpeg_keepalive() -> None:
    task = state.mjpeg_keepalive_task
    state.mjpeg_keepalive_task = None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


def _is_device_absent_error(msg: str) -> bool:
    """Errors that mean the user needs to plug the iPhone in."""
    return ("USB 接続されていません" in msg
            or "no RSD discovered" in msg
            or "device not found" in msg)


def _is_unlock_timeout_error(msg: str) -> bool:
    return "アンロック待ちがタイムアウト" in msg


async def _maintain_until_drop() -> None:
    """健康チェックループを desired_connected && ready の間回す。

    接続が切れたら（state.tunnel_died をセットして）戻り、呼び出し元の
    keep_tunnel 判定に使う。desired が False に反転しても戻る（呼び出し元が
    再チェック）。wakeup event で Disconnect クリックを即時検知する。
    """
    while state.desired_connected:
        # Wake early on desired flips during the watchdog sleep.
        try:
            await asyncio.wait_for(state.wakeup.wait(), timeout=settings.watchdog_interval)
            state.wakeup.clear()
            if not state.desired_connected:
                state.tunnel_died = False
                return
        except asyncio.TimeoutError:
            pass
        if state.tunnel is None or not await state.tunnel.is_alive():
            state.tunnel_died = True
            logger.warning("tunnel down, reconnecting")
            return
        if state.wda is not None:
            try:
                data = await state.wda.status()
                # /status normally OMITS sessionId (WDA returns it only on
                # /session). Use WDA's ready flag as the liveness signal;
                # recreate only when WDA reports not ready.
                if not data.get("value", {}).get("ready", True):
                    logger.info("WDA not ready, recreating session")
                    await state.wda.create_session()
                    state.screen = state.wda.screen_info
            except Exception:
                logger.warning("WDA health check failed, reconnecting")
                state.tunnel_died = False
                return
    state.tunnel_died = False


async def _lifecycle_loop() -> None:
    """Desired-state 調整ループ（ページ駆動接続/切断の本体）。

    desired_connected=False なら wakeup.wait() でアイドル待機（起動時もここ）。
    True なら接続＋維持。維持中の一時切断（5秒kill・ロック等）は desired 仍 True
    の間は自動再接続。False に反転したら teardown(keep_tunnel=True) してアイドルへ。
    すべての connect/teardown は lifecycle_lock 内で実行し API ハンドラと直列化。
    """
    backoff = 5.0
    while True:
        try:
            if not state.desired_connected:
                # Idle: wait until the user clicks Connect.
                state.wakeup.clear()
                await state.wakeup.wait()
                state.wakeup.clear()
                backoff = 5.0
                state.error = None
                # Backstop: clear any stale actionable hint so the idle UI shows
                # "未接続" (the /api/disconnect handler also clears these).
                state.user_action = None
                continue

            if state.ready:
                # Maintain phase. Returns on drop or desired flip.
                await _maintain_until_drop()
                if not state.desired_connected:
                    # User clicked Disconnect during maintain.
                    async with state.lifecycle_lock:
                        state.busy = True
                        try:
                            await _teardown_connection(keep_tunnel=True)
                        finally:
                            state.busy = False
                    continue
                # Dropped while desired still True -> reconnect.
                keep = not state.tunnel_died
                async with state.lifecycle_lock:
                    state.busy = True
                    try:
                        await _teardown_connection(keep_tunnel=keep)
                    finally:
                        state.busy = False
                continue

            # desired True, not ready -> connect.
            async with state.lifecycle_lock:
                if not state.desired_connected:
                    continue  # user flipped to disconnect during wait
                state.busy = True
                try:
                    await _connect_once()
                    logger.info("connected")
                    backoff = 5.0
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    state.ready = False
                    state.error = str(e)
                    msg = str(e)
                    # Keep the actionable hint (connect_usb / unlock) for UI;
                    # clear it for non-user-actionable failures.
                    if not (_is_device_absent_error(msg) or _is_unlock_timeout_error(msg)):
                        state.user_action = None
                    logger.warning("connect failed: %s", e)
                    # testmanagerd / サービス不在 / デバイス未接続 は tunnel を作り直す。
                    # それ以外（WDA 5秒kill 等）は tunnel を維持して WDA だけ再起動。
                    keep = ("testmanagerd" not in msg
                            and "No such service" not in msg
                            and not _is_device_absent_error(msg))
                    try:
                        await _teardown_connection(keep_tunnel=keep)
                    except Exception:
                        pass
                    # User-actionable states (plug in / unlock) get a longer,
                    # fixed backoff; others exponential up to 30s.
                    if _is_device_absent_error(msg) or _is_unlock_timeout_error(msg):
                        backoff = 10.0
                    else:
                        backoff = min(backoff * 2, 30.0)
                finally:
                    state.busy = False

            # Backoff before retry, but wake early if desired flips.
            if state.error is not None and state.desired_connected:
                try:
                    await asyncio.wait_for(state.wakeup.wait(), timeout=backoff)
                    state.wakeup.clear()
                except asyncio.TimeoutError:
                    pass

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("lifecycle loop crashed: %s", e)
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Idle startup: start the reconciler but do NOT set desired_connected.
    # The user opens the browser and clicks 接続 to connect for the first time.
    task = asyncio.create_task(_lifecycle_loop(), name="lifecycle")
    try:
        yield
    finally:
        state.desired_connected = False
        state.wakeup.set()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        async with state.lifecycle_lock:
            state.busy = True
            try:
                await _teardown_connection(keep_tunnel=False)
            finally:
                state.busy = False


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.include_router(routes_status.router)
    app.include_router(routes_input.router)
    app.include_router(routes_stream.router)
    if _WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")
    return app


app = create_app()


def main() -> None:
    # Force uvicorn onto an asyncio SelectorEventLoop on EVERY platform.
    # uvicorn.run -> asyncio.run picks uvloop on Mac (when installed) and
    # ProactorEventLoop on Windows (3.14+ ignores set_event_loop_policy).
    # BOTH break bonjour / browse_remoted UDP multicast reception, so
    # TunnelManager._discover_rsd (get_rsds) discovers zero RSDs. The default
    # SelectorEventLoop (KqueueSelector on Mac / SelectSelector on Windows)
    # receives multicast fine -- verified via verify_smoke.py, which uses plain
    # asyncio.run and successfully establishes the tunnel. So we create the
    # SelectorEventLoop explicitly and drive uvicorn.Server.serve() ourselves.
    # Note: SelectorEventLoop cannot run subprocesses on Windows, but this app
    # no longer uses subprocess for tunnel/WDA (Python API only), so that
    # limitation is acceptable. SelectorEventLoop is deprecated for removal in
    # 3.16 -- revisit then.
    config = uvicorn.Config(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        loop="asyncio",
        reload=False,
    )
    server = uvicorn.Server(config)
    loop = asyncio.SelectorEventLoop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(server.serve())


if __name__ == "__main__":
    main()