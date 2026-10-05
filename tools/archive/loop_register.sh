#!/usr/bin/env bash
# 讓 Spark 上的 LoopEngineering 直接讀 Grab 的機台資料夾（遠端路徑 grab:/srv/cfaoi，經 SSH，不複製）。
# 在 Grab 上執行；經 ssh 呼叫 Spark 本機的 LoopEngineering API。冪等，可重跑。
#
#   bash tools/archive/loop_register.sh [--watch]
#
# 前提：
#   1. LoopEngineering 已含「遠端路徑」功能（feat/remote-paths，2026-10-05）
#   2. Spark 能用金鑰 SSH 到 Grab：在 Grab 上把 Spark 的公鑰加進 ~/.ssh/authorized_keys（限定來源 192.168.3.1）
#
# 做的事：
#   機台   grab  = user@192.168.3.2（RDMA 直連網段；fab 內唯一通路）→ 按「檢查」確認是 Linux
#   來源   grab:/srv/cfaoi（RAG）：收 log / jsonl / json / md / txt / ini / csv；
#          排除 40_defects（每片數千檔，摘要已在 70_knowledge）、50_raw、00_software、20_docs/cf-aoi（Spark 已有 git 來源）
#   --watch  另把 grab:/srv/cfaoi/10_logs 加進機況監看（新 incident 自動跑本地模型診斷；會在生產 Spark 上用 GPU，預設不開）
set -euo pipefail
CONF=/etc/default/cfaoi-archive
[ -f "$CONF" ] && { set -a; . "$CONF"; set +a; }
SPARK="${SPARK:-auo001@192.168.3.1}"
HOME_DIR="${CFAOI_HOME:-/srv/cfaoi}"
GRAB_TARGET="$(id -un)@192.168.3.2"
WATCH=0; [ "${1:-}" = "--watch" ] && WATCH=1

ssh -o BatchMode=yes "$SPARK" bash -s -- "$GRAB_TARGET" "$HOME_DIR" "$WATCH" <<'EOS'
set -euo pipefail
GRAB="$1"; ROOT="$2"; WATCH="$3"; L=http://127.0.0.1:4711
j() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1"; }
post() { curl -s --max-time "${3:-30}" -X POST -H 'Content-Type: application/json' -d "$2" "$L$1"; }

ssh -o BatchMode=yes -o ConnectTimeout=5 "$GRAB" true 2>/dev/null \
  || { echo "✗ Spark 還不能用金鑰 SSH 到 $GRAB（先把 Spark 公鑰加進 Grab 的 authorized_keys）"; exit 1; }

if curl -s "$L/api/machines/grab" | grep -q '"name"'; then echo "  機台 grab 已登錄"
else
  post /api/machines "{\"name\":\"grab\",\"ssh_target\":\"$GRAB\",\"work_root\":\"/home/${GRAB%@*}/loop\",\"os\":\"linux\",\"labels\":\"camera,cf-aoi\",\"description\":\"CF-AOI 上方 Grab（截取主機）；機台資料夾 $ROOT\"}" >/dev/null
  echo "✓ 機台 grab = $GRAB"
fi
echo "  健康檢查：$(post /api/machines/grab/check '{}' 120 | j "'OK' if d.get('ok') or (d.get('machine') or {}).get('last_check_ok') else d" 2>/dev/null || echo '?')"

URI="grab:$ROOT"
SID=$(curl -s "$L/api/sources" | j "next((s['id'] for s in d['sources'] if s['uri']=='$URI'), '')")
if [ -z "$SID" ]; then
  BODY=$(python3 -c "import json,sys; print(json.dumps({'kind':'remote','uri':sys.argv[1],'enabled':True,
    'include':['**/*.md','**/*.log','**/*.jsonl','**/*.json','**/*.txt','**/*.ini','**/*.csv'],
    'exclude':['40_defects/**','50_raw/**','00_software/**','20_docs/cf-aoi/**']}))" "$URI")
  R=$(post /api/sources "$BODY")
  SID=$(echo "$R" | j "(d.get('source') or {}).get('id','')")
  [ -n "$SID" ] || { echo "✗ 登錄來源失敗：$R"; exit 1; }
  echo "✓ RAG 來源 $SID ← $URI"
else
  echo "  RAG 來源已登錄（$SID）"
fi

if [ "$WATCH" = 1 ]; then
  CUR=$(curl -s "$L/api/settings" | j "d['settings'].get('diag_watch_dirs','')")
  ENTRY="cf-aoi=$URI/10_logs"
  if printf '%s\n' "$CUR" | grep -qxF "$ENTRY"; then echo "  機況監看已設定"
  else
    NEW=$(python3 -c "import json,sys; c=sys.argv[1].strip(); print(json.dumps({'key':'diag_watch_dirs','value':(c+'\n' if c else '')+sys.argv[2]}))" "$CUR" "$ENTRY")
    post /api/settings "$NEW" >/dev/null && echo "✓ 機況監看 += $ENTRY"
  fi
fi

echo "收錄中（第一次含 embedding，可能數分鐘；之後只讀有變的檔）…"
post /api/ingest "{\"source_id\":\"$SID\"}" 1800 | head -c 400; echo
EOS
