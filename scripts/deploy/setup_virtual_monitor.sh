#!/bin/bash
# 用法（在 Spark 上、桌面帳號，不需 sudo）：bash setup_virtual_monitor.sh
# 實測 2026-10-13：spark-02/03 的 NVIDIA 輸出全是 disconnected → RandR Monitors: 0 → RustDesk "No Displays"
# 沒接實體螢幕時，宣告一個涵蓋整個 X 畫面的虛擬 RandR 螢幕 → RustDesk 才列得到螢幕（否則 "No Displays"）
mkdir -p ~/.local/bin ~/.config/autostart
cat > ~/.local/bin/virtual-monitor.sh <<'S'
#!/bin/bash
sleep 3
xrandr --listmonitors | grep -q "^Monitors: 0" || exit 0      # 有實體螢幕就不加
read W H < <(xdpyinfo | awk '/dimensions:/{split($2,a,"x"); print a[1], a[2]}')
xrandr --setmonitor VIRTUAL-1 "${W}/508x${H}/286+0+0" none
S
chmod +x ~/.local/bin/virtual-monitor.sh
cat > ~/.config/autostart/virtual-monitor.desktop <<D
[Desktop Entry]
Type=Application
Name=Virtual monitor (RustDesk)
Exec=$HOME/.local/bin/virtual-monitor.sh
X-GNOME-Autostart-enabled=true
NoDisplay=true
D
pkill -9 -u "$(id -u)" -f "rustdesk --server"; pkill -9 -u "$(id -u)" -f "rustdesk --tray"
sleep 6; pgrep -af "rustdesk --server" | cut -c1-80; echo "$(hostname): autostart ok"
