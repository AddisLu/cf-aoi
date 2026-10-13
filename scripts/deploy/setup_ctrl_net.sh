#!/bin/bash
# setup_ctrl_net.sh — Spark 網路整理：控制網固定 IP（RJ45 enP7s7）＋ 關掉一直失敗的預設 DHCP 連線
#
# 1. 系統預設「有線連線 N」（不綁網卡的 DHCP）會在沒有 DHCP 的網口（例 enP2p1s0f0np0）反覆失敗
#    → GNOME 一直跳「啟動網路連線失敗」。關掉它們的自動連線。
# 2. 有給 IP 時：RJ45 enP7s7 建 cf-ctrl，固定 IP、不設預設閘道（控制網只連 Control/Grab，不搶對外路由），
#    開機自動啟用 → 網路線一接上就生效。
#    下排 Spark（spark-03）= 192.168.10.12（docs/CLAUDE.md 控制網規劃：.12 Spark / .22 Grab）。
#
# 用法（在 Spark 上）：sudo bash setup_ctrl_net.sh [IP/前綴，如 192.168.10.12/24] [網卡，預設 enP7s7]
set -e
IP="${1:-}"; IF="${2:-enP7s7}"

nmcli -t -f NAME,TYPE con show | while IFS=: read -r name type; do
  case "$name" in
    有線連線*|"Wired connection"*)
      [ "$type" = 802-3-ethernet ] && nmcli con mod "$name" connection.autoconnect no && echo "  關閉自動連線：$name" ;;
  esac
done

if [ -n "$IP" ]; then
  nmcli con delete cf-ctrl >/dev/null 2>&1 || true
  nmcli con add type ethernet ifname "$IF" con-name cf-ctrl \
    ipv4.method manual ipv4.addresses "$IP" ipv4.never-default yes \
    ipv6.method disabled connection.autoconnect yes >/dev/null
  nmcli con up cf-ctrl >/dev/null 2>&1 && echo "  cf-ctrl 已啟用（$IF $IP）" \
    || echo "  cf-ctrl 已建立（$IF $IP）；網路線還沒接 → 接上後自動啟用"
fi
nmcli -t -f NAME,DEVICE,AUTOCONNECT con show | grep -vE "^(lo|docker|br-|tailscale)"
