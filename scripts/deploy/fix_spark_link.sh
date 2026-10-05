#!/usr/bin/env bash
# 第二台 Spark（spark-3961，下方 18 × L803K）的 Spark↔Spark 直連線修正 — 在 spark-3961 本機執行（螢幕或區網 ssh）。
#
# 現況（2026-10-05 查）：192.168.177.12 / 178.12 設在「沒接線」的 port，接線的 port 在發 DHCP →
# 主 Spark ping 不到 177.12，大模型雙機叢集（TP=2）無法啟動/會卡住。
# 主 Spark 的對應設定（照抄）：spark-link-177 = enp1s0f1np1 192.168.177.11/24、
#                             spark-link-178 = enP2p1s0f1np1 192.168.178.11/24、MTU 9000、無閘道。
#
#   bash scripts/deploy/fix_spark_link.sh            看現況 + 列出要改什麼（不改）
#   bash scripts/deploy/fix_spark_link.sh --apply    確認後套用（nmcli；舊的同名設定會被取代）
set -euo pipefail
APPLY=0; [ "${1:-}" = "--apply" ] && APPLY=1
carrier() { cat "/sys/class/net/$1/carrier" 2>/dev/null || echo 0; }

echo "═══ 網口現況 ═══"
for i in enp1s0f0np0 enp1s0f1np1 enP2p1s0f0np0 enP2p1s0f1np1; do
  [ -e "/sys/class/net/$i" ] || continue
  printf '  %-15s carrier=%s  %s\n' "$i" "$(carrier "$i")" "$(ip -br -4 addr show "$i" | awk '{print $3}')"
done
nmcli -t -f NAME,DEVICE con show --active | sed 's/^/  active: /'

# 接線的那個 port（f0 或 f1）：177 走 enp1s0fX，178 走同一實體埠的另一半 enP2p1s0fX
if [ "$(carrier enp1s0f1np1)" = 1 ]; then P=f1
elif [ "$(carrier enp1s0f0np0)" = 1 ]; then P=f0
else echo "✗ 兩個 port 都沒有 carrier：先確認 Spark↔Spark 線有插好"; exit 1; fi
IF177="enp1s0${P}np${P#f}"; IF178="enP2p1s0${P}np${P#f}"
echo "═══ 計畫 ═══"
echo "  spark-link-177 → $IF177 192.168.177.12/24 MTU 9000（無閘道、不設 DNS）"
echo "  spark-link-178 → $IF178 192.168.178.12/24 MTU 9000"
echo "  $IF177 / $IF178 上其他設定（例 DHCP「Wired connection」）→ 取消自動連線"
[ "$APPLY" = 1 ] || { echo "（未套用；確認無誤後加 --apply）"; exit 0; }
read -r -p "套用？[y/N] " a; [ "$a" = y ] || exit 0

for pair in "177:$IF177" "178:$IF178"; do
  net=${pair%%:*}; dev=${pair#*:}; name="spark-link-$net"
  # 同一網口上的其他設定取消自動連線（不刪，要回復可 nmcli con mod <名稱> connection.autoconnect yes）
  nmcli -t -f NAME,DEVICE con show | awk -F: -v d="$dev" -v n="$name" '$2==d && $1!=n {print $1}' |
    while read -r other; do sudo nmcli con mod "$other" connection.autoconnect no; sudo nmcli con down "$other" || true; done
  # 舊的 177/178 設定（可能綁在沒接線的 port）一併改掉
  nmcli -t -f NAME con show | grep -qx "$name" && sudo nmcli con delete "$name"
  sudo nmcli con add type ethernet ifname "$dev" con-name "$name" ipv4.method manual \
       ipv4.addresses "192.168.$net.12/24" ipv4.never-default yes ipv6.method disabled 802-3-ethernet.mtu 9000
  sudo nmcli con up "$name"
done
# 原本設在沒接線 port 上的 177.12 / 178.12（名稱不同的舊設定）
nmcli -t -f NAME con show | while IFS= read -r c; do           # 名稱可能有空白（Wired connection 1）
  case "$c" in spark-link-177|spark-link-178) continue;; esac
  if nmcli -g ipv4.addresses con show "$c" 2>/dev/null | grep -qE '192\.168\.17[78]\.12'; then
    sudo nmcli con mod "$c" connection.autoconnect no; sudo nmcli con down "$c" || true
    echo "  已停用舊設定 $c（含 177/178.12）"
  fi
done
sleep 2
ping -c2 -W1 192.168.177.11 && echo "✓ 主 Spark 177.11 通" || echo "⚠ 177.11 不通（確認主 Spark 那端是 port1 + 線材）"
ping -c2 -W1 192.168.178.11 && echo "✓ 主 Spark 178.11 通" || echo "⚠ 178.11 不通"
