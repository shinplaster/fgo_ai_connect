# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **2026-06-21 方針転換（最重要）**: Windows 版は実装断念し **Mac 版へ移行** しました。理由: (1) iOS15+ が WDA 起動時にデバイスをロック（パスコード要求・変更不可）で運用不能、(2) Windows 単独では HID/display 経路が RSD に公開されない（iPhone Mirroring 相当のロック中 touch 不可）。remote start-tunnel 切替自体は Windows で実機検証成功済み。現在このリポジトリは Mac（`~/ios_connect`）で作業中。以下の記述は **Windows 版時代の内容を多く含む**（NCM ドライバ・AMDS・SelectorEventLoop workaround・管理者 PowerShell など Mac では不要のもの）。

> **2026-06-21 Mac 実機検証結果（Phase 0 完遂・目標確定）**: 方針転換の懸案「Mac なら HID/display が RSD に公開されるか」を `.venv313`（Python 3.13）で実機検証 → **Mac でも公開されない（personalized DDI mount 後も同じ）= Windows と同じ。pymobiledevice3 経由の「ロック中 HID touch」は Mac でも不可。** Mac 純正（iPhone Mirroring / devicectl）は capabilities に `viewdevicescreen`/`getdisplayinfo` を持ち別のプライベート経路で HID/display に到達するが、pymobiledevice3 はその経路を持たない。ただし RSD に `testmanagerd` が公開されるため **本線の WDA 経由（アンロック中の操作・画面ストリーム）は Mac で達成可能** で、これを Mac 版の目標とする。Mac 環境の確定事項: tunnel は張れる（pymobiledevice3 標準 `get_rsds` 内の `remoted.suspend()` で SIP 保護下の remoted を一時停止 + `xcrun devicectl manage unpair` 後に autopair で新規 pair record 作成）・**Python 3.13（`.venv313`）必須**（3.12 はバンドル OpenSSL 3.5.7 と sslpsk-pmd3 が非互換で TCP `NO_CIPHERS_AVAILABLE`・QUIC も iOS26 で別エラー）・`TunnelManager` は `get_rsds` ベースに Mac/Windows 両対応化済み（Windows 依存 if_nameindex workaround 削除）。bonjour 受信と TUN 作成で **sudo 必須**（Mac は非 root で `browse_remoted` が広告 0 件）。壁の全経緯は自動メモリ `ios-connect-phase0-findings` の「2026-06-21 Mac 実機検証の結果」セクション参照。

> **2026-06-21 Windows 再検証で E2E 完遂（方針転換の一部撤回）**: scripts/windows_fgo_verify.md に従い Windows 側（`C:\Users\shinp\ios_connect`）で FGO target 検証 → **Windows でも Mac と同一コードで E2E 成功**（tunnel UP → WDA ready screen 852×393 → MJPEG keepalive フレーム増加継続 n=931@1.5min 〜9.7fps で5秒kill回避 → tap API ok → /stream 配信 → /api/status ready）。target app に前面アプリ（FGO）を指定すると iOS15+ のパスコード要求ロックを回避できる件は **Windows でも有効**（方針転換理由(1)は target=springboard のみで起きる挙動だった）。なお HID/display の RSD 非公開（方針転換理由(2)）は Windows・Mac 共通で変わらず、本線目標は WDA 経由（アンロック中操作・画面ストリーム）。**Windows scope-id 回帰修正**: Mac 移行で削除した Windows 依存 if_nameindex workaround の代替として app/device/tunnel.py に `sys.platform=='win32'` gate で pymobiledevice3.bonjour.Address.full_ip を数値 scope id 化（`fe80::...%60`）するパッチを追加（Mac は no-op・import 破壊なし・Mac 側にも同期済み）。Windows 側は他に .venv313 を uv+Py3.13.14 で作成・lzfse は stub wheel で代替（pyimg4 は restore 経路のみ）・port 8000 が VS Code 占有で一時 8001。詳細は自動メモリ `windows-rsd-scope-id-fix` 参照。
## プロジェクト概要

Windows PC のブラウザで iPhone（iOS 17+）の画面を表示し、PC からタップ・スワイプ等の操作を行う Web アプリ。Appium サーバを使わず **pymobiledevice3 で直接 WebDriverAgent (WDA) を起動・操作**する構成が本プロジェクトの根幹。

## よく使うコマンド

```bash
# 開発依存込みでインストール
pip install -e .[dev]

# アプリ起動（http://localhost:8000 をブラウザで開く）
python -m app.main

# Phase 0 スモーク検証（デバイス認識 → tunnel → WDA 起動 → /status）。要管理者権限
python scripts/verify_smoke.py

# テスト全実行（pytest-asyncio は asyncio_mode=auto）
pytest

# 単一テスト
pytest tests/test_wda_client.py::test_move_action_structure
```

**tunnel 起動には管理者権限が必須**（TUN インターフェース作成のため）。バックエンド起動は管理者 PowerShell で `python -m app.main *> backend.log 2>&1` を実行する。本番は tunneld デーモン方式（管理者で1回起動→アプリは非管理者から利用）で解決予定。前提として NCM ドライバ導入済み・CoreDevice pair record 既存（`scripts/windows_setup.md` セクション5）が必要。

## アーキテクチャ（複数ファイルをまたぐ構造）

### 2系統の通信経路（ここが最重要）
本アプリは iPhone との通信に **用途の異なる2つの経路** を使い分ける。これを混同しないこと:

1. **tunnel (RSD)** — WDA を「起動」するためだけの経路。`TunnelManager` が Python API で **remote start-tunnel（CoreDevice pair）** を常駐 task として起動する: `browse_remoted` で NCM 広告（`ncm._remoted._tcp.local`・ifindex 数値 scope id で Windows の gaierror 回避）から RSD を発見 → `create_core_device_tunnel_service_using_rsd(rsd, autopair=True)` → `start_tunnel_over_core_device(service, protocol=TCP)`（`@asynccontextmanager`・常駐 task が `async with` を握り続け `TunnelResult.address/port` を `TunnelInfo.rsd_host/rsd_port` に格納）。`WdaDeployer.install_and_launch` がこの RSD に接続して `InstallationProxyService`（インストール）と `XCUITestService`（常駐起動）を呼ぶ。remote start-tunnel は tunnel 経由 RSD でサービス74個・`com.apple.dt.testmanagerd.remote`/`lockdown.remote.trusted` が公開され、旧 lockdown start-tunnel で起きていた **testmanagerd 不在問題（障害#4・デバイス再起動後に Mac 接続で復旧が必要だった）が解消** する。前提: NCM ドライバ導入済み・CoreDevice pair record 既存（`~/.pymobiledevice3/remote_<id>.plist`）。
2. **usbmux 経由（device 固定ポート）** — WDA 起動後の HTTP(8100)/MJPEG(9100) へのアクセスは **RSD 不要**。`ServiceConnection.create_using_usbmux(udid, port)` でデバイスの localhost ポートに直接リレーする。`WdaClient`（操作 API）と `MjpegStreamer`（画面ストリーム）はこちらを使う。

tunnel は WDA 起動用で、HTTP/MJPEG アクセスに RSD は絡まない。この分離が `wda_http_port`/`wda_mjpeg_port` が `TunnelInfo` とは別に `settings` に持たれている理由。

### 接続ライフサイクル（app/main.py）
`lifespan` は即座に完了してサーバを常時応答可能にし、接続処理は `_connect_loop()` のバックグラウンドタスクで行う（iPhone 未接続・WDA 未設定でも `/api/status` は応答）。

**tunnel は可能な限り維持し、WDA だけ再起動する設計**。理由: tunnel を毎回作り直すと RSD エンドポイント（IPv6+port）が変わり、新しい RSD のサービス一覧に `testmanagerd` 等が含まれないことがある（iOS26 再起動直後に顕著）。`_teardown_connection(keep_tunnel=...)` で制御:
- WDA ヘルスチェック失敗 → `keep_tunnel=True`（WDA だけ再起動）
- tunnel 死亡 / エラーメッセージに `testmanagerd` or `No such service` を含む → `keep_tunnel=False`（tunnel ごと再作成、RSD サービス一覧問題の回避）

`deployer` は毎回再作成する（RSD は `install_and_launch` 内で都度 connect/close するため）。`TunnelManager.start()` は既存の常駐 task が生きて `info.ready` なら再利用する（task が `async with start_tunnel_over_core_device(...)` を握り続ける常駐パターン）。

### Windows の event loop（ここも重要）
Windows 既定の ProactorEventLoop は UDP マルチキャストを受信できず `browse_remoted` が壊れる（tunnel 確立不可）。**Python 3.14 は `asyncio.set_event_loop_policy` を無視** し `uvicorn.run`→`asyncio.run` が ProactorEventLoop を作るため、`app/main.py` の `main()` は `uvicorn.run` を使わず `uvicorn.Config(loop="asyncio")` + `uvicorn.Server` + 明示 `asyncio.SelectorEventLoop()` 作成 + `loop.run_until_complete(server.serve())` で駆動する。SelectorEventLoop は Windows で subprocess 非サポートだが、本アプリは tunnel/WDA を Python API で動かす（subprocess 不使用）ので問題なし。3.16 で SelectorEventLoop 削除予定・要再訪。

### keepalive（2種・混同禁止）
1. **MJPEG keepalive（runner 延命・5秒kill 対策）**: `_mjpeg_keepalive()` が WDA の MJPEG ストリームを連続受信して runner を I/O-hot に保ち、iOS26 の5秒kill（testmanagerd idle retirement 疑い）を回避。`_connect_once` で create_session 成功後に起動。**有効・維持**。
2. **autolock リセットのスワイプ keepalive（廃止・2026-06-21）**: かつて `_keepalive()` が画面中央に 1px スワイプを 240s ごとに送り autolock タイマーをリセットしていたが、FGO で画面中央スワイプが誤入力を誘発するため廃止。現在は「ユーザーが iPhone 側で Auto-Lock を最長(5分)に設定・アイドル5分でロックされたらバックエンドが WDA のみ再起動して自動再接続（`_teardown_connection(keep_tunnel=True)`→既存の再接続パス）」に委ねる。操作中のリモートタップが autolock タイマーをリセットするので実使用中はロックしない。`config.keepalive_enabled`/`keepalive_interval` は削除済み。長時間安定性は `scripts/verify_stability.py` で計測。

### 座標系
WDA は常にデバイスの **論理座標 (points)** でタップ座標を受け取る（Retina 物理 px ではない）。ブラウザ表示座標(CSS px) → points の変換は `app/utils/scale.py`（サーバ側）と `app/web/app.js` の `toDevice()`（ブラウザ側）の両方に同等ロジックがある。API ルート (`routes_input.py`) に渡る座標は既に points 変換済み。

### タッチ操作
`WdaClient` のタップ/スワイプは **W3C Actions**（標準・安定）で実装。ホーム/ボタン/キー入力は WDA 固有エンドポイント（`/wda/pressButton`, `/wda/keys`）。トランスポートは生 HTTP を usbmux 経由で送る独自実装（pymobiledevice3 同梱 `WdaServiceClient._request_json` と同方式。`WdaServiceClient` は要素ベース click しかなく座標タップがないため座標系操作を維持しつつ同一トランスポートを使用）。

### ストリーム
`MjpegStreamer` は usbmux 経由で WDA の localhost:9100 に接続し、`multipart/x-mixed-replace` を読んで JPEG フレーム（SOI `\xff\xd8`〜EOI `\xff\xd9`）を切り出す。`/stream` エンドポイントが同形式でブラウザ `<img>` に転送。`scale` クエリで帯域削減（`config.mjpeg_scale`）。MJPEG がオープン失敗/フレーム零のときは `ScreenshotStreamer`（lockdown `ScreenshotService` / `com.apple.mobile.screenshotr` を usbmux 経由でポーリング、PNG）に自動フォールバック（`app/stream/screenshot.py`）。`streamer: "screenshot"` で最初から screenshot 強制。ScreenshotService は RSD 不要・`create_using_usbmux` で lockdown 取得。

### 状態
`app/state.py` の `state` シングルトンが `tunnel`/`deployer`/`wda`/`tunnel_info`/`ready`/`error`/`screen` を保持。lifespan が初期化し、API ルート (`routes_status`/`routes_input`/`routes_stream`) が参照する。

## 実機検証で判明した iOS 26 固有の制約（コードに反映済み）

これらは実機でのみ再現し、コードの挙動を説明する背景知識:

- **AFC shim インストール失敗**: iOS26 で `install_from_local` が `No such service: com.apple.afc.shim.remote` で失敗する。→ `config.wda_skip_install=True`（WDA は一度インストール済み前提）。本番は署名7日ごとの再ビルド時のみ `False` で再インストール。
- **5秒kill問題**: iOS26 が WDA ランナーを5秒で OS レベル kill する（アンロック状態でも）。根本対策（MJPEG→DVT 軽量化、4分ごとのキープアライブ）は未実装の保留タスク。
- **testmanagerd 不在**: デバイス再起動後に RSD に `com.apple.dt.testmanagerd.remote` が公開されなくなる。復旧には Mac（Xcode/CoreDevice）接続での初期化が必要（USB を Mac に一時切替 → `xcrun devicectl device info` → Windows に戻す）。

## WDA .app のビルド（Mac 必須）

WDA は Xcode プロジェクトのため **Windows 単独ではビルド不可**。Mac + Xcode でビルドした `WebDriverAgentRunner-Runner.app` を `vendor/` に配置する（`.gitignore` で `vendor/` は除外）。手順は `scripts/mac_build.md`。要点:
- 2 target（WebDriverAgentRunner / WebDriverAgentLib）の Signing を個人 Apple ID（Personal Team）で設定、bundle id を一意化
- `USE_MJPEG_SERVER=1` で MJPEG サーバ有効化
- iOS 17+ はビルド後に組み込み XCTest フレームワークを削除しないと起動直後にクラッシュ
- 無料 Apple ID 署名は7日で失効。`WdaDeployer.sign_remaining_days()` が期限監視、`/api/status` が残り日数を返し UI が3日前から警告
- `wda_runner_bundle_id` は .app の `CFBundleIdentifier` と **完全一致** させる必要がある（例: `com.example.WebDriverAgentRunner.xctrunner`）

## Windows 環境の前提

iPhone 認識には **Apple Mobile Device Service (AMDS)** と Apple Mobile Device USB Driver が必要。iTunes standalone 版（Store 版でないもの）で導入。セットアップ詳細は `scripts/windows_setup.md`。

## 実装フェーズの現在地

Phase 0（スモーク検証）で実機手順を確定中。Phase 1（WDA ビルド自動化）は `scripts/mac_build.md` で手順化済み。Phase 2（バックエンド: tunnel/WDA 管理 + 操作 API）は **tunnel を remote start-tunnel（CoreDevice pair・Python API）に切り替え・実機検証済み**（tunnel 確立 → testmanagerd 経由 WDA 起動 → WDA ready → tunnel 再利用まで確認・testmanagerd 不在問題 解消）。Phase 3（ストリームプロキシ）はコード実装済み・実機 E2E 検証待ち。Phase 4（UI）は `app/web/` に実装済み。Phase 5（watchdog/再接続/署名期限/フォールバック）は部分実装（`_connect_loop` の再接続・署名警告・tunnel 再利用は動く、5秒kill根本対策は MJPEG keepalive で解決済み、iOS15+ パスコード要求ロックは根本変更不可で保留）。

詳細な進捗・障害履歴は自動メモリ（`ios-connect-phase0-findings` / `ios-connect-architecture`）を参照。