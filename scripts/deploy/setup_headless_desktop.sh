#!/bin/bash
# setup_headless_desktop.sh — 沒接螢幕的 Spark 也有一個「本機 GNOME 桌面」（給 x11vnc 分享）
#
# 1. NVIDIA X 設定加虛擬解析度（/etc/X11/xorg.conf 已有 AllowEmptyInitialConfiguration → 無螢幕可開 X）
# 2. GDM 自動登入這個帳號 → seat0 上有一個 GNOME 桌面，軟體開在這裡，遠端（x11vnc）看到的就是它
# 3. 重啟 gdm
# 之後接著跑 setup_x11vnc.sh。
# ⚠️ 自動登入：接上螢幕即見已登入桌面；GNOME 閒置會自動鎖定，遠端解鎖要帳號密碼。
#
# 用法：sudo bash setup_headless_desktop.sh [寬 高]（預設 1920 1080）
set -e
U="${SUDO_USER:?請用 sudo 執行}"
W="${1:-1920}"; Hh="${2:-1080}"
TS=$(date +%Y%m%d_%H%M%S)

# 1. 虛擬解析度
X=/etc/X11/xorg.conf
if [ -f "$X" ] && ! grep -q "Virtual" "$X"; then
  cp -a "$X" "$X.bak_$TS"
  sed -i "/SubSection[[:space:]]*\"Display\"/,/EndSubSection/ s/^\([[:space:]]*Depth[[:space:]]*24\)$/\1\n        Virtual     $W $Hh/" "$X"
  echo "== xorg.conf 加 Virtual $W×$Hh（備份 $X.bak_$TS）"
fi
grep -n "Virtual" "$X" || echo "（xorg.conf 沒有 Virtual；無螢幕時解析度可能很小）"

# 2. GDM 自動登入
C=/etc/gdm3/custom.conf
cp -a "$C" "$C.bak_$TS"
sed -i '/^\s*AutomaticLoginEnable\s*=/d; /^\s*AutomaticLogin\s*=/d' "$C"
sed -i "/^\[daemon\]/a AutomaticLoginEnable=true\nAutomaticLogin=$U" "$C"
echo "== GDM 自動登入 $U（備份 $C.bak_$TS）"
grep -A3 "^\[daemon\]" "$C"

# 3. 重啟 gdm（本機沒人用；xrdp 的遠端桌面不受影響）
systemctl restart gdm
for i in $(seq 1 20); do
  sleep 2
  if loginctl list-sessions --no-legend --no-pager | grep -w "$U" | grep -qw seat0; then
    echo "== OK：$U 已在本機（seat0）自動登入"; break
  fi
done
loginctl list-sessions --no-legend --no-pager
ls /tmp/.X11-unix/
