#!/usr/bin/env bash
# 安裝 Pleora eBUS SDK 7.0（Ubuntu 22.04 x86_64）
#
# 用法:  sudo ./install_ebus.sh /path/to/eBUS_SDK_Ubuntu-22.04-x86_64-7.0.1-7536.deb
#
# 套件的 postinst 會自行處理：授權目錄、函式庫路徑、Python wheels、
# eBUS Universal Pro filter driver 編譯與安裝、rp_filter、udev rules。
# 本腳本負責前置檢查與事後的環境變數設定。
#
# 注意：postinst 會執行 set_rp_filter.sh --restartnetworkstack=yes，
#       過程中會 systemctl restart network-manager，網路會短暫中斷。
set -euo pipefail

DEB="${1:-}"
if [[ -z "$DEB" || ! -f "$DEB" ]]; then
    echo "用法: sudo $0 <eBUS_SDK_*.deb>" >&2
    echo "本機需要 eBUS_SDK_Ubuntu-22.04-x86_64-<版本>-<build>.deb" >&2
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    echo "請用 sudo 執行" >&2
    exit 1
fi

# 擋掉架構不符的套件（Jetson / Raspberry Pi 版是 aarch64）
ARCH=$(dpkg-deb -f "$DEB" Architecture 2>/dev/null || echo '?')
if [[ "$ARCH" != "amd64" && "$ARCH" != "all" ]]; then
    echo "錯誤: 套件架構為 $ARCH，本機是 $(dpkg --print-architecture)" >&2
    exit 1
fi
if [[ "$DEB" == *aarch64* || "$DEB" == *Jetson* || "$DEB" == *Raspberry* ]]; then
    echo "錯誤: 「$(basename "$DEB")」是 ARM 版，本機是 $(uname -m)" >&2
    exit 1
fi

echo "==> 檢查 filter driver 的編譯前置"
MISSING=()
dpkg -s build-essential >/dev/null 2>&1 || MISSING+=(build-essential)
dpkg -s "linux-headers-$(uname -r)" >/dev/null 2>&1 || MISSING+=("linux-headers-$(uname -r)")
if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo "    安裝: ${MISSING[*]}"
    apt-get update && apt-get install -y "${MISSING[@]}"
else
    echo "    已齊備（kernel $(uname -r)）"
fi

echo "==> 安裝 eBUS SDK（Qt 由套件自帶，不需額外相依）"
dpkg -i "$DEB" || apt-get install -f -y

ROOT=$(find /opt/pleora -maxdepth 3 -type d -name 'Ubuntu-*-x86_64' 2>/dev/null | sort -V | tail -1)
if [[ -z "$ROOT" ]]; then
    echo "找不到安裝目錄（預期 /opt/pleora/ebus/Ubuntu-22.04-x86_64）" >&2
    exit 1
fi
echo "==> PUREGEV_ROOT = $ROOT"

echo "==> filter driver 狀態"
# postinst 剛重啟過網路，模組載入可能還沒完成，等一下再判斷
for _ in 1 2 3 4 5; do
    lsmod | grep -q ebUniversalProForEthernet && break
    sleep 1
done
if [[ -x "$ROOT/module/ebdriverlauncher.sh" ]]; then
    "$ROOT/module/ebdriverlauncher.sh" status 2>&1 | sed 's/^/    /' || true
fi
if lsmod | grep -q ebUniversalProForEthernet; then
    echo "    模組已載入：$(lsmod | grep ebUniversalProForEthernet | awk '{print $1}')"
    ls -l /dev/ebUniversalProForEthernet 2>/dev/null | sed 's/^/    /'
else
    echo "    未載入（非必要，缺少時走一般 socket，效能略低）"
fi

echo "==> 寫入環境變數 /etc/profile.d/ebus.sh"
# 直接沿用 SDK 自己產生的 set_puregev_env.sh —— 手寫容易寫錯 GenICam 版本
# （7.0 用 GENICAM_*_V3_4，舊版是 V3_3），交給官方腳本最保險
cat >/etc/profile.d/ebus.sh <<EOF
[ -f "$ROOT/bin/set_puregev_env.sh" ] && . "$ROOT/bin/set_puregev_env.sh"
EOF
chmod 644 /etc/profile.d/ebus.sh

echo
echo "完成。開新 shell 或執行 source /etc/profile.d/ebus.sh 後："
echo "  \$PUREGEV_ROOT/bin/eBUSPlayer            # GUI 取像"
echo "  \$PUREGEV_ROOT/bin/PleoraFirmwareUpdater # iPORT 韌體更新"
echo "  cd ~/下載/Grab && cmake -S . -B build && cmake --build build -j"
