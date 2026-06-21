"""Prototype: CoreDevice tunnel -> trusted RSD -> DDI mount -> HID/display check.

Builds on verify_coredevice_pair.py, which established the remote pair record on
Windows. That single-shot NCM RSD handshake did NOT publish HID/display services
and could not obtain a trusted lockdown (lockdown=None) -- because those are
registered by DDI daemons (dtuhidd / mediastreamd) only after the personalized
DDI is mounted, and DDI mount needs a trusted lockdown, which a one-shot RSD
does not offer.

This script takes the next step: establish a CoreDevice TCP tunnel over the NCM
RSD (the tunnel IS the CoreDevice session), then re-handshake RSD THROUGH the
tunnel. The tunnel-path RSD should publish lockdown.remote.trusted, which is the
gate for auto_mount -> HID/display services. If they appear, the iPhone
Mirroring HID touch path is reachable on Windows.

Needs an ADMIN shell: the tunnel creates a TUN interface (wintun on Windows).
The pair record from verify_coredevice_pair.py must already exist.

Usage (run from an admin PowerShell):
    python scripts/verify_coredevice_tunnel.py

Output is teed to coredevice_tunnel_result.txt as UTF-8.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import sys
from pathlib import Path

# Windows asyncio: ProactorEventLoop cannot receive UDP multicast (breaks
# bonjour). Force SelectorEventLoop so browse_remoted works. Mac's default
# Kqueue SelectorEventLoop already handles multicast, so no policy swap needed.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymobiledevice3.bonjour import browse_remoted
from pymobiledevice3.exceptions import AlreadyMountedError, RemotePairingCompletedError
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.remote.tunnel_service import (
    TunnelProtocol,
    create_core_device_tunnel_service_using_rsd,
    start_tunnel_over_core_device,
)
from pymobiledevice3.services.mobile_image_mounter import auto_mount

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_LOG_PATH = Path("coredevice_tunnel_result.txt")
_LOG = open(_LOG_PATH, "w", encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=_LOG,
)
_console = logging.StreamHandler(sys.stdout)
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.getLogger().addHandler(_console)
logger = logging.getLogger("verify_coredevice_tunnel")

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


async def _ncm_rsd() -> RemoteServiceDiscoveryService:
    """Discover the NCM remoted advertisement and complete the one-shot RSD handshake."""
    insts = await browse_remoted(timeout=10)
    logger.info("remoted advertisement count=%d", len(insts))
    if not insts:
        raise RuntimeError("no remoted advertisement found (NCM iface down or driver not loaded?)")
    inst = insts[0]
    addr = inst.addresses[0]
    # Windows getaddrinfo needs a numeric ifindex scope id, not the iface name.
    ifindex_map = {name: idx for idx, name in socket.if_nameindex()}
    if addr.iface in ifindex_map:
        host = f"{addr.ip}%{ifindex_map[addr.iface]}"
    else:
        host = addr.full_ip
    port = inst.port
    logger.info("NCM RSD %s:%d", host, port)
    rsd = RemoteServiceDiscoveryService((host, port))
    await rsd.connect()
    logger.info("NCM RSD connected: udid=%s product=%s ios=%s",
                rsd.udid, rsd.product_type, rsd.product_version)
    return rsd


async def _dump(rsd: RemoteServiceDiscoveryService, tag: str) -> list[str]:
    services = rsd.peer_info.get("Services", {}) if rsd.peer_info else {}
    names = sorted(services.keys())
    logger.info("RSD services %s (%d): %s", tag, len(names), names)
    present = [w for w in _WANTED_HID_DISPLAY if w in names]
    missing = [w for w in _WANTED_HID_DISPLAY if w not in names]
    logger.info("HID/display %s: present=%s missing=%s", tag, present, missing)
    ld_name = None
    if rsd.lockdown is not None:
        ld_name = getattr(rsd.lockdown, "service_name", None)
    logger.info("lockdown service=%s (trusted vs untrusted indicator)", ld_name)
    return names


async def _main() -> None:
    rsd = await _ncm_rsd()
    try:
        await _dump(rsd, "NCM single-shot (BEFORE tunnel)")
        logger.info("creating CoreDevice tunnel service (autopair; existing pair record should validate)...")
        service = await create_core_device_tunnel_service_using_rsd(rsd, autopair=True)
        logger.info("CoreDevice tunnel service connected")
        try:
            logger.info("starting TCP tunnel (admin required for TUN iface)...")
            async with start_tunnel_over_core_device(service, protocol=TunnelProtocol.TCP) as tunnel_result:
                logger.info("tunnel UP: interface=%s address=%s rsdPort=%d protocol=%s",
                            tunnel_result.interface, tunnel_result.address,
                            tunnel_result.port, tunnel_result.protocol)
                # RSD handshake THROUGH the tunnel. This is the path that should
                # expose lockdown.remote.trusted (and, after DDI mount, HID/display).
                trsd = RemoteServiceDiscoveryService((tunnel_result.address, tunnel_result.port))
                await trsd.connect()
                logger.info("tunnel RSD connected: udid=%s product=%s ios=%s",
                            trsd.udid, trsd.product_type, trsd.product_version)
                try:
                    await _dump(trsd, "tunnel RSD (BEFORE DDI mount)")
                    if trsd.lockdown is None:
                        logger.info("no lockdown via tunnel either -- DDI mount skipped, HID/display unreachable")
                        return
                    logger.info("mounting personalized DDI via trusted lockdown (first run may download)...")
                    try:
                        await auto_mount(trsd)
                        logger.info("DDI auto-mount done")
                    except AlreadyMountedError:
                        logger.info("DDI already mounted (AlreadyMountedError)")
                    # remoted registers HID/display services once the DDI daemons
                    # start; give it a moment, then refresh the service snapshot.
                    logger.info("waiting 3s for remoted to register DDI services, then refreshing snapshot")
                    await asyncio.sleep(3.0)
                    await trsd.close()
                    trsd2 = RemoteServiceDiscoveryService((tunnel_result.address, tunnel_result.port))
                    await trsd2.connect()
                    logger.info("tunnel RSD reconnected (refreshed snapshot)")
                    try:
                        names2 = await _dump(trsd2, "tunnel RSD (AFTER DDI mount)")
                        present = [w for w in _WANTED_HID_DISPLAY if w in names2]
                        if present:
                            logger.info("*** HID/display services NOW PRESENT via tunnel: %s ***", present)
                        else:
                            logger.info("HID/display still absent after DDI mount -- Windows HID path likely blocked")
                    finally:
                        try:
                            await trsd2.close()
                        except Exception as e:
                            logger.debug("trsd2 close error: %s", e)
                finally:
                    try:
                        await trsd.close()
                    except Exception as e:
                        logger.debug("trsd close error: %s", e)
        finally:
            try:
                await service.close()
            except Exception as e:
                logger.debug("service close error: %s", e)
    finally:
        try:
            await rsd.close()
        except Exception as e:
            logger.debug("rsd close error: %s", e)


def main() -> None:
    logger.info("verify_coredevice_tunnel start")
    try:
        asyncio.run(_main())
        logger.info("verify_coredevice_tunnel OK")
    except Exception as e:
        logger.error("FAILED: %r", e)
        logger.error("traceback", exc_info=True)
        _LOG.close()
        sys.exit(1)
    _LOG.close()


if __name__ == "__main__":
    main()