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


state = AppState()