"""Phase 0: end-to-end smoke verification against a real device (Mac).

Stages run in order and stop on the first failure:
  1. pymobiledevice3 import + version
  2. device detection (usbmux Python API)
  3. tunnel establishment via the production TunnelManager
     (app.device.tunnel.TunnelManager: remote start-tunnel over CoreDevice pair,
     get_rsds-based RSD discovery) -> RSD HOST PORT
  4. WDA install + resident launch via the production Python API
     (app.device.wda.WdaDeployer) -- not the one-shot `developer wda launch` CLI,
     which kills WDA on exit.
  5. WDA /status over usbmux (app.device.wda_client.WdaClient) -- the real
     production path, no RSD CLI.
  6. screenshot via the lockdown ScreenshotService (usbmux) -> screen_smoke.png

WDA stages (4-6) are skipped automatically when the WDA .app is not present
under ./vendor/, so this script also doubles as a tunnel-only smoke test.

Usage (run from the repo root with the project venv):
    # Mac: bonjour reception for browse_remoted requires root, so use sudo.
    #     .venv313 is the required interpreter (Python 3.13 + native TCP PSK).
    sudo .venv313/bin/python scripts/verify_smoke.py --skip-install

    # force a fresh install of the WDA .app first (needs the .app in ./vendor/)
    sudo .venv313/bin/python scripts/verify_smoke.py

    # UDID explicit
    sudo .venv313/bin/python scripts/verify_smoke.py --udid 00008130-...

On Windows the equivalent still needs admin (TUN interface creation); on Mac
sudo is required because non-root browse_remoted sees zero NCM advertisements.

All output is also teed to smoke_result.txt as UTF-8.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from importlib.metadata import version as _pkg_version
from pathlib import Path

# The app package is at the repo root; make sure it is importable when this
# script is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymobiledevice3 import usbmux
from pymobiledevice3.remote.tunnel_service import TunnelProtocol

from app.device.tunnel import TunnelManager
from app.device.wda import WdaDeployer
from app.device.wda_client import WdaClient

# Console Unicode safety (harmless on Mac, useful on Windows cp932).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Tee results to a UTF-8 file (elevated runs / encoding safety).
_LOG_PATH = Path("smoke_result.txt")
_LOG = open(_LOG_PATH, "w", encoding="utf-8")


def _print(*args, **kwargs) -> None:
    line = " ".join(str(a) for a in args)
    print(line, **kwargs)
    _LOG.write(line + "\n")
    _LOG.flush()


def _fail(msg: str) -> int:
    _print(f"\n[FAIL] {msg}")
    _LOG.close()
    return 1


async def detect_udid() -> str | None:
    """Return the first device UDID (usbmux ``serial``) via the Python API."""
    try:
        devices = await usbmux.list_devices()
    except Exception as e:
        _print(f"[NOTE] usbmux.list_devices failed: {e}")
        return None
    if not devices:
        return None
    d = devices[0]
    _print(f"[INFO] usbmux device: serial={d.serial} type={d.connection_type}")
    return d.serial


async def _verify_wda(udid: str, host: str, port: int, args) -> None:
    """Stages 4-6: launch WDA via the production API, check status, screenshot."""
    deployer = WdaDeployer(
        app_path=args.wda_app,
        udid=udid,
        runner_bundle_id=args.wda_bundle,
        target_bundle_id=args.target_bundle,
        http_port=8100,
        mjpeg_port=9100,
        skip_install=args.skip_install,
    )
    try:
        # 4. install (unless skipped) + resident launch via RSD.
        _print("\n--- Stage 4: WDA install + resident launch (production API) ---")
        await deployer.install_and_launch(host, port)
        sp = deployer.service_provider
        _print(f"\n[OK] WDA launched (udid={sp.udid if sp else udid}, http=8100, mjpeg=9100)")

        # 5. WDA /status over usbmux (the production transport, no RSD CLI).
        _print("\n--- Stage 5: WDA /status over usbmux ---")
        wda_udid = sp.udid if sp is not None else udid
        client = WdaClient(wda_udid, http_port=8100, ready_timeout=30.0)
        try:
            data = await client.wait_until_ready()
            _print(f"\n[OK] WDA ready: {json.dumps(data, ensure_ascii=False)[:500]}")
            sid = data.get("value", {}).get("sessionId")
            if not sid:
                # status endpoint may omit sessionId; create a session explicitly.
                sid = await client.create_session(args.target_bundle)
            _print(f"[OK] sessionId={sid} screen={client.screen_info}")
        finally:
            try:
                await client.delete_session()
                await client.aclose()
            except Exception:
                pass

        # 6. screenshot via lockdown ScreenshotService (usbmux, no RSD).
        _print("\n--- Stage 6: screenshot via lockdown ScreenshotService ---")
        try:
            from pymobiledevice3.lockdown import create_using_usbmux
            from pymobiledevice3.services.screenshot import ScreenshotService

            lockdown = await create_using_usbmux(serial=udid)
            try:
                png = await ScreenshotService(lockdown).take_screenshot()
            finally:
                await lockdown.close()
            out_png = "screen_smoke.png"
            Path(out_png).write_bytes(png)
            _print(f"\n[OK] screenshot saved: {out_png} ({len(png)} bytes)")
        except Exception as e:
            _print(f"[NOTE] screenshot failed (WDA is up): {e}")
    finally:
        try:
            await deployer.stop()
        except Exception:
            pass


async def amain() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wda-app", default="./vendor/WebDriverAgentRunner-Runner.app",
                    help="path to the Mac-built WDA .app")
    ap.add_argument("--wda-bundle", default="com.example.WebDriverAgentRunner.xctrunner",
                    help="WDA xctrunner bundle id (must match .app CFBundleIdentifier)")
    ap.add_argument("--target-bundle", default="com.aniplex.fategrandorder",
                    help="bundle id WDA targets at launch (use a foreground app to avoid the iOS26 launch-lock)")
    ap.add_argument("--skip-install", action="store_true",
                    help="skip install (WDA .app already on device)")
    ap.add_argument("--udid", default=None, help="device UDID (auto-detected if omitted)")
    ap.add_argument("--tunnel-timeout", type=float, default=120.0,
                    help="tunnel ready timeout in seconds (leave room for first-time "
                         "consent / autopair on the device)")
    args = ap.parse_args()

    # 1. pymobiledevice3 version
    try:
        v = _pkg_version("pymobiledevice3")
    except Exception as e:
        return _fail(f"pymobiledevice3 not installed: {e}")
    _print(f"\n[OK] pymobiledevice3 {v}")

    # 2. device detection (usbmux Python API)
    udid = args.udid or await detect_udid()
    if not udid:
        return _fail("usbmux could not detect a device. Check the USB cable and device Trust.")
    _print(f"\n[OK] device detected: {udid}")

    # 3. tunnel via the production TunnelManager (remote start-tunnel, CoreDevice pair).
    _print("\n--- Stage 3: tunnel (TunnelManager: remote start-tunnel, sudo required) ---")
    tunnel = TunnelManager(
        udid=udid,
        protocol=TunnelProtocol.TCP,
        bonjour_timeout=10.0,
        autopair=True,
    )
    try:
        try:
            info = await tunnel.start(timeout=args.tunnel_timeout)
        except Exception as e:
            return _fail(f"tunnel start failed: {e}")
        host, port = info.rsd_host, info.rsd_port
        _print(f"\n[OK] RSD = {host}:{port}")

        if args.wda_app and Path(args.wda_app).exists():
            await _verify_wda(udid, host, port, args)
        else:
            _print(f"\n( WDA .app not present, skipping WDA stages: {args.wda_app} )")
            _print("    Place a Mac-built WebDriverAgentRunner-Runner.app in ./vendor/")
            _print("    and re-run to verify WDA launch + status + screenshot.")

        _print("\n[OK] smoke verification complete.")
        _LOG.close()
        return 0
    finally:
        try:
            await tunnel.stop()
        except Exception:
            pass


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())