#!/usr/bin/env bash
# iPORT 找不到的原因診斷 — 需要 root（tcpdump 抓包 + 暫時加 link-local IP）
#
# 用法:  sudo ./diagnose_link.sh [介面] [本機IP]
#
# 這支會做三件事，全部可逆：
#   1. 抓封包，看對面到底回了什麼（這一步就能看出裝置的真實 IP/MAC）
#   2. 暫時加一個 169.254.x.x/16 位址，重掃一次（GigE Vision 裝置沒有 DHCP 時
#      會 fallback 到 link-local，這是最常見的情形）
#   3. 移除該位址，還原原狀
set -uo pipefail

IF="${1:-enp0s31f6}"
SRC="${2:-192.168.5.2}"
HERE="$(cd "$(dirname "$0")" && pwd)"
LLA=169.254.99.99/16

if [[ $EUID -ne 0 ]]; then
    echo "請用 sudo 執行: sudo $0 $IF $SRC" >&2
    exit 1
fi

CAP=$(mktemp /tmp/iport_cap.XXXXXX.pcap)
cleanup() {
    ip addr del "$LLA" dev "$IF" 2>/dev/null || true
    rm -f "$CAP"
}
trap cleanup EXIT

echo "############ 1. 抓封包 + GVCP 廣播 ############"
tcpdump -i "$IF" -nn -e -s 512 -w "$CAP" 2>/dev/null &
TCPDUMP_PID=$!
sleep 1.5
python3 "$HERE/gvcp_probe_raw.py" "$IF" "$SRC" 3 >/dev/null 2>&1 || true
sleep 1.5
kill $TCPDUMP_PID 2>/dev/null; wait $TCPDUMP_PID 2>/dev/null

echo "--- 線上實際流量（排除我們自己送出的廣播）---"
tcpdump -r "$CAP" -nn -e 2>/dev/null | grep -v "$SRC.*3956 *$" | head -40
echo
echo "--- 所有出現過的來源 MAC ---"
tcpdump -r "$CAP" -nn -e 2>/dev/null | grep -oE '^[0-9:.]+ [0-9a-f:]{17}' | awk '{print $2}' | sort -u
echo
echo "--- ARP 封包 ---"
tcpdump -r "$CAP" -nn arp 2>/dev/null | head -20 || echo "  無"

echo
echo "############ 2. 加 link-local 位址後重掃 ############"
ip addr add "$LLA" dev "$IF" 2>/dev/null && echo "已暫時加上 $LLA" || echo "位址已存在或加入失敗"
sleep 1
python3 "$HERE/gvcp_probe_raw.py" "$IF" 169.254.99.99 2 || true

echo
echo "############ 3. 還原 ############"
ip addr del "$LLA" dev "$IF" 2>/dev/null && echo "已移除 $LLA"
echo
echo "=== 最終 ARP 表 ==="
ip neigh show dev "$IF"
