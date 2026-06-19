#!/usr/bin/env bash
# CF-AOI 一鍵啟動：scripts/run.sh <target> [extra args]
#   自動 build（缺 build/ 才 configure）+ 帶預設參數執行。各跑在對應機器：
#   control=Mac/Linux ｜ ip=Linux RTX/Spark ｜ grab=Linux damac(需相機) ｜ golden=有影像即可 ｜ sim=任意。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
target="${1:-}"; shift 2>/dev/null || true

case "$target" in
  control)   # Control（C# Avalonia）。UpstreamServer 會自動監聽 8787。
    cd "$ROOT/control/src"; exec dotnet run "$@" ;;

  ip)        # IP offline-tcp，監聽 8200（RTX 2080 = sm_75）。
    cd "$ROOT/ip"
    [ -d build ] || cmake -S . -B build -DCMAKE_CUDA_ARCHITECTURES=75 -DCMAKE_BUILD_TYPE=Release
    cmake --build build -j"$JOBS"
    exec ./build/cfaoi_ip --mode offline-tcp --control-port 8200 "$@" ;;

  grab)      # Grab（需相機 + RDMA）。args 透傳，例：run.sh grab --rdma-dest 192.168.3.1:18515
    cd "$ROOT/grab"
    [ -d build ] || cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
    cmake --build build -j"$JOBS"
    echo "提示：cfaoi_grab 需相機+RDMA，例 --rdma-dest 192.168.3.1:18515" >&2
    exec ./build/cfaoi_grab "$@" ;;

  golden)    # golden_maker（對位 Mark）。args 透傳，例：run.sh golden --image frame.png --mark-rect 1170,770,60,60
    cd "$ROOT/tools/golden_maker"
    [ -d build ] || cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
    cmake --build build -j"$JOBS"
    exec ./build/golden_maker "$@" ;;

  sim)       # 上位機 CF_ 模擬器（需先開 Control）。args 透傳，例：run.sh sim --host 127.0.0.1
    exec python3 "$ROOT/scripts/upstream_simulator.py" "$@" ;;

  selftest)  # Control 無頭自測，例：run.sh selftest upstream / store / topology / singleccd
    cd "$ROOT/control/src"; exec dotnet run -- --selftest "$@" ;;

  *)
    cat >&2 <<EOF
用法: scripts/run.sh <target> [args]
  control   Control（Mac/Linux）— dotnet run，自動監聽上位機 8787
  ip        IP offline-tcp:8200（Linux RTX/Spark）— build+run
  grab      Grab（Linux damac，需相機+RDMA）— build+run，args 透傳
  golden    golden_maker（對位 Mark）— build+run，args 透傳
  sim       上位機 CF_ 模擬器（python，需先開 Control）
  selftest  Control 自測（run.sh selftest upstream|store|topology|singleccd|camera）
EOF
    exit 1 ;;
esac
