#!/usr/bin/env bash
# 一鍵啟動 Grab（截取中心，桌面圖示「CF-AOI Grab」呼叫本檔）。
#   - 帶齊產線參數；已在跑就不重複開（兩個 grab 會搶相機 → 後開的開不到相機）
#   - log 同時進畫面與 ~/cfaoi_logs/grab_YYYYMMDD.log（stdbuf：導到檔案時不延遲）
#   - 關掉這個視窗 = 停止 Grab
# 台數 / Spark 位址可用環境變數覆寫：CFAOI_CAM_COUNT（預設 6）、CFAOI_RDMA_DEST。
cd "$(dirname "$0")/../.." || exit 1
CAM_COUNT="${CFAOI_CAM_COUNT:-6}"            # ★ 用實際台數，不用 ALL（某台沒起來時 ALL 照樣 OK 只跑 N-1 台）
RDMA_DEST="${CFAOI_RDMA_DEST:-192.168.3.1:18515}"

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
