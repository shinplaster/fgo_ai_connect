"""Prototype: CoreDevice HID touch injection over RemoteXPC (iPhone Mirroring stack).

Verifies that pymobiledevice3's ``remote/core_device/hid_service`` path — the
same protocol stack iPhone Mirroring / Xcode mirroring use — works on Windows
over the existing tunnel (RSD):

  tunnel (RSD) -> RemoteServiceDiscoveryService -> touch_session(...)
     -> DisplayService.start_video_stream (opens backboardd's HID auth gate)
     -> UniversalHIDServiceService.send_touchscreen (real UIEventTypeTouches)

This is a THIRD path, independent of WDA: lockdown/DVT services have no touch
injection, so CoreDevice/RemoteXPC is the only route. Crucially the auth gate
is the media stream, not the device unlock state — so touches should reach the
lock screen UI (e.g. the "swipe up to unlock" surface) even while locked.

Stages:
  1. tunnel establishment (admin required) -> RSD HOST PORT
  2. RSD connect (RemoteServiceDiscoveryService)
  3. touch_session: list connected HID surfaces + send one gesture

Actions:
  list   enumerate the device's registered HID surfaces (no auth gate needed)
  tap    one mainTouchscreen CONTACT + RELEASE at (X, Y)
  drag   streaming CONTACT from (X1,Y1) to (X2,Y2), then RELEASE
         (use this for the lock-screen "swipe up to unlock": --y1 60000 --y2 8000)

Coordinates are 16-bit (0..65535) normalised across the display regardless of
pixel resolution: (0,0)=top-left, (65535,65535)=bottom-right.

Usage (run from an admin PowerShell — tunnel needs admin for the TUN iface):
    python scripts/verify_hid_touch.py
    python scripts/verify_hid_touch.py --action list
    python scripts/verify_hid_touch.py --action tap --x 32768 --y 32768
    python scripts/verify_hid_touch.py --action drag --x1 32768 --y1 60000 --x2 32768 --y2 8000

Output is teed to hid_touch_result.txt as UTF-8.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# The app package is at the repo root; make sure it is importable when this
# script is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.device.tunnel import TunnelManager

from pymobiledevice3.remote.core_device.hid_service import (
    DIGITIZER_SURFACE_MAIN_TOUCHSCREEN,
    TOUCHSCREEN_STATE_CONTACT,
    TOUCHSCREEN_STATE_RELEASE,
    UniversalHIDServiceService,
    touch_session,
)
from pymobiledevice3.exceptions import AlreadyMountedError
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.services.mobile_image_mounter import auto_mount

# Windows console (cp932) Unicode mangling prevention.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Tee results to a UTF-8 file (elevated runs / encoding safety).
_LOG_PATH = Path("hid_touch_result.txt")
_LOG = open(_LOG_PATH, "w", encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=_LOG,
)
# Also mirror INFO+ to the console for the admin user watching the shell.
_console = logging.StreamHandler(sys.stdout)
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.getLogger().addHandler(_console)
logger = logging.getLogger("verify_hid_touch")


def _log(msg: str) -> None:
    logger.info(msg)


# CoreDevice operation/display services that only appear once the personalized
# DDI is mounted (dtuhidd / mediastreamd register them with remoted). Their
# absence in the RSD service snapshot is what blocks the HID touch path.
_WANTED_HID_DISPLAY = [
    "com.apple.coredevice.displayservice",
    "com.apple.coredevice.hid.universalhidservice",
    "com.apple.coredevice.hid.indigo",
    "com.apple.coredevice.screencaptureservice",
    "com.apple.coredevice.devicecontrol",
    "com.apple.coredevice.locationservice",
    "com.apple.coredevice.pasteboardservice",
    "com.apple.coredevice.configuration",
]


async def _do_tap(svc: UniversalHIDServiceService, x: int, y: int) -> None:
    """One CONTACT + one RELEASE at the same position (matches the CLI _do_tap)."""
    await svc.send_touchscreen(TOUCHSCREEN_STATE_CONTACT, x, y)
    await asyncio.sleep(0.05)
    await svc.send_touchscreen(TOUCHSCREEN_STATE_RELEASE, x, y)


async def _do_drag(
    svc: UniversalHIDServiceService,
    x1: int, y1: int, x2: int, y2: int,
    steps: int = 30, duration: float = 0.6,
) -> None:
    """Streaming CONTACT reports from (x1,y1) to (x2,y2), then RELEASE."""
    interval = duration / steps
    await svc.send_touchscreen(TOUCHSCREEN_STATE_CONTACT, x1, y1)
    for i in range(1, steps + 1):
        x = x1 + (x2 - x1) * i // steps
        y = y1 + (y2 - y1) * i // steps
        await svc.send_touchscreen(TOUCHSCREEN_STATE_CONTACT, x, y)
        await asyncio.sleep(interval)
    await svc.send_touchscreen(TOUCHSCREEN_STATE_RELEASE, x2, y2)


async def _run(args: argparse.Namespace) -> None:
    tunnel = TunnelManager(settings.device_udid)
    _log("Starting tunnel (requires sudo on Mac / admin on Windows)...")
    # 60s leaves time to approve the on-device "Trust this computer" consent
    # dialog the first autopair raises on a fresh host (no pair record yet).
    info = await tunnel.start(timeout=60.0)
    _log(f"tunnel RSD={info.rsd_host}:{info.rsd_port}")

    rsd = RemoteServiceDiscoveryService((info.rsd_host, info.rsd_port))
    await rsd.connect()
    _log(f"RSD connected: udid={rsd.udid}")
    # Dump the RSD service list up front. CoreDevice HID/display services are
    # published here (peer_info["Services"]); their absence — like testmanagerd
    # before — points to a stale RSD service table that a Mac/Xcode (CoreDevice)
    # attach usually refreshes. Printing this first makes that condition visible
    # regardless of which action was requested.
    services = rsd.peer_info.get("Services", {}) if rsd.peer_info else {}
    service_names = sorted(services.keys())
    _log(f"RSD services ({len(service_names)}): {service_names}")
    try:
        if args.action == "rsd-services":
            for name, info_svc in services.items():
                _log(f"  {name}: {info_svc}")
            return
        if args.action == "ddi-mount":
            # HID/display services are registered by DDI daemons (dtuhidd /
            # mediastreamd) when the personalized DDI is mounted. testmanagerd
            # is OS-persistent so it shows without DDI; HID/display do not.
            # After mounting, reconnect the RSD on the same tunnel to refresh
            # peer_info["Services"] (the snapshot is taken at handshake).
            _log("auto-mounting personalized DDI (may download image on first run)...")
            try:
                await auto_mount(rsd)
                _log("DDI auto-mount done")
            except AlreadyMountedError:
                _log("DDI already mounted (AlreadyMountedError) — mount is not the blocker")
            _log("waiting 2s for remoted to register services, then refreshing snapshot")
            await asyncio.sleep(2.0)
            await rsd.close()
            rsd2 = RemoteServiceDiscoveryService((info.rsd_host, info.rsd_port))
            await rsd2.connect()
            _log(f"RSD reconnected (refreshed snapshot): udid={rsd2.udid}")
            try:
                services2 = rsd2.peer_info.get("Services", {}) if rsd2.peer_info else {}
                names2 = sorted(services2.keys())
                _log(f"RSD services AFTER DDI mount ({len(names2)}): {names2}")
                present = [w for w in _WANTED_HID_DISPLAY if w in names2]
                missing = [w for w in _WANTED_HID_DISPLAY if w not in names2]
                _log(f"HID/display services present: {present}")
                _log(f"HID/display services missing: {missing}")
            finally:
                try:
                    await rsd2.close()
                except Exception as e:
                    logger.debug("rsd2 close error: %s", e)
            return
        if args.action == "list":
            # list_connected_services does not need the media-stream auth gate.
            async with UniversalHIDServiceService(rsd) as svc:
                services = await svc.list_connected_services()
                _log(f"connected HID surfaces: {services}")
            return

        # tap / drag: open a media stream (auth gate) via touch_session, then
        # dispatch the gesture. The stream's RTP payload is drained/discarded
        # inside touch_session — its sole job here is to hold the gate open.
        async with touch_session(rsd) as svc:
            services = await svc.list_connected_services()
            _log(f"connected HID surfaces: {services}")
            if args.action == "tap":
                _log(f"tap at ({args.x}, {args.y}) surface={DIGITIZER_SURFACE_MAIN_TOUCHSCREEN}")
                await _do_tap(svc, args.x, args.y)
                _log("tap sent")
            elif args.action == "drag":
                _log(f"drag ({args.x1},{args.y1})->({args.x2},{args.y2}) "
                     f"steps={args.steps} duration={args.duration}")
                await _do_drag(svc, args.x1, args.y1, args.x2, args.y2,
                               steps=args.steps, duration=args.duration)
                _log("drag sent")
            # Hold the session briefly so the RELEASE is fully dispatched.
            await asyncio.sleep(0.5)
    finally:
        try:
            await rsd.close()
        except Exception as e:
            logger.debug("rsd close error: %s", e)
        try:
            await tunnel.stop()
        except Exception as e:
            logger.debug("tunnel stop error: %s", e)


def main() -> None:
    ap = argparse.ArgumentParser(description="CoreDevice HID touch injection prototype")
    ap.add_argument("--action", choices=["tap", "drag", "list", "rsd-services", "ddi-mount"], default="tap")
    ap.add_argument("--x", type=int, default=32768, help="tap X (0..65535, default center)")
    ap.add_argument("--y", type=int, default=32768, help="tap Y (0..65535, default center)")
    ap.add_argument("--x1", type=int, default=32768, help="drag start X")
    ap.add_argument("--y1", type=int, default=60000, help="drag start Y (lower screen)")
    ap.add_argument("--x2", type=int, default=32768, help="drag end X")
    ap.add_argument("--y2", type=int, default=8000, help="drag end Y (upper screen, swipe-up)")
    ap.add_argument("--steps", type=int, default=30)
    ap.add_argument("--duration", type=float, default=0.6)
    args = ap.parse_args()

    _log(f"verify_hid_touch start: action={args.action}")
    try:
        asyncio.run(_run(args))
        _log("verify_hid_touch OK")
    except Exception as e:
        _log(f"HID smoke FAILED: {e!r}")
        logger.error("traceback", exc_info=True)
        _LOG.close()
        sys.exit(1)
    _LOG.close()


if __name__ == "__main__":
    main()