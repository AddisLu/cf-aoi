#!/usr/bin/env bash
# 雙擊啟動 Control（Mac）。被 Gatekeeper 擋 → 右鍵「打開」一次。
cd "$(dirname "$0")/../.." && scripts/run.sh control
echo; read -r -p "Control 已結束。按 Enter 關閉…" _
