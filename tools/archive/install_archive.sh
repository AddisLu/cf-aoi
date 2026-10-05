#!/usr/bin/env bash
# 在 Grab 主機安裝機台資料夾 + 自動歸檔（cfaoi-archive.timer，每 10 分鐘；開機 5 分鐘後第一次）。
# 冪等，可重跑（已存在的 /etc/default/cfaoi-archive 不覆蓋）。sudo 會提示密碼則輸入。
#
#   bash tools/archive/install_archive.sh [機台編號，預設沿用設定檔或主機名稱]
#
# 建立：/srv/cfaoi（擁有者 = 目前帳號）、/etc/default/cfaoi-archive、cfaoi-archive.{service,timer}、
#       桌面捷徑「機台資料夾」→ /srv/cfaoi。命名規則：tools/archive/README_CFAOI_HOME.md
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
RUN_USER="$(id -un)"
HOME_DIR=/srv/cfaoi
CONF=/etc/default/cfaoi-archive
MID="${1:-}"

sudo mkdir -p "$HOME_DIR"
sudo chown "$RUN_USER:$RUN_USER" "$HOME_DIR"
echo "✓ $HOME_DIR（擁有者 $RUN_USER）"

if [ ! -f "$CONF" ]; then
    [ -n "$MID" ] || MID="$(hostname -s)"
    sudo tee "$CONF" >/dev/null <<EOF
# CF-AOI 機台資料夾自動歸檔（cfaoi-archive.timer；改完不必重啟，下一輪生效）
# 命名規則：$HOME_DIR/README.md
CFAOI_HOME=$HOME_DIR
# 機台編號（檔案帶出機台時的前綴；三台進 fab 各自不同，例 CFAOI-01）
MACHINE_ID=$MID
REPO=$REPO
# Spark（RDMA 直連網段；fab 內唯一通路）
SPARK=auo001@192.168.3.1
SPARK_NODE=spark1
SPARK_OUTPUT=/home/auo001/cfaoi_output
SPARK_REPO=/home/auo001/Addis/cf-aoi
# 原始圖拉取限速 KB/s（~2.4Gbps；RDMA 走反方向，全雙工互不影響，留餘裕）
RAW_BWLIMIT_KB=300000
# 保留天數
KEEP_LOG_DAYS=365
KEEP_DEFECT_DAYS=90
KEEP_RAW_DAYS=14
KEEP_TEST_DAYS=365
KEEP_CONFIG_DAYS=365
KEEP_KNOWLEDGE_DAYS=730
# 水位保護（先刪最舊原始圖、再刪最舊檢測結果；今天的不刪）
MAX_USED_PCT=85
TARGET_USED_PCT=80
EOF
    echo "✓ 已建立 $CONF（機台編號 $MID）"
elif [ -n "$MID" ]; then
    sudo sed -i "s/^MACHINE_ID=.*/MACHINE_ID=$MID/" "$CONF"
    echo "✓ $CONF 已存在，機台編號改為 $MID"
else
    echo "  $CONF 已存在（保留）"
fi

sudo tee /etc/systemd/system/cfaoi-archive.service >/dev/null <<EOF
[Unit]
Description=CF-AOI 機台資料夾歸檔（log / defect 小圖 / 原始圖 / 文件 / 測試說明 / 參數 → $HOME_DIR）
After=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
EnvironmentFile=$CONF
ExecStart=/usr/bin/python3 -u $REPO/tools/archive/cfaoi_archive.py
Nice=15
IOSchedulingClass=idle
TimeoutStartSec=2h
EOF
sudo tee /etc/systemd/system/cfaoi-archive.timer >/dev/null <<'EOF'
[Unit]
Description=CF-AOI 機台資料夾歸檔（每 10 分鐘、開機 5 分鐘後）

[Timer]
OnBootSec=5min
OnUnitInactiveSec=10min
Persistent=true

[Install]
WantedBy=timers.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now cfaoi-archive.timer >/dev/null 2>&1
echo "✓ cfaoi-archive.timer 已啟用"

# Grab 使用者需能讀系統 journald（adm 群組）
id -nG "$RUN_USER" | grep -qw adm || { sudo usermod -aG adm "$RUN_USER"; echo "  已加入 adm 群組（重新登入後生效）"; }

DESK="$HOME/桌面"; [ -d "$DESK" ] || DESK="$HOME/Desktop"
if [ -d "$DESK" ]; then
    cat > "$DESK/cfaoi-data.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=機台資料夾（log・defect・原始圖・文件）
Comment=CF-AOI 機台資料 $HOME_DIR；命名規則見其中 README.md
Exec=xdg-open $HOME_DIR
Icon=folder-documents
Terminal=false
EOF
    chmod +x "$DESK/cfaoi-data.desktop"
    gio set "$DESK/cfaoi-data.desktop" metadata::trusted true 2>/dev/null || true
    echo "✓ 桌面捷徑：機台資料夾"
fi

# 先前暫放在家目錄的參考圖 → 50_raw/reference/
if [ -d "$HOME/cfaoi_reference" ]; then
    mkdir -p "$HOME_DIR/50_raw/reference"
    for d in "$HOME/cfaoi_reference"/*/; do
        [ -d "$d" ] || continue
        n=$(basename "$d")
        if [ -e "$HOME_DIR/50_raw/reference/$n" ]; then echo "  參考圖 $n 已存在（保留家目錄那份）"
        else mv "$d" "$HOME_DIR/50_raw/reference/$n" && echo "✓ 參考圖 → 50_raw/reference/$n"; fi
    done
    rmdir "$HOME/cfaoi_reference" 2>/dev/null || true
fi

echo "第一次歸檔（之後每 10 分鐘自動）…"
set -a; . "$CONF"; set +a
python3 -u "$REPO/tools/archive/cfaoi_archive.py" || echo "⚠ 有項目未成功，見上方訊息與 $HOME_DIR/STATUS.json"
