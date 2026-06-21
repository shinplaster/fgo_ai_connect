"""ブラウザ表示座標 ↔ デバイス points 座標の変換。

WDA は常にデバイスの論理座標（points）でタップ座標を受け取る。
Retina の物理 px ではない点に注意。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScreenInfo:
    """WDA の /status から取得する画面情報。"""

    width: int  # デバイスの論理幅 (points)
    height: int  # デバイスの論理高 (points)
    scale: float  # Retina スケール（情報用）

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 1.0


@dataclass(frozen=True)
class DisplaySize:
    """ブラウザ上の表示サイズ（CSS px）。"""

    width: float
    height: float


def device_points(browser_x: float, browser_y: float, display: DisplaySize, screen: ScreenInfo) -> tuple[int, int]:
    """ブラウザ表示上の座標 → デバイス points 座標。"""
    if display.width <= 0 or display.height <= 0:
        return int(round(browser_x)), int(round(browser_y))
    sx = screen.width / display.width
    sy = screen.height / display.height
    return int(round(browser_x * sx)), int(round(browser_y * sy))


def fit_display(screen: ScreenInfo, max_w: int, max_h: int) -> DisplaySize:
    """デバイス解像度を max_w/max_h に収まるようにアスペクト比を保って縮小。"""
    if screen.width <= 0 or screen.height <= 0:
        return DisplaySize(float(max_w), float(max_h))
    scale = min(max_w / screen.width, max_h / screen.height, 1.0)
    return DisplaySize(screen.width * scale, screen.height * scale)