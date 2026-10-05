#!/usr/bin/env bash
# RDMA「線」故障實驗（需 sudo，故獨立成腳本由人執行）：Grab RDMA 網卡 link down → 一鍵健檢 → 一定接回 → 再健檢。
# 模擬「Grab ↔ Spark 直連線斷了」，驗證健檢能分辨「線」與「機器/程式」（後者見 fault_inject.py ip_down）。
#   bash tools/triage/rdma_cable_test.sh [輸出資料夾，預設 ~/cfaoi_logs/fault_exp]
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$HOME/cfaoi_logs/fault_exp}"; mkdir -p "$OUT"
NIC=$(ip -o -4 addr show | awk '/192\.168\.3\.2\//{print $2; exit}')
[ -n "$NIC" ] || { echo "找不到 192.168.3.2 的網卡"; exit 1; }
echo "RDMA 網卡：$NIC（實驗期間 Grab ↔ Spark 會斷線約 1–2 分鐘；生產中請勿執行）"
sudo -v || exit 1
restore() { sudo ip link set "$NIC" up; }
trap restore EXIT
sudo ip link set "$NIC" down
sleep 4
python3 "$HERE/cfaoi_triage.py" --only rdma,gpu --json --out "$OUT" > "$OUT/rdma_cable.stdout.json"
R=$(python3 -c "import json;print(json.load(open('$OUT/rdma_cable.stdout.json'))['report'])")
mv "$R" "$OUT/rdma_cable.md"; mv "${R%.md}.json" "$OUT/rdma_cable.triage.json"
restore; trap - EXIT
echo "已接回，等 link 與 RoCE 恢復…"; sleep 12
python3 "$HERE/cfaoi_triage.py" --only rdma --json --out "$OUT" > "$OUT/rdma_cable.after.stdout.json"
R=$(python3 -c "import json;print(json.load(open('$OUT/rdma_cable.after.stdout.json'))['report'])")
mv "$R" "$OUT/rdma_cable.after.md"; mv "${R%.md}.json" "$OUT/rdma_cable.after.triage.json"
python3 - "$OUT" <<'PY'
import json, sys, os
out = sys.argv[1]
def probs(f):
    d = json.load(open(os.path.join(out, f)))
    return [f'{i["level"]} [{i["area"]}] {i["title"]}' for i in d['items'] if i['level'] in ('FAIL', 'WARN')]
res = {'scenario': 'rdma_cable',
       'symptom': '上位機送料後沒有結果，Control 的 IP 燈是紅的，Spark 前面燈有亮。到底是線還是 Spark 壞了？',
       'injected': {'grab_nic_down': True},
       'fault': {'report': os.path.join(out, 'rdma_cable.md'), 'problems': probs('rdma_cable.triage.json')},
       'restored': True, 'after': {'report': os.path.join(out, 'rdma_cable.after.md'), 'problems': probs('rdma_cable.after.triage.json')}}
json.dump(res, open(os.path.join(out, 'rdma_cable.json'), 'w'), ensure_ascii=False, indent=1)
print('斷線時健檢：', '；'.join(res['fault']['problems']) or '（無異常）')
print('接回後健檢：', '；'.join(res['after']['problems']) or '正常')
PY
