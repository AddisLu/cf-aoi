#!/usr/bin/env bash
# 雙擊啟動 Grab（Linux damac，需相機 + RDMA）：build + 跑。
# 預設帶 --rdma-dest（請依實機改）；其餘參數可在此追加。
cd "$(dirname "$0")/../.." && scripts/run.sh grab --rdma-dest 192.168.3.1:18515
echo; read -r -p "Grab 已結束。按 Enter 關閉…" _
