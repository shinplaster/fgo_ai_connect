# WDA ビルド手順（Mac + Xcode）— iOS 17+ / pymobiledevice3 構成

WebDriverAgent (WDA) は Xcode プロジェクトのため Mac + Xcode でビルドする。Windows 単独では不可。
本アプリは Appium サーバを使わず pymobiledevice3 で直接 WDA を起動・操作するため、
Appium サーバ側の capability 設定は不要。**.app をビルドして署名し、Windows へ持ち込む** だけ。

ビルドは初回 1 回＋無料 Apple ID 署名の 7 日ごとの再ビルドが必要。

> 参考: [Run Prebuilt WDA](https://appium.github.io/appium-xcuitest-driver/latest/guides/run-prebuilt-wda/) / [Run Preinstalled WDA](https://appium.github.io/appium-xcuitest-driver/latest/guides/run-preinstalled-wda/)

## 1. 前提
- Xcode 15 以上（App Store からインストール）。iOS 26 対応なら Xcode 16+ を推奨。
- Xcode → Settings → Accounts で個人 Apple ID（無料）を追加（Personal Team）。
- iPhone 側: 開発者モード ON（設定→プライバシーとセキュリティ→開発者モード）。
  ※ 開発者モード項目が表示されない場合は、iPhone を Mac の Xcode に一度接続すると出現する。

## 2. クローン
```bash
git clone https://github.com/appium/WebDriverAgent.git
cd WebDriverAgent
```
※ 現行版は `.xcodeproj`（CocoaPods/Carthage 不要、Swift Package 依存）。リポジ直下に
`WebDriverAgent.xcodeproj` があることを確認。

## 3. 署名設定（無料 Apple ID / 7日有効 / 一意 bundle id）
Xcode で `WebDriverAgent.xcodeproj` を開く。以下の **2 target** の Signing & Capabilities を設定:
- **WebDriverAgentRunner** target
- **WebDriverAgentLib** target（存在する場合）

各 target で:
- Team: 個人 Apple ID（Personal Team）
- Bundle Identifier を一意に変更:
  - Runner: `io.appium.WebDriverAgentRunner.xctrunner` が既定。被る場合は
    `com.<あなたのappleid>.WebDriverAgentRunner.xctrunner` のように変更。
  - Lib: `com.<あなたのappleid>.WebDriverAgentLib` のように変更。
- Provisioning Profile は Personal Team で自動署名（"Automatically manage signing" ON）。

> bundle id は Windows 側 `config.py` の `wda_runner_bundle_id` と**完全一致**させること。

## 4. MJPEG サーバの有効化
appium WDA は既定で MJPEG サーバを内蔵するが、確実に有効化するため build 設定で定義:
- xcodebuild に `GCC_PREPROCESSOR_DEFINITIONS='USE_MJPEG_SERVER=1'` で渡す、または
- Xcode → WebDriverAgent target → Build Settings → Preprocessor Macros に `USE_MJPEG_SERVER=1` 追加。

MJPEG は WDA 起動後に `/mjpeg` エンドポイント（ポート 8100 または 9100）で配信される。
実機でエンドポイント/ポートを実確認し `config.py` の `wda_mjpeg_port` を調整。

## 5. ビルド（.app 生成）
```bash
xcodebuild build-for-testing \
  -project WebDriverAgent.xcodeproj \
  -scheme WebDriverAgentRunner \
  -destination 'generic/platform=iOS' \
  -derivedDataPath ./DerivedData \
  -allowProvisioningUpdates \
  CODE_SIGNING_ALLOWED=YES
```
成果物: `./DerivedData/Build/Products/Debug-iphoneos/WebDriverAgentRunner-Runner.app`

## 6. iOS 17+ 必須: 組み込み XCTest フレームワークを削除
iOS 17+ は testmanagerd のサービス名が変わり、.app に組み込まれた XCTest フレームワークの
ままでは WDA プロセスが起動直後にクラッシュする。**ビルド後に以下を削除**:
```bash
cd ./DerivedData/Build/Products/Debug-iphoneos
APP=WebDriverAgentRunner-Runner.app
rm -rf "$APP/Frameworks/XC*.framework"
rm -rf "$APP/Frameworks/Testing.framework"
rm -rf "$APP/Frameworks/libXCTestSwiftSupport.dylib"
# 念のため Frameworks 内に XCT* / XCTest* が残っていないか確認
ls "$APP/Frameworks" 2>/dev/null
```
> 参考: WebDriverAgent v5.10.0+ はデバイスローカルの XCTest FW を参照する設定があり、
> その場合は削除不要。クラッシュする場合のみ本手順を実施。

## 7. Windows 側へ持込み
`WebDriverAgentRunner-Runner.app` を **.app バンドル全体**（ディレクトリごと）Windows 側へ配置:
```
ios_connect/vendor/WebDriverAgentRunner-Runner.app
```
- Mac では `.app` はディレクトリ。Windows へは zip などでディレクトリ構造を維持して持込。
- `config.py` の `wda_app_path` と一致させる（既定値に合わせた）。

## 8. Windows 側で検証
iPhone を Windows PC に USB 接続・信頼済みであること（Phase 0 で確認済み）。
管理者権限で実行（tunnel 起動に必要）:
```
powershell -Command "Start-Process powershell -ArgumentList '-Command','cd C:\Users\shinp\claudecode\ios_connect; python scripts/verify_smoke.py' -Verb RunAs -Wait"
```
結果は `smoke_result.txt` に出力。以下が全て [OK] なら Phase 0 完了:
1. デバイス検出
2. tunnel 確立 → RSD
3. WDA .app インストール（apps install --rsd）
4. WDA 起動（developer wda launch --xctrunner --rsd）
5. WDA status / スクリーンショット

## 9. 7 日ごとの再ビルド
無料 Apple ID 署名は 7 日で失効。失効前に手順 5〜7 を再実行し .app を再持込・再インストール。
アプリ（Phase 5）は署名期限を監視し、期限 3 日前にブラウザ UI に警告を表示する。

## トラブルシュート
- **WDA 起動直後にクラッシュ**: 手順6（組み込み XCTest FW 削除）が未実施の可能性。
- **署名エラー**: bundle id が他者と重複している。手順3で一意に変更。
- **「デベロッパープロファイルが見つからない」**: iPhone で 設定→一般→VPNとデバイス管理 で
  Apple ID のデベロッパー証明書を信頼。
- **UI Automation 無効**: 設定→デベロッパー→UI Automation を ON。