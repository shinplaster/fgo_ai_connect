# ios-connect

Windows PC のブラウザで iPhone（iOS 17+）の画面を表示し、PC からタップ・スワイプ等の操作を行う Web アプリ。

## 仕組み

- **pymobiledevice3** が iOS 17 の RemoteXPC トンネルを確立し、WebDriverAgent を iPhone 上に転送・起動する。
- **Appium WebDriverAgent (WDA)** が画面キャプチャ（MJPEG）とタッチ操作の HTTP API を提供する。
- **FastAPI** が WDA のストリーム/操作 API をプロキシし、**ブラウザ** で表示・操作する。

PC からの操作（タップ等）は非脱獄の iPhone では WDA が唯一の標準手段。WDA は Xcode プロジェクトのため **.app のビルドには Mac + Xcode が必要**（Windows 単独ではビルド不可）。ビルドは Mac で 1 回 + 無料 Apple ID 署名の 7 日ごとに再ビルドが必要。

## セットアップ

詳細は [`scripts/windows_setup.md`](scripts/windows_setup.md) と [`scripts/mac_build.md`](scripts/mac_build.md) を参照。

### 1. Windows 側
1. iTunes（standalone 推奨）をインストールし Apple Mobile Device Service / USB ドライバを導入。
2. Python 3.10+ をインストール。
3. `pip install -e .[dev]`

### 2. iPhone 側
1. 設定 → プライバシーとセキュリティ → **開発者モード** を ON。
2. USB 接続し「信頼」を許可。

### 3. Mac 側（WDA ビルド）
[`scripts/mac_build.md`](scripts/mac_build.md) に従い `WebDriverAgentRunner.app`（MJPEG 有効化・個人 Apple ID 署名）を生成し、Windows 側へ持込み。

## 実行

```bash
# Phase 0 のスモーク検証（デバイス認識 → tunnel → WDA 起動 → /status）
python scripts/verify_smoke.py

# アプリ起動
python -m app.main
# ブラウザで http://localhost:8000 を開く
```

## 実装フェーズ
- Phase 0: スモーク検証（実機で pymobiledevice3 / WDA の正確な手順を確定）
- Phase 1: WDA ビルド手順の確立・自動化
- Phase 2: バックエンド（tunnel/WDA 管理 + 操作 API）
- Phase 3: 画面 MJPEG ストリームプロキシ
- Phase 4: ブラウザ操作 UI
- Phase 5: watchdog/再接続/署名期限/フォールバック

> 多くの細部（pymobiledevice3 の正確な CLI、MJPEG 有効化、fps 到達性）は実機検証で確定する前提。Phase 0 で確立した手順を以降のコードに反映する。