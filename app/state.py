"""アプリ全体の実行状態を保持するシングルトン。

main.py の lifespan で tunnel/deployer/wda を初期化し、API ルートが参照する。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.device.tunnel import TunnelInfo, TunnelManager
from app.device.wda import WdaDeployer
from app.device.wda_client import WdaClient


@dataclass
class AppState:
    tunnel: TunnelManager | None = None
    deployer: WdaDeployer | None = None
    wda: WdaClient | None = None
    tunnel_info: TunnelInfo | None = None
    ready: bool = False
    error: str | None = None
    # デバイス画面情報（points）
    screen: dict = field(default_factory=dict)
    # MJPEG keepalive: a background task that continuously consumes WDA's MJPEG
    # frames to keep the runner I/O-hot (idle-retirement countermeasure on iOS 26).
    mjpeg_keepalive_task: asyncio.Task | None = None

    # --- page-driven connect/disconnect lifecycle ---
    # User intent: True after /api/connect, False after /api/disconnect. The
    # lifecycle loop reconciles state to match this. Idle startup = False.
    desired_connected: bool = False
    # True while _connect_once / _teardown_connection is mid-flight (UI spinner).
    busy: bool = False
    # Serializes connect/teardown between the lifecycle loop and the API handlers.
    # Single SelectorEventLoop (forced in main()) -> asyncio.Lock suffices.
    lifecycle_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Wakes the lifecycle loop immediately when desired_connected flips.
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    # Set by _maintain_until_drop so the reconnect path knows whether the tunnel
    # died (keep_tunnel=False) vs just WDA dropped (keep_tunnel=True).
    tunnel_died: bool = False
    # Hint surfaced to the UI for an action the USER must take:
    # None | "unlock" (device locked, awaiting manual unlock)
    #      | "connect_usb" (no iPhone detected via usbmux)
    # Takes priority over the generic busy/error status line.
    user_action: str | None = None


state = AppState()