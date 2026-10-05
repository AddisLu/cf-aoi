#!/usr/bin/env bash
# 一鍵切換/啟動 Spark 上的 IP（桌面圖示「CF-AOI IP（生產）／（調參）」呼叫本檔）。
#   production：cfaoi-ip-production（rdma-process，收 Grab 的 RDMA 影像 → 檢測）
#   tuning    ：cfaoi-ip-offline（offline-tcp，Control「檢測複判」上傳影像調參用）
# 兩個 systemd 服務互斥（Conflicts=），啟動其一會自動停另一個。啟動後顯示即時 log；
# 關掉這個視窗**不會**停 IP（服務在背景跑，Grab 斷線後生產模式會自動重生）。
mode="${1:-production}"
case "$mode" in
  production) unit=cfaoi-ip-production; label='生產（rdma-process）' ;;
  tuning)     unit=cfaoi-ip-offline;    label='調參（offline-tcp）' ;;
  *) echo "用法：$0 production|tuning"; exit 1 ;;
esac
echo "═══ CF-AOI IP → $label  $(date '+%F %T') ═══"
if ! systemctl cat "$unit" >/dev/null 2>&1; then
  echo "找不到服務 $unit → 先安裝：scripts/deploy/install_linux_services.sh ip"
  read -r -p "按 Enter 關閉…" _; exit 1
fi
# polkit 規則（install_linux_services.sh 裝）讓本帳號免密碼；沒有規則時退回 sudo
systemctl restart "$unit" 2>/dev/null || sudo systemctl restart "$unit" || {
  read -r -p "啟動失敗（見上方）。按 Enter 關閉…" _; exit 1; }
sleep 2
systemctl --no-pager --lines=0 status "$unit" | head -5
echo "── 即時 log（Ctrl+C 或關視窗只停看 log，IP 繼續跑）──"
journalctl -u "$unit" -f -n 30 --no-pager -o cat
