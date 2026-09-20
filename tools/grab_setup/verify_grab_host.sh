#!/usr/bin/env bash
# 截取中心主機實機驗收 — 接上相機/交換機/Spark 之後跑。
# 只讀不改（除非加 --arm）。每項都印出實際數值，PASS/FAIL 一目了然。
#
#   ./verify_grab_host.sh [--arm]
#   --arm   額外跑一次 GRAB_ARM（會開相機、連 RDMA；需 Spark 端已起 rdma-process）
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ARM=0; [ "${1:-}" = '--arm' ] && ARM=1
PASS=0; FAIL=0
ok()   { printf '  \033[32mPASS\033[0m  %-42s %s\n' "$1" "${2:-}"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %-42s %s\n' "$1" "${2:-}"; FAIL=$((FAIL+1)); }
info() { printf '  ····  %-42s %s\n' "$1" "${2:-}"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

head_ '1. 網路（相機網段 / RDMA 直連）'
CAM_IF=$(ip -o -4 addr show | awk '/192\.168\.5\.200/{print $2; exit}')
RDMA_IF=$(ip -o -4 addr show | awk '/192\.168\.3\./{print $2; exit}')
[ -n "$CAM_IF" ] && ok '相機網段 192.168.5.200' "$CAM_IF" || bad '相機網段 192.168.5.200' '未設定'
[ -n "$RDMA_IF" ] && ok 'RDMA 網段 192.168.3.x' "$RDMA_IF" || bad 'RDMA 網段 192.168.3.x' '未設定'
ip -o -4 addr show | grep -q '169\.254\.0\.200' \
  && ok '169.254 自動 IP 網段（看得到出廠新相機）' \
  || bad '169.254 自動 IP 網段' '未設定 → 出廠 AutoIP 的新相機在 pylon 裡看不到'
ip -o -4 addr show | grep -q '192\.168\.4\.2' \
  && ok '192.168.4.2（L803K 經 iPORT 網段）' \
  || bad '192.168.4.2（L803K 經 iPORT）' '未設定 → tools/cam_align 的 --srcip 預設值連不到 iPORT'
for i in $CAM_IF $RDMA_IF; do
  m=$(cat "/sys/class/net/$i/mtu")
  [ "$m" = 9000 ] && ok "MTU 9000（$i）" || bad "MTU（$i）" "目前 $m，應為 9000"
  s=$(ethtool "$i" 2>/dev/null | awk -F': ' '/Speed/{print $2}')
  info "連線速率（$i）" "$s"
done

head_ '2. 系統參數'
r=$(sysctl -n net.core.rmem_max)
[ "$r" -ge 33554432 ] && ok 'net.core.rmem_max ≥ 32MB' "$r" || bad 'net.core.rmem_max' "$r（太小會掉包）"
p=$(sysctl -n net.ipv4.conf.all.rp_filter)
[ "$p" = 0 ] && ok 'rp_filter=0（多網段廣播收得到）' || bad 'rp_filter' "$p（相機 GVCP 廣播可能被丟）"

head_ '3. 軟體'
dpkg -s pylon >/dev/null 2>&1 \
  && ok 'pylon' "$(dpkg -s pylon | awk '/^Version/{print $2}')" || bad 'pylon' '未安裝'
dpkg -s ebus-sdk >/dev/null 2>&1 \
  && ok 'eBUS SDK（L803K 路徑）' "$(dpkg -s ebus-sdk | awk '/^Version/{print $2}')" \
  || info 'eBUS SDK' '未安裝（L803K 到貨前不需要）'
[ -x "$REPO/grab/build/cfaoi_grab" ] && ok 'cfaoi_grab 已建置' || bad 'cfaoi_grab' '未建置（跑 bootstrap.sh）'

head_ '4. 相機（pylon 列舉）'
if [ -x "$REPO/grab/build/cam_provision" ]; then
  out=$("$REPO/grab/build/cam_provision" list 2>&1)
  n=$(echo "$out" | grep -c 'raL\|L803\|Basler' || true)
  [ "$n" -gt 0 ] && ok '列舉到相機' "$n 台" || bad '列舉到相機' '0 台（檢查交換機埠 speed 1000 與供電）'
  echo "$out" | sed 's/^/        /'
  if [ "$n" -gt 0 ]; then   # 0 台時「未命名數=0」會假性通過，故先擋
    nouid=$(echo "$out" | awk 'NR>1 && $6=="" {c++} END{print c+0}')
    [ "$nouid" = 0 ] && ok '每台都有 CCD 身分（DeviceUserID）' \
      || bad 'CCD 身分' "$nouid 台未命名 → ARM 會被拒（用 cam_provision set <SN> CCDnn）"
  else
    info 'CCD 身分' '無相機可檢查'
  fi
  for ip in $(echo "$out" | awk 'NR>1{print $4}' | grep '^192\.168\.5\.'); do
    ping -c1 -W1 "$ip" >/dev/null 2>&1 && ok "ping $ip" || bad "ping $ip" '不通'
  done
else
  bad 'cam_provision' '未建置'
fi

head_ '5. RDMA（往 Spark）'
if command -v ibv_devinfo >/dev/null; then
  if ibv_devinfo 2>/dev/null | grep -q 'PORT_ACTIVE'; then
    ok 'RDMA port ACTIVE' "$(ibv_devinfo 2>/dev/null | awk '/hca_id/{h=$2} /fw_ver/{print h, "fw", $2; exit}')"
  else
    bad 'RDMA port' '非 ACTIVE（線未接或對端未起）'
  fi
  ibv_devinfo 2>/dev/null | grep -q 'Ethernet' \
    && ok 'link_layer = Ethernet（RoCE v2）' || bad 'link_layer' '非 Ethernet'
else
  bad 'ibverbs-utils' '未安裝'
fi
ping -c1 -W1 192.168.3.1 >/dev/null 2>&1 && ok 'ping Spark 192.168.3.1' || bad 'ping Spark 192.168.3.1' '不通'

head_ '6. 離線測試'
for t in "b1_fault_containment b1_fault_test.cpp cam_pylon.cpp" \
         "stitch stitch_test.cpp cam_pylon.cpp" \
         "ccd_identity ccd_identity_test.cpp cam_manager.cpp cam_pylon.cpp"; do
  set -- $t; name=$1; main=$2; shift 2
  srcs=(); for s in "$@"; do srcs+=("$REPO/grab/src/$s"); done
  bin=$(mktemp)
  if g++ -std=c++17 -pthread -I"$REPO/grab/test/b1_fault_containment/pylon_stub" -I"$REPO/grab/src" \
        "$REPO/grab/test/$name/$main" "${srcs[@]}" -o "$bin" 2>/dev/null && "$bin" >/dev/null 2>&1; then
    ok "grab 測試：$name"
  else
    bad "grab 測試：$name"
  fi
  rm -f "$bin"
done
python3 "$REPO/tools/cam_align/test_offline.py" >/dev/null 2>&1 \
  && ok '調機工具離線測試（34 項）' || bad '調機工具離線測試'

if [ "$ARM" = 1 ]; then
  head_ '7. GRAB_ARM 實機（需 Spark 端已起 rdma-process）'
  "$REPO/grab/build/cfaoi_grab" --rdma-dest 192.168.3.1:18515 --cam-count 4 --ctrl-port 8197 \
      > /tmp/verify_arm.log 2>&1 &
  gp=$!; sleep 3
  resp=$(python3 - <<'EOF'
import socket, json
try:
    s = socket.create_connection(("127.0.0.1", 8197), timeout=30)
    s.sendall(b'{"cmd":"GRAB_ARM","seq":1}\n')
    print(s.makefile("rb").readline().decode().strip())
except Exception as e:
    print(f'{{"status":"ERR","error":"{e}"}}')
EOF
)
  kill $gp 2>/dev/null; wait $gp 2>/dev/null
  echo "$resp" | grep -q '"status":"OK"' && ok 'GRAB_ARM' "$resp" || bad 'GRAB_ARM' "$resp"
  grep -E '開啟 raL|cam_manager\]   cam' /tmp/verify_arm.log | sed 's/^/        /'
fi

printf '\n\033[1m合計：%d 通過 / %d 失敗\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" = 0 ] || echo '（對照 docs/grab_machine_migration.md「驗收」節逐項排除）'
exit $([ "$FAIL" = 0 ] && echo 0 || echo 1)
