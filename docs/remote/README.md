# Spark 遠端桌面（spark-02 上排 IP、spark-03 下排 IP）

| 要看什麼 | 怎麼連 | 位址（實驗室走 Tailscale）| 帳密 |
|---|---|---|---|
| **本機螢幕上那個桌面，隔著網路也順（建議）**| **RustDesk** 客戶端（Mac：`brew install --cask rustdesk`）→ ID 欄直接輸入 IP | spark-02：`100.104.224.17`（fab：`192.168.3.1`）；spark-03：`100.96.175.96` | RustDesk 連線密碼（安裝時自訂）|
| **本機螢幕上那個桌面**（與本機同步，本機開的軟體都看得到）| Mac：Finder ⌘K / 螢幕共享 → `vnc://<主機>`；Windows App / mstsc：連 RDP → Session 選 **Local Desktop (same as monitor)** | spark-02：`spark-3961.tailffdb68.ts.net`（fab：`192.168.3.1`）；spark-03：`spark-010f.tailffdb68.ts.net` | VNC 密碼（各台設定時自訂）|
| ~~另開一個獨立遠端桌面（xrdp XFCE）~~ | **2026-10-13 已停用**（`systemctl disable --now xrdp xrdp-sesman`）：xrdp 的登入紀錄帶顯示編號、GNOME 自動登入的沒有 → RustDesk 會優先接到 xrdp 的 XFCE（「老鼠」桌面）| — | — |

- 本機桌面：spark-02 = 接著的螢幕（GNOME，X11 `:1`）；spark-03 沒接螢幕 → GDM 自動登入 + 虛擬 1920×1080（`:0`）。
  GNOME 閒置會鎖定 → 用系統密碼解鎖。
- 為什麼不用 GNOME 內建遠端桌面：NVIDIA 機台 GDM 強制 X11，GNOME 46 RDP 需要 Wayland。
- 安裝腳本：`scripts/deploy/setup_xrdp.sh`、`setup_x11vnc.sh`、`setup_headless_desktop.sh`（無螢幕機台）。
- VNC 本身不加密：只經 Tailscale（WireGuard）或 fab 內網使用。fab 無外網 → 進場前把 xrdp/xorgxrdp/xfce4/x11vnc 的 .deb 放進離線更新包。

## RustDesk（2026-10-13）

- 為什麼：影片編碼（VP9/H.264），~100 ms 延遲下比 VNC 順；開源免費、不需帳號。NoMachine 10 Personal Edition 要訂閱 → 已移除。
- 設定：IP 直連（direct-server，port 21118，不經 RustDesk 公用伺服器 → fab 無外網可用）、固定密碼、輸入密碼即可（不需有人在螢幕前按接受）。
- ⚠️ 「No Displays」：NVIDIA 輸出全 disconnected → RandR 0 台螢幕 → `scripts/deploy/setup_virtual_monitor.sh`（登入時宣告虛擬螢幕，有實體螢幕就不加）。
- ⚠️ 不能直接改 `RustDesk2.toml`（服務會寫回蓋掉）→ 用 `sudo rustdesk --option <key> <value>`。
- 腳本：`scripts/deploy/setup_rustdesk.sh`（安裝檔放 /tmp/rustdesk.deb：github.com/rustdesk/rustdesk releases，aarch64.deb，核對 SHA-256）。

## 2026-10-13 最終狀態（spark-02 / spark-03 相同）

- RustDesk（主要）：IP 直連 21118、固定密碼、免按接受；分享本機 GNOME 桌面（`:0`）。
- 本機桌面：GDM 自動登入 + NVIDIA 虛擬螢幕（`setup_nvidia_virtual_display.sh`，EDID 預設 1440×900，可切 1280×800 / 1680×1050 / 1920×1080）、
  不閒置鎖定、文字 1.25×。
- 備用：x11vnc 5900（本機桌面）。xrdp 已停用。
