#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
#  上位機對測工具 — 一鍵安裝（Grab / 任何 Linux x64 桌面，免 sudo）
#
#  用法（擇一）：
#    1. 檔案管理員：對本檔按右鍵 →「以程式執行」(Run as a Program)
#    2. 終端機：    bash tools/upstream_tester/install.sh
#
#  做的事：
#    ① 檢查 python3（工具只用標準函式庫，不需 pip）
#    ② 複製程式到 ~/.local/share/cfaoi/upstream_tester/（repo 搬走/切分支也不影響桌面圖示）
#    ③ 桌面 + 應用程式選單放「上位機對測」圖示（GNOME 標記 trusted，可直接雙擊）
#    ④ 立刻開啟工具（瀏覽器開 http://127.0.0.1:8790/）
#
#  其他：
#    bash install.sh --no-launch   只安裝不開啟（SSH 遠端安裝用）
#    bash install.sh --uninstall   移除程式與圖示（保留設定檔與紀錄檔）
#  更新：git pull 後再執行一次本檔即可（設定檔 ~/.config/cfaoi/upstream_tester.json 會保留）。
# ═══════════════════════════════════════════════════════════════════════
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
APP="$HOME/.local/share/cfaoi/upstream_tester"
BIN="$HOME/.local/bin/cfaoi-upstream-tester"
ENTRY=cfaoi-upstream-tester.desktop
DESK=$(xdg-user-dir DESKTOP 2>/dev/null || true)
[ -n "$DESK" ] && [ -d "$DESK" ] || { DESK="$HOME/桌面"; [ -d "$DESK" ] || DESK="$HOME/Desktop"; }

say() {   # 終端機印字；從檔案管理員執行（沒有終端機）時改跳視窗
  if [ -t 1 ] || ! command -v zenity >/dev/null; then echo -e "$1"
  else zenity --"${2:-info}" --title="上位機對測工具" --width=460 --text="$1" 2>/dev/null || true; fi
}

if [ "$1" = "--uninstall" ]; then
  pkill -f "$APP/upstream_tester.py" 2>/dev/null || true
  rm -rf "$APP" "$BIN" "$HOME/.local/share/applications/$ENTRY" "$DESK/$ENTRY"
  say "已移除上位機對測工具（設定檔 ~/.config/cfaoi/upstream_tester.json 與紀錄 ~/cfaoi_upstream_tester/ 保留）。"
  exit 0
fi

# ① python3
if ! command -v python3 >/dev/null; then
  say "找不到 python3，請先安裝：\nsudo apt-get install -y python3" error; exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
  say "python3 版本太舊（需要 3.8 以上）：$(python3 --version 2>&1)" error; exit 1
fi

# ② 複製程式
for f in upstream_tester.py ui.html README.md; do
  [ -f "$HERE/$f" ] || { say "安裝檔不完整：缺 $HERE/$f" error; exit 1; }
done
pkill -f "$APP/upstream_tester.py" 2>/dev/null && sleep 0.5 || true   # 舊版在跑 → 先關，才會換成新版
mkdir -p "$APP" "$HOME/.local/bin" "$HOME/.local/share/applications" "$DESK"
install -m 755 "$HERE/upstream_tester.py" "$APP/upstream_tester.py"
install -m 644 "$HERE/ui.html" "$HERE/README.md" "$APP/"
rm -rf -- "$APP/__pycache__"
cat > "$BIN" <<EOF
#!/bin/bash
# 上位機對測工具啟動器（install.sh 產生）
exec python3 "$APP/upstream_tester.py" "\$@"
EOF
chmod 755 "$BIN"
python3 -c 'import ast,sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' "$APP/upstream_tester.py" \
  || { say "程式語法檢查失敗（安裝檔可能不完整）" error; exit 1; }

# ③ 桌面圖示
cat > "$HOME/.local/share/applications/$ENTRY" <<EOF
[Desktop Entry]
Type=Application
Name=上位機對測（CF_ 協議）
Comment=扮演上位機連 Control:8787，或扮演 AOI 端讓上位機連入；CF_ 指令收發、參數監控、Log
Exec=$BIN
Icon=network-transmit-receive
Terminal=false
Categories=Network;Development;
EOF
install -m 755 "$HOME/.local/share/applications/$ENTRY" "$DESK/$ENTRY"
# GNOME：桌面啟動器要標 trusted 才能雙擊執行（經 SSH 執行時沒有 session bus，失敗不影響）
gio set "$DESK/$ENTRY" metadata::trusted true 2>/dev/null || true
command -v update-desktop-database >/dev/null && update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

# ④ 開啟
MSG="✅ 安裝完成\n• 桌面圖示：$DESK/$ENTRY\n• 程式位置：$APP\n• 設定檔：~/.config/cfaoi/upstream_tester.json\n• 紀錄檔：~/cfaoi_upstream_tester/logs/"
if [ "$1" != "--no-launch" ] && [ -n "$DISPLAY$WAYLAND_DISPLAY" ]; then
  nohup setsid "$BIN" >/dev/null 2>&1 &
  say "$MSG\n\n工具已開啟（瀏覽器 http://127.0.0.1:8790/）。之後雙擊桌面「上位機對測」即可。"
else
  say "$MSG\n\n（沒有桌面環境或指定 --no-launch：未自動開啟。到 Grab 桌面雙擊「上位機對測」即可。）"
fi
