#!/usr/bin/env bash
# 一鍵啟動 Grab（截取中心，桌面圖示「CF-AOI Grab」呼叫本檔）。
#   ★ 已裝 cfaoi-grab 服務（正式）：重啟服務 + 看即時 log；關視窗**不會**停 Grab（服務開機自啟、掛了自動重生）。
#     台數/位址改 /etc/default/cfaoi-grab。
#   未裝服務（過渡/開發）：前景執行如下——
#   - 帶齊產線參數；已在跑就不重複開（兩個 grab 會搶相機 → 後開的開不到相機）
#   - log 同時進畫面與 ~/cfaoi_logs/grab_YYYYMMDD.log（stdbuf：導到檔案時不延遲）
#   - 關掉這個視窗 = 停止 Grab
# 台數 / Spark 位址可用環境變數覆寫：CFAOI_CAM_COUNT（預設 6）、CFAOI_RDMA_DEST。
cd "$(dirname "$0")/../.." || exit 1
CAM_COUNT="${CFAOI_CAM_COUNT:-6}"            # ★ 用實際台數，不用 ALL（某台沒起來時 ALL 照樣 OK 只跑 N-1 台）
RDMA_DEST="${CFAOI_RDMA_DEST:-192.168.3.1:18515}"

if systemctl cat cfaoi-grab >/dev/null 2>&1; then
  echo "═══ CF-AOI Grab（服務）$(date '+%F %T')  設定：$(grep -E '^CAM_COUNT' /etc/default/cfaoi-grab 2>/dev/null) ═══"
  if pid=$(pgrep -x cfaoi_grab) && ! systemctl is-active -q cfaoi-grab; then
    echo "⚠ 有手動開的 Grab 在跑（PID $pid），會跟服務搶相機 → 先關掉那個視窗。"
    read -r -p "按 Enter 關閉…" _; exit 1
  fi
  # polkit 規則讓本帳號免密碼；沒有規則時退回 sudo（會問密碼）
  systemctl restart cfaoi-grab 2>/dev/null || sudo systemctl restart cfaoi-grab || {
    read -r -p "重啟失敗（見上方）。按 Enter 關閉…" _; exit 1; }
  sleep 1; systemctl --no-pager --lines=0 status cfaoi-grab | head -4
  echo "── 即時 log（關視窗只停看 log，Grab 繼續跑）──"
  exec journalctl -u cfaoi-grab -f -n 30 --no-pager -o cat
fi

if pid=$(pgrep -x cfaoi_grab); then
  echo "Grab 已在執行（PID $pid），不重複啟動。"
  echo "要重啟：先關掉原本那個 Grab 視窗（或 pkill -x cfaoi_grab），再按一次圖示。"
  read -r -p "按 Enter 關閉…" _; exit 0
fi

LOG_DIR="$HOME/cfaoi_logs"; mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/grab_$(date +%Y%m%d).log"
echo "═══ CF-AOI Grab  $(date '+%F %T')  台數=$CAM_COUNT  RDMA→$RDMA_DEST  log=$LOG ═══" | tee -a "$LOG"
if [ ! -x grab/build/cfaoi_grab ]; then   # 首次：先建置（之後改 code 請跑 bootstrap 或 cmake --build）
  cmake -S grab -B grab/build -DCMAKE_BUILD_TYPE=Release && cmake --build grab/build -j"$(nproc)" || {
    read -r -p "建置失敗（見上方）。按 Enter 關閉…" _; exit 1; }
fi
stdbuf -oL -eL grab/build/cfaoi_grab --rdma-dest "$RDMA_DEST" --cam-count "$CAM_COUNT" 2>&1 | tee -a "$LOG"
echo; read -r -p "Grab 已結束（見上方訊息）。按 Enter 關閉…" _
