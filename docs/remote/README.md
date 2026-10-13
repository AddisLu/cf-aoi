# Spark 遠端桌面（spark-02 上排 IP、spark-03 下排 IP）

| 要看什麼 | 怎麼連 | 位址（實驗室走 Tailscale）| 帳密 |
|---|---|---|---|
| **本機螢幕上那個桌面**（與本機同步，本機開的軟體都看得到）| Mac：Finder ⌘K / 螢幕共享 → `vnc://<主機>`；Windows App / mstsc：連 RDP → Session 選 **Local Desktop (same as monitor)** | spark-02：`spark-3961.tailffdb68.ts.net`（fab：`192.168.3.1`）；spark-03：`spark-010f.tailffdb68.ts.net` | VNC 密碼（各台設定時自訂）|
| 另開一個獨立遠端桌面（XFCE，斷線後保留）| Windows App / mstsc → Session 選 **Xorg**；或 `spark-02.rdp` / `spark-03.rdp` | 同上，port 3389 | 系統帳號 spark-02 / spark-03 |

- 本機桌面：spark-02 = 接著的螢幕（GNOME，X11 `:1`）；spark-03 沒接螢幕 → GDM 自動登入 + 虛擬 1920×1080（`:0`）。
  GNOME 閒置會鎖定 → 用系統密碼解鎖。
- 為什麼不用 GNOME 內建遠端桌面：NVIDIA 機台 GDM 強制 X11，GNOME 46 RDP 需要 Wayland。
- 安裝腳本：`scripts/deploy/setup_xrdp.sh`、`setup_x11vnc.sh`、`setup_headless_desktop.sh`（無螢幕機台）。
- VNC 本身不加密：只經 Tailscale（WireGuard）或 fab 內網使用。fab 無外網 → 進場前把 xrdp/xorgxrdp/xfce4/x11vnc 的 .deb 放進離線更新包。
