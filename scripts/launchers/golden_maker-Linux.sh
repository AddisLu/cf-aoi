#!/usr/bin/env bash
# 雙擊啟動 golden_maker（Linux，需一張代表影像）。
# GUI 模式需 --image；沒帶會顯示用法。請把影像路徑填進下行的 --image。
cd "$(dirname "$0")/../.." && scripts/run.sh golden --image "${1:-}"
echo; read -r -p "golden_maker 已結束。按 Enter 關閉…" _
