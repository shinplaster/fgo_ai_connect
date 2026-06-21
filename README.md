# fgo_ai_connect

PC（**Windows / Mac 両対応**）のブラウザから iPhone の **FGO（Fate/Grand Order）** を操作する Web アプリ。iPhone の画面をブラウザにストリーム表示し、PC からタップ・スワイプ等の操作を行う。Appium サーバを使わず **pymobiledevice3 で直接 WebDriverAgent (WDA) を起動・操作** する構成。Mac・Windows 両方で実機 E2E（画面ストリーム＋操作）を検証済み。

> 検証環境: iPhone 16 Pro (iPhone16,1) / iOS 26.5 / FGO 日本版（`com.aniplex.fategrandorder`）。

---

## 目次

- [仕組み](#仕組み)
- [動作確認済み環境](#動作確認済み環境)
- [セットアップ](#セットアップ)
  - [1. Python 3.13 仮想環境](#1-python-313-仮想環境)
  - [2. パッケージインストール](#2-パッケージインストール)
  - [3. iPhone 側の設定](#3-iphone-側の設定)
  - [4. WDA .app のビルド（Mac + Xcode 必須）](#4-wda-app-のビルドmac--xcode-必須)
  - [5. CoreDevice ペアリング](#5-coredevice-ペアリング)
- [設定（環境変数）](#設定環境変数)
- [実行](#実行)
  - [スモーク検証](#スモーク検証)
  - [アプリ起動](#アプリ起動)
- [トラブルシューティング](#トラブルシューティング)

---

## 仕組み

- **pymobiledevice3** が iOS 17+ の RemoteXPC トンネル（remote start-tunnel / CoreDevice pair）を確立し、WDA を iPhone 上に転送・常駐起動する。
- **WDA** が画面キャプチャ（MJPEG）とタッチ操作（W3C Actions）の HTTP API を提供する。WDA は **FGO を起動対象** としてセッションを維持する（springboard 等のシステム UI を対象にすると iOS 26 がデバイスをロックさせるため — 詳細は [トラブルシューティング](#トラブルシューティング)）。
- **FastAPI** が WDA のストリーム/操作 API をプロキシし、**ブラウザ** で FGO 画面の表示・操作を行う。
- WDA 起動後は usbmux 経由でデバイスの localhost ポート（HTTP 8100 / MJPEG 9100）に直接アクセスし、画面ストリームと操作を行う。

WDA は Xcode プロジェクトのため **.app のビルドには Mac + Xcode が必要**（Windows 単独ではビルド不可）。ビルドは Mac で 1 回 + 無料 Apple ID 署名の **7 日ごとに再ビルド** が必要。ビルド済み .app は Windows でもそのまま使える（iOS .app はホスト OS に依存しない）。

---

## 動作確認済み環境

| 項目 | Mac | Windows |
|------|-----|---------|
| OS | macOS（Darwin 25.5） | Windows 10/11 |
| Python | **3.13**（`.venv313`） | **3.13**（`.venv313`・uv で構築） |
| 起動権限 | **sudo 必須**（bonjour 受信に root・TUN 作成） | **管理者 PowerShell 必須**（TUN 作成） |
| NCM ドライバ | Mac 標準（不要） | WeTest/PerfDog 系 NCM ドライバが必要 |
| iPhone 認識 | devicectl / usbmux | Apple Mobile Device Service (AMDS) + USB ドライバ |
| WDA .app ビルド | 可能（Xcode） | 不可（Mac でビルドして `vendor/` に配置） |

> **Python 3.13 が必須**。3.12 はバンドル OpenSSL と sslpsk-pmd3 が非互換で TCP tunnel が `NO_CIPHERS_AVAILABLE` になる。

---

## セットアップ

### 1. Python 3.13 仮想環境

Python 3.13 をインストール（Mac は [python.org](https://www.python.org/) / Homebrew、Windows は [python.org](https://www.python.org/) / [uv](https://docs.astral.sh/uv/)）。

**Mac**:
```bash
cd ~/fgo_ai_connect
python3.13 -m venv .venv313
```

**Windows**（uv 推奨・PowerShell）:
```powershell
cd C:\Users\<you>\fgo_ai_connect
uv venv .venv313 --python 3.13
```

### 2. パッケージインストール

**Mac**:
```bash
.venv313/bin/pip install -e .[dev]
```

**Windows**:
```powershell
.venv313\Scripts\pip install -e .[dev]
```

`pymobiledevice3>=2.5`・`fastapi`・`uvicorn[standard]`・`httpx`・`websockets` が入る。

### 3. iPhone 側の設定

1. 設定 → プライバシーとセキュリティ → **開発者モード** を ON（再起動が必要）。
2. USB 接続し「このコンピュータを信頼しますか？」→ **信頼**。
3. iPhone を NCM モードにする:
   - **Windows**: WeTest / PerfDog 系の NCM ドライバを導入（詳細は [`scripts/windows_setup.md`](scripts/windows_setup.md)）。iPhone が `ncm._remoted._tcp.local` を広告する。
   - **Mac**: Mac 標準の NCM クラスドライバで機能する（特別なドライバ不要）。
4. **FGO（日本版）をインストール済み**にする。

### 4. WDA .app のビルド（Mac + Xcode 必須）

[`scripts/mac_build.md`](scripts/mac_build.md) / [`scripts/MAC_BUILD_INSTRUCTIONS.md`](scripts/MAC_BUILD_INSTRUCTIONS.md) に従い、`WebDriverAgentRunner-Runner.app`（`USE_MJPEG_SERVER=1`・個人 Apple ID の Personal Team 署名・iOS 17+ は組み込み XCTest フレームワーク削除）を生成。

要点:
- 2 target（WebDriverAgentRunner / WebDriverAgentLib）の Signing を個人 Apple ID（Personal Team）で設定、bundle id を一意化（例: `com.<your-reverse-domain>.WebDriverAgentRunner.xctrunner`）。
- `USE_MJPEG_SERVER=1` で MJPEG サーバ有効化。
- iOS 17+ はビルド後に組み込み XCTest フレームワークを削除しないと起動直後にクラッシュ。
- 無料 Apple ID 署名は 7 日で失効。期限が近づくと UI が警告し `/api/status` が残り日数を返す。

ビルドした `.app` を `vendor/WebDriverAgentRunner-Runner.app` に配置（`.gitignore` で `vendor/` は除外）。**Windows でも同じ `.app` を使う**（Mac から持込み）。

### 5. CoreDevice ペアリング

tunnel の remote start-tunnel には CoreDevice pair record（`~/.pymobiledevice3/remote_<udid>.plist`）が必要。初回は [スモーク検証](#スモーク検証) の autopair が iPhone 側に「このコンピュータを信頼しますか？」ダイアログを出すので承認する。

> **Mac の注意**: Mac 純正 CoreDevice pair（devicectl / iPhone Mirroring）が既存だと、pymobiledevice3 の新規 pair を consent 無しで弾く場合がある。そのときは `xcrun devicectl manage unpair --device <udid>` で Mac 純正 pair を削除してから autopair する。

---

## 設定（環境変数）

`app/config.py` の主要項目は環境変数で上書き可能。実機検証時は設定する。target app は FGO 固定（変更不要）。

| 環境変数 | 既定値 | 説明 |
|----------|--------|------|
| `IOS_CONNECT_UDID` | None（auto-detect） | iPhone の UDID。未設定時は最初に見つけたデバイスを使用（1 台なら自動）。 |
| `IOS_CONNECT_WDA_BUNDLE` | `com.example.WebDriverAgentRunner.xctrunner` | WDA xctest runner の bundle id。ビルドした .app の `CFBundleIdentifier` と完全一致させる。 |

例（Mac）:
```bash
export IOS_CONNECT_UDID=00008130-000250C201F0001C
export IOS_CONNECT_WDA_BUNDLE=com.<your-reverse-domain>.WebDriverAgentRunner.xctrunner
```

---

## 実行

### スモーク検証

デバイス認識 → tunnel → WDA 起動 → /status → screenshot までの一括検証。セットアップが整っているか確認する最初のステップ。

**Mac**（sudo 必須）:
```bash
sudo .venv313/bin/python scripts/verify_smoke.py --skip-install
```

**Windows**（管理者 PowerShell で）:
```powershell
.venv313\Scripts\python scripts\verify_smoke.py --skip-install
```

- `--skip-install`: WDA .app は既にデバイスにインストール済み前提でスキップ。初回インストール時は `--skip-install` を外す。
- `--udid <UDID>`: UDID 明示（環境変数未使用時）。

成功すれば `smoke_result.txt` に `[OK]` が並ぶ。

### アプリ起動

**tunnel 起動には管理者権限が必須**（TUN インターフェース作成のため）。バックエンドは管理者権限で起動し、ブラウザは非管理者から開いて構わない。

**Mac**（ターミナル.app で sudo）:
```bash
cd ~/fgo_ai_connect
sudo .venv313/bin/python -m app.main > backend.log 2>&1
```

**Windows**（管理者 PowerShell で）:
```powershell
cd C:\Users\<you>\fgo_ai_connect
.venv313\Scripts\python -m app.main *> backend.log 2>&1
```

起動後、ブラウザで **http://localhost:8000** を開く。FGO の画面がストリーム表示され、タップ・スワイプが効く。

成功時のログパターン:
```
tunnel UP: interface=... RSD=<ipv6>:<port> protocol=TCP
WDA ready. screen={'width': 852, 'height': 393, 'scale': 1.0}
mjpeg keepalive started (udid=...)
connected
mjpeg keepalive consuming frames (n=31)   # 3 秒ごとに増え続ける = WDA が安定稼働
```

---

## トラブルシューティング

- **`Unable to launch ... not, or could not be, unlocked` / デバイスがロックループする**
  → target app が springboard（ホーム画面）等のシステム UI だと、iOS 26 が WDA 起動時にデバイスをロックさせる。本アプリは **target を FGO に固定** することでこれを回避する。起動検証は iPhone をアンロック状態で行う。

- **WDA がすぐに終了して画面が出ない**
  → iOS 26 が WDA ランナーを 5 秒で kill する問題。本アプリは MJPEG ストリームを連続受信して WDA を稼働状態に保つことで自動回避する（既定で有効）。ログで `mjpeg keepalive consuming frames` が増え続ければ回避成功。

- **`no RSD discovered via get_rsds`**
  → デバイス未接続 / NCM 広告なし / pair record 無し。USB 接続・NCM モード・開発者モードを確認。Mac は sudo 必須（非 root で広告 0 件）。Windows は NCM ドライバ導入済みか。

- **tunnel がタイムアウトする**
  → 初回は iPhone 側に consent ダイアログが出る（承認する）。Mac 純正 pair が干渉する場合は [セットアップ 5](#5-coredevice-ペアリング) の unpair 手順。

- **WDA 起動後すぐにクラッシュする（iOS 17+）**
  → WDA .app の組み込み XCTest フレームワーク削除漏れ。[ビルド手順](#4-wda-app-のビルドmac--xcode-必須) の FW 削除ステップを確認。

- **署名期限（7 日）切れで WDA が起動しない**
  → Mac で WDA .app を再ビルドし `vendor/` に配置。`/api/status` が残り日数を返す（UI は 3 日前から警告）。

- **WDA ビルドで CodeSign が `errSecInternalComponent` で失敗する**
  → キーチェーン ACL 問題。`security unlock-keychain` でロックを解除し、`security set-key-partition-list` で codesign に鍵アクセスを許可してから再ビルド（詳細は [`scripts/MAC_BUILD_INSTRUCTIONS.md`](scripts/MAC_BUILD_INSTRUCTIONS.md)）。