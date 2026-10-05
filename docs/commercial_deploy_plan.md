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
| Grab 啟動 | 人按 Grab 桌面圖示（終端機視窗） | systemd 服務、開機自啟 |
| Spark 開機模式 | **開機自啟調參模式**（offline） | 開機自啟生產模式 |
| Control `CF_READY` | **一律回 OK**（不管 Grab/IP 有沒有連上） | 三個節點都就緒才 OK，否則 ERR + 原因 |
| 卡死偵測 | 只有 Control 心跳燈（紅燈要人處理） | systemd watchdog 自動重啟 + 代理可遠端重啟 |
| 遠端管理 | 無（要到機台前或 SSH） | Control「系統」頁 + 節點代理 |
| 時間同步 | 靠 Internet NTP | Grab 當校時主機 |
| 程式更新 | git pull（要網路） | USB 帶版本包，Control 顯示三台版本是否一致 |

## 4. 分階段

**階段 1 — 開機即就緒（最優先，進 fab 前必做）**
- Grab 改 systemd 服務（`--cam-count` 用實際台數、`LimitMEMLOCK=infinity`、`--cpus pcore`）
- Spark 開機自啟改生產模式
- BIOS 復電自動開機、chrony 校時
- `CF_READY` 改為真的檢查三節點
- 驗收：三台同時斷電復電，不碰鍵盤，Control 燈自己全綠、上位機流程可跑

**階段 2 — Control 單一入口**
- 節點代理（Grab、Spark 各一）：狀態 / 重啟 / 切模式 / 收 log / 重開機，白名單命令
- Control「系統」頁：三台狀態、版本、磁碟、按鈕；「一鍵全部重啟」
- 收診斷包：一鍵把三台 log 打包存到 Windows（給工程師帶出 fab 分析）

**階段 3 — 自我修復與維運**
- systemd watchdog（主迴圈定期回報，卡住自動重啟）
- 磁碟保留天數自動清理、journald 上限
- 版本包 + 一致性檢查（三台版本不同時 Control 顯示警告）

## 5. 現在已有的（2026-10-05）

- Grab 桌面圖示「CF-AOI Grab」：帶齊產線參數、防重複啟動、log 存 `~/cfaoi_logs/`
- Spark 桌面圖示「CF-AOI IP（生產）／（調參）」：切模式 + 即時 log
- Windows 免安裝包：`Create-Desktop-Shortcut.cmd`、`Enable-Autostart.cmd`
- 這些是**過渡期 / 工程用**；階段 1 完成後，線上人員就不需要碰 Grab/Spark 的圖示。
