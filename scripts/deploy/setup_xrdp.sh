#!/bin/bash
# setup_xrdp.sh — Spark（DGX OS / Ubuntu 24.04 aarch64）遠端桌面：xrdp + XFCE
#
# 為什麼不用 GNOME 內建遠端桌面：NVIDIA 機台 GDM 被 udev 規則強制 X11（/run/gdm3/custom.conf WaylandEnable=false），
# GNOME 46 的 RDP（桌面分享 / 遠端登入）都需要 Wayland → 不能用。
# 為什麼遠端用 XFCE：同一帳號本機已登入 GNOME 時，xrdp 再開 GNOME 常黑畫面；XFCE 是獨立的輕量桌面、用 CPU 繪圖，
# 不搶生產 IP 的 GPU。
#
# 用法（在 Spark 上，需 sudo）：sudo bash setup_xrdp.sh
# 連線：Windows「遠端桌面連線」(mstsc) / Linux Remmina → <Spark IP>:3389，帳號密碼 = Spark 的系統帳號
# fab 無外網：進場前把 xrdp xorgxrdp xfce4 xfce4-terminal dbus-x11 及相依 .deb 放進離線更新包（apt-get download）。
set -e
U="${SUDO_USER:?請用 sudo 執行}"

echo "== 安裝 xrdp + XFCE（$(hostname)，帳號 $U）"
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q xrdp xorgxrdp xfce4 xfce4-terminal dbus-x11

# xrdp 讀 TLS 私鑰需要 ssl-cert 群組
adduser xrdp ssl-cert >/dev/null

# 這個帳號的遠端桌面 = XFCE（本機 GNOME 不受影響）
sudo -u "$U" bash -c 'printf "%s\n" "unset DBUS_SESSION_BUS_ADDRESS" "unset XDG_RUNTIME_DIR" "exec startxfce4" > ~/.xsession && chmod +x ~/.xsession'

# 遠端登入後不要一直跳「建立色彩管理裝置需要驗證」視窗（xrdp 常見）
cat > /etc/polkit-1/rules.d/45-xrdp-colord.rules <<EOF
polkit.addRule(function(action, subject) {
  if (action.id.indexOf("org.freedesktop.color-manager.") == 0 && subject.user == "$U") {
    return polkit.Result.YES;
  }
});
EOF

systemctl enable xrdp >/dev/null
systemctl restart xrdp

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 3389/tcp
fi

sleep 2
if ss -ltn | grep -q ":3389 "; then
  echo "== OK：xrdp 在 3389 等待連線（$(hostname)）"
else
  echo "== 失敗：3389 沒有在聽"; systemctl status xrdp --no-pager | tail -15; exit 1
fi
