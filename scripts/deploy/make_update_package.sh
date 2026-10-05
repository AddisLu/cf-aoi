#!/usr/bin/env bash
# 產生離線更新包（在有網路、已 git pull 到要發佈版本的機器上執行；商業化階段 3）。
#   scripts/deploy/make_update_package.sh [輸出目錄，預設 ~/cfaoi_updates]
# 產出 <輸出目錄>/cfaoi-update-<版本>/：
#   cf-aoi.bundle                        整個 git repo（含全部歷史；fab 內 apply_update.sh 從這裡 fast-forward）
#   cfaoi-control-win-x64-<版本>.zip     同一版的 Windows Control 免安裝包
#   apply_update.sh、更新說明.txt
# 整個資料夾複製到 USB 帶進 fab。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_BASE="${1:-$HOME/cfaoi_updates}"
cd "$ROOT"
if ! git diff --quiet HEAD || ! git diff --cached --quiet; then
  echo "✗ 工作樹有未提交的修改 → 先 commit（更新包必須對應一個明確的版本）"; git status --short; exit 1
fi
VER=$(git rev-parse --short HEAD)
OUT="$OUT_BASE/cfaoi-update-$VER"
rm -rf "$OUT"; mkdir -p "$OUT"
echo "═══ 產生離線更新包 $VER → $OUT ═══"

git bundle create "$OUT/cf-aoi.bundle" --all 2>/dev/null
git bundle verify -q "$OUT/cf-aoi.bundle" >/dev/null
echo "  ✓ cf-aoi.bundle（$(du -h "$OUT/cf-aoi.bundle" | cut -f1)）"

scripts/deploy/package_control_windows.sh >/dev/null
cp "control/publish/cfaoi-control-win-x64-$VER.zip" "$OUT/"
echo "  ✓ cfaoi-control-win-x64-$VER.zip"

cp scripts/deploy/apply_update.sh "$OUT/"
cat > "$OUT/更新說明.txt" <<EOF
CF-AOI 離線更新包  版本 $VER（$(date +%F)）
$(git log -1 --format='%s')
==========================================================

一、Grab + Spark（在 Grab 主機上，開終端機）
  1) 插 USB，進到這個資料夾
  2) bash apply_update.sh . --spark
     - Grab：驗證 → 往前更新 → 編譯 → 重啟 Grab 與代理
     - Spark：經直連網段（192.168.3.1）送過去做同樣的事
     - 工作樹有未提交修改、或更新包比現在舊 → 會拒絕，不會亂蓋
  3) 最後一行應印出兩台版本都是 $VER

二、Windows Control
  1) 關閉 Control
  2) 解壓 cfaoi-control-win-x64-$VER.zip，把裡面 cf-aoi\\control\\ 整個覆蓋到
     C:\\Users\\<帳號>\\cf-aoi\\control\\（appsettings.json 若現場改過，先備份再蓋回）
  3) 開 Control →「系統設定 › 系統狀態」不應出現「版本不一致」警告

三、出問題
  - 編譯失敗：服務不會重啟，仍跑舊版；畫面會印回退指令
  - 不確定時：Control 系統狀態頁「收集診斷包」帶出 fab 給工程師
EOF
sed -i 's/$/\r/' "$OUT/更新說明.txt"
echo "✓ 完成：$OUT"
ls -la "$OUT"
