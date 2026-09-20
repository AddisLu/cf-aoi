#!/usr/bin/env bash
# 截取中心主機（grab）一鍵安裝 — Ubuntu 22.04 / 24.04
#
# 冪等：重跑不會弄壞已完成的步驟；每步都印出做了什麼。
# 需要 sudo（會裝套件、改網路設定、寫 sysctl）。
#
#   ./bootstrap.sh [--debs DIR] [--skip-ebus] [--skip-network] [--skip-build] [--dry-run]
#
# --debs DIR  放 pylon_*.deb / codemeter_*.deb / *ebus*.deb 的目錄（預設 ~/下載 與 ~/下載/pylon）
#
# 網路設定值集中在下面 CONFIG 區塊，與 damac 一致（docs/grab_machine_migration.md 有說明）。
set -uo pipefail

# ── CONFIG ──────────────────────────────────────────────────────────────────
# 相機網段：.200 = 本機；.1–.37 給 37 台 CCD。169.254 是為了看得到出廠 AutoIP 的新相機。
# 192.168.4.2 = L803K 經 iPORT 的網段（iPORT 在 .53/.54）；tools/cam_align 的 --srcip 預設值，
# 少了這條 l800_serial.py / cl_health.py / multi_trigger.py 全都連不到 iPORT。
CAM_ADDRS='192.168.5.200/24,169.254.0.200/16,192.168.4.2/24'
# RDMA 直連 Spark（對端 192.168.3.1）
RDMA_ADDRS='192.168.3.2/24'
MTU=9000
# 兩張網卡以「哪張連交換機、哪張直連 Spark」區分；預設沿用 damac 的 ConnectX-5 埠序：
#   f0（…:18）= RDMA 直連、f1（…:19）= 相機網段。拆卡搬過去 MAC 不變，可直接用。
RDMA_MAC='98:03:9B:06:CC:18'
CAM_MAC='98:03:9B:06:CC:19'
# ────────────────────────────────────────────────────────────────────────────

DEB_DIRS=("$HOME/下載" "$HOME/下載/pylon" "$HOME/Downloads" "$HOME/Downloads/pylon")
DO_EBUS=1 DO_NET=1 DO_BUILD=1 DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --debs) DEB_DIRS=("$2"); shift 2 ;;
    --skip-ebus) DO_EBUS=0; shift ;;
    --skip-network) DO_NET=0; shift ;;
    --skip-build) DO_BUILD=0; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知參數：$1"; exit 1 ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WARN=()
step() { printf '\n\033[1m── %s\033[0m\n' "$1"; }
note() { printf '   %s\n' "$1"; }
warn() { printf '   ⚠ %s\n' "$1"; WARN+=("$1"); }
run()  { if [ "$DRY" = 1 ]; then printf '   [dry-run] %s\n' "$*"; else "$@"; fi; }

find_deb() {  # find_deb <glob>
  local d f
  for d in "${DEB_DIRS[@]}"; do
    [ -d "$d" ] || continue
    f=$(ls -1 "$d"/$1 2>/dev/null | sort | tail -1)
    [ -n "$f" ] && { echo "$f"; return 0; }
  done
  return 1
}

. /etc/os-release
step "環境：$PRETTY_NAME  kernel $(uname -r)  repo=$REPO"
[ "${VERSION_ID%%.*}" -ge 22 ] || warn "未在 Ubuntu <22.04 驗證過"

# ── 1. apt 套件 ─────────────────────────────────────────────────────────────
step '1/7 apt 套件（建置 + RDMA + 診斷 + 調機工具）'
PKGS=(build-essential cmake pkg-config git nlohmann-json3-dev
      rdma-core libibverbs-dev librdmacm-dev ibverbs-utils infiniband-diags perftest
      python3 python3-pip python3-pil ethtool network-manager openssh-server gdb tcpdump)
MISSING=()
for p in "${PKGS[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || MISSING+=("$p"); done
if [ ${#MISSING[@]} -eq 0 ]; then note '全部已安裝'; else
  note "待裝：${MISSING[*]}"
  run sudo apt-get update -qq
  run sudo apt-get install -y "${MISSING[@]}"
fi

# ── 2. pylon ────────────────────────────────────────────────────────────────
step '2/7 Basler pylon（raL8192 / GigE 相機）'
if dpkg -s pylon >/dev/null 2>&1; then
  note "已安裝 pylon $(dpkg -s pylon | awk '/^Version/{print $2}')"
else
  if deb=$(find_deb 'pylon_*.deb'); then
    note "安裝 $deb"
    run sudo apt-get install -y "$deb"
    if cm=$(find_deb 'codemeter_*.deb'); then
      note "安裝 $cm（pylon 授權元件）"
      run sudo apt-get install -y "$cm"
    fi
  else
    warn ' 找不到 pylon deb：請從 damac ~/下載/pylon/ 複製過來，或到 baslerweb.com 下載後用 --debs 指定目錄'
  fi
fi
[ -x /opt/pylon/bin/pylonviewer ] && note 'pylon Viewer 就緒：QT_QPA_PLATFORM=xcb /opt/pylon/bin/pylonviewer'

# ── 3. eBUS SDK（L803K 經 iPORT；調機工具本身不需要它）────────────────────
step '3/7 Pleora eBUS SDK（18 台 L803K 路徑）'
if [ "$DO_EBUS" = 0 ]; then note '略過（--skip-ebus）'
elif dpkg -s ebus-sdk >/dev/null 2>&1; then note "已安裝 $(dpkg -s ebus-sdk | awk '/^Version/{print $2}')"
elif deb=$(find_deb '*[eE]BUS*.deb'); then
  case "$deb" in
    *22.04*) [ "${VERSION_ID}" = '24.04' ] && warn "手上只有 22.04 版 eBUS（$(basename "$deb")）；24.04 請向 Pleora 索取對應版本，裝不起來屬預期" ;;
  esac
  note "安裝 $deb"
  run sudo apt-get install -y "$deb" || warn 'eBUS SDK 安裝失敗（見上方訊息）——不影響 raL8192 與調機工具'
else
  note '找不到 eBUS deb，略過（L803K 到貨前不需要）'
fi

# ── 4. 網路（相機網段 / RDMA 直連 / MTU 9000）──────────────────────────────
step '4/7 網路設定'
if [ "$DO_NET" = 0 ]; then note '略過（--skip-network）'; else
  iface_of_mac() {  # 以 MAC 找介面名（換機/換槽位介面名會變，MAC 不會）
    local mac=${1,,} i
    for i in $(ls /sys/class/net); do
      [ "$(cat /sys/class/net/$i/address 2>/dev/null)" = "$mac" ] && { echo "$i"; return 0; }
    done
    return 1
  }
  setup_nic() {  # setup_nic <MAC> <addrs> <用途>
    local mac=$1 addrs=$2 what=$3 dev name
    if ! dev=$(iface_of_mac "$mac"); then
      warn "找不到 MAC $mac 的網卡（$what）→ 網路未設定。確認卡已插上：lspci | grep -i mellanox"
      return
    fi
    name="cfaoi-$what"
    note "$what：$mac → $dev  ($addrs, MTU $MTU)"
    if nmcli -t -f NAME con show | grep -qx "$name"; then
      run sudo nmcli con mod "$name" ipv4.addresses "$addrs" ipv4.method manual \
          802-3-ethernet.mtu "$MTU" connection.interface-name "$dev" connection.autoconnect yes
    else
      run sudo nmcli con add type ethernet con-name "$name" ifname "$dev" \
          ipv4.method manual ipv4.addresses "$addrs" 802-3-ethernet.mtu "$MTU" \
          connection.autoconnect yes ipv6.method ignore
    fi
    run sudo nmcli con up "$name" >/dev/null
  }
  setup_nic "$CAM_MAC"  "$CAM_ADDRS"  cam
  setup_nic "$RDMA_MAC" "$RDMA_ADDRS" rdma
fi

# ── 5. sysctl ───────────────────────────────────────────────────────────────
step '5/7 sysctl（GVSP 收包緩衝 + 多網段回路檢查）'
SYSCTL=/etc/sysctl.d/90-cfaoi.conf
read -r -d '' WANT <<'EOF'
# CF-AOI 截取中心：相機 GigE 取像用
# GVSP 是 UDP，一幀數千包瞬間湧入 → 收包緩衝要夠大（預設 212KB 會掉包）
net.core.rmem_max=33554432
# 主機同時掛多個網段（相機 192.168.5 / 169.254 / RDMA 192.168.3）→ 嚴格回路檢查會丟掉
# 從「非預期介面」進來的相機廣播（GVCP 探索回應）
net.ipv4.conf.all.rp_filter=0
net.ipv4.conf.default.rp_filter=0
EOF
if [ -f "$SYSCTL" ] && diff -q <(echo "$WANT") "$SYSCTL" >/dev/null 2>&1; then
  note "已是最新：$SYSCTL"
else
  note "寫入 $SYSCTL"
  [ "$DRY" = 1 ] || echo "$WANT" | sudo tee "$SYSCTL" >/dev/null
  run sudo sysctl -q --system
fi

# ── 6. 群組 / 桌面捷徑 ──────────────────────────────────────────────────────
step '6/7 使用者群組與桌面捷徑'
if id -nG | grep -qw dialout; then note '已在 dialout 群組（交換機 console 線）'; else
  note '加入 dialout（USB 轉序列的交換機 console 線；重新登入後生效）'
  run sudo usermod -aG dialout "$USER"
fi
DESK="$HOME/桌面"; [ -d "$DESK" ] || DESK="$HOME/Desktop"
if [ -d "$DESK" ]; then
  if [ "$DRY" = 0 ]; then
    cat > "$DESK/cam-align.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=光學調機（線掃相機）
Comment=線掃相機直線度調整（raL8192 原生 GigE / L803K 經 iPORT）
Exec=python3 $REPO/tools/cam_align/cam_align.py
Icon=camera-video
Terminal=false
Categories=Science;
EOF
    cat > "$DESK/pylon-viewer.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=pylon Viewer
Comment=Basler pylon Viewer（強制 XCB / X11 後端）
Exec=env QT_QPA_PLATFORM=xcb /opt/pylon/bin/pylonviewer
Icon=pylon-viewer
Terminal=false
Categories=Science;
EOF
    chmod +x "$DESK"/*.desktop
    # GNOME 規定桌面啟動器除了可執行，還要標記 trusted，否則圖示打紅叉、
    # 顯示成檔名而非 Name=、雙擊也不會啟動（等同右鍵「允許啟動」）。
    # 經 SSH 跑時沒有 session bus，要自己指到使用者的 bus。
    if command -v gio >/dev/null; then
      export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}"
      ok=1
      for f in "$DESK"/*.desktop; do gio set "$f" metadata::trusted true || ok=0; done
      [ "$ok" = 1 ] && note '桌面啟動器已標記 trusted（桌面按 F5 重整生效）' \
        || warn '桌面啟動器標記 trusted 失敗 → 圖示會打紅叉、雙擊不啟動；在桌面右鍵該圖示選「允許啟動」即可'
    fi
  fi
  note "桌面捷徑已寫入 $DESK"
fi

# ── 7. 建置 grab + 離線測試 ────────────────────────────────────────────────
step '7/7 建置 grab 與離線測試'
if [ "$DO_BUILD" = 0 ]; then note '略過（--skip-build）'; else
  if [ "$DRY" = 1 ]; then note '[dry-run] cmake + 測試'; else
    if cmake -S "$REPO/grab" -B "$REPO/grab/build" >/dev/null && \
       cmake --build "$REPO/grab/build" -j"$(nproc)" >/dev/null 2>&1; then
      note '建置成功：cfaoi_grab / cam_provision / probe_cam_nodes …'
    else
      warn '建置失敗 → 重跑 `cmake --build grab/build` 看完整訊息（常見：pylon 未裝、gcc 版本差異）'
    fi
    run_test() {   # run_test <測試名> <測試檔> <原始檔...>
      local name=$1 main=$2; shift 2
      local srcs=() s out
      for s in "$@"; do srcs+=("$REPO/$s"); done
      out=$(mktemp)
      if g++ -std=c++17 -pthread -I"$REPO/grab/test/b1_fault_containment/pylon_stub" \
             -I"$REPO/grab/src" "$REPO/$main" "${srcs[@]}" -o "$out" 2>/dev/null \
         && "$out" >/dev/null 2>&1; then
        note "測試通過：$name"
      else
        warn "測試未過：$name（見 grab/test/$name/README.md）"
      fi
      rm -f "$out"
    }
    run_test b1_fault_containment grab/test/b1_fault_containment/b1_fault_test.cpp grab/src/cam_pylon.cpp
    run_test stitch               grab/test/stitch/stitch_test.cpp               grab/src/cam_pylon.cpp
    run_test ccd_identity         grab/test/ccd_identity/ccd_identity_test.cpp   grab/src/cam_manager.cpp grab/src/cam_pylon.cpp
    python3 "$REPO/tools/cam_align/test_offline.py" >/dev/null 2>&1 \
      && note '測試通過：cam_align 離線（34 項）' || warn 'cam_align 離線測試未過'
  fi
fi

step '完成'
if [ ${#WARN[@]} -eq 0 ]; then
  echo '   無警告。下一步：./verify_grab_host.sh（接上相機/Spark 後的實機檢查）'
else
  printf '   %d 項要處理：\n' "${#WARN[@]}"
  printf '     - %s\n' "${WARN[@]}"
fi
