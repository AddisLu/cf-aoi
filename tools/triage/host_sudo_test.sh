#!/usr/bin/env bash
# 主機層故障實驗（需 sudo，故獨立成腳本由人執行），每項一定還原：
#   grab_mtu        Grab 相機網卡 MTU 9000 → 1500   → 相機大封包被丟（換網卡/重灌後沒設 jumbo）
#   ip_forward_off  Grab ip_forward 1 → 0             → Control（192.168.10.x）連不到 Spark 的 IP
#   bash tools/triage/host_sudo_test.sh [輸出資料夾，預設 ~/cfaoi_logs/fault_exp2]
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$HOME/cfaoi_logs/fault_exp2}"; mkdir -p "$OUT"
CAM=$(ip -o -4 addr show | awk '/192\.168\.5\.200\//{print $2; exit}')
[ -n "$CAM" ] || { echo "找不到相機網卡"; exit 1; }
echo "相機網卡：$CAM（實驗期間相機取像/Control→Spark 會受影響約 3 分鐘；生產中請勿執行）"
sudo -v || exit 1
MTU0=$(cat /sys/class/net/$CAM/mtu); FWD0=$(cat /proc/sys/net/ipv4/ip_forward)
restore() { sudo ip link set "$CAM" mtu "$MTU0"; sudo sysctl -q -w net.ipv4.ip_forward="$FWD0"; }
trap restore EXIT

run_case() {   # $1=情境 $2=症狀 $3=注入指令 $4=--only
  local name=$1 symptom=$2 inject=$3 only=$4
  echo "═══ $name ═══"
  eval "$inject"; sleep 3
  python3 "$HERE/cfaoi_triage.py" --only "$only" --json --out "$OUT" > "$OUT/$name.stdout.json"
  restore; sleep 5
  python3 "$HERE/cfaoi_triage.py" --only "$only" --json --out "$OUT" > "$OUT/$name.after.stdout.json"
  python3 - "$OUT" "$name" "$symptom" "$inject" <<'PY'
import json, os, sys
out, name, symptom, inject = sys.argv[1:5]
def move(stdout, tag):
    j = json.load(open(os.path.join(out, stdout)))
    os.replace(j['report'], os.path.join(out, f'{tag}.md')); os.replace(j['report'][:-3] + '.json', os.path.join(out, f'{tag}.triage.json'))
    return [f'{i["level"]} [{i["area"]}] {i["title"]}' for i in j['items'] if i['level'] in ('FAIL', 'WARN')]
f, a = move(f'{name}.stdout.json', name), move(f'{name}.after.stdout.json', f'{name}.after')
json.dump({'scenario': name, 'symptom': symptom, 'injected': {'cmd': inject},
           'fault': {'report': os.path.join(out, f'{name}.md'), 'problems': f}, 'restored': True,
           'after': {'report': os.path.join(out, f'{name}.after.md'), 'problems': a}},
          open(os.path.join(out, f'{name}.json'), 'w'), ensure_ascii=False, indent=1)
print('  健檢：', '；'.join(f) or '（無異常）'); print('  還原後：', '；'.join(a) or '正常')
PY
}

run_case grab_mtu 'Grab 主機換過網卡（或重灌）之後，所有相機的影像都有缺、常常 NG，相機和交換機燈都正常。' \
  "sudo ip link set $CAM mtu 1500" "camera,capture,host"
run_case ip_forward_off 'Control 上 IP 的燈一直是紅的，Grab 的燈是綠的；Spark 有開機，重開 Control 也一樣。' \
  "sudo sysctl -q -w net.ipv4.ip_forward=0" "rdma,host"
trap - EXIT
