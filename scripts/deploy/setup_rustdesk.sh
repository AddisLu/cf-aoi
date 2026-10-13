#!/bin/bash
# setup_rustdesk.sh — Spark 遠端桌面（隔著網路也順）：RustDesk，IP 直連、固定密碼、不需有人按「接受」
#
# 為什麼 RustDesk：開源免費、不需帳號；畫面用影片編碼（VP9/H.264），在 ~100 ms 延遲下比 VNC 順；
# 分享的是本機螢幕上那個桌面（X11），本機開的軟體都看得到。
# 為什麼不用 NoMachine：10 版 Personal Edition 要訂閱/啟用，fab 無外網也無法維持授權。
# fab 無外網：用「IP 直連」（direct-server，port 21118），不經 RustDesk 公用伺服器。
#
# 用法（在 Spark 上）：sudo bash setup_rustdesk.sh [/tmp/rustdesk.deb]   （會請你設定 RustDesk 連線密碼）
# 連線：客戶端（Mac/Windows 的 RustDesk）的 ID 欄直接輸入 Spark 的 IP（實驗室用 Tailscale IP）→ 輸入密碼
set -e
U="${SUDO_USER:?請用 sudo 執行}"
DEB="${1:-/tmp/rustdesk.deb}"
H=$(getent passwd "$U" | cut -d: -f6)

if dpkg -l | grep -qE "^ii +nomachine"; then
  echo "== 移除 NoMachine（未授權）"
  /usr/NX/scripts/setup/nxserver --uninstall >/dev/null 2>&1 || true
  dpkg --purge nomachine-personal-edition nomachine 2>/dev/null || true
fi

echo "== 安裝 RustDesk（$(hostname)）"
DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$DEB"

read -r -s -p "設定 RustDesk 連線密碼（遠端連進來用，至少 8 碼）: " P; echo
read -r -s -p "再輸入一次: " P2; echo
[ "$P" = "$P2" ] || { echo "兩次不一致"; exit 1; }
[ ${#P} -ge 8 ] || { echo "至少 8 碼"; exit 1; }

systemctl enable rustdesk >/dev/null 2>&1 || true
systemctl restart rustdesk; sleep 3
rustdesk --password "$P" >/dev/null 2>&1 || true

# 選項：IP 直連 21118、只用固定密碼、輸入密碼即可（不需本機按接受）
# ⚠️ 不能直接改 RustDesk2.toml：執行中的服務會把記憶體裡的設定寫回、蓋掉（實測 spark-02）。
#    用官方 CLI（root）經服務設定：rustdesk --option <key> <value>
for kv in "direct-server Y" "direct-access-port 21118" "approve-mode password" "verification-method use-permanent-password"; do
  rustdesk --option $kv >/dev/null 2>&1 || echo "  （--option $kv 失敗）"
done
for k in direct-server direct-access-port approve-mode verification-method; do
  echo "  $k = $(rustdesk --option $k 2>/dev/null | tail -1)"
done

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow 21118/tcp; fi

for i in $(seq 1 15); do
  sleep 2
  ss -ltn | grep -q ":21118 " && { echo "== OK：RustDesk IP 直連在 21118（$(hostname)）"; exit 0; }
done
echo "== 21118 還沒在聽"; systemctl status rustdesk --no-pager | tail -10; exit 1
