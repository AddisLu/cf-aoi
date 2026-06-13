#!/usr/bin/env bash
# bootstrap.sh — CF-AOI 一鍵建立完整目錄結構並複製 Reference 程式碼
# 使用方式：
#   1. 把這個 repo 的所有檔案放到 ~/cf-aoi/
#   2. 把舊版程式放到 Reference/（見下方說明）
#   3. cd ~/cf-aoi && bash bootstrap.sh
set -e

ROOT="$HOME/cf-aoi"
cd "$ROOT"

echo "╔══════════════════════════════════════════════════╗"
echo "║         CF-AOI Bootstrap 一鍵初始化              ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ─────────────────────────────────────────────────────────
# 1. 建立完整目錄結構
# ─────────────────────────────────────────────────────────
echo "[1/4] 建立目錄結構..."
mkdir -p docs
mkdir -p ip/src/gpu ip/src/ai ip/src/config ip/src/image_source ip/src/modes ip/src/tests
mkdir -p ip/config ip/models
mkdir -p grab/src grab/config
mkdir -p control/src
mkdir -p shared
mkdir -p scripts
mkdir -p test_images
mkdir -p Reference/gpu_algo Reference/legacy_win Reference/phase1_tests
echo "  ✅ 目錄完成"

# ─────────────────────────────────────────────────────────
# 2. 驗證 Reference 存在
# ─────────────────────────────────────────────────────────
echo "[2/4] 檢查 Reference 程式碼..."

GPU_OK=false
if [ -f "Reference/gpu_algo/src/cuda_kernels_fast.cu" ]; then
    GPU_OK=true
    echo "  ✅ Reference/gpu_algo（GPU 演算法）"
elif [ -f "Reference/gpu_algo/Demo/src/cuda_kernels_fast.cu" ]; then
    # 自動修正：把 Demo/ 內容上移
    echo "  ⚠️  偵測到 Reference/gpu_algo/Demo/，自動修正..."
    mv Reference/gpu_algo/Demo/* Reference/gpu_algo/
    rmdir Reference/gpu_algo/Demo 2>/dev/null || true
    GPU_OK=true
    echo "  ✅ 已修正 Reference/gpu_algo"
else
    echo "  ❌ Reference/gpu_algo/src/cuda_kernels_fast.cu 不存在"
    echo "     請把舊版 GPU 演算法（含 src/cuda_kernels_fast.cu）放到 Reference/gpu_algo/"
fi

if [ -d "Reference/legacy_win" ] && [ "$(ls -A Reference/legacy_win 2>/dev/null)" ]; then
    echo "  ✅ Reference/legacy_win（舊版 Windows 程式）"
else
    echo "  ⚠️  Reference/legacy_win 為空（Control 遷移時才需要，Step 1 可略過）"
fi

# ─────────────────────────────────────────────────────────
# 3. 複製 GPU kernels（不修改）+ config
# ─────────────────────────────────────────────────────────
if [ "$GPU_OK" = true ]; then
    echo "[3/4] 複製 GPU kernels（不修改任何邏輯）..."
    GA="Reference/gpu_algo"

    cp "$GA/src/cuda_kernels_fast.cu"             ip/src/gpu/cuda_kernels.cu
    cp "$GA/src/tensor_core_classifier.cu"        ip/src/ai/ai_kernels.cu
    cp "$GA/include/cuda_kernels.h"               ip/src/gpu/        2>/dev/null || true
    cp "$GA/include/tensor_core_classifier.h"     ip/src/ai/         2>/dev/null || true
    cp "$GA/include/config_parser.h"              ip/src/config/     2>/dev/null || true
    cp "$GA/include/inline_types.h"               ip/src/config/     2>/dev/null || true
    cp "$GA/include/rf_model_config.h"            ip/src/ai/         2>/dev/null || true

    # config.ini 可能在根目錄或 config/ 子目錄
    if   [ -f "$GA/config.ini" ];        then cp "$GA/config.ini"        ip/config/default_zone.ini
    elif [ -f "$GA/config/config.ini" ]; then cp "$GA/config/config.ini" ip/config/default_zone.ini
    fi
    [ -f "$GA/config_real.ini" ]      && cp "$GA/config_real.ini"      ip/config/ 2>/dev/null || true
    [ -f "$GA/config_optimized.ini" ] && cp "$GA/config_optimized.ini" ip/config/ 2>/dev/null || true

    # AI 模型（若有）
    [ -d "$GA/models" ] && cp -r "$GA/models/"* ip/models/ 2>/dev/null || true

    echo "  ✅ GPU kernels + config 複製完成"
else
    echo "[3/4] 跳過 GPU kernel 複製（Reference 未就緒）"
fi

# ─────────────────────────────────────────────────────────
# 4. 複製 FrameHeader（若 phase1_tests 存在）
# ─────────────────────────────────────────────────────────
echo "[4/4] 複製 FrameHeader.h..."
if [ -f "Reference/phase1_tests/shared/FrameHeader.h" ]; then
    cp Reference/phase1_tests/shared/FrameHeader.h shared/
    echo "  ✅ FrameHeader.h（從 phase1_tests）"
else
    echo "  ⚠️  phase1_tests 不存在，FrameHeader.h 將由 Claude Code 依 CLAUDE.md 規格生成"
fi

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║                  Bootstrap 完成                  ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""
echo "下一步："
echo "  1. VS Code 開啟 cf-aoi.code-workspace"
echo "  2. 開啟 Claude Code 面板，貼上 docs/PROMPTS.md 中的 Step 1 prompt"
echo "  3. Build：cd ip && cmake -S . -B build -DCMAKE_CUDA_ARCHITECTURES=75 && cmake --build build -j\$(nproc)"
echo ""
echo "完整步驟見 docs/SETUP.md"
