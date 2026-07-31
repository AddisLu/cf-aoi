#!/usr/bin/env bash
# 在 Linux 端（Spark=IP / damac=Grab）安裝 systemd 服務：開機自動起、崩潰自動重啟，
# 線上人員不需碰終端機。須在目標機器上、於 repo 內執行（sudo 會提示密碼則輸入）。
#
# 用法：
#   bash scripts/deploy/install_linux_services.sh ip      # Spark：裝 cfaoi-ip-offline + cfaoi-ip-production
#   bash scripts/deploy/install_linux_services.sh grab    # damac：裝 cfaoi-grab
#
# 之後切換：
#   sudo systemctl start|stop cfaoi-ip-offline      # Step 1 調參模式（預設開機自啟）
#   sudo systemctl start cfaoi-ip-production        # Step 4/5 生產模式（rdma-process；兩者互斥自動擋）
#   sudo systemctl start|stop cfaoi-grab
set -euo pipefail

ROLE="${1:?用法: $0 ip|grab}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
RUN_USER="$(id -un)"
OUT="$HOME/cfaoi_output"
mkdir -p "$OUT"

install_unit() {  # $1=名稱 $2=unit 內容
    echo "$2" | sudo tee "/etc/systemd/system/$1.service" >/dev/null
    echo "  已安裝 /etc/systemd/system/$1.service"
}

if [ "$ROLE" = "ip" ]; then
    [ -x "$REPO/ip/build/cfaoi_ip" ] || { echo "找不到 $REPO/ip/build/cfaoi_ip（先編譯）"; exit 1; }
    # WorkingDirectory=ip/ → config/default_zone.ini 找得到
    install_unit cfaoi-ip-offline "[Unit]
Description=CF-AOI IP (offline-tcp, port 8200) - Step 1 調參
After=network-online.target
Conflicts=cfaoi-ip-production.service

[Service]
User=$RUN_USER
WorkingDirectory=$REPO/ip
ExecStart=$REPO/ip/build/cfaoi_ip --mode offline-tcp --output $OUT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target"

    # rdma-process 在 Grab 斷線後依設計退出 → Restart=always = 每片 session 自動重生
    install_unit cfaoi-ip-production "[Unit]
Description=CF-AOI IP (rdma-process, 8200+18515) - Step 4/5 生產
After=network-online.target
Conflicts=cfaoi-ip-offline.service

[Service]
User=$RUN_USER
WorkingDirectory=$REPO/ip
ExecStart=$REPO/ip/build/cfaoi_ip --mode rdma-process --recipe $REPO/recipes/DEFAULT/IP0/RecipeInfo.xml --output $OUT --rdma-port 18515
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target"

    sudo systemctl daemon-reload
    sudo systemctl enable --now cfaoi-ip-offline
    echo "✓ cfaoi-ip-offline 已啟用並啟動（開機自啟）；生產時改：sudo systemctl disable --now cfaoi-ip-offline && sudo systemctl enable --now cfaoi-ip-production"

elif [ "$ROLE" = "grab" ]; then
    [ -x "$REPO/grab/build/cfaoi_grab" ] || { echo "找不到 $REPO/grab/build/cfaoi_grab（先編譯）"; exit 1; }
    install_unit cfaoi-grab "[Unit]
Description=CF-AOI Grab (相機擷取, port 8100)
After=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$REPO/grab/build
ExecStart=$REPO/grab/build/cfaoi_grab --rdma-dest 192.168.3.1:18515 --cam-count ALL
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target"

    sudo systemctl daemon-reload
    sudo systemctl enable --now cfaoi-grab
    echo "✓ cfaoi-grab 已啟用並啟動（開機自啟）"
else
    echo "未知角色：$ROLE（ip|grab）"; exit 1
fi

echo "狀態： systemctl status cfaoi-*  ｜ log： journalctl -u <服務名> -f"
