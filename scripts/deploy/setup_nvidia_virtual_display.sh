#!/bin/bash
# setup_nvidia_virtual_display.sh — 沒接螢幕的 Spark（NVIDIA GB10）讓驅動以為 HDMI（DFP-0）接著一台虛擬螢幕（預設 1440×900）
#
# 為什麼：輸出全部 disconnected 時 GNOME（mutter）沒有可用螢幕 → 不繪製畫面 → 遠端（RustDesk / VNC）全黑
#   （實測 2026-10-13 spark-03：X 畫面每個像素 = 0）。xrandr --setmonitor 只騙得過 RustDesk，騙不過 GNOME。
# 做法：xorg.conf 加 ConnectedMonitor + CustomEDID（files/edid-virtual.bin：預設 1440×900（13 吋 Mac 全螢幕近 1:1），另含 1280×800 / 1680×1050 / 1920×1080），重啟 gdm。
# 硬體替代：插一顆 HDMI 假負載（EDID 模擬器）效果相同。
# ⚠️ 重啟 gdm 會關掉本機桌面上開著的程式。
#
# 用法（在 Spark 上）：sudo bash setup_nvidia_virtual_display.sh /tmp/edid-virtual.bin [DFP-0]
set -e
EDID_SRC="${1:?EDID 檔}"; OUT="${2:-DFP-0}"
X=/etc/X11/xorg.conf
TS=$(date +%Y%m%d_%H%M%S)

install -m 644 "$EDID_SRC" /etc/X11/edid-virtual.bin
cp -a "$X" "$X.bak_$TS"
sed -i '/ConnectedMonitor\|CustomEDID\|IgnoreEDIDChecksum/d' "$X"
sed -i "/Option[[:space:]]*\"AllowEmptyInitialConfiguration\"/a\\    Option         \"ConnectedMonitor\" \"$OUT\"\n    Option         \"CustomEDID\" \"$OUT:/etc/X11/edid-virtual.bin\"\n    Option         \"IgnoreEDIDChecksum\" \"$OUT\"" "$X"
echo "== xorg.conf（備份 $X.bak_$TS）："
sed -n '/Section "Screen"/,/^EndSection/p' "$X"

systemctl restart gdm
sleep 12
U="${SUDO_USER:-}"
D=$(ls /tmp/.X11-unix/ | sed -n 's/^X\([0-9]\)$/\1/p' | head -1)
if [ -n "$U" ] && [ -n "$D" ]; then
  sudo -u "$U" env DISPLAY=":$D" XAUTHORITY="/run/user/$(id -u "$U")/gdm/Xauthority" xrandr | head -3
fi
