#!/usr/bin/env bash
# 在 Linux 端（Spark=IP / Grab 主機=Grab）安裝 systemd 服務：開機自動起、結束自動重啟，
# 線上人員不需碰終端機（商業化規劃階段 1，見 docs/commercial_deploy_plan.md）。
# 須在目標機器上、於 repo 內執行（sudo 會提示密碼則輸入）。冪等，可重跑。
#
# 用法：
#   bash scripts/deploy/install_linux_services.sh ip      # Spark：cfaoi-ip-production（開機自啟）+ cfaoi-ip-offline（調參，手動）
#   bash scripts/deploy/install_linux_services.sh grab    # Grab 主機：cfaoi-grab（開機自啟）
#
# 兩者都會裝 polkit 規則：本機操作帳號可**免密碼**啟停/重啟 cfaoi-*.service（桌面圖示、日後節點代理用）。
# 日常操作（免 sudo）：
#   systemctl restart cfaoi-grab | cfaoi-ip-production      systemctl start cfaoi-ip-offline（調參，與生產互斥）
#   journalctl -u <服務> -f                                  機台設定：/etc/default/cfaoi-grab（台數、Spark 位址）
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

# polkit：操作帳號免密碼管理 cfaoi-* 服務（只限這些 unit、只限 start/stop/restart/reload）
install_polkit() {
    sudo tee /etc/polkit-1/rules.d/50-cfaoi.rules >/dev/null <<EOF
// CF-AOI：$RUN_USER 可免密碼啟停 cfaoi-*.service（install_linux_services.sh 產生）
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.systemd1.manage-units" &&
        subject.user == "$RUN_USER" &&
        /^cfaoi-[A-Za-z0-9_-]+\.service\$/.test(action.lookup("unit")) &&
        ["start", "stop", "restart", "reload-or-restart", "try-restart"].indexOf(action.lookup("verb")) >= 0) {
        return polkit.Result.YES;
    }
    // 節點代理（以 $RUN_USER 執行）代 Control 重開機/關機（POWER 命令；代理端另需 confirm=true）
    if (subject.user == "$RUN_USER" &&
        ["org.freedesktop.login1.reboot", "org.freedesktop.login1.reboot-multiple-sessions",
         "org.freedesktop.login1.power-off", "org.freedesktop.login1.power-off-multiple-sessions"
        ].indexOf(action.id) >= 0) {
        return polkit.Result.YES;
    }
});
EOF
    echo "  已安裝 /etc/polkit-1/rules.d/50-cfaoi.rules（$RUN_USER 免密碼管理 cfaoi-*、重開機/關機）"
}

# 節點代理（商業化階段 2）：Control 遠端查狀態/重啟/看 log/收診斷包/重開機。以一般帳號執行。
install_agent() {  # $1=角色 grab|ip  $2=OUTPUT_DIR
    sudo tee /etc/default/cfaoi-agent >/dev/null <<EOF
# CF-AOI 節點代理設定（cfaoi-agent.service 讀取；install_linux_services.sh 產生）
ROLE=$1
REPO=$REPO
OUTPUT_DIR=$2
PORT=8300
# 允許來源：本機 / 控制網 / RDMA 直連網段 / Tailscale（開發期）
ALLOW=127.0.0.0/8,192.168.10.0/24,192.168.3.0/24,100.64.0.0/10
EOF
    install_unit cfaoi-agent "[Unit]
Description=CF-AOI 節點代理（Control 遠端管理，port 8300）
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
EnvironmentFile=/etc/default/cfaoi-agent
ExecStart=/usr/bin/python3 -u $REPO/tools/node_agent/cfaoi_agent.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target"
    sudo systemctl daemon-reload
    sudo systemctl enable cfaoi-agent >/dev/null 2>&1
    sudo systemctl restart cfaoi-agent
    echo "✓ cfaoi-agent 已啟用並啟動（port 8300，角色 $1）"
    install_cleanup
}

# 磁碟自動清理（階段 3）：每天 03:30 + 開機 15 分鐘後；ROLE/OUTPUT_DIR 沿用 /etc/default/cfaoi-agent
install_cleanup() {
    if [ ! -f /etc/default/cfaoi-cleanup ]; then
        sudo tee /etc/default/cfaoi-cleanup >/dev/null <<'EOF'
# CF-AOI 磁碟自動清理（cfaoi-cleanup.timer；改完不必重啟，下次執行生效）
# 檢測結果日期夾 / Grab log 保留天數
RETAIN_DAYS=30
# 原始影像（SaveSourceImage，每張 ~41MB）保留天數
SOURCE_RETAIN_DAYS=7
# 行車紀錄（_diag，很小）保留天數
DIAG_RETAIN_DAYS=180
# 水位保護：用量超過 MAX 就從最舊的刪到 TARGET（今天的資料永遠不刪）
MAX_USED_PCT=85
TARGET_USED_PCT=80
EOF
        echo "  已建立 /etc/default/cfaoi-cleanup（結果 30 天、原始影像 7 天、水位 85%→80%）"
    fi
    install_unit cfaoi-cleanup "[Unit]
Description=CF-AOI 磁碟自動清理（結果/原始影像/log 依保留天數 + 水位保護）

[Service]
Type=oneshot
User=$RUN_USER
EnvironmentFile=/etc/default/cfaoi-agent
EnvironmentFile=/etc/default/cfaoi-cleanup
ExecStart=/usr/bin/python3 -u $REPO/tools/node_agent/cfaoi_cleanup.py
Nice=19
IOSchedulingClass=idle"
    sudo tee /etc/systemd/system/cfaoi-cleanup.timer >/dev/null <<'EOF'
[Unit]
Description=CF-AOI 磁碟自動清理（每天 03:30、開機 15 分鐘後）

[Timer]
OnCalendar=*-*-* 03:30
OnBootSec=15min
Persistent=true

[Install]
WantedBy=timers.target
EOF
    # journald 上限：系統 log 最多 4GB（預設是磁碟 10%，Spark 3.7T = 370GB 太多）
    sudo mkdir -p /etc/systemd/journald.conf.d
    printf '[Journal]\n# CF-AOI：系統 log 上限（install_linux_services.sh 產生）\nSystemMaxUse=4G\n' \
        | sudo tee /etc/systemd/journald.conf.d/50-cfaoi.conf >/dev/null
    sudo systemctl restart systemd-journald
    sudo systemctl daemon-reload
    sudo systemctl enable --now cfaoi-cleanup.timer >/dev/null 2>&1
    echo "✓ cfaoi-cleanup.timer 已啟用（$(systemctl show cfaoi-cleanup.timer -p NextElapseUSecRealtime --value)）；journald 上限 4G"
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
# systemd watchdog（階段 3）：程式 30 秒內沒回報健康（命令迴圈卡死/單張處理卡住）→ 砍掉重啟；
# 中止訊號 10 秒內沒結束就強制終止（卡到連結束都卡住時）
WatchdogSec=30
NotifyAccess=all
TimeoutAbortSec=10s
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target"

    # rdma-process 在 Grab 斷線後依設計退出 → Restart=always = 每片 session 自動重生
    install_unit cfaoi-ip-production "[Unit]
Description=CF-AOI IP (rdma-process, 8200+18515) - Step 4/5 生產
After=network-online.target
Wants=network-online.target
Conflicts=cfaoi-ip-offline.service

[Service]
User=$RUN_USER
WorkingDirectory=$REPO/ip
ExecStart=$REPO/ip/build/cfaoi_ip --mode rdma-process --recipe $REPO/recipes/DEFAULT/IP0/RecipeInfo.xml --output $OUT --rdma-port 18515
# RDMA ring 要 ibv_reg_mr 鎖 155MB；systemd 預設 memlock 只有 8MB → 不設就 Cannot allocate memory 重啟迴圈
LimitMEMLOCK=infinity
# systemd watchdog（階段 3）：程式 30 秒內沒回報健康（命令迴圈卡死/單張處理卡住）→ 砍掉重啟；
# 中止訊號 10 秒內沒結束就強制終止（卡到連結束都卡住時）
WatchdogSec=30
NotifyAccess=all
TimeoutAbortSec=10s
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target"

    install_polkit
    sudo systemctl daemon-reload
    # 開機自啟 = 生產模式（2026-10-05 起；原本是調參模式，Spark 一重開就進錯模式）
    sudo systemctl disable cfaoi-ip-offline >/dev/null 2>&1 || true
    sudo systemctl enable cfaoi-ip-production
    systemctl is-active -q cfaoi-ip-offline || sudo systemctl start cfaoi-ip-production
    # 已在跑的服務重啟一次，套用新的 unit 設定（watchdog 等）；沒在跑的不動
    sudo systemctl try-restart cfaoi-ip-production cfaoi-ip-offline
    echo "✓ 開機自啟：cfaoi-ip-production（生產）；調參時手動 systemctl start cfaoi-ip-offline（會自動停生產）"

    # 校時：fab 內無 NTP → 跟 Grab 主機對時（Grab 跑 chrony，經 RDMA 直連網段 192.168.3.2）
    sudo mkdir -p /etc/systemd/timesyncd.conf.d
    printf '[Time]\n# CF-AOI：跟 Grab 主機對時（fab 內無網路；Grab 跑 chrony server）\nNTP=192.168.3.2\n' \
        | sudo tee /etc/systemd/timesyncd.conf.d/50-cfaoi.conf >/dev/null
    sudo systemctl restart systemd-timesyncd 2>/dev/null || true
    echo "✓ 校時來源：192.168.3.2（Grab）"
    install_agent ip "$OUT"

elif [ "$ROLE" = "grab" ]; then
    [ -x "$REPO/grab/build/cfaoi_grab" ] || { echo "找不到 $REPO/grab/build/cfaoi_grab（先編譯）"; exit 1; }
    # 手動開的 grab 還在跑 → 服務起來會搶相機（後開的開不到）→ 先請人關掉
    if pids=$(pgrep -x cfaoi_grab) && ! systemctl is-active -q cfaoi-grab; then
        echo "⚠ 有手動啟動的 cfaoi_grab 在跑（PID $pids）→ 先關掉那個視窗（或 pkill -x cfaoi_grab）再重跑本腳本"
        exit 1
    fi
    # 機台設定（不進 git、每台自己的）：台數與 Spark 位址。已存在就不覆蓋。
    if [ ! -f /etc/default/cfaoi-grab ]; then
        sudo tee /etc/default/cfaoi-grab >/dev/null <<'EOF'
# CF-AOI Grab 機台設定（cfaoi-grab.service 讀取；改完 systemctl restart cfaoi-grab）
# 台數用「實際接上的台數」，不要用 ALL（某台沒起來時 ALL 照樣 OK 只跑 N-1 台；數字則當場報錯）
CAM_COUNT=6
# Spark IP 端 RDMA（rdma-process 監聽）
RDMA_DEST=192.168.3.1:18515
# 其他 cfaoi_grab 參數（例：--cpus all）
GRAB_EXTRA=
EOF
        echo "  已建立 /etc/default/cfaoi-grab（CAM_COUNT=6）"
    else
        echo "  保留既有 /etc/default/cfaoi-grab：$(grep -E '^CAM_COUNT' /etc/default/cfaoi-grab)"
    fi
    install_unit cfaoi-grab "[Unit]
Description=CF-AOI Grab (相機取像 → RDMA 送 Spark；ctrl port 8100)
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
EnvironmentFile=/etc/default/cfaoi-grab
WorkingDirectory=$REPO/grab
# stdbuf：stdout 進 journald 時預設整批緩衝，開相機等訊息會延遲好幾分鐘才出現
ExecStart=/usr/bin/stdbuf -oL -eL $REPO/grab/build/cfaoi_grab --rdma-dest \${RDMA_DEST} --cam-count \${CAM_COUNT} \$GRAB_EXTRA
SyslogIdentifier=cfaoi_grab
# RDMA 發送緩衝要 ibv_reg_mr（systemd 預設 memlock 8MB 不夠）
LimitMEMLOCK=infinity
# systemd watchdog（階段 3）：程式 30 秒內沒回報健康（命令迴圈卡死/單張處理卡住）→ 砍掉重啟；
# 中止訊號 10 秒內沒結束就強制終止（卡到連結束都卡住時）
WatchdogSec=30
NotifyAccess=all
TimeoutAbortSec=10s
# 任何原因結束都重啟（相機/RDMA 的恢復由 Control GRAB_ARM 處理；行程本身要一直在）
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target"

    install_polkit
    sudo systemctl daemon-reload
    sudo systemctl enable --now cfaoi-grab
    sudo systemctl try-restart cfaoi-grab   # 已在跑 → 重啟套用新的 unit 設定（watchdog 等）
    echo "✓ cfaoi-grab 已啟用並啟動（開機自啟）；log：journalctl -u cfaoi-grab -f"
    mkdir -p "$HOME/cfaoi_logs"
    install_agent grab "$HOME/cfaoi_logs"
else
    echo "未知角色：$ROLE（ip|grab）"; exit 1
fi

echo "狀態： systemctl status cfaoi-*  ｜ log： journalctl -u <服務名> -f"
