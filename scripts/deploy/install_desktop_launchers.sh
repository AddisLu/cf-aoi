#!/usr/bin/env bash
# 在本機桌面放 CF-AOI 一鍵圖示（工程/維護用；線上人員只操作 Windows 上的 Control）。
#   grab：Grab 主機 →「CF-AOI Grab」
#   ip  ：Spark     →「CF-AOI IP（生產）」「CF-AOI IP（調參）」
# 用法：scripts/deploy/install_desktop_launchers.sh grab|ip   （冪等，可重跑；不需 sudo）
set -euo pipefail
ROLE="${1:-}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DESK="$HOME/桌面"; [ -d "$DESK" ] || DESK="$HOME/Desktop"
[ -d "$DESK" ] || { echo "找不到桌面資料夾（~/桌面 或 ~/Desktop）"; exit 1; }

entry() {  # entry <檔名> <名稱> <說明> <指令> <圖示>
  cat > "$DESK/$1.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$2
Comment=$3
Exec=bash $4
Icon=$5
Terminal=true
Categories=Science;
EOF
  chmod +x "$DESK/$1.desktop"
  # GNOME：除了可執行還要 trusted，否則打紅叉、雙擊不啟動（等同右鍵「允許啟動」）。
  # 經 SSH 跑時沒有 session bus → 指到使用者的 bus。
  if command -v gio >/dev/null; then
    DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}" \
      gio set "$DESK/$1.desktop" metadata::trusted true 2>/dev/null \
      || echo "  ⚠ $1：標記 trusted 失敗 → 桌面右鍵該圖示選「允許啟動」"
  fi
  echo "  ✓ $DESK/$1.desktop（$2）"
}

case "$ROLE" in
  grab)
    entry cfaoi-grab "CF-AOI Grab" "啟動截取中心（相機取像 → RDMA 送 Spark）；關視窗 = 停止" \
          "$REPO/scripts/launchers/Grab-Linux.sh" camera-video ;;
  ip)
    entry cfaoi-ip-production "CF-AOI IP（生產）" "Spark 切到生產模式（收 Grab 影像檢測）並看即時 log" \
          "$REPO/scripts/launchers/IP-Spark-Linux.sh production" applications-science
    entry cfaoi-ip-tuning "CF-AOI IP（調參）" "Spark 切到調參模式（Control 檢測複判上傳影像）並看即時 log" \
          "$REPO/scripts/launchers/IP-Spark-Linux.sh tuning" applications-engineering ;;
  *) echo "用法：$0 grab|ip"; exit 1 ;;
esac
echo "桌面按 F5 重整即可看到圖示。"
