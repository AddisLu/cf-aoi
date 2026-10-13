#!/bin/bash
# setup_x11vnc.sh — 分享 Spark「本機螢幕上那個桌面」（不是另開桌面）：x11vnc + xrdp「本機桌面」入口
#
# 為什麼：xrdp 每次連線是另開一個 X 桌面（:10…），看不到本機螢幕（GNOME，X11，:0/:1）上正在跑的軟體。
# x11vnc 直接分享本機 X 顯示 → 畫面、滑鼠、鍵盤與本機同步。
# 連法：
#   Mac：Finder ⌘K / 螢幕共享 → vnc://<Spark Tailscale 名稱>（內建，不必裝軟體）
#   Windows App / mstsc：連 xrdp → 登入畫面 Session 選「本機桌面」→ 輸入 VNC 密碼
# 安全：要 VNC 密碼；VNC 本身不加密 → 只走 Tailscale（WireGuard 加密）或 fab 內網。
#
# 用法（在 Spark 上）：sudo bash setup_x11vnc.sh   （會請你設定 VNC 密碼）
# fab 無外網：進場前把 x11vnc 及相依 .deb 放進離線更新包。
set -e
U="${SUDO_USER:?請用 sudo 執行}"
UID_U=$(id -u "$U")
H=$(getent passwd "$U" | cut -d: -f6)

echo "== 安裝 x11vnc（$(hostname)，帳號 $U）"
DEBIAN_FRONTEND=noninteractive apt-get install -y -q x11vnc x11-utils

if [ ! -s "$H/.vnc/passwd" ]; then
  echo "== 設定 VNC 連線密碼（遠端看本機桌面用；輸入兩次，問 Write password to ... 時回 y）"
  sudo -u "$U" mkdir -p "$H/.vnc"
  sudo -u "$U" x11vnc -storepasswd "$H/.vnc/passwd"
fi

# 找這個帳號在本機螢幕（seat0）上的 X 顯示：xrdp 用 :10 以上，排除；Xauthority 在 /run/user/<uid>/gdm/
cat > /usr/local/bin/x11vnc-local-desktop <<'EOF'
#!/bin/bash
export XAUTHORITY=/run/user/$(id -u)/gdm/Xauthority
while true; do
  for s in /tmp/.X11-unix/X*; do
    n=${s##*/X}
    [ "$n" -ge 10 ] 2>/dev/null && continue
    if xdpyinfo -display ":$n" >/dev/null 2>&1; then
      exec /usr/bin/x11vnc -display ":$n" -auth "$XAUTHORITY" -forever -shared -noxdamage -repeat \
           -rfbauth "$HOME/.vnc/passwd" -rfbport 5900 -nopw -quiet
    fi
  done
  sleep 5      # 本機桌面還沒登入（開機中 / 登入畫面）→ 等
done
EOF
chmod 755 /usr/local/bin/x11vnc-local-desktop

mkdir -p /etc/systemd/user
cat > /etc/systemd/user/x11vnc-local-desktop.service <<'EOF'
[Unit]
Description=分享本機螢幕桌面（x11vnc :5900）
After=graphical-session.target

[Service]
ExecStart=/usr/local/bin/x11vnc-local-desktop
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
loginctl enable-linger "$U"
sudo -u "$U" XDG_RUNTIME_DIR=/run/user/$UID_U systemctl --user daemon-reload
sudo -u "$U" XDG_RUNTIME_DIR=/run/user/$UID_U systemctl --user enable --now x11vnc-local-desktop.service

# xrdp 登入畫面多一個「本機桌面」：經 libvnc 轉到本機 x11vnc（Windows App 不必另裝 VNC 客戶端）
if ! grep -q "^\[LocalDesktop\]" /etc/xrdp/xrdp.ini; then
  cat >> /etc/xrdp/xrdp.ini <<'EOF'

[LocalDesktop]
name=Local Desktop (same as monitor)
lib=libvnc.so
ip=127.0.0.1
port=5900
username=na
password=ask
EOF
  systemctl restart xrdp
fi

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow 5900/tcp; fi

sleep 3
if ss -ltn | grep -q ":5900 "; then
  echo "== OK：本機桌面分享在 5900（$(hostname)）；xrdp 已加「本機桌面」入口"
else
  echo "== 5900 還沒在聽：本機桌面可能還沒登入（登入後會自動開始）"
  sudo -u "$U" XDG_RUNTIME_DIR=/run/user/$UID_U systemctl --user status x11vnc-local-desktop --no-pager | tail -8
fi
