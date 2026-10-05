#!/usr/bin/env bash
# 產出 Control 的「免裝 .NET 單一執行檔」（給純操作人員雙擊，無需裝任何環境）。
# 用法: scripts/build-control-app.sh [rid]   rid 預設依本機；可指定 osx-arm64 / osx-x64 / linux-x64 / win-x64
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
rid="${1:-}"
if [ -z "$rid" ]; then
  case "$(uname -s)" in
    Darwin) [ "$(uname -m)" = arm64 ] && rid=osx-arm64 || rid=osx-x64 ;;
    Linux)  rid=linux-x64 ;;
    *)      rid=win-x64 ;;
  esac
fi
out="$ROOT/control/publish/$rid"
# 每次清空再發佈：對既有資料夾增量發佈時，appsettings.json（PreserveNewest）會消失且不補回
# （2026-10-05 產生離線更新包時踩到 → 打包 cp 失敗）
rm -rf "$out"
echo "發佈 Control 自含單檔 → rid=$rid"
cd "$ROOT/control/src"
dotnet publish -c Release -r "$rid" --self-contained true \
  -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true \
  -o "$out"
echo "完成 → $out"
echo "操作人員雙擊其中的 CfAoiControl（Windows 為 CfAoiControl.exe）即可，免裝 .NET。"
