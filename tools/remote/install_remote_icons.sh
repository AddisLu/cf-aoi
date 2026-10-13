#!/bin/bash
# install_remote_icons.sh — Grab 桌面放「spark-02 / spark-03 遠端桌面」圖示（使用者層，不需 sudo；RustDesk 需先安裝）
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
DESK=$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" "$DESK"
install -m 755 "$HERE/cfaoi-remote" "$HOME/.local/bin/cfaoi-remote"
ICON=rustdesk
for x in "spark-02|上排 IP|Spark 上排（37 台相機的 IP）遠端桌面" "spark-03|下排 IP|Spark 下排（18 台相機的 IP）遠端桌面"; do
  n=${x%%|*}; r=${x#*|}; role=${r%%|*}; cm=${r#*|}
  f="cfaoi-remote-$n.desktop"
  cat > "$HOME/.local/share/applications/$f" <<D
[Desktop Entry]
Type=Application
Name=$n 遠端桌面（$role）
Comment=$cm
Exec=$HOME/.local/bin/cfaoi-remote $n
Icon=$ICON
Terminal=false
Categories=Network;RemoteAccess;
D
  install -m 755 "$HOME/.local/share/applications/$f" "$DESK/$f"
  gio set "$DESK/$f" metadata::trusted true 2>/dev/null || true      # GNOME：允許直接雙擊啟動
done
echo "已放到 $DESK"
