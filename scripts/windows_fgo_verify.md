# Windows で FGO 操作を検証する手順（2026-06-21）

## 目的

iPhone の FGO（Fate/Grand Order, bundle id `com.aniplex.fategrandorder`）を Windows PC から
画面表示＋タップ/スワイプ操作できるか検証する。Mac で本線 E2E を完遂済み（FGO 画面ストリーム＋
操作が動いた）。Windows でも同じコードで動くかがこの検証の目的。

## 背景（Mac で判明した知見）

Windows 版は以前「iOS15+ が WDA 起動時にデバイスをロック（パスコード要求）する」ため断念し
Mac へ移行した。しかし Mac（iOS 26.5 実機）での検証で、このロックは **target app に
springboard（システムUI）を指定したときに起きる** ことが判明。**target app に前面アプリ
（FGO など通常アプリ）を指定すると create_session が通りロックループを回避できる**。
この挙動は iOS 側のものでホスト OS に依存しないため、Windows でも FGO target で回避できる見込み。

Mac で確定した変更点（本パッケージに同梱済み）:
- `app/config.py`: `wda_target_bundle_id` = `com.aniplex.fategrandorder`、`tunnel_ready_timeout` 120s
- `app/main.py`: SelectorEventLoop 強制（Windows ProactorEventLoop 回避・bonjour 受信必須）、
  create_session のロック待ちリトライ（`session_unlock_wait_timeout`）、MJPEG keepalive は
  create_session 成功後に開始、session lost 誤判定修正
- `app/device/tunnel.py`: `get_rsds(udid=...)` ベース（Mac/Windows 両対応・Windows 依存
  if_nameindex workaround 削除）
- `scripts/verify_smoke.py`: Mac/本線方式化（Python API・TunnelManager）
- `vendor/WebDriverAgentRunner-Runner.app`: Mac+Xcode でビルドした WDA .app（bundle id
  `com.example.WebDriverAgentRunner.xctrunner`・Personal Team 署名・USE_MJPEG_SERVER=1・
  iOS17+ 組み込み XCTest FW 削除済み）

## 前提（Windows 側）

- NCM ドライバ導入済み（iPhone が NCM モード・`ncm._remoted._tcp.local` 広告）→ `scripts/windows_setup.md`
- Apple Mobile Device Service (AMDS) + Apple Mobile Device USB Driver（iTunes standalone 版）
- Python 3.13 venv（`.venv313`）に pymobiledevice3 が入っていること
  - 3.12 は sslpsk-pmd3 と OpenSSL が非互換で TCP `NO_CIPHERS_AVAILABLE` になるため 3.13 必須
- tunnel 起動には管理者権限必須（TUN インターフェース作成のため）

## 手順

### 1. Mac からパッケージを取り込む

Windows 側から `ssh mac` でログインできる前提。

```powershell
# パッケージを Mac から取得（Mac 側で /tmp/ios_connect_mac.tar.gz を作成済みの前提）
scp mac:/tmp/ios_connect_mac.tar.gz C:\Users\shinp\ios_connect\

# 既存リポジトリを最新で上書き（app/ scripts/ vendor/ が Mac 版に入れ替わる）
cd C:\Users\shinp\ios_connect
tar xzf ios_connect_mac.tar.gz
```

> Mac 側でまだ tar を作成していない場合は、Mac 側 Claude Code セッションで
> `cd ~/ios_connect && tar czf /tmp/ios_connect_mac.tar.gz --exclude='__pycache__' --exclude='*.pyc' app/ scripts/ pyproject.toml CLAUDE.md vendor/WebDriverAgentRunner-Runner.app`
> を実行してもらうこと。

### 2. Windows 環境の確認

```powershell
cd C:\Users\shinp\ios_connect
.venv313\Scripts\python -V                      # Python 3.13.x であること
.venv313\Scripts\python -c "import pymobiledevice3; from importlib.metadata import version; print(version('pymobiledevice3'))"

# CoreDevice pair record（Mac で作成したものと同じデバイス）
dir $env:USERPROFILE\.pymobiledevice3\remote_*.plist
```

pair record が無い場合は初回に consent ダイアログが出る（iPhone 側で「信頼」を承認）。
Mac で既に pair 済みでも Windows 側には別途 pair record が必要な場合がある。

### 3. iPhone をアンロック状態にしておく

WDA 起動検証はアンロック状態で行う。FGO はインストール済みであること（apps list で確認可）。

### 4. app.main を起動（管理者 PowerShell で）

**管理者権限の PowerShell** で実行（tunnel の TUN 作成に必要）:

```powershell
cd C:\Users\shinp\ios_connect
.venv313\Scripts\python -m app.main *> backend.log 2>&1
```

`app/main.py` の `main()` は全プラットフォームで SelectorEventLoop を強制するため、
Windows の ProactorEventLoop（bonjour UDP マルチキャスト受信不可）問題は回避済み。

### 5. 結果を確認

別 PowerShell でログを監視:

```powershell
Get-Content C:\Users\shinp\ios_connect\backend.log -Wait -Tail 20
```

成功時のログパターン（Mac で観測したもの）:
```
tunnel UP: interface=... RSD=<ipv6>:<port> protocol=TCP
RSD discovered via get_rsds: udid=... product=iPhone16,1 ios=26.5
tunnel ready: RSD=...
Installing + launching WDA via RSD ...
WDA ready. screen={'width': 852, 'height': 393, 'scale': 1.0}
mjpeg keepalive started (udid=...)
connected
mjpeg keepalive consuming frames (n=1)
mjpeg keepalive consuming frames (n=31)   # 3秒ごとに増え続ける = 5秒kill 回避
```

ブラウザで `http://127.0.0.1:8000` を開き:
- FGO の画面がストリーム表示されるか
- タップ・スワイプが効くか

### 6. 報告すべき観察点

以下を Mac 側セッション（またはユーザー）に報告すること:

1. **tunnel 確立**: 成功/失敗・エラー内容
2. **RSD の testmanagerd 公開**: ログの `RSD discovered` 後、`testmanagerd` サービスが
   取れるか（Mac では74サービス）。取れないと WDA 起動で `No such service` になる
3. **create_session（FGO target）**: ロックで弾かれず成功するか
   - `Unable to launch ... not, or could not be, unlocked` が出るか → ロック壁が Windows でも再発
   - 出なければ FGO target で Windows もロック回避成功
4. **MJPEG ストリーム**: フレーム受信が継続するか（5秒kill 回避）
5. **操作**: ブラウザからタップ/スワイプが効くか
6. **長時間安定**: 1分以上無停止で動くか

## 既知の注意点

- **WDA .app ビルドは Mac 必須**: Windows ではビルド不可。本パッケージの `vendor/` の .app を
  そのまま使う（iOS .app はホスト OS に依存しない）。署名は7日で失効（Personal Team）。
  `WdaDeployer.sign_remaining_days()` が期限を監視し `/api/status` が残り日数を返す。
- **wda_skip_install=True**: WDA は既にデバイスにインストール済み前提（Mac でインストール済み・
  同じデバイスなので Windows でもインストール済み）。iOS26 で `install_from_local` が
  `No such service: com.apple.afc.shim.remote` で失敗するためスキップ。署名失効時の再ビルド時のみ
  `config.wda_skip_install=False` で再インストール（要 Mac ビルド → vendor 配置）。
- **5秒kill**: iOS26 が WDA ランナーを5秒で OS レベル kill する問題。MJPEG keepalive（連続
  フレーム受信で I/O-hot に保つ）で回避確認済み（Mac で90秒以上生存）。
- **autolock**: Face ID 端末は自動ロック「なし」不可（最大5分）。`keepalive_interval` 240s ごとに
  画面中央 1px スワイプで autolock タイマーをリセット（`config.keepalive_enabled`）。