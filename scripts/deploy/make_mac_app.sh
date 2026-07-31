#!/usr/bin/env bash
# 把 Control 打包成 macOS 雙擊即開的 .app（線上人員用，免終端機、免裝 .NET）。
# 用法：bash scripts/deploy/make_mac_app.sh [輸出目錄，預設 ~/Desktop]
# 產物：<輸出目錄>/CF-AOI Control.app
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="${1:-$HOME/Desktop}"
APP="$DEST/CF-AOI Control.app"
SRC="$REPO/control/src"
PUB="$SRC/publish/osx-arm64"

echo "[1/3] dotnet publish（self-contained osx-arm64）…"
(cd "$SRC" && dotnet publish -c Release -r osx-arm64 --self-contained true -o publish/osx-arm64 >/dev/null)

echo "[2/3] 組 .app bundle → $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp -R "$PUB/." "$APP/Contents/MacOS/"
chmod +x "$APP/Contents/MacOS/CfAoiControl"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>CF-AOI Control</string>
  <key>CFBundleDisplayName</key><string>CF-AOI Control</string>
  <key>CFBundleIdentifier</key><string>com.cfaoi.control</string>
  <key>CFBundleExecutable</key><string>CfAoiControl</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

echo "[3/3] 完成。雙擊或： open \"$APP\""
echo "設定檔在 app 內：$APP/Contents/MacOS/appsettings.json（改連線目標直接編輯它）"
