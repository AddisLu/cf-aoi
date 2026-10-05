# 進 fab 前準備清單（上方陣列：Grab + Spark + Control/上位機 Windows）

> 前提：**fab 內完全沒有網路**（無 Internet、無 Tailscale、無 GitHub、Claude 也連不進去）。
> 凡是「需要網路」或「需要遠端協助」的事，都要在搬進去**之前**做完。
> 網路配置與線位規則見 [CLAUDE.md §2「實機網路與 IP 配置」](CLAUDE.md)；建立日期 2026-10-05。
> 勾選規則：做完並**實際驗證過**才勾（有數據寫在後面）。

## A. 版本凍結與備份（出發前一天）

- [ ] 三處同一版：Grab、Spark 的 `git log --oneline -1` 與 GitHub main 相同；記下短碼：`________`
- [ ] Windows 免安裝包用同一版重打：`scripts/deploy/package_control_windows.sh`（檔名帶短碼）
- [ ] Grab 執行 `tools/grab_setup/collect_machine_state.sh` 存快照（網路/sysctl/套件/相機清單）
- [ ] 備份機台本地檔（不在 git 裡）：
  - Grab：`grab/cam_config.json`（曝光/增益）
  - Spark：`/etc/systemd/system/cfaoi-ip-production.service.d/memlock.conf`、`nmcli con show cf-rdma`（含回程路由）
  - 交換機：`display current-configuration` 存檔、`save force`
- [ ] 產生離線更新包：`scripts/deploy/make_update_package.sh`（git bundle + Windows 包 + apply_update.sh），
      複製到 USB 帶進 fab；fab 內更新：Grab 上 `bash apply_update.sh . --spark`（流程 2026-10-05 已實測）

## B. 離線安裝與授權（需要網路，進場前完成）

- [ ] 離線安裝檔備齊到 USB：pylon `.deb`、eBUS `.deb`、CodeMeter、`apt` 相依套件的 `.deb`
      （`bootstrap.sh` 現在會用 apt 連網裝相依 → 重灌時無網路會失敗）
- [ ] CodeMeter / eBUS 授權在**下方 Grab** 先啟用並確認（L803K 路徑用；離線無法啟用）
- [ ] Windows：系統啟用、更新做完後**關閉自動更新**（產線中重開機 = 停線）
- [ ] Windows：Control 免安裝包解壓到 `C:\Users\<帳號>\cf-aoi\`，跑 `Create-Desktop-Shortcut.cmd`、
      `Enable-Autostart.cmd`；第一次啟動防火牆選「允許」
- [ ] 上位機程式（同仁提供）在同一台 Windows 安裝好，連 `127.0.0.1:8787`

## B2. 機台資料夾與 LoopEngineering（詳見 CLAUDE.md §2「機台資料夾」）

- [ ] Grab：`bash tools/archive/install_archive.sh CFAOI-0n`（三台各自編號）→ `/srv/cfaoi` + `cfaoi-archive.timer`；`STATUS.json` 全 OK
- [ ] 廠商手冊/SOP 放進 `20_docs/vendor`、`20_docs/sop`（命名照 README）；參考圖在 `50_raw/reference/`
- [ ] Spark → Grab 金鑰 SSH（authorized_keys 限定 `from="192.168.3.1"`）；LoopEngineering 部署含遠端路徑功能後跑 `tools/archive/loop_register.sh`
- [ ] 運作模式（生產只跑三支；機況助手由 Control 開）：主 Spark `scripts/deploy/setup_loop_mode.sh --fab`
- [x] 第二台 Spark 直連線：spark-3961 上 `scripts/deploy/fix_spark_link.sh --apply` → 177/178 雙向通、MTU 9000 — 2026-10-05
- [ ] 實測：Control「開啟機況助手」→ 兩台載入就緒 → 開啟畫面 → 「結束並回生產」→ CF_READY 回 OK、Spark 可用記憶體恢復
      （2026-10-05 已用代理實測雙機載入/推論 33 tok/s/釋放；剩 Windows 畫面按鈕）

- [ ] 一鍵健檢基準：進場接好線、全部正常後在 Grab 跑 `tools/triage/cfaoi_triage.py --save-baseline`（存各 CCD 相機 MAC、交換機設定）
- [ ] SN2201 到貨：補 `tools/triage` 交換機驅動並重跑故障注入實驗（目前只驗證借用的 HPE 5945）

## C. 時間同步（fab 內沒有 NTP 伺服器）

- [x] **Grab 當校時主機**（chrony：有網路跟 pool、無網路用本地時鐘 stratum 10）— 2026-10-05 bootstrap 收編
- [x] Spark 跟 Grab 對時（timesyncd `NTP=192.168.3.2`）— 2026-10-05 實測 Server 192.168.3.2、offset +31ms
- [ ] Windows 跟 Grab 對時：安裝說明.txt 第二節 4)（`w32tm /config /manualpeerlist:192.168.10.21 …`）
- [ ] 進場後抽查三台時間差 < 1 秒

## D. 開機即就緒（無人值守；詳見 [commercial_deploy_plan.md](commercial_deploy_plan.md)）

- [ ] 三台 BIOS：**斷電復電後自動開機**（AC Power Recovery = Power On）
- [x] Spark：開機自動進**生產模式**（enabled production / disabled offline）— 2026-10-05
- [x] Grab：`cfaoi-grab` systemd 服務開機自啟、Restart=always；台數在 `/etc/default/cfaoi-grab` — 2026-10-05
- [x] 免密碼管理（polkit）、Control `CF_READY` 真檢查 Grab+IP 連線 — 2026-10-05
- [x] 服務化後全鏈 `verify_step3_trigger` 7/7、Spark recv 60/0 — 2026-10-05
- [x] Control 遠端管理（節點代理 + 系統狀態頁：重啟/切模式/看 log/診斷包/重開機）— 2026-10-05
- [x] 卡死自動重啟（systemd watchdog 30s；兩台 SIGSTOP 實測皆自動恢復）— 2026-10-05
- [ ] 實測：三台**同時斷電再復電**，不碰任何鍵盤，Control 三顆燈自己變綠、上位機流程可跑

## E. 線材與標籤

- [ ] 依 CLAUDE.md 線位表貼標籤（**固定位置**的線：Grab `enp3s0`↔Windows、Grab f0↔Spark port0、
      Spark port1↔spark-3961、Grab f1↔交換機 100G）
- [ ] 相機線**不必照順序**（身分存在相機裡），但每台相機貼 CCD 編號，方便換相機時對照
- [ ] 交換機 console USB 線跟著 Grab 走（`/dev/ttyUSB0`）
- [ ] 若換成 **SN2201**：到貨先設好 1G 埠 / MTU 9000，並更新 runbook（5945 的設定指令不適用）

## F. 相機

- [ ] persistent IP **斷電複驗**（相機斷電再上電，IP/CCD 名稱仍在）— 尚未驗
- [ ] 37 台全數命名完成（CCD01–CCD37，IP 尾碼 = 編號）：桌面「相機工具」→ 🛠 裝置設定（或 `cam_provision`）
- [ ] 光源到位後重調曝光，`cam_config.json` 入帳（暗場的 70µs/256 只是預設）

## G. 資料與磁碟

- [x] 自動清理（cfaoi-cleanup.timer）：結果 30 天、原始影像 7 天、行車紀錄 180 天、Grab log 30 天、水位 85%→80% — 2026-10-05
- [x] journald 上限 4GB（兩台）— 2026-10-05
- [ ] 依產線實際產量確認保留天數夠不夠（`/etc/default/cfaoi-cleanup` 可調）

## H. 進場後驗收（照順序，每項貼數據）

- [ ] `tools/grab_setup/verify_grab_host.sh` → 全過（2026-10-05 實驗室：26/0）
- [ ] `scripts/verify_step3_trigger.py 127.0.0.1 8100 5 <台數>` → 全 PASS，Spark 端 recv err=0
- [ ] Windows：`ping 192.168.10.21`、`ping 192.168.3.1` 通；Control 三顆燈綠
- [ ] 上位機流程：CF_LOAD_RECIPE → CF_GRAB_START → CF_STOP → CF_GET_RESULT
- [ ] 斷電復電全自動回到就緒（D 項）

## I. 下方陣列（18 × L803K）— 進場前仍未完成的項目

- [ ] Control 同時管 2 台 Grab + 2 台 Spark、結果合併回上位機（尚未實作）
- [x] Grab eBUS 取像後端（`--camera ebus`）+ iPORT CCD 命名工具（`iport_provision`）— 2026-10-05 模擬驗通
- [ ] 實體 iPORT + L803K 實測：取像/掉幀率、UART 曝光/行速率、`iport_provision` ForceIP
- [ ] 18 台命名 CCD38–55（桌面「相機工具」🛠 裝置設定，或 `iport_provision set <SN> CCDnn` → 192.168.4.nn）、行速率統一
- [ ] spark-3961 網路整理（SSH host key）

## J. 文件（Claude 在 fab 內無法協助）

- [ ] runbook 印出或存離線版：本清單、CLAUDE.md §2、6cam_setup_runbook、grab_machine_migration
- [ ] 現場「異常處理一頁紙」：燈號意義 → 該按什麼 / 該找誰
