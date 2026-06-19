#!/usr/bin/env bash
# 雙擊啟動 IP（Linux RTX/Spark）：build + 跑 offline-tcp，監聽 8200。
cd "$(dirname "$0")/../.." && scripts/run.sh ip
echo; read -r -p "IP 已結束。按 Enter 關閉…" _
