#!/usr/bin/env bash
# 套用離線更新包（商業化階段 3；fab 內無網路、無 GitHub，靠 USB 帶 git bundle）。
#
# 在 Grab 主機上執行（更新包從 USB 複製過來或直接指 USB 路徑）：
#   scripts/deploy/apply_update.sh <更新包目錄或 cf-aoi.bundle> [--spark] [--no-restart]
#     --spark       順便更新 Spark（經 RDMA 直連網段 SSH：$CFAOI_SPARK_SSH，預設 auo001@192.168.3.1）
#     --no-restart  只更新 + 編譯，不重啟服務（等換班再重啟）
#
# 每台做的事：驗證 bundle → 確認工作樹乾淨 → 只允許「往前」更新（fast-forward，不會倒退/分岔）→
#   編譯（grab 或 ip，依 /etc/default/cfaoi-agent 的 ROLE）→ 重啟在跑的 cfaoi 服務 + 代理 → 印新版本。
# 任何一步失敗就停（set -e），**不會**留下半套：git 只在編譯前 fast-forward，編譯失敗會提示回退指令。
# Windows Control：把更新包裡的 cfaoi-control-win-x64-<版本>.zip 解壓覆蓋 cf-aoi\control（見 更新說明.txt）。
set -euo pipefail

SRC=""; DO_SPARK=0; RESTART=1
for a in "$@"; do
  case "$a" in
    --spark) DO_SPARK=1 ;;
    --no-restart) RESTART=0 ;;
    --local) ;;   # 舊參數，相容用
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) SRC="$a" ;;
  esac
done
[ -n "$SRC" ] || { echo "用法：$0 <更新包目錄或 .bundle> [--spark] [--no-restart]"; exit 1; }
BUNDLE="$SRC"; [ -d "$SRC" ] && BUNDLE="$SRC/cf-aoi.bundle"
[ -f "$BUNDLE" ] || { echo "找不到 bundle：$BUNDLE"; exit 1; }
BUNDLE="$(cd "$(dirname "$BUNDLE")" && pwd)/$(basename "$BUNDLE")"

# 本機角色與 repo：以節點代理設定為準（安裝時寫入）；沒有就用本腳本所在 repo
ROLE=""; REPO=""
[ -f /etc/default/cfaoi-agent ] && . /etc/default/cfaoi-agent
REPO="${REPO:-$(cd "$(dirname "$0")/../.." && pwd)}"
[ -n "$ROLE" ] || { echo "不知道本機角色（/etc/default/cfaoi-agent 沒有 ROLE）"; exit 1; }
cd "$REPO"

echo "═══ CF-AOI 離線更新  $(hostname)  角色=$ROLE  repo=$REPO ═══"
git bundle verify -q "$BUNDLE" >/dev/null || { echo "✗ bundle 驗證失敗（檔案損毀？）"; exit 1; }
if ! git diff --quiet HEAD || ! git diff --cached --quiet; then
  echo "✗ 工作樹有未提交的修改（git status 檢查）→ 不更新，以免蓋掉現場改動"; git status --short; exit 1
fi
OLD=$(git rev-parse --short HEAD)
git fetch -q "$BUNDLE" "+refs/heads/main:refs/remotes/usb/main"
NEW=$(git rev-parse --short usb/main)
if [ "$OLD" = "$NEW" ]; then
  echo "已是最新版 $OLD，不需更新"
elif git merge-base --is-ancestor HEAD usb/main; then
  echo "更新 $OLD → $NEW："; git log --oneline HEAD..usb/main | head -20
  git merge -q --ff-only usb/main
else
  echo "✗ 更新包 $NEW 不是目前版本 $OLD 的後續版本（會倒退或分岔）→ 拒絕。確認拿的是最新的更新包。"; exit 1
fi

# 編譯（cmake --build 失敗即非 0 結束碼）
echo "── 編譯（$ROLE）"
dir=$([ "$ROLE" = grab ] && echo grab || echo ip)
if ! out=$(cmake --build "$dir/build" -j"$(nproc)" 2>&1); then
  echo "$out" | grep -E "error" | head -20
  echo "✗ 編譯失敗 → 服務沒有重啟（仍跑舊程式）。回退：git reset --hard $OLD && cmake --build $dir/build"
  exit 1
fi
echo "  ✓ 編譯完成"

# 重啟：在跑的 cfaoi 服務 + 代理（polkit 免密碼；代理最後重啟，因為本腳本可能經代理以外的路徑執行）
if [ "$RESTART" = 1 ] && [ "$OLD" != "$NEW" ]; then
  echo "── 重啟服務"
  if [ "$ROLE" = grab ]; then units="cfaoi-grab"; else units="cfaoi-ip-production cfaoi-ip-offline"; fi
  for u in $units; do
    if systemctl is-active -q "$u"; then systemctl --no-ask-password restart "$u" && echo "  ✓ $u"; fi
  done
  systemctl --no-ask-password restart cfaoi-agent 2>/dev/null && echo "  ✓ cfaoi-agent" || true
fi
echo "✓ $(hostname) 版本：$(git rev-parse --short HEAD)"
# 服務設定（unit / polkit / 清理排程）有變 → apply 只換程式，unit 要重跑安裝腳本才會生效（需 sudo）
if [ "$OLD" != "$NEW" ] && ! git diff --quiet "$OLD" "$NEW" -- scripts/deploy/install_linux_services.sh; then
  echo "⚠ 這次更新改了服務設定 → 請在 $(hostname) 執行：bash $REPO/scripts/deploy/install_linux_services.sh $ROLE"
fi

# Spark：把 bundle 送過去，在那邊跑同一支腳本（用本機這份更新後的腳本，經 stdin 傳過去）
if [ "$DO_SPARK" = 1 ]; then
  SPARK="${CFAOI_SPARK_SSH:-auo001@192.168.3.1}"
  echo; echo "── 更新 Spark（$SPARK）"
  scp -q "$BUNDLE" "$SPARK:/tmp/cf-aoi.bundle"
  args="/tmp/cf-aoi.bundle"; [ "$RESTART" = 0 ] && args="$args --no-restart"
  ssh "$SPARK" "bash -s -- $args" < "$REPO/scripts/deploy/apply_update.sh"
  ssh "$SPARK" "rm -f /tmp/cf-aoi.bundle"
fi
