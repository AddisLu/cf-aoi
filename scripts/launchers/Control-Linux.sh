#!/usr/bin/env bash
# 雙擊啟動 Control（Linux 桌面：選「在終端機中執行」）。
cd "$(dirname "$0")/../.." && scripts/run.sh control
echo; read -r -p "Control 已結束。按 Enter 關閉…" _
