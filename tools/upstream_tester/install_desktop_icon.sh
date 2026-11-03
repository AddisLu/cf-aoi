#!/bin/bash
# install_desktop_icon.sh — Grab 桌面放「上位機對測」圖示（使用者層，不需 sudo；只依賴 python3）
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
DESK=$(xdg-user-dir DESKTOP 2>/dev/null || true)
[ -d "$DESK" ] || { DESK="$HOME/桌面"; [ -d "$DESK" ] || DESK="$HOME/Desktop"; }
mkdir -p "$DESK" "$HOME/.local/share/applications"
F=cfaoi-upstream-tester.desktop
cat > "$HOME/.local/share/applications/$F" <<D
[Desktop Entry]
Type=Application
Name=上位機對測（CF_ 協議）
Comment=扮演上位機 Controller 連 Control:8787，送 CF_LOAD_RECIPE/GRAB_START/CHECK_ALIGN/SET_ALIGN/GET_RESULT 並對照規格檢查回應
Exec=python3 $HERE/upstream_tester.py
Icon=network-transmit-receive
Terminal=false
Categories=Network;Development;
D
install -m 755 "$HOME/.local/share/applications/$F" "$DESK/$F"
# GNOME：桌面啟動器要標 trusted 才能雙擊執行（同 grab_setup/bootstrap.sh）
gio set "$DESK/$F" metadata::trusted true 2>/dev/null || true
echo "已放到 $DESK/$F（雙擊開啟；預設目標 Control 192.168.10.1:8787，畫面上可改）"
