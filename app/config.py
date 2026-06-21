"""アプリケーション設定。

実機検証（Phase 0）で確定した値を反映済み。
- デバイス: 実機検証済み（iOS 17+）。UDID は環境変数 IOS_CONNECT_UDID で指定（未設定時は最初に見つけたデバイス）。
- tunnel: remote start-tunnel（CoreDevice pair・Python API・iOS 17.4+。NCM ドライバ + CoreDevice pair record が前提）
- WDA: Mac+Xcode ビルド .app、personal Apple ID 署名、USE_MJPEG_SERVER 有効
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    # iPhone の UDID。環境変数 IOS_CONNECT_UDID で指定（例: 00008130-...）。
    # 未設定時は None = 最初に見つけたデバイスを使用（実機1台なら自動で選ばれる）。
    device_udid: str | None = os.environ.get("IOS_CONNECT_UDID")

    # Mac+Xcode でビルドした WDA .app のパス（iOS 17+ は組み込み XCTest フレームワーク削除済み）。
    wda_app_path: str = "./vendor/WebDriverAgentRunner-Runner.app"

    # WDA xctest runner の bundle id（= インストールされる .app の CFBundleIdentifier）。
    # Mac+Xcode ビルド時に一意化した bundle id と完全一致させる（scripts/mac_build.md）。
    # 例: com.<your-reverse-domain>.WebDriverAgentRunner.xctrunner
    wda_runner_bundle_id: str = os.environ.get(
        "IOS_CONNECT_WDA_BUNDLE", "com.example.WebDriverAgentRunner.xctrunner"
    )

    # WDA 起動時に WDA が起動する target app（セッションの起動対象）。
    # 画面キャプチャ(MJPEG)・座標タップ/スワイプは target に依存せず全画面に作用するが、
    # セッションは target app が生存している間だけ維持される。
    # 2026-06-21: springboard(ホーム画面) を target にすると iOS26 の WDA 起動ロック壁で
    # create_session が "not, or could not be, unlocked" で失敗した。検証として Fate/GO
    # (com.aniplex.fategrandorder) を target に切り替え: ゲームアプリは前面維持されやすく
    # バックグラウンド kill に強い。Fate/GO 起動中の画面・操作を本線検証する。
    wda_target_bundle_id: str = os.environ.get(
        "IOS_CONNECT_TARGET_BUNDLE", "com.aniplex.fategrandorder"
    )

    # サーバが listen するホスト/ポート（ブラウザ UI 用）。
    host: str = "127.0.0.1"
    port: int = 8000

    # 画面ストリーム方式: "mjpeg" (WDA 内蔵, MJPEG オープン失敗時は screenshot にフォールバック)
    # or "screenshot" (lockdown ScreenshotService ポーリングを強制)
    streamer: str = "mjpeg"

    # MJPEG 配信のスケール（1.0=実解像度, <1.0 で帯域削減）。要検証で最適値を決定。
    mjpeg_scale: float = 0.5

    # 目標 fps（DVT ポーリング時の上限や目安）。
    target_fps: int = 12

    # WDA 起動待ちタイムアウト（秒）。
    wda_ready_timeout: float = 30.0

    # iOS26 は WDA 起動時にデバイスをロックさせる（Xcode 未接続の XCTest へのセキュリティ）。
    # ロック中は create_session が "Unable to launch ... not, or could not be, unlocked" で失敗する。
    # WDA プロセスは install_and_launch で起動済み・MJPEG keepalive で延命中なので、再起動せず
    # ユーザーの手動アンロックを待って create_session をリトライする（再起動するとまたロックするため循環）。
    # このリトライの最大待ち時間（秒）。
    session_unlock_wait_timeout: float = 180.0

    # WDA の HTTP/MJPEG ポート。デバイス localhost に立ち上がり、usbmux リレーで PC から到達。
    # tunnel (RSD) は WDA 起動用。これらは tunnel とは別系（device 固定ポート）。
    wda_http_port: int = 8100
    wda_mjpeg_port: int = 9100

    # --- tunnel (CoreDevice remote start-tunnel, Python API) ---
    # Tunnel transport protocol. TCP is stable on Windows wintun; QUIC is
    # pymobiledevice3's default but has stricter requirements. Keep TCP.
    tunnel_protocol: str = "tcp"  # "tcp" | "quic"

    # browse_remoted timeout (seconds). NCM iface must advertise
    # ncm._remoted._tcp.local. Raise if discovery is flaky.
    tunnel_bonjour_timeout: float = 10.0

    # autopair: attempt CoreDevice pair verify / first-time pair if no pair
    # record. Keep True; the pair record is created once via
    # scripts/verify_coredevice_pair.py and trusted on the device.
    tunnel_autopair: bool = True

    # tunnel ready timeout (seconds) passed to TunnelManager.start().
    # Mac: leave room for first-time consent / autopair and for remoted (the
    # native CoreDevice daemon) contention on the NCM RSD endpoint. 30s is too
    # short when remoted holds a tunnel (utun) that shadows the RSD route; the
    # resident task needs time to suspend remoted and finish the handshake.
    # Match verify_smoke.py's 120s.
    tunnel_ready_timeout: float = 120.0

    # WDA .app の署名期限（無料 Apple ID は 7 日）。期限切れ警告を何日前から出すか。
    sign_validity_days: int = 7
    sign_warn_days_before: int = 3

    # watchdog の監視間隔（秒）。
    # iOS26 の5秒kill は testmanagerd の idle retirement 疑い: WDA への定期 /status
    # アクティビティで idle 判定を回避するため、5秒 window 内に複数回叩くよう短めに設定。
    watchdog_interval: float = 2.0

    # autolock 抑制: Face ID 端末は自動ロック「なし」不可（最大5分）。画面中央の 1px
    # 微小スワイプを keepalive_interval ごとに送り autolock タイマーをリセットする。
    # 5秒kill(OSレベル)の対策ではなく、ロックによる WDA セッション切断の対策。
    keepalive_enabled: bool = True
    keepalive_interval: float = 240.0  # < 300s (Face ID 端末の最大 autolock)

    # MJPEG keepalive: continuously consume WDA's MJPEG stream (open socket +
    # sustained frame reads) to keep the runner I/O-hot. Hypothesis: iOS 26's 5s
    # kill is a testmanagerd idle retirement; a persistent MJPEG read is stronger
    # than intermittent /status polling. Frames are discarded (the read is the
    # keepalive). Disable to compare baseline behavior.
    mjpeg_keepalive_enabled: bool = True

    # WDA .app のインストールをスキップ（既にデバイスにインストール済み前提）。
    # 検証中: WDA は Mac でビルド後、verify_smoke.py / 手動で一度インストール済み。
    # 毎回インストールすると iOS26 で com.apple.afc.shim.remote が取れず失敗するため True。
    # 本番は署名7日ごとの再ビルド時に一度だけ False で再インストールする運用にする。
    wda_skip_install: bool = True

    # 署名基準日（.app のビルド日時）。運用時に上書き。
    wda_build_epoch: float | None = None

    @property
    def wda_http_url(self) -> str:
        # WDA HTTP は usbmux リレー経由でアクセス。localhost:8100 はローカルプロキシ有効時のみ。
        return f"http://127.0.0.1:{self.wda_http_port}"

    @property
    def wda_mjpeg_url(self) -> str:
        return f"http://127.0.0.1:{self.wda_mjpeg_port}?scale={self.mjpeg_scale}"


settings = Settings()