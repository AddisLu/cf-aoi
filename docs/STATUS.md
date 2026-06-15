# CF-AOI 系統狀態盤點 (STATUS.md)

> 本文件用 meta 不變式 #0 的 L0–L4 分級，誠實標註每個模組的真實完成度。
> 規則：**標低不標高；有疑慮時標保守級別。「寫好 ≠ 驗證過」。**
> 每一列的級別皆**逐項核實程式碼 / selftest 後**標定；與初版草稿不同者於該列加註。
> 最後更新：**2026-06-15**（納入 2026-06-15 ARM/GB10 運算驗證；前次 2026-06-11 Phase-1 硬體實機驗證）| 對應 commit：**7433f34**

## 分級定義

| 級別 | 意義                 | 判定標準                                     |
| ---- | -------------------- | -------------------------------------------- |
| L0   | 未實作               | 沒有程式碼，或僅有空殼/佔位                  |
| L1   | 已寫程式碼           | 邏輯寫完，但未執行驗證                       |
| L2   | 已單元自測           | selftest / unit test 通過                    |
| L3   | 已端到端驗證         | 本系統內跑通完整流程（如 Mac↔Linux offline） |
| L4   | 已與真實外部系統驗證 | 接真實上位機 / 相機 / 硬體驗證過             |

⚠️ L3→L4 通常藏最多意外（真實硬體/協議的邊界條件），跨越時標註尤其保守。

> **本盤點的證據來源**：① IP 端我在開發機（Linux + RTX 2080 Super）實跑 offline-file / offline-tcp +
> python TCP 客戶端驅動。② Control 端 headless `--selftest`（9 個 case，皆在 Linux 跑過）。
> ③ 使用者在 **Mac** 跑 Control 連 Linux IP 的觀察（多個 bug 回報＋截圖即為 Mac↔Linux 實測佐證）。
> 凡「僅程式碼+編譯、無 selftest、最新版未經 Mac 目視」者一律降到 L1。

---

## 三層架構總覽

| 層      | 平台                           | 角色                 | 整體狀態          |
| ------- | ------------------------------ | -------------------- | ----------------- |
| Control | C# / Avalonia（Mac·Win·Linux） | 控制平面 + 操作 UI   | **L1–L3（混合）** |
| Grab    | Linux / C++                    | 相機擷取 + RDMA 發送 | **主程式 L0；底層硬體能力 L4（Phase-1 實機驗證）** |
| IP      | Linux / CUDA（→ DGX Spark）    | GPU 演算法 + 推論    | **L4（DGX Spark GB10 sm_121 實機：編譯+運算正確性+跨架一致性+速度，2026-06-15）** |

---

## Control 端（C# / Avalonia）

| 模組 | 級別 | 驗證方式 / 缺什麼 |
| ---- | ---- | ----------------- |
| 主視窗 MainWindow（1:1 frmCfAoi） | **L2** | 編譯過；早期核心（CF 鈕/log/status）使用者 Mac 目視過。**(原草稿 L3→L2：無 selftest；ShareSetting/RecipeSetting 兩面板為最新 commit、僅 selftest settings 驗邏輯+編譯，版面未經 Mac 複測；Win/Linux 桌面未跑)** |
| Step1View（離線分析工具：載圖→Test→結果/縮圖） | **L3** | 使用者 Mac↔Linux 跑通：按 Test→Linux IP→回傳，看到 DefectCnt 561 + 縮圖牆。**(維持 L3，但註：Debug 接線 `b5c6d2a`、圓圈 overlay `00b759f` 為事後修正，最新版待 Mac 複測)** |
| 影像 Viewer 互動（縮放/平移/像素值/量 Pitch） | **L1** | 程式碼+編譯（Matrix 變換/WriteableBitmap/反矩陣換算寫好）。**(原草稿 L3→L1：滾輪縮放/平移/像素值/量 Pitch 這些互動無 selftest、未見 Mac 確認；影像「顯示」本身有跑到，但互動操作沒有佐證)** |
| FFT 估算 Pitch | **L2** | `--selftest fft <image>` 通過（落在合理範圍）。(草稿 L2 ✓) |
| 缺陷標示 overlay（圓圈/刷新/導航） | **L1** | 程式碼+編譯。**(原草稿 L3→L1：舊「方框」版曾於 Mac 顯示過[L3 概念]，但現行為「中空圓圈 + 開窗自動重建」最新版 `00b759f`，未經 Mac 目視確認、無 selftest)** |
| ZoneParamEditor（27 列參數編輯） | **L2** | `--selftest store` 驗三處共用同一 ZoneSettingModel 實例（雙向同步）。**(原草稿 L3→L2：資料同步有 selftest；27 列表單版面待 Mac 目視)** |
| 配方單一資料來源 RecipeStore | **L2** | `--selftest store`：三處同步 + 存檔 XML 含改後值。(草稿 L2 ✓) |
| ShareSetting（全域，appsettings.json） | **L2** | `--selftest settings`：JSON round-trip + 只覆寫該節點 + AiRootPath 預設空。(草稿 L2 ✓) |
| RecipeSetting 面板（per-recipe XML 編輯/存讀） | **L2** | `--selftest settings`：per-recipe XML round-trip + 不存在回預設 + MaxDefectCountPass 預設 10000。(草稿 L2 ✓) |
| 連線心跳偵測（CHECK_HEALTH） | **L2** | `--selftest heartbeat` 存在但為**手動 harness**（需起停 IP 觀察綠↔紅，無自動斷言）。重連/IsBusy 邏輯寫好；綠↔紅曾於 Linux 本機觀察。**(原草稿 L3→L2：非自動 unit test，且非跨機定論)** |
| DefectSort（看小圖人工分類 TrueDefect/Particle） | **L2** | `--selftest patches`（filter/即時持久化/統計/UTF-8）+ `--selftest sort` 通過。使用者 Mac 跑過（中文亂碼、1122 重複等 bug 已修）。**(原草稿 L3→L2：Control 分類 UI 的 selftest 為假 IP server；filter/即時存最新版待 Mac 複測。IP 側遠端命令另計為 L3，見 IP 表)** |
| UpstreamServer（CF_/8787/9 參數） | **L1** | ⚠️ 核實：`UpstreamServer.cs` 內有 `TcpListener(8787)` + `.Start()` + `CF_` switch 完整邏輯，但**全專案無任何處 `new UpstreamServer`／呼叫其 Start／綁 On* 回呼到 IpClient，從未接真機**。接真機前需：接線啟動 + 綁回呼 + 實機/模擬器驗證。(草稿 L1 ✓ 確認) |
| Step 2-3 / Step 4-5 操作 UI（RDMA 監控 / 存圖瀏覽 / 生產） | **L0** | 無對應 View（只有 MainWindow/Step1/ZoneParamEditor/DefectSort/SystemSettings）。**(草稿漏列，補上)** |

---

## IP 端（Linux / CUDA, RTX 2080 Super → DGX Spark）

| 模組 | 級別 | 驗證方式 / 缺什麼 |
| ---- | ---- | ----------------- |
| GPU 演算法引擎（DIV-only 比例式閾值） | **L4** | RTX 2080S offline-file（2606 缺陷）；**2026-06-15 DGX Spark GB10/sm_121 實機**：26 張真實面板 vs reference ground truth 25/26 缺陷數完全一致（見 [ARM 驗證報告](verification/verification_report_arm_20260615.md)）。 |
| CCL 決定性（收斂迴圈 + canonical 排序） | **L4** | `--verify-deterministic`：x86 bit-exact(2606)；**GB10 sm_121 上 26 張兩跑全 bit-exact（2026-06-15）**。 |
| 結構化輸出（{yyyyMMdd}/{panel}_{recipe}/） | **L3** | 真機跑出該結構；`[Diag]` 三數一致（DetectionResult=JSON DefectInfo=寫出 patch=2606）。(草稿 L3 ✓) |
| 缺陷 patch 存圖（PNG、多執行緒、debug gate、重測清舊） | **L3** | 真機 offline 驗證：PNG overlay、多緒寫、`debug` 門檻 off→0/on→全存、無條件清舊 Defect_*。(草稿 L3 ✓) |
| ControlServer（port 8200, newline-JSON 命令） | **L3** | python TCP 客戶端於真機驅動 LIST/SORT/patches/SEND_IMAGE 跑通；使用者 Mac Control 亦連上。(草稿 L3 ✓) |
| DefectSort 遠端命令（LIST/SORT/LIST_PATCHES/GET_BATCH/SAVE_CLASSIFICATION） | **L3** | 真機 python 驗 round-trip + 實際產生 TrueDefect/Particle/ 子夾 + classification.json + base64 PNG。(草稿 L3 ✓) |
| AI 推論（RF model, Tensor Core FP16） | **L1** | ⚠️ 核實：`ai_classifier` 有載入（`ai_on=true`），但 `ai_active` 預設 **false**、`--use-ai` 才開；Step 7 過濾 gated on `ai_on && ai_active`，預設缺陷全標 `待人工複核`。因 TrueDefect 樣本不足暫停用。(草稿 L1 ✓ 確認) |
| RecipeSetting 接線（max_save / patch_size 吃設定） | **L0** | ⚠️ 核實：`result_saver` **不讀** RecipeSetting.xml，固定用 `SaveOptions.patch_size=100` + `debug` gate（甚至把 100 寫進結果 XML 的 SaveDefectWidth）。待擴 TCP 協議。(草稿 L0 ✓ 確認) |
| rdma-validate / image-capture / online 模式 | **L0** | ⚠️ 核實：`main.cpp` 只有 `offline-file` / `offline-tcp` 兩分支；`ip/src/modes/` **空目錄**、`image_source/` 無 `rdma_source`。CMake 雖有 IBVERBS 條件項但對應檔不存在。**(草稿漏列，補上)** |

---

## Grab 端（Linux / C++）

> ⚠️ **必須分兩件事看**：① 正式 Grab **主程式**（多相機陣列 / cam_manager / 連 Control）**仍未寫 = L0**；
> ② 但 Grab 依賴的**底層硬體能力**（相機擷取、RDMA→GPU、端到端）已由 `Reference/phase1_tests/` 測試套件
> **2026-06-11 實機 PASS = L4**（見下「Phase-1 硬體路徑」表）。**L4 僅限那條測試路徑**；正式 Grab 主程式從測試
> 工具升級、接上 Control/IP 之前，整體 Grab **不算 L4**。

| 模組 | 級別 | 驗證方式 / 缺什麼 |
| ---- | ---- | ----------------- |
| Grab 正式主程式（cam_manager / 多相機 / control_client / rdma_sender） | **L0** | 未寫：`grab/src/` 空（只有 CLAUDE.md + 空 src/config）。待從 phase1_tests 升級 |
| ⤷ 底層能力：相機擷取 + RDMA→GPU + 端到端 | **L4** | 見下表（Phase-1 測試套件實機 PASS）|

### Phase-1 硬體路徑（2026-06-11 實機 PASS）→ 證據：[docs/verification/verification_report_20260611.md](verification/verification_report_20260611.md)

> 主機：截取中心 `damac`（Ryzen7700 + ConnectX-5 MCX516A）↔ DGX Spark `spark-c16f`（GB10/CUDA13/sm_121）；
> 相機 Basler raL8192-12gm @192.168.5.1；RDMA 192.168.3.2↔192.168.3.1（100G DAC）。

| 測試路徑 | 級別 | 實機結果 |
| ---- | ---- | ---- |
| 相機偵測 `t30_pylon_probe` | **L4** | raL8192-12gm SN=25445953 偵測到 |
| 取像穩定 `t31_pylon_grab`（500 幀） | **L4** | **零掉幀**（0.0000%）、FPS 23.3、光源+600% 亮度確認真感光 |
| RDMA 鏈路 `10_rdma_linkcheck` | **L4** | 100G PORT_ACTIVE、RoCE v2（Ethernet）|
| RDMA→GPU 正確性 `t21_rdma_gpu_*` | **L4** | 32MB **逐位元** CRC=0x1591755899 兩端一致、零錯 |
| RDMA 延遲 `ib_write_lat` | **L4** | avg **1.44μs**、P99 1.57μs |
| 端到端 `t40_e2e_*`（含 `_file` 回放） | **L4** | 相機/檔案→FrameHeader→GPU 全幀 CRC 正確 |
| GB10 cudaHostAlloc 收圖（取代 nvidia_peermem） | **L4** | `t40_e2e_server` 用 `cudaHostAlloc(Portable\|Mapped)` 收圖驗證通過（見不變式）|

---

## 跨機 / 協議

| 項目 | 級別 | 驗證方式 |
| ---- | ---- | -------- |
| 跨架構一致性（x86 sm_75 ↔ ARM GB10 sm_121） | **L4** | 2026-06-15 GB10 實機：26 張真實面板 vs reference ground truth，**25/26 缺陷數完全一致**；整數/幾何完全一致，浮點邊界像素可能單像素 ULP 翻面（028 案例，DTH 0.60→0.58 即消失，偏保守方向，**已判定接受**）→ **off-line 調參對 on-line 有效**。見 [ARM 驗證報告](verification/verification_report_arm_20260615.md)。 |
| network-clean（配方 XML 內容 / 結果 JSON / patch base64 over TCP） | **L3** | IP 側真機驗證不依賴對方檔案系統；使用者 Mac↔Linux 實跑（過程發現並修了中文亂碼/重複小圖/存圖 bug，即跨機實測佐證）。 |
| 配方 round-trip（Mac 改→IP 套用） | **L2** | IP offline-tcp `LOAD_RECIPE` 吃 `recipe_xml` 內容並套用，已於 Linux 驗。**(原草稿 L3→L2：完整「Mac 改值→IP 套用→結果反映」由使用者調參時跑過，但本盤點無乾淨可復現的單一佐證，保守標 L2)** |
| 上位機協議（CF_/8787/gg4mida/timeout 40000） | **L1** | ⚠️ Control 端寫好（含 .Start/CF_ switch），**從未接真實上位機、且未被任何啟動處呼叫**（見 UpstreamServer 列）。 |
| Grab↔IP RDMA 線格式 — **phase1 版** `Reference/phase1_tests/FrameHeader.h`（magic `0xA01CF00D`） | **L4** | 此版**實機收發驗證過**（t21/t40 全幀 CRC 正確）。定為正式線格式。 |
| Grab↔IP RDMA 線格式 — `shared/FrameHeader.h` | **已對齊 phase1（L2 編譯驗證，wire 仍待 L4 真機收發）** | ✅ 2026-06-14 已把 `shared/FrameHeader.h` 對齊 phase1 版（magic `0xA01CF00D` + frameSeq u64/panelId/sliceIndex/machineCoordXY）；IP 編譯通過、offline 偵測 2606 bit-exact 不受影響（只 rename camId/frameSeq 2 處 + make_frame_header 相容層）。舊 `0xCFA0A001` 版作廢。**wire 真機收發**仍待 RDMA 主程式（明天起）。 |
| RDMA 收發實作（RdmaSender/Receiver 主程式） | **L0** | 正式 Grab/IP 主程式尚無收發碼（phase1 的 `t40`/`RcConn` 為可升級樣板）。 |

---

## 硬體狀態

| 硬體 | 狀態 |
| ---- | ---- |
| 開發機 RTX 2080 Super（sm_75, CUDA 12.x） | ✅ IP offline 演算法運作中（本盤點實跑） |
| DGX Spark（GB10 sm_121, CUDA 13） | **RDMA→GPU 收圖路徑 2026-06-11 實機 PASS（L4）**；**AOI 演算法 2026-06-15 GB10 實機驗證 PASS（L4）**：編譯零警告、26 張真實面板跨架一致、~7.4ms/張 → **1 台 Spark 足夠**（餘裕 ~73%）|
| Basler raL8192-12gm（1 台，pylon 26.05） | ✅ 實機取像 PASS（500 幀零掉幀）；37 台陣列＋Switch 未接（Step 3+）|
| 18× L803K+iPORT（eBUS） | 未接（eBUS SDK 未裝；Step 2+）|
| Mellanox ConnectX-5（截取中心）/ ConnectX-7（Spark） | ✅ 100G RDMA 鏈路實測 PASS |
| SN2201 交換器 | 未到貨 / 未確認（明天為 1 台相機直連，無 Switch）|

---

## 已驗證的關鍵不變式（血淚教訓，詳見各 CLAUDE.md）

- **DIV-only**：只接受 DIV 配方，SUB 無法精確對應 GPU 比例式閾值，直接拒絕載入並報錯
- **CCL 決定性**：host wrapper 收斂迴圈直到 `d_changed==0` + canonical 排序；從 Reference 重抄 kernel 會覆蓋此迴圈（本次 `--verify-deterministic` 實測 bit-exact）
- **Pitch 正確性**：偏差 1–4px 就讓正常網格爆量（26→30 → 561→10000 觸頂）；新面板先 FFT 估算
- **配方單一資料來源**：三處共用同一 ZoneSettingModel 實例（`--selftest store` 驗）
- **重測前清 panel 夾**：`result_saver` 無條件清舊 `Defect_*`，避免 DefectSort 歷史殘留疊加（曾 561→1122）
- **缺陷檔名 IpName 取自 panel 前綴**：與資料夾一致（修正 Defect_IP01 vs 夾 IP02）
- **TCP 整行 UTF-8 解碼**：`IpClient` 累積 bytes 整行解 UTF-8（修中文亂碼，不可逐 byte→char）
- **跨架構一致性（x86↔ARM）**：整數/幾何欄位完全一致；浮點閾值邊界像素可能單像素 ULP 翻面，方向偏保守（**寧抓勿漏**）→ 可接受，進 DefectSort 人工複核。案例：IP04_028 單像素 dark @(1852,2372)，DTH 0.60→0.58 即消失（2026-06-15 GB10 實測）
- **GB10 容量：1 台 Spark 足夠**：實測正常面板 ~7.4ms/張（cudaEvent），1110×7.4ms=8.2s/面板 < 30s 節拍（餘裕 ~73%）。block_dim 16×16 vs 32×32 無差異。vs reference 4.9ms 慢 1.5× 屬 CCL 收斂迴圈+zero-copy+canonical 排序的決定性代價，非 bug
- **誠實分級（meta #0）**：功能狀態須分 L0–L4，不可把「寫好」說成「驗證過」（UpstreamServer / AI 推論即案例）

---

## 核實摘要：相對初版草稿改了哪些級別

**降級（標低不標高）：**
1. **MainWindow L3→L2**：無 selftest；新增的 ShareSetting/RecipeSetting 面板未經 Mac 目視。
2. **影像 Viewer 互動 L3→L1**：縮放/平移/像素值/量 Pitch 互動無 selftest、未見 Mac 確認。
3. **缺陷標示 overlay L3→L1**：現行「圓圈+開窗重建」為最新 commit，未經 Mac 目視（舊方框版曾顯示過）。
4. **ZoneParamEditor L3→L2**：僅資料同步有 selftest，27 列表單版面待 Mac 目視。
5. **連線心跳 L3→L2**：`--selftest heartbeat` 是手動 harness（無自動斷言），非跨機定論。
6. **DefectSort（Control UI）L3→L2**：分類 UI selftest 用假 IP server；最新 filter/即時存待 Mac 複測（IP 側遠端命令另計 L3）。
7. **配方 round-trip L3→L2**：完整「Mac 改→IP 套用」無乾淨可復現佐證，保守標 L2。

**確認草稿正確（未改）：**
- UpstreamServer **L1**、IP AI 推論 **L1**、RecipeSetting 接 IP **L0** — 三個重點全部核實無誤（分別查到：無 .Start 呼叫處／`ai_active` 預設 false／result_saver 不讀 RecipeSetting.xml）。
- FFT **L2**、RecipeStore/ShareSetting/RecipeSetting 面板 **L2**、IP GPU 引擎/CCL/結構化輸出/patch 存圖/ControlServer/DefectSort 遠端命令 **L3**、Grab **L0**、上位機協議 **L1**。
- CCL 決定性：草稿 L3，本次補跑 `--verify-deterministic` 取得 bit-exact 硬證據，L3 站得住。

**草稿漏列、本次補上：**
- **IP rdma-validate / image-capture / online 模式 = L0**（`main.cpp` 只有 offline-file/offline-tcp，`modes/` 空）。
- **Control Step 2-5 操作 UI = L0**（無對應 View）。
- 跨機協議補列 **Grab↔IP RDMA = L1**（header 定義好、收發 L0）。

---

## 下一階段：Step 2（Grab + RDMA）前置

進入硬體階段前需要：
1. 傳 `Reference/phase1_tests/` 到開發機
2. 裝 pylon SDK（Basler）+ eBUS SDK（Pleora）
3. 確認 ConnectX-5 NIC + SN2201 交換器 + DAC 線實體狀態
4. UpstreamServer 接線（online 階段需要）：L1→（接線啟動+綁回呼）→ L4
5. RDMA 收發實作（FrameHeader.h 已備）：L0/L1 → L3 → L4
