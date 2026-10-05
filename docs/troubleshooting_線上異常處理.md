# 線上異常處理（給線上人員與機況助手）

> 用途：線上人員只描述「看到什麼」，照這份找原因與該做的事；機況助手（LoopEngineering）也以這份為主要依據回答。
> 原則：**先按「一鍵健檢」**（自動收證據、給結論），再照下面對應的段落處理。相機一律講 **CCD 編號**（線材上有 CCD 標示；交換機埠位會變，不必記埠位）。
> 每一條處理方式都在實驗室實際製造過故障驗證（見文末「已驗證案例」與 `docs/troubleshooting/cases/`）。新的現場案例請照同一格式補進 cases。

## ⛔ 不可以建議線上人員做的事（機況助手必守）

- **不要刪 log**（`journalctl --vacuum`、刪 `/srv/cfaoi`、清 `_diag`）：log 是排障證據，刪了就查不到原因。
- **不要在生產機上重置 GPU / 重灌驅動**（`nvidia-smi --gpu-reset` 等）：先跑 `cfaoi_triage.py --gpu` 判斷，硬體類才找工程。
- **不要叫線上人員改設定檔**（`default_zone.ini`、`cam_config.json`、`appsettings.json`、配方 XML、交換機設定）：要改找工程。
- **不要猜交換機埠號**：埠位會變，一律講 CCD 編號（線材有標示）。
- **不要把無關的 Xid 牽扯進來**：健檢已分類「程式類 + 來自哪支程式」，不是 cfaoi_ip 的程式類 Xid 與生產問題無關。
- 證據不足時說「先按一鍵健檢」，不要編造畫面、按鈕或指令（例：不存在的 `run_tests.sh`）。
- 主機名稱：截取主機現在是 **Grab（user-IMB-M47，192.168.10.21）**；舊文件的 damac 是前一台機器。

## 0. 一鍵健檢

- 在 Grab：`python3 ~/Addis/cf-aoi/tools/triage/cfaoi_triage.py`（約 1 分鐘；產線在取像時自動略過取像測試）
  - `--gpu`：另跑 GPU 深度檢查（離線單元測試 + 參考圖測圖，約 1–2 分鐘，不影響生產 IP）
  - `--save-baseline`：系統**正常時**存基準（各 CCD 相機的 MAC、交換機設定）；換相機或交換機設定確定後重存
- 報告：`/srv/cfaoi/10_logs/<日期>/grab_triage_<時間>.md`（沒有機台資料夾時在 `~/cfaoi_logs/triage/<日期>/`）
- 報告第一段「結論」就是線上人員要看的：❌ 異常、⚠️ 注意，每條都有「可能原因」「請這樣做」「找誰」。
- 結束碼：0 全正常、1 有注意事項、2 有異常。

## 1. 相機

| 症狀 | 健檢會怎麼說 | 原因 | 處理 |
|---|---|---|---|
| 某一台（例 CCD05）沒影像，其他正常 | ❌ CCD05 不見了：**線路沒有訊號** | 標示 CCD05 的網路線/光纖或交換機端模組鬆脫、損壞；或相機沒電 | ① 看 CCD05 相機燈號（不亮 = 沒電，查電源線）② 重插標示 CCD05 的線兩端 ③ 換線，或交換機換一個空埠（埠位不必固定）|
| 同上 | ❌ CCD05 不見了：**線路有訊號，但相機沒回應** | 相機 IP 被改到別的網段、或相機韌體卡住 | ① 開「相機工具」掃描，看 CCD05 是否出現在別的 IP ② CCD05 斷電 10 秒再上電 |
| 同上 | ❌ 線路有 link 但沒有封包 | 相機開機中或當機 | 等 30 秒重跑健檢；仍不見 → 斷電重開 |
| **全部**相機都沒影像 | ❌ 全部 N 台都不見 → 問題在共用的那一段 | Grab ↔ 交換機 100G 上行斷/模組鬆、交換機沒電或當機、交換機設定被重置 | ① 交換機電源燈 ② 重插 Grab ↔ 交換機 100G 線兩端 ③ 相機總電源 ④ 找工程看交換機設定 |
| 影像缺一截、黑色橫條、常判 NG | ❌/⚠️ CCDnn 掉封包（完整度 < 99.5%）| 依證據：交換機埠 CRC/輸入錯誤增加 → **線材/接頭/模組**；埠只協商到 100M → 線材劣化；交換機該埠有限速設定 → **設定被改**；Grab 網卡丟包 → 主機端；MTU 不是 9000 → 設定被改 | 線材類 → 換標示該 CCD 的線或換埠；設定類 → 找工程依基準還原；換完重跑健檢確認 100% |
| 某台偏暗/偏亮、缺陷數怪 | ⚠️ CCDnn 相機參數和設定不同（曝光/增益）| 有人調機沒還原（相機工具 / pylon Viewer）| **Control 重新載入配方**（Grab ARM 會套回 `cam_config.json` 的曝光/增益）；或相機工具設回 |
| 同上 | ❌ CCDnn 偏暗：參數正常，問題在光學 | 光源沒亮/老化/角度偏、鏡頭光圈被轉、鏡頭髒污或遮擋 | 檢查該 CCD 的光源、鏡頭、光圈環 |
| 全部都很暗 | ℹ️ 全部相機畫面都很暗 | 光源沒開或沒有玻璃 | 開光源（實驗室沒光源時屬正常）|
| 新換的相機 | ⚠️ 有一台沒命名的相機 / IP 不符規則 | 新相機還沒設 CCD 編號 | 桌面「相機工具」→ 🛠 裝置設定：填 CCD 編號 → 寫入（IP 自動 = 192.168.5.nn）|

補充：
- 相機身分存在相機裡（DeviceUserID = CCDnn、IP 尾碼 = 編號），**換交換機埠不影響**；換相機要重新命名。
- Grab 取像途中某台斷線，只有那台停，其他照跑；修好後要重新 ARM（Control 重新載入配方或下一片）。

## 2. 交換機

> ⚠️ **實驗室的 HPE 5945 是廠商借用機；正式上線是 NVIDIA SN2201**（指令與連線方式不同，到貨後補 SN2201 版）。
> 下面的 Comware 指令只適用 5945；判斷邏輯（看 link、MAC、最大框長、設定與基準差異）兩台通用。

| 症狀 | 健檢 | 原因 | 處理 |
|---|---|---|---|
| 全部相機不見 | ❌ 交換機 → Grab 上行埠斷線 | 100G 線/模組、或上行埠被關閉（ADM）| 重插/換 100G 線；被關閉 → 工程 `undo shutdown` |
| 某些相機不見 | ⚠️ 交換機有埠被手動關閉 | 有人下了 shutdown | 相機改插其他埠；或工程 `undo shutdown` |
| 換交換機/重置後相機連不上 | ❌ 交換機設定和基準不同 —很可能就是 CCDxx 不見的原因 | 新交換機/重置後**相機埠沒有 `speed 1000`**（25G 埠插 1G 模組不會自動協商成功，看起來像壞線）| 照下方「交換機更換/重置後設定」|
| 某台影像缺一截/完整度很低 | ❌ CCDnn 掉封包 + 「該埠最大框長只有 1536」+ 設定差異 `jumboframe enable 1536` | 相機送 jumbo 封包（約 8KB），埠最大框長 < 9000 → 被交換機丟（超長封包計數暴增）。常見於換交換機/重置後 jumbo 沒開 | 該埠設回最大框長（5945：`jumboframe enable 9416`；SN2201：MTU 9216）|
| — | ℹ️ console 讀取有雜訊 | USB-console 線/轉接頭品質（實測每次讀約 5% 行有 1 個字元錯；健檢已用多次讀取共識避免誤報） | 不影響運作；有空換線 |
| — | ℹ️ 交換機時鐘未設定（2001 年）| 交換機沒對時 | 不影響取像；只影響交換機 log 時間 |

### 交換機更換/重置後設定（HPE 5945；console = Grab `/dev/ttyUSB0`，9600 8N1）

1. 相機埠（25G 埠插 1G 模組）一律 `speed 1000`：**四埠一組（port-group）連動**，對組內任一埠下指令會跳 `[Y/N]`，一定要回 `Y`（腳本化時特別注意，否則後面指令被當成答案吃掉 → 整組 4 台相機失聯）。
2. 相機埠 `stp edged-port`（相機插上立即轉發，不等 STP）。
3. jumbo：5945 原廠 `Maximum frame length 9416`，不需設定；換其他型號要確認 ≥ 9000（不夠 → 相機掉封包，見上表）。
   5945 **不支援**入方向限速（`qos lr inbound` → The operation is not supported）。
4. 上行（接 Grab 100G）：HGE 埠，原廠自動協商即可；確認 `display interface brief` 為 UP 100G。
5. `save force` 存檔。
6. 驗證：`display interface brief | include WGE` 相機埠 UP 1G；跑一鍵健檢全綠後執行 `cfaoi_triage.py --save-baseline` 存新基準。
- SN2201（Mellanox/Cumulus）指令不同，到貨後另補。

## 3. RDMA（Grab ↔ Spark 直連線）：是線還是機器？

健檢會同時看兩端的 link、ping、大封包、RoCE 狀態、IP 服務，直接給出判斷：

| 健檢結論 | 意思 | 處理 |
|---|---|---|
| ❌ 直連線沒有 link（線的問題，或 Spark 沒開機）| 兩端都沒訊號 | ① Spark 電源燈/風扇 ② 重插 Grab ↔ Spark 線兩端 ③ 換備用線：換線就好 = 線壞；換線仍無 link 且 Spark 有開 = 網卡/機器 |
| ❌ 有 link 但網路不通（設定問題，不是線）| 線是好的，IP 位址沒起來 | 重開 Spark（Control 系統狀態）；仍不通找工程查 nmcli |
| ❌ 大封包不通（MTU 不一致）| 兩端 MTU 應 9000 | 找工程設回 9000 |
| ❌ 線和網路都正常，是 Spark 上的 IP 程式沒在跑（**機器/程式問題，不是線**）| IP 服務停止或反覆當掉 | Control 系統狀態 → Spark →「IP 生產」重新啟動；反覆失敗收診斷包 |
| ⚠️ IP 生產在跑但 RDMA 埠還沒在聽 | IP 正在重啟/初始化 | 等 30 秒重跑；仍沒在聽 → 重啟 IP 生產 |
| ❌ RoCE 狀態不是 ACTIVE | 網卡驅動/韌體 | 重開 Grab 與 Spark |

> 註：IP 的 RDMA 監聽（18515）是 RDMA CM，不是 TCP，`ss -ltn` 看不到；要用 `rdma resource show cm_id`。

## 4. GPU / IP 運算

| 症狀/問題 | 怎麼判斷 | 處理 |
|---|---|---|
| 「GPU 是不是壞了？」 | `cfaoi_triage.py --gpu`：① nvidia-smi 讀得到 ② 離線單元測試 5 組（crc/align/coord/edge/rules）全過 ③ **參考圖測圖**：舊機台 IP04 第 27 張，標準答案 1 顆暗缺陷 (5894,725)，±2 px 內相符且**跑兩次結果完全一致** → GPU 正常 | 三項都過 = GPU 沒壞，往配方/光源/相機找；跑兩次不一致 = GPU 不穩（硬體）→ 重開後仍不一致送修 |
| 系統 log 很多「Xid」 | 健檢會分類：**硬體類**（48 DBE、63/64 頁退役、79 掉匯流排、92/94/95 ECC、119/120 GSP…）vs **程式類**（13 非法指令、31 非法記憶體、43 被停止）並列出是哪支程式 | 硬體類 → 重開觀察、重複則送修；程式類且不是 cfaoi_ip → 不影響生產（例：2026-09-29 的 105 筆來自開發測試程式 ccl_bench/dbg）|
| 缺陷數突然爆多、整片 NG | 爆點停算（同台連續 N 張 ≥ 門檻 1000）會寫 DefectCnt=門檻、JSON `flood_skip`；先看是否某台參數被改/偏暗/對焦跑掉 | 跑一鍵健檢看相機參數與取像；確認配方沒換錯 |
| 參考圖結果和標準答案不同 | 先確認 `ip/config/default_zone.ini` 沒被改（標準答案是用 ini 預設參數跑的，不載配方）| 還原 ini；重開 Spark 重測；仍不同找軟體工程 |

## 5. 機況助手（大模型）相關

- 「上位機 CF_READY 一直回未就緒：診斷模式中」→ 機況助手的大模型還開著（佔 Spark 約 80% 記憶體）→ Control 系統狀態 →「結束並回生產」。
- 機況助手只在**停線**（機台有問題/調機）時開；開著期間不能生產。

## 已驗證案例（實驗室故障注入，2026-10-05）

| 案例 | 線上人員說的 | 根因 |
|---|---|---|
| [20261005_camera_cam_missing](troubleshooting/cases/20261005_camera_cam_missing.md) | CCD05 一直沒有影像 | 交換機埠被關閉 → 線路沒訊號 |
| [20261005_switch_packet_loss](troubleshooting/cases/20261005_switch_packet_loss.md) | CCD03 影像缺一截、黑色橫條 | 該埠最大框長 1536（jumbo 沒開）|
| [20261005_camera_param_drift](troubleshooting/cases/20261005_camera_param_drift.md) | CCD02 比其他台暗、昨天有人調機 | 曝光被改 10µs（設定 70）|
| [20261005_switch_uplink_down](troubleshooting/cases/20261005_switch_uplink_down.md) | 所有相機都沒影像、Grab 綠燈 | 交換機上行到 Grab 斷 |
| [20261005_switch_switch_reset](troubleshooting/cases/20261005_switch_switch_reset.md) | 換交換機後 CCD05、06 連不上、換線也沒用 | 相機埠少 speed 1000（port-group）|
| [20261005_rdma_ip_down](troubleshooting/cases/20261005_rdma_ip_down.md) | IP 燈紅、是不是線壞了 | 不是線：Spark IP 程式沒在跑 |
| [20261005_rdma_rdma_cable](troubleshooting/cases/20261005_rdma_rdma_cable.md) | IP 燈紅、Spark 燈亮，線還是 Spark？ | Grab↔Spark 直連線沒 link |

原始資料（健檢報告、注入紀錄、助手回答）：`docs/troubleshooting/experiments/20261005_fault_injection/`；
評估報告：`docs/verification/machine_assistant_eval_20261005.md`。
