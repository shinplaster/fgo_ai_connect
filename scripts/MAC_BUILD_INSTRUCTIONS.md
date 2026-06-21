# Mac 側 Claude Code への指示：WDA keepalive ビルド〜実機インストール

appium WebDriverAgent に idle keepalive パッチを当てた `.app` をビルドし、実機へインストールしてください。
パッチは既に適用済み（`~/ios_connect_wda/WebDriverAgent/WebDriverAgentRunner/UITestingUITests.m`、オリジナルは `UITestingUITests.m.bak.20260621`）。
あなたは再パッチ不要、**ビルド〜インストールのみ**を行う。

## 対象デバイス
- UDID: 環境変数 `$IOS_CONNECT_UDID`（例: 00008130-... / iPhone16,1 / iOS 26.5）。事前に `export IOS_CONNECT_UDID=...` で設定。
- USB でこの Mac に接続済み

## 手順

### 1) ビルド（成果物は `./DerivedData` に出力）
```bash
cd ~/ios_connect_wda/WebDriverAgent
xcodebuild build-for-testing \
  -project WebDriverAgent.xcodeproj \
  -scheme WebDriverAgentRunner \
  -destination 'generic/platform=iOS' \
  -derivedDataPath ./DerivedData \
  -allowProvisioningUpdates \
  CODE_SIGNING_ALLOWED=YES \
  GCC_PREPROCESSOR_DEFINITIONS='USE_MJPEG_SERVER=1'
```

### 2) CodeSign が `errSecInternalComponent` で失敗した場合
ssh / 非GUI 実行で典型のキーチェーン ACL 問題。以下で鍵アクセスを許可してから 1) を再実行:
```bash
security unlock-keychain ~/Library/Keychains/login.keychain-db
# ↑パスワードを聞かれたらユーザーに入力してもらう
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k <ログインパスワード> ~/Library/Keychains/login.keychain-db
# <ログインパスワード> はユーザーに尋ねること。推測・保存しない。
```
その後 1) を再実行。

### 3) 組み込み XCTest フレームワークを削除（iOS17+ 必須）
`.app` と xctest バンドル**両方**の Frameworks から削除:
```bash
cd ~/ios_connect_wda/WebDriverAgent/DerivedData/Build/Products/Debug-iphoneos
APP=WebDriverAgentRunner-Runner.app
rm -rf "$APP/Frameworks/XC*.framework" "$APP/Frameworks/Testing.framework" "$APP/Frameworks/libXCTestSwiftSupport.dylib"
rm -rf "$APP/PlugIns/WebDriverAgentRunner.xctest/Frameworks/XC*.framework" "$APP/PlugIns/WebDriverAgentRunner.xctest/Frameworks/Testing.framework" "$APP/PlugIns/WebDriverAgentRunner.xctest/Frameworks/libXCTestSwiftSupport.dylib"
ls "$APP/Frameworks" "$APP/PlugIns/WebDriverAgentRunner.xctest/Frameworks" 2>/dev/null
# XCT*/XCTest* が残っていないか確認
```

### 4) デバイスへインストール（CoreDevice 経由）
```bash
xcrun devicectl device install app \
  --device "$IOS_CONNECT_UDID" \
  ~/ios_connect_wda/WebDriverAgent/DerivedData/Build/Products/Debug-iphoneos/WebDriverAgentRunner-Runner.app
```

## 報告してほしいこと
- ビルド成功/失敗と（失敗時）error の要因
- FW削除後の Frameworks 一覧
- `devicectl install` の成功/失敗
- 最終的な `.app` のフルパス

## 背景（参考）
- Windows 側 pymobiledevice3 からの .app インストールは iOS26 で AFC shim エラー(`No such service: com.apple.afc.shim.remote`)で失敗するため、Mac の CoreDevice(devicectl) でインストールする。
- keepalive パッチの目的: iOS26 が Xcode 未接続で起動した XCTest ランナーをアイドルで ~5秒で kill する問題（callstack/agent-device PR #700 と同じ機序）を、ランナー内の DispatchSource タイマー(3秒ごとに `WDA_RUNNER_IDLE_KEEPALIVE` を NSLog)で防ぐ。
- インストール後、Windows 側で .app を vendor/ に取り込み、バックエンドを `wda_skip_install=True` で再起動して keepalive 効果を検証する。