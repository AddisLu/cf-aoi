# 商業化部署規劃：線上人員只碰一個螢幕

> 2026-10-05 起草。前提：線上人員**只有一個螢幕** = 上位機 Windows（同機跑 Control）；
> Grab / Spark 是**不接螢幕、不需要人碰的設備**；fab 內無網路。

## 1. 目標

| 角色 | 會碰到的東西 |
|---|---|
| 線上人員 | Windows 桌面一個圖示「CF-AOI Control」（或開機自動開）。看燈號、按上位機流程。 |
| 工程/維護 | 同上 + Control 的「系統」頁（重啟/切模式/收 log）；必要時 Grab/Spark 桌面圖示 |
| 開發 | SSH / git / 原始碼（fab 外） |

**原則：Grab 和 IP 不需要「被打開」——開機就自己跑、掛了自己重生；Control 是唯一入口。**

## 2. 「可以從 Control 直接開啟全部程式嗎？」

可以，但不是讓 Control 去「執行」Linux 上的程式，而是分三層：

1. **開機即運作**：Grab、IP 都是 systemd 服務，開機自動啟動、異常結束自動重啟（`Restart=always`）。
   三台的開機順序不重要：IP 先聽著、Grab 在 ARM 時才連 RDMA、Control 每 2.5 秒自動重連。
2. **Control 遠端管理**：每台 Linux 跑一支很小的常駐「節點代理」（node agent，systemd 服務，
   只聽控制網）。Control 透過它做：查狀態、重啟 Grab/IP、IP 切生產/調參、收診斷 log、關機/重開機。
   代理只接受**白名單命令**（不開 shell），所以就算主程式卡死，Control 仍能把它重啟。
3. **電源**：三台 BIOS 設「復電自動開機」；機櫃上電 = 全系統自己起來。
   遠端開機（Wake-on-LAN）只能直達 Grab（同一條控制網），Spark 要靠 Grab 代轉，列為選配。

**不建議**用「Control 經 SSH 遠端執行指令」：Windows 要存 Linux 密碼/金鑰、權限難控、出錯難診斷。

## 3. 現況差距

| 項目 | 現況 | 目標 |
|---|---|---|
| Grab 啟動 | ✅ systemd 服務、開機自啟（2026-10-05） | — |
| Spark 開機模式 | ✅ 開機自啟生產模式（2026-10-05） | — |
| Control `CF_READY` | ✅ Grab+IP 皆連線才 OK，否則 ERR + 原因（2026-10-05） | 階段 2：加相機台數/故障相機判定 |
| 卡死偵測 | ✅ systemd watchdog 自動重啟 + 代理可遠端重啟（2026-10-05） | — |
| 遠端管理 | ✅ 節點代理 + Control「系統設定 › 系統狀態」（2026-10-05） | — |
| 時間同步 | ✅ Grab chrony 校時主機、Spark 已跟上（Windows 待設） | — |
| 程式更新 | ✅ USB 離線更新包（make_update_package / apply_update）+ Control 版本一致性警告（2026-10-05） | — |

## 4. 分階段

**階段 1 — 開機即就緒（最優先，進 fab 前必做）** — 2026-10-05 軟體部分完成；剩 BIOS 與斷電復電實測
- Grab 改 systemd 服務（`--cam-count` 用實際台數、`LimitMEMLOCK=infinity`、`--cpus pcore`）
- Spark 開機自啟改生產模式
- BIOS 復電自動開機、chrony 校時
- `CF_READY` 改為真的檢查三節點
- 驗收：三台同時斷電復電，不碰鍵盤，Control 燈自己全綠、上位機流程可跑

**階段 2 — Control 單一入口** — ✅ 2026-10-05 完成（見下方「階段 2 實作」）
- 節點代理（Grab、Spark 各一）：狀態 / 重啟 / 切模式 / 收 log / 重開機，白名單命令
- Control「系統」頁：三台狀態、版本、磁碟、按鈕；「一鍵全部重啟」
- 收診斷包：一鍵把三台 log 打包存到 Windows（給工程師帶出 fab 分析）

**階段 3 — 自我修復與維運** — ✅ 2026-10-05 完成（見下方「階段 3 實作」）
- systemd watchdog（主迴圈定期回報，卡住自動重啟）
- 磁碟保留天數自動清理、journald 上限
- 版本包 + 一致性檢查（三台版本不同時 Control 顯示警告）

## 5. 現在已有的（2026-10-05）

- Grab 桌面圖示「CF-AOI Grab」：帶齊產線參數、防重複啟動、log 存 `~/cfaoi_logs/`
- Spark 桌面圖示「CF-AOI IP（生產）／（調參）」：切模式 + 即時 log
- Windows 免安裝包：`Create-Desktop-Shortcut.cmd`、`Enable-Autostart.cmd`
- 這些是**過渡期 / 工程用**；階段 1 完成後，線上人員就不需要碰 Grab/Spark 的圖示。

## 6. 階段 2 實作（2026-10-05）

- **節點代理** `tools/node_agent/cfaoi_agent.py`（Python 標準庫）：Grab、Spark 各一，`cfaoi-agent.service`，port **8300**。
  命令白名單：CHECK_HEALTH / STATUS / SERVICE（只限本機 cfaoi-*，start/stop/restart）/ LOGS / DIAG / POWER（需 confirm）。
  只收控制網、RDMA 網段、本機、開發期 Tailscale；以一般帳號執行，權限靠 polkit。
- **Control「系統設定 › 系統狀態」**（第一個分頁）：每 5 秒更新；每個節點顯示主機、版本、時間差、校時、磁碟、開機時間；
  每個服務可「重新啟動 / 看 log」，IP 可「切到此模式」（生產↔調參）；節點「重新開機」；全域「全部重新啟動」（先 IP 再 Grab）、
  「收集診斷包」（各節點 + Control log → `輸出資料夾/diag/<時間>/`）。會中斷生產的動作都要先確認。
  版本不一致（含 `-dirty`）或時間差 >1 秒會顯示警告。
- 工程用：`CfAoiControl --page settings` 直接開到系統設定頁。
- 驗證：`--selftest agents`（真代理：狀態/log/診斷包/白名單）兩台 PASS；經代理切調參↔生產、重啟 Grab、
  重開機權限（pkcheck）皆通過；之後全鏈 7/7、Spark recv 60/0。畫面截圖：`docs/verification/control_system_status_20261005.png`。
- **尚未實測**：真的按「重新開機」（只驗了權限）——建議跟階段 1 的斷電復電測試一起做。

## 7. 階段 3 實作（2026-10-05）

- **watchdog**（`shared/sd_watchdog.h`，免 libsystemd）：Grab/IP 的服務設 `WatchdogSec=30`；程式每 10 秒回報一次健康，
  卡住就停止回報 → systemd 約 30 秒後重啟。判定：命令迴圈（8100/8200）還在、單一命令 ≤120 秒；IP 另加單張影像處理 ≤60 秒。
  實測：兩台各以 `SIGSTOP` 凍住 → 都在 30 秒時被 systemd 判定逾時並重啟、恢復運作；全鏈測試期間 0 誤觸發
  （修掉兩個誤報：啟動競態、正常結束）。apport 對這兩支程式不留 core 檔，不佔磁碟。
- **磁碟自動清理**（`tools/node_agent/cfaoi_cleanup.py` + `cfaoi-cleanup.timer`，每天 03:30、開機 15 分鐘後）：
  結果 30 天、原始影像 7 天、行車紀錄 180 天、Grab log 30 天；水位 >85% 從最舊刪到 80%（先原始影像）；
  今天的資料永不刪、只動 CF-AOI 產物。設定 `/etc/default/cfaoi-cleanup`。journald 上限 4GB。
- **離線更新**：有網路處跑 `scripts/deploy/make_update_package.sh` → USB → fab 內 Grab 上
  `bash apply_update.sh . --spark`（驗 bundle、工作樹須乾淨、只允許往前、編譯失敗不重啟並印回退指令；
  改到服務設定時提醒重跑安裝腳本）。Windows 解壓包內 zip 覆蓋 control 資料夾。
  實測：用此流程把 Spark 從 8b197ee → 99cc5f3 → a0cab72 → 05ce700 連續更新 3 次，全鏈皆 7/7。
