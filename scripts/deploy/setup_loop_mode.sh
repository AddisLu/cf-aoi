#!/usr/bin/env bash
# 機況助手（Loop + 本地大模型）運作模式設定 — 在「主 Spark」（跑 LoopEngineering 的那台）執行。冪等，可重跑。
#
# 運作模式（2026-10-05 定案）：
#   生產（run 貨）：只跑 Control / Grab / IP。大模型佔 Spark 約 80% 統一記憶體，與 IP 生產並存實測只剩約 5GB → 生產不載入。
#   機台有問題 / 調機：Windows Control「系統狀態 → 機況助手」→ 啟動 Loop + 載入大模型（兩台 Spark 叢集）；
#   用完按「結束並回生產」→ 兩台的模型容器與 Loop 都停掉。開著期間 Control 對上位機 CF_READY 回未就緒。
#
#   bash scripts/deploy/setup_loop_mode.sh [--model <Loop 本地模型 id>] [--fab]
#
# 做的事：
#   1. 節點代理（/etc/default/cfaoi-agent）加上 LOOP_* 設定 → Control 可開/關機況助手、看狀態；
#      代理在 192.168.3.1:4711 開轉送口給 Windows 開 Loop 畫面（Loop 本身仍只聽 127.0.0.1，Tailscale 存取照舊）
#   2. vLLM 叢集網卡改到 Spark↔Spark 線（port1：enp1s0f1np1 / rocep1s0f1,roceP2p1s0f1）；
#      2026-10-05 改接線後 port0 = 接 Grab 的 RDMA 線，原設定會把大模型的跨機流量送到 Grab 那條線上
#   3. 停掉目前載入的大模型（釋放記憶體 → 回生產狀態）
#   --fab  另外取消 Loop 開機自啟並停止 Loop（進 fab 前做；實驗室還要用 Loop 跑開發任務時先不要加）
set -euo pipefail
MODEL=deepseek-v4-flash-256k
FAB=0
while [ $# -gt 0 ]; do
  case "$1" in
    --model) MODEL="$2"; shift 2;;
    --fab) FAB=1; shift;;
    *) echo "用法：$0 [--model <id>] [--fab]"; exit 2;;
  esac
done
LOOP_DIR="$HOME/Addis/LoopEngineering"
VLLM_DIR="$HOME/Addis/spark-vllm-docker"
ENVF="$LOOP_DIR/.env"
L=http://127.0.0.1:4711
TOKEN=$(grep -E '^LOOP_API_TOKEN=' "$ENVF" 2>/dev/null | cut -d= -f2- | tr -d '"'"'" || true)
AUTH=(); [ -n "$TOKEN" ] && AUTH=(-H "Authorization: Bearer $TOKEN")
CONF=/etc/default/cfaoi-agent
[ -f "$CONF" ] || { echo "✗ 找不到 $CONF（先跑 scripts/deploy/install_linux_services.sh ip）"; exit 1; }

echo "═══ 1. 節點代理：機況助手設定 ═══"
if curl -s -m 5 "${AUTH[@]}" "$L/api/local/models" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if any(m['id']=='$MODEL' for m in d.get('models',[])) else 1)" 2>/dev/null; then
  echo "  模型 $MODEL ✓（Loop 已登錄）"
else
  echo "  ⚠ 無法向 Loop 確認模型 $MODEL（Loop 沒在跑？）— 仍寫入設定，開啟時若不存在 Control 會顯示錯誤"
fi
TMP=$(mktemp)
grep -v '^LOOP_\|^# 機況助手' "$CONF" > "$TMP" || true
cat >> "$TMP" <<EOF
# 機況助手（scripts/deploy/setup_loop_mode.sh 產生）：Control 開/關 Loop + 大模型
LOOP_ENABLED=1
LOOP_UNIT=loop-engineering
LOOP_URL=http://127.0.0.1:4711
LOOP_MODEL=$MODEL
LOOP_PUBLIC_URL=http://192.168.3.1:4711
LOOP_PROXY_LISTEN=192.168.3.1:4711
LOOP_ENV_FILE=$ENVF
EOF
sudo cp "$TMP" "$CONF"; rm -f "$TMP"
sudo systemctl restart cfaoi-agent
echo "  ✓ $CONF 已更新、cfaoi-agent 已重啟（轉送口 192.168.3.1:4711）"

echo "═══ 2. vLLM 叢集網卡 → Spark↔Spark 線（port1） ═══"
if [ -f "$VLLM_DIR/.env" ]; then
  cp -n "$VLLM_DIR/.env" "$VLLM_DIR/.env.bak-$(date +%Y%m%d)" || true
  sed -i -E 's/^ETH_IF=.*/ETH_IF="enp1s0f1np1"/; s/^IB_IF=.*/IB_IF="rocep1s0f1,roceP2p1s0f1"/' "$VLLM_DIR/.env"
  grep -E '^(CLUSTER_NODES|ETH_IF|IB_IF)=' "$VLLM_DIR/.env" | sed 's/^/  /'
  ping -c1 -W1 192.168.177.12 >/dev/null 2>&1 && echo "  ✓ 第二台 Spark 192.168.177.12 通" \
    || echo "  ⚠ 第二台 Spark 192.168.177.12 不通 → 在 spark-3961 上跑 scripts/deploy/fix_spark_link.sh（雙機載入前必須先通）"
else
  echo "  ⚠ 找不到 $VLLM_DIR/.env（略過）"
fi

echo "═══ 3. 停止目前的大模型（回生產狀態） ═══"
R=$(curl -s -m 200 -X POST "${AUTH[@]}" -H 'Content-Type: application/json' -d '{}' "$L/api/local/stop" || true)
echo "  ${R:0:200}"
if [ "$FAB" = 1 ]; then
  systemctl --user disable --now loop-engineering
  echo "  ✓ Loop 已停止並取消開機自啟（生產只跑 Control/Grab/IP；需要時由 Control 開）"
fi
sleep 2
echo "  可用記憶體：$(awk '/MemAvailable/{printf "%.1f GB", $2/1048576}' /proc/meminfo)"
echo "完成。Windows Control → 系統設定 → 系統狀態 → 「機況助手」卡片。"
