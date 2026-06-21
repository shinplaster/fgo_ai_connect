"""Prototype: establish a CoreDevice trusted pair over USB NCM RSD (autopair).

Why this script exists
----------------------
The WiFi ``_remotepairing-manual-pairing._tcp.local`` Bonjour service that
``pymobiledevice3 remote pair`` relies on is **not advertised by standard iOS
17+ iPhones** -- that service type is for Apple TV / Corellium. So the WiFi
manual-pairing route to a CoreDevice trusted pair is closed on a stock iPhone.

The standard route on iOS 17+ is the USB RSD / CoreDevice flow: connect to the
``com.apple.internal.dt.coredevice.untrusted.tunnelservice`` over RemoteXPC
(even an untrusted RSD publishes this service) and let
``RemotePairingProtocol`` autopair. The device shows a Trust dialog and asks
for the passcode; once completed, a remote pair record is saved on Windows
(``~/.pymobiledevice3/remote_<identifier>.plist``) and
``lockdown.remote.trusted`` becomes usable. That trusted lockdown is the gate
for HID/display services (displayservice / hid.universalhidservice /
screencaptureservice / devicecontrol) in the RSD service snapshot -- the same
services iPhone Mirroring uses.

This script needs no admin shell: it goes over the already-up NCM iface and
performs only the pairing handshake (no TUN interface is created).

Usage (run from a normal, non-admin shell while the iPhone is on USB NCM):
    python scripts/verify_coredevice_pair.py

While it runs, the iPhone will show a Trust dialog -- tap Trust and enter the
device passcode. Output is teed to coredevice_pair_result.txt as UTF-8.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import sys
from pathlib import Path

# Windows asyncio: the default ProactorEventLoop cannot receive UDP multicast,
# which breaks pymobiledevice3's bonjour. Force the SelectorEventLoop so
# browse_remoted works. (Deprecated in 3.14, removed in 3.16 -- revisit then.)
asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Make the app package importable when this script is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymobiledevice3.bonjour import browse_remoted
from pymobiledevice3.exceptions import RemotePairingCompletedError
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.remote.tunnel_service import create_core_device_tunnel_service_using_rsd

# Windows console (cp932) Unicode mangling prevention.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_LOG_PATH = Path("coredevice_pair_result.txt")
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
logger = logging.getLogger("verify_coredevice_pair")

# CoreDevice HID/display services that only appear on a TRUSTED RSD. Their
# absence before pairing and (hopefully) presence after is the whole signal.
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


async def _rsd_from_bonjour() -> RemoteServiceDiscoveryService:
    """Discover the NCM remoted advertisement and complete the RSD handshake."""
    insts = await browse_remoted(timeout=10)
    logger.info("remoted advertisement count=%d", len(insts))
    for s in insts:
        logger.info("  inst=%s host=%s port=%d addrs=%s",
                    s.instance, s.host, s.port,
                    [(a.ip, a.iface) for a in s.addresses])
    if not insts:
        raise RuntimeError("no remoted advertisement found (NCM iface down or driver not loaded?)")
    inst = insts[0]
    addr = inst.addresses[0]
    # bonjour's Address.full_ip appends "%<iface-name>", but Windows getaddrinfo
    # cannot resolve a name-based IPv6 scope id (gaierror 11001). It needs the
    # numeric ifindex, so map the iface name -> ifindex ourselves.
    ifindex_map = {name: idx for idx, name in socket.if_nameindex()}
    iface = addr.iface
    if iface in ifindex_map:
        host = f"{addr.ip}%{ifindex_map[iface]}"
    else:
        host = addr.full_ip
    port = inst.port
    logger.info("using RSD %s:%d (iface=%s ifindex=%s)", host, port, iface, ifindex_map.get(iface))
    rsd = RemoteServiceDiscoveryService((host, port))
    await rsd.connect()
    logger.info("RSD connected: udid=%s product=%s ios=%s",
                rsd.udid, rsd.product_type, rsd.product_version)
    return rsd


async def _dump_services(rsd: RemoteServiceDiscoveryService, tag: str) -> list[str]:
    """Print the RSD service snapshot and report which HID/display services exist."""
    services = rsd.peer_info.get("Services", {}) if rsd.peer_info else {}
    names = sorted(services.keys())
    logger.info("RSD services %s (%d): %s", tag, len(names), names)
    present = [w for w in _WANTED_HID_DISPLAY if w in names]
    missing = [w for w in _WANTED_HID_DISPLAY if w not in names]
    logger.info("HID/display %s: present=%s missing=%s", tag, present, missing)
    # Also report which lockdown.remote variant the RSD settled on: trusted vs
    # untrusted is the direct indicator of whether the pair record took.
    ld_name = None
    if rsd.lockdown is not None:
        ld_name = getattr(rsd.lockdown, "service_name", None)
    logger.info("RSD lockdown service=%s (None => no lockdown at all)", ld_name)
    return names


async def _main() -> None:
    # 1. Snapshot BEFORE pairing (expect untrusted / HID-display absent).
    rsd = await _rsd_from_bonjour()
    paired_this_run = False
    try:
        await _dump_services(rsd, "BEFORE pair")
        logger.info("starting CoreDevice autopair -- tap Trust + enter passcode on the iPhone now...")
        # create_core_device_tunnel_service_using_rsd swallows the initial
        # RemotePairingCompletedError and reconnects internally, so a normal
        # return means the pair record was saved and a trusted RemoteXPC
        # connection is up.
        service = await create_core_device_tunnel_service_using_rsd(rsd, autopair=True)
        paired_this_run = True
        logger.info("CoreDevice service connected -- pair record saved on Windows")
        await service.close()
    except RemotePairingCompletedError:
        # Should not escape (handled inside create_...), but keep a guard.
        paired_this_run = True
        logger.info("RemotePairingCompletedError surfaced -- pairing done, re-handshake below")
    finally:
        try:
            await rsd.close()
        except Exception as e:
            logger.debug("rsd close error: %s", e)

    # 2. Re-handshake AFTER pairing and re-snapshot. If lockdown.remote.trusted
    #    now succeeds, HID/display services should appear in the snapshot.
    logger.info("re-handshaking RSD to refresh the service snapshot...")
    rsd2 = await _rsd_from_bonjour()
    try:
        await _dump_services(rsd2, "AFTER pair")
    finally:
        try:
            await rsd2.close()
        except Exception as e:
            logger.debug("rsd2 close error: %s", e)

    if paired_this_run:
        logger.info("RESULT: pairing attempt completed -- check AFTER-pair snapshot for HID/display")


def main() -> None:
    logger.info("verify_coredevice_pair start")
    try:
        asyncio.run(_main())
        logger.info("verify_coredevice_pair OK")
    except Exception as e:
        logger.error("FAILED: %r", e)
        logger.error("traceback", exc_info=True)
        _LOG.close()
        sys.exit(1)
    _LOG.close()


if __name__ == "__main__":
    main()