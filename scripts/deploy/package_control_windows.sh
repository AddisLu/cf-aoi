#!/usr/bin/env bash
# 產出 Windows 免安裝包：control/publish/cfaoi-control-win-x64-<git短碼>.zip
#   cf-aoi/control/  CfAoiControl.exe（自含 .NET 單檔）+ appsettings.json + config/
#   cf-aoi/recipes/  DEFAULT 配方        cf-aoi/output/（空）
#   cf-aoi/安裝說明.txt、Create-Desktop-Shortcut.cmd、Enable-Autostart.cmd
# 解壓到 Windows 使用者資料夾（C:\Users\<帳號>\cf-aoi）即符合 Control 預設路徑 ~/cf-aoi/...
# 需要 .NET 8 SDK（本機 user-local：~/.dotnet）；可在 Linux 交叉發佈 win-x64。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
[ -x "$HOME/.dotnet/dotnet" ] && export DOTNET_ROOT="$HOME/.dotnet" PATH="$HOME/.dotnet:$PATH"
export DOTNET_CLI_TELEMETRY_OPTOUT=1
ver="$(git rev-parse --short HEAD)$(git diff --quiet HEAD -- control recipes/DEFAULT || echo -dirty)"
scripts/build-control-app.sh win-x64
PKG="$ROOT/control/publish/pkg"; rm -rf "$PKG"
mkdir -p "$PKG/cf-aoi/control" "$PKG/cf-aoi/recipes" "$PKG/cf-aoi/output"
pub="$ROOT/control/publish/win-x64"
cp "$pub/CfAoiControl.exe" "$pub/appsettings.json" "$PKG/cf-aoi/control/"
cp -r "$pub/config" "$PKG/cf-aoi/control/"
cp -r recipes/DEFAULT "$PKG/cf-aoi/recipes/"
touch "$PKG/cf-aoi/output/.keep"
cp scripts/deploy/windows/*.cmd "$PKG/cf-aoi/"
sed "s/@VERSION@/git $ver（$(date +%F)）/" scripts/deploy/windows/安裝說明.txt | sed 's/$/\r/' > "$PKG/cf-aoi/安裝說明.txt"
zip_out="$ROOT/control/publish/cfaoi-control-win-x64-$ver.zip"
rm -f "$zip_out"; (cd "$PKG" && zip -qr "$zip_out" cf-aoi)
echo "完成 → $zip_out（$(du -h "$zip_out" | cut -f1)）"
