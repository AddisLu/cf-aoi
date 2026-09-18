#!/usr/bin/env bash
# 盤點本機「不在版控裡」的設定，輸出成一份純文字快照。
# 用途：換截取中心主機時的對照基準（新機器裝完拿它 diff）。
#   ./collect_machine_state.sh [輸出檔]      預設 machine_state_<hostname>_<日期>.txt
set -uo pipefail
OUT="${1:-machine_state_$(hostname)_$(date +%Y%m%d).txt}"

section() { printf '\n===== %s =====\n' "$1"; }

{
  printf 'CF-AOI 截取中心主機狀態快照\n主機：%s   時間：%s\n' "$(hostname)" "$(date -Is)"

  section 'OS / 核心'
  . /etc/os-release && echo "$PRETTY_NAME"
  uname -r

  section 'CPU / 記憶體'
  lscpu | grep -E '^(Model name|CPU\(s\)|Thread|Core)' | sed 's/  */ /g'
  free -h | head -2

  section '相機 / RDMA 相關套件'
  dpkg -l | awk '$1=="ii"{print $2"  "$3}' | grep -E \
    'pylon|ebus-sdk|codemeter|rdma-core|libibverbs|librdmacm|ibverbs|nlohmann|cmake|build-essential|^gcc' || true

  section '手動安裝的 apt 套件（apt-mark showmanual，已濾掉桌面/語系）'
  apt-mark showmanual 2>/dev/null | grep -vE '^(lib|linux-|x11|xserver|gnome|ubuntu-|fonts-|language-|thunderbird|ibus|hyphen|mythes)' | tr '\n' ' '
  echo

  section 'pip（使用者層）'
  python3 -c 'import PIL,sys; print("Pillow", PIL.__version__)' 2>/dev/null || echo 'Pillow 未安裝'

  section '網路介面'
  ip -br link
  ip -br addr

  section 'NetworkManager 連線（相機網段 / RDMA 直連）'
  nmcli -t -f NAME,DEVICE,TYPE con show
  for c in $(nmcli -t -f NAME con show | grep -E '^[0-9A-F]{2}-'); do
    echo "--- $c"
    nmcli -g connection.interface-name,ipv4.method,ipv4.addresses,ipv4.gateway,802-3-ethernet.mtu,connection.autoconnect con show "$c"
  done

  section '網卡連線狀態 / 速率'
  for i in $(ls /sys/class/net | grep -vE 'lo|tailscale|docker|virbr'); do
    printf '%-16s %s\n' "$i" "$(ethtool "$i" 2>/dev/null | grep -E 'Speed|Link detected' | tr '\n' ' ')"
  done

  section 'RDMA 裝置'
  ibv_devinfo 2>/dev/null | grep -E 'hca_id|fw_ver|state|link_layer' || echo '（無 RDMA 裝置或未裝 ibverbs-utils）'

  section 'sysctl 自訂（/etc/sysctl.conf）'
  grep -vE '^#|^$' /etc/sysctl.conf 2>/dev/null
  echo "--- 生效值"
  sysctl net.core.rmem_max net.ipv4.conf.all.rp_filter 2>/dev/null

  section 'udev 規則（相機/授權相關）'
  ls /etc/udev/rules.d/ | grep -viE 'snap|ipoib' || true

  section '使用者群組'
  id -nG

  section 'repo'
  for d in "$HOME"/Addis/*/; do
    [ -d "$d/.git" ] && echo "$d  $(git -C "$d" remote get-url origin 2>/dev/null)  $(git -C "$d" rev-parse --short HEAD 2>/dev/null)"
  done

  section '機器本地設定檔（不版控，換機要帶）'
  for f in "$HOME/Addis/cf-aoi/grab/cam_config.json" "$HOME/Addis/cf-aoi/grab/cam_map.json"; do
    [ -f "$f" ] && { echo "--- $f"; cat "$f"; } || echo "--- $f （不存在）"
  done

  section '桌面捷徑'
  for f in "$HOME"/桌面/*.desktop "$HOME"/Desktop/*.desktop; do
    [ -f "$f" ] && { echo "--- $f"; grep -E '^(Name|Exec)=' "$f"; }
  done 2>/dev/null

  section 'SSH 設定（只列主機，不含金鑰）'
  grep -E '^Host |HostName|User ' "$HOME/.ssh/config" 2>/dev/null || echo '（無 ~/.ssh/config）'
  ls "$HOME/.ssh"/*.pub 2>/dev/null

  section '安裝檔位置（換機要複製）'
  find "$HOME" -maxdepth 3 \( -name 'pylon_*.deb' -o -name '*ebus*SDK*.deb' -o -name 'codemeter_*.deb' \) 2>/dev/null
} > "$OUT" 2>&1

echo "已輸出：$OUT"
