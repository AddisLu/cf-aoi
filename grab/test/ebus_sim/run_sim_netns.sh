#!/usr/bin/env bash
# 在獨立網路命名空間跑 ebus_sim（軟體假扮的 L803K/iPORT），讓本機 grab 用 --camera ebus 測取像。
# 為何要命名空間：eBUS 連不到「同一台主機自己跑的」軟體裝置（實測 NETWORK_ERROR）→ 用 veth 隔成兩台。
#
#   sudo grab/test/ebus_sim/run_sim_netns.sh start [寬=8160] [高=5000] [fps=2.4]
#   sudo grab/test/ebus_sim/run_sim_netns.sh stop
# 起來後：主機端 vgrab 192.168.77.1 ↔ 模擬裝置 192.168.77.2（MTU 9000）。grab 端：
#   CFAOI_EBUS_NO_SERIAL=1 grab/build/cfaoi_grab --camera ebus --line-rate keep --serial 192.168.77.2 ...
set -euo pipefail
NS=cfaoi-ebussim
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
SIM="$REPO/grab/build/ebus_sim"
ENV_SH=$(ls /opt/pleora/ebus/*/bin/set_puregev_env.sh | head -1)
RUN_AS="${SUDO_USER:-$(id -un)}"
LOG="/tmp/ebus_sim_netns.log"

case "${1:-}" in
  start)
    [ -x "$SIM" ] || { echo "先建置：cmake --build $REPO/grab/build --target ebus_sim"; exit 1; }
    ip netns list | grep -qw "$NS" && { echo "已在跑（先 stop）"; exit 1; }
    ip netns add "$NS"
    ip link add vgrab type veth peer name vsim
    ip link set vsim netns "$NS"
    ip addr add 192.168.77.1/24 dev vgrab
    ip link set vgrab mtu 9000 up
    ip netns exec "$NS" ip addr add 192.168.77.2/24 dev vsim
    ip netns exec "$NS" ip link set vsim mtu 9000 up
    ip netns exec "$NS" ip link set lo up
    ip netns exec "$NS" sudo -u "$RUN_AS" bash -c \
      "source '$ENV_SH'; exec '$SIM' vsim ${2:-8160} ${3:-5000} ${4:-2.4} SIM0038" > "$LOG" 2>&1 &
    sleep 2
    cat "$LOG"
    echo "✓ 模擬裝置 192.168.77.2（命名空間 $NS）；停止：sudo $0 stop" ;;
  stop)
    ip netns pids "$NS" 2>/dev/null | xargs -r kill || true
    sleep 1
    ip netns del "$NS" 2>/dev/null || true
    ip link del vgrab 2>/dev/null || true
    echo "✓ 已停止並清除" ;;
  *) echo "用法：sudo $0 start [寬 高 fps] | stop"; exit 1 ;;
esac
