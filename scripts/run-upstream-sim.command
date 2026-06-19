#!/usr/bin/env bash
# Mac 雙擊執行上位機 CF_ 模擬器（需先開著 Control）。連 127.0.0.1:8787。
cd "$(dirname "$0")" && ./run.sh sim --host 127.0.0.1 --port 8787
echo; read -r -p "按 Enter 關閉…" _
