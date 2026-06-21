# Windows 側セットアップ手順

## 1. iTunes / Apple Mobile Device Service
iOS 17 以降のトンネルには Apple Mobile Device Service (AMDS) と Apple Mobile Device USB Driver が必要。

- **推奨**: iTunes の standalone インストーラ（Microsoft Store 版でないもの）を入手しインストール。
  - https://www.apple.com/jp/itunes/ から 64bit 版をダウンロード
  - Store 版は AMDS の挙動が不安定という報告あり → standalone 推奨（要検証）
- インストール後、iPhone を USB 接続して「信頼」を許可。
- デバイスマネージャーで `Apple Mobile Device USB Driver` が当たっていることを確認。

## 2. Python
- Python 3.10 以上をインストール（https://www.python.org/）。
- `python --version` で確認。

## 3. 依存パッケージ
```bash
pip install -e .[dev]
```

> pymobiledevice3 の wheel が libusb をバンドルしているかは要確認。USB 認識に失敗する場合は pyusb / libusb のネイティブ DLL 配置が必要になる場合あり（Phase 0 で検証）。

## 4. iPhone 側の準備
1. 設定 → プライバシーとセキュリティ → **開発者モード** を ON（再起動が必要な場合あり）。
2. PC に USB 接続し、iPhone 画面の「このコンピュータを信頼しますか？」で「信頼」を選択。

## 5. CoreDevice pair（remote start-tunnel の前提・初回のみ）

本アプリの tunnel は **remote start-tunnel（CoreDevice pair・Python API）** を使う。
iOS 17.4+ で `pymobiledevice3 remote pair`（WiFi manual-pairing）は**標準 iPhone では広告しない**
（Apple TV / Corellium 向け）。代わりに **USB NCM 経由の autopair** で CoreDevice pair を確立する。

### 5-1. NCM ドライバ導入（iPhone を NCM モードにする）
iPhone を NCM（Network Control Model）モードにするため、WeTest / PerfDog 製 NCM ドライバを導入する。
- 同梱の `driveradd.exe /install` を管理者 PowerShell で実行（`drivers/ncm-win10|win11/*.inf` +
  `drivers/usbfilter/*.inf` を pnputil で導入・OS が自動判定・usbfilter が USB configuration 2=NCM モードに切替）。
- 導入後、iPhone を USB 接続すると `UsbNcm Network(WeTest)` アダプタが2つ現れる（control / data）。
- iPhone が `ncm._remoted._tcp.local` を広告するようになる（bonjour で検出可能）。
- 元に戻す場合は `driverdelete.exe /uninstall`。

> Windows asyncio は既定 ProactorEventLoop が UDP マルチキャストを受信できず bonjour が壊れる。
> 本アプリは `app/main.py` で `WindowsSelectorEventLoopPolicy` を強制設定済み（検証スクリプトも同様）。

### 5-2. CoreDevice pair の確立（初回のみ・要管理者）
```powershell
python scripts/verify_coredevice_pair.py
```
- iPhone 画面に「信頼」＋パスコード入力を求められたら応じる（autopair の consent）。
- 成功すると `~/.pymobiledevice3/remote_<identifier>.plist` に pair record が保存される。
- 2回目以降は既存 record の validate でスキップ（再ペアリング不要）。

## 6. スモーク検証（旧 lockdown start-tunnel 方式・参考）
```bash
python scripts/verify_smoke.py
```
デバイス認識 → tunnel 確立 → WDA 起動 → `/status` が `ready:true` になることを確認。
> `verify_smoke.py` は従来の lockdown start-tunnel（subprocess）方式を独立に検証するもので、
> 本番の remote start-tunnel 切替の対象外。本番起動前の実機確認は次のセクション7を使う。

## 7. 本番 tunnel 検証（remote start-tunnel・要管理者）

### 7-1. tunnel 単体確認
```powershell
python scripts/verify_coredevice_tunnel.py
```
- `tunnel UP: interface=pymobiledevice3-tunnel-<udid> address=fd63:.. rsdPort=<port> protocol=TCP`
- RSD サービス 74個・`com.apple.dt.testmanagerd.remote` / `testmanagerd.remote.automation` /
  `com.apple.mobile.lockdown.remote.trusted` / `installation_proxy.shim.remote` が含まれることを確認
  （これらが揃うことで testmanagerd 不在問題が解消する）。
- 結果は `coredevice_tunnel_result.txt` に出力される。

### 7-2. 本番起動
```powershell
python -m app.main *> backend.log 2>&1
```
- tunnel 起動には管理者権限が必須（TUN iface 作成・wintun）。
- ブラウザで `http://localhost:8000` を開く。
- `backend.log` で `tunnel ready: RSD=fd63:..:<port>` → `WDA ready` を確認。
- `/api/status` で `ready:true` を確認。

> `backend.log` は PowerShell `*>` で UTF-16LE BOM 付きになる。Python で `raw.decode('utf-16')` で読むか、
> UTF-8 に書き出してから確認すること。