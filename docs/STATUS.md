# CF-AOI 系統狀態盤點 (STATUS.md)

> 本文件用 meta 不變式 #0 的 L0–L4 分級，誠實標註每個模組的真實完成度。
> 規則：**標低不標高；有疑慮時標保守級別。「寫好 ≠ 驗證過」。**
> 每一列的級別皆**逐項核實程式碼 / selftest 後**標定；與初版草稿不同者於該列加註。
> 最後更新：**2026-06-17**（① 二次考古：Reference 源碼回頭驗 #1–#28 → 100% 一致,補登 #29–#31,見表四。② UI 設定專項複查：legacy 15 表單 ↔ 新版 5 View 逐控制項對照,補登 #32–#33,見表五/表六。前次：Gap #2 CCD 參數 UI — Grab 側 Stage 0+1+4 實機驗通 → L3）

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
| Grab    | Linux / C++                    | 相機擷取 + RDMA 發送 | **單相機路徑 L4（2026-06-15 Step 2 實機驗證）；多相機全陣列 L0（Step 3，待 Switch）** |
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
| RecipeSetting 接線（max_save / patch_size 吃設定） | **L3** | **2026-06-17 DGX Spark GB10 實機**：MaxDefectCountPass 截斷決定性 4 run 全 bit-exact（截斷點 50/149/150 均 bit-exact；`[Verify] ✓ 全部影像兩次執行 bit-exact`）；SaveOptions save_width/height 由 recipe_saving 正確覆蓋。合成影像 150 缺陷（100 亮+50 暗），不觸 GPU cap。見 ip/CLAUDE.md 不變式 21/22。 |
| FrameQueue 背壓 + buffer 安全計算器 | **L3** | **2026-06-17 DGX Spark GB10 實機（no_wait 串流 20 幀）**：`[BufferCalc] host可用RAM=112488MB 幀大小~39MB FrameQueue上限=1幀 SourceRing上限=4幀`（--max-queue-size 1）。no_wait=true 快速送 20 幀（--test-consumer-delay-ms 2000 模擬慢消費端）：14/20 幀收到 `ERR "FrameQueue 滿（上限 1 幀）：系統繁忙，請稍後重試"`；6/20 幀 OK（Consumer 偶爾空閒時）；queue.size() 始終 ≤ 上限。VmRSS 增長 155MB < 778MB（20幀×38MB）。水位 100% 觸發 `queue_high_watermark` incident JSON 17 次。見 ip/CLAUDE.md 不變式 18。 |
| TuningRecipe（量速模式：GPU 跑但不寫磁碟） | **L3** | **2026-06-17 DGX Spark GB10 實機**：LOAD_RECIPE tuning_recipe=true → TCP 回 `status=OK DefectCnt=150`（結果仍回傳），output 目錄新增 0 個檔案（`[PASS] TuningRecipe 模式：output 目錄零新增檔案`）。IP log 確認 `[TuningRecipe] 跳過存圖（結果仍回傳）`。見 ip/CLAUDE.md 不變式 20。 |
| SaveSourceImage + SourceImageWriter（原圖非同步存檔） | **L3** | **2026-06-17 DGX Spark GB10 實機（no_wait 20 幀，ring=2，--test-source-writer-delay-ms 600ms 模擬慢碟）**：VmRSS 增長 10MB（基準 330MB，峰值 447MB，消化後 340MB），100 幀×38MB=3800MB 資料，VmRSS 完全不線性成長；穩定期抖動 29MB < 5×38=190MB。`[SourceWriter] WARN ring 滿（2 槽），drop panel=SRC_OOM_0009`（20 幀中 2 幀觸發 drop，ring 上限生效）。`source/SRC_OOM_*.bin` 按實際寫入幀數產生（非 100%=ring 固定上限，非 List 囤積）。見 ip/CLAUDE.md 不變式 19。 |
| rdma-validate 模式（N-slot ring + credit 背壓） | **L3** | **2026-06-17 damac↔Spark 實機**：Phase 1 連續 120 幀 CRC=OK（ok=120 err=0，slot 0→3 繞回正確，1375fps/86MB/s）；Phase 2 背壓（`--test-consumer-delay-ms 200`）20 幀全通（ok=20 err=0，Grab 降至 9.6fps 而非斷線，QP 未進 error state）；CM DISCONNECTED 偵測乾淨退出（commit `de047a3`）。見 ip/CLAUDE.md 不變式 23。 |
| image-capture / online 模式 | **L0** | 未實作（`main.cpp` 無此分支；需相機陣列 + Control 完整接線）|
| 對位 pipeline（Gap #1：golden_maker + align_engine + CHECK/SET_ALIGN + ZoneConfig eff_*）| **L3** | **2026-06-17 DGX Spark GB10 實機驗通**：Stage 1 align_verify 14/14 PASS（sub-pixel 誤差全 <0.1px，最差 0.087px；旋轉誤差 0.000°~0.062px；空白圖 ok=false+ERR 路徑確認；eff_* fallback + SET_ALIGN 套回邏輯確認）；Stage 2 verify_alignment.py 8/8 PASS（n0=7 缺陷基準；偏移 7px 整張面板→對位 ShiftX=7.001 ShiftY=3.000 誤差 <0.001px→SET_ALIGN→偵測 n_aligned=7 = n0，缺陷數一致；Stage 3A 空白 ROI→ERR）。見 `ip/src/align_verify.cpp` + `scripts/verify_alignment.py`。 |
| 缺陷座標 pixel→μm（Gap #5：OpticalParams INI [Optical] + GlobalPosX/Y_um + CcdIndex） | **L3** | **2026-06-17 DGX Spark GB10 實機驗通**：Stage 1A coord_verify 8/8 PASS（含負數/缺 section/垃圾 INI smoke）；Stage 1B（真實 LOAD_RECIPE inline XML）：GlobalPosX=1202 × opt_res=0.5 → GlobalPosX_um=601.000，CcdIndex=0（μm 不是 0）；Stage 2 bit-exact：7顆缺陷 pixel+μm 兩次完全一致；Stage 3A opt_res=0.0 sentinel：GlobalPosX_um=0.000（pixel 欄位 bit-exact 不變）。架構：INI→OpticalParams→process_image參數→InspectionResult→ResultSaver；LOAD_RECIPE 換 zones 不碰 OpticalParams。⚠️ **follow-up（待確認後才 L4）**：欄位名（GlobalPosX_um/Y_um）、單位（μm）、精度（3位小數）為 IP 端片面提議，尚未與上位機確認；上位機是否確實從 ResultInfo.xml 讀 μm 待接真機驗證 → 與 UpstreamServer 接真實上位機屬同一條 follow-up。見 `ip/src/coord_verify.cpp` + `scripts/verify_coord.py`。 |
| 行車紀錄（flight recorder：結構化診斷 JSONL/incident） | **L3**（src 欄位 **L1**） | `diag/flight_recorder` 環形緩衝+只記出事；2026-06-15 RTX 2080 端到端驗證五種 incident kind（cuda_fatal 經人為 OOM 觸發、frame_validation/bad_json/recipe_load/uncaught_exception）+ JSON 全可解析 + 決定性不破 + bench 無 `_diag`（recorder no-op，gpu_ms 零擾動）。見 ip/CLAUDE.md 不變式 16。**2026-06-18 新增 incident `src`（出錯源碼 `檔名:行號`，repo 相對）欄位 + `FR_RECORD_INCIDENT` 巨集（`__FILE__:__LINE__` 編譯期常數，零成本、不破 bit-exact）+ `docs/html/incident-viewer.html`（log→VS Code `vscode://file` 跳轉，可攜）。`src` 欄位 **L1**：已寫碼（11 呼叫點轉巨集 + `repo_relative()` 正規化），待 Linux/Spark 重編 + 觸發 incident 確認 `src` 內容正確再升 L3。** |
| 收圖入口 magic/version/CRC32 + 尺寸驗證 | **L3（offline-tcp）/ L1（RDMA wire）** | offline-tcp：尺寸防呆 + client 宣告 `crc32` 比對於 RTX 2080 實測拒收+記 incident（L3）。**RDMA wire header 的 magic/version/CRC 驗證分支待 `rdma_source` 實作後才生效（L1）**。見 ip/CLAUDE.md 不變式 17。 |

---

## Grab 端（Linux / C++）

> **2026-06-15 Step 2 更新**：正式 `cfaoi_grab` 單相機路徑已實機驗通（L4）。
> 見 [Step 2 驗證報告](verification/verification_report_step2_20260615.md)。
> 多相機全陣列（--cam-count ALL）+ Switch + N-slot ring buffer 屬 Step 3，尚未實作（L0）。

| 模組 | 級別 | 驗證方式 / 缺什麼 |
| ---- | ---- | ----------------- |
| 正式 cfaoi_grab 單相機→RDMA→Spark（cam_pylon + rdma_sender + control_server + main） | **L4** | 2026-06-15 Step 2 實機：raL8192-12gm → pylon → FrameHeader(0xA01CF00D)+CRC32 → RDMA 18515 → Spark GB10 pinned memory → CRC 20/20，FAIL=0（見 [Step 2 報告](verification/verification_report_step2_20260615.md)）|
| 多相機全陣列（cam_manager / --cam-count ALL / N-slot ring buffer） | **L0** | 未實作；Step 3 待 Switch 到貨 |
| rdma_nslot_test（合成幀送器，驗 N-slot ring + 背壓，不需相機） | **L3** | **2026-06-17 damac↔Spark 實機**：120 幀連送 CRC=OK；背壓 20 幀（IP 200ms 延遲）ok=20 err=0；commit `de047a3` |
| Control↔Grab 8100 完整接線 | **L1** | CHECK_HEALTH/LOAD_RECIPE/GRAB_START/GRAB_STOP 命令解析正確；以 nc hardcode 觸發；未接真正 Control UI |
| ⤷ Gap #2：參數控制（SET/GET_CAM_PARAMS）| **L3** | **2026-06-17 damac raL8192-12gm 實機**：Stage 0（ExposureTimeAbs 2~10000µs；GainRaw 256~2047；TLParamsLocked=0）；Stage 1（SET actual 誤差 0%：exp 200/500µs actual 完全一致；gain 256/512 actual 完全一致）；Stage 4（4 ERR 路徑全 PASS）；cam_config.json 持久化。close() 空 QP bug fix：重現 connect 127.0.0.1 route 失敗 → `rdma_destroy_qp(nullptr)` SIGSEGV；修後 `if (id && id->qp)` guard + null-clear，乾淨退出。⚠️ 假設：曝光/增益為機器層（cam_config.json），若日後隨產品調 → 補 recipe-override。|
| ⤷ Gap #2：光度效果（Stage 2+3 mean gray 單調性）| **L3** | **2026-06-17 damac 加光源後 cam_mean_gray_test 全 PASS**：Stage 2 曝光 exp=70µs mean=3.30 / exp=500µs mean=7.63 → **ratio=2.314（>1.4）PASS**；Stage 3 增益 gain=256 mean=4.65 / gain=1024 mean=10.54 → **ratio=2.267（>1.2）PASS**。證 set_params 確實驅動 sensor 積分（曝光/增益皆單調遞增）。（暗環境基線：mean≈2.5/255 noise floor，ratio≈0.95，加光源後響應正確。）|
| ⤷ Gap #2：Control UI（相機 tab + SystemSettings TabControl 改版）| **L1（待 Mac 目視）** | TabControl（連線設定/相機）；相機 tab：Grab Ellipse 指示器、ExposureUs NumericUpDown 2~10000µs + actual 回顯行、GainRaw 256~2047 + actual 回顯行、Apply（IsEnabled=IsGrabConnected）/讀取 btn + CamStatus。MVVM `[ObservableProperty]/[RelayCommand]` 對齊現有面板；0 警告 0 錯誤。**連線設定 tab = 原有內容搬入 TabItem，待 Mac 重新目視確認版面無誤**；相機 tab 互動亦待 Addis Mac 目視。**（2026-06-18 此相機 tab 已演進為「相機陣列總覽」，見下兩列。）**|
| ⤷ Gap #2+：LIST_CAMERAS 唯讀列舉（cam_pylon enumerate + control_server）| **L3（idle）** | **2026-06-18 damac raL8192-12gm 實機（idle，未 GRAB_START）**：`{"cmd":"LIST_CAMERAS"}` → 回 1 台:`mac=00:30:53:53:19:41 model=raL8192-12gm serial=25445953 ip=192.168.5.1 persistent=true device_class=BaslerGigE`（`CTlFactory::EnumerateDevices`+`CDeviceInfo`,不開相機）。⚠️ **GRABBING 中並存呼叫未驗**：本次 Spark RDMA 收端 `rdma_bind_addr: Cannot assign 192.168.3.1`（RDMA 鏈路未就緒）→ 無法讓相機真的串流 → 「列舉 vs 取像並存不掉幀」**待 RDMA 鏈路就緒後補測**；在驗證前建議 LIST_CAMERAS 僅於 idle 呼叫（程式未加守門，誠實列為 follow-up）。`ip_config` 目前回原始碼值（如 "5"），persistent bool 為權威狀態。|
| ⤷ Gap #2+：Control 相機陣列總覽 view（KPI/實體陣列/分群/明細）| **L2（邏輯）/ L1（版面待 Mac 目視）** | SystemSettings 相機 tab 演進為總覽（資訊架構照 `camera_overview_mockup.html`）：KPI（配置/上線/已綁定/待綁定/離線）+ 實體陣列色碼（綠/琥珀/灰）+ 分群清單 + 明細面板（重用 Gap #2 曝光/增益,改 `SelectedCamera.CamId`）。**2026-06-18 `dotnet build` 0 警告 0 錯誤 + `--selftest camera`（假 server 多台 bound+unbound）全 PASS**：列舉 2 台 / KPI 配置2上線2綁1待綁1離0 / 分群 bound[CCD00]+unbound[CCD01]+offline 空 / 預選第一台 / 欄位解析。**離線群維持 0（無 config↔CCD 映射,不假造）**。版面待 Addis Mac 目視 → L1。**defer（= Gap #21）**：綁定動作（指派 IP/位置映射,按鈕停用標 #21）/ 配置 vs 偵測映射（現配置數=偵測數）/ cam_id↔MAC 穩定映射（現以列舉 index 暫派,重啟可能對到別台,cam_config per-cam 存檔亦繼承此不穩,多台改 MAC keying）。多相機（37 台）/離線格待 SN2201 Switch + 相機陣列。|
| ⤷ 底層能力：相機擷取 + RDMA→GPU + 端到端（Phase-1 測試套件） | **L4** | 見下表（Phase-1 測試套件實機 PASS）|

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
| Grab↔IP RDMA 線格式 — `shared/FrameHeader.h` | **L4** | ✅ 2026-06-14 對齊 phase1 版（magic `0xA01CF00D`）；2026-06-15 Step 2 cfaoi_grab 以此 Header 實機傳輸，Spark 端 magic/CRC 全對，wire 真機收發驗通。舊 `0xCFA0A001` 版作廢。 |
| RDMA 收發實作（RdmaSender 正式主程式 / 單相機） | **L4** | 2026-06-15 Step 2：`grab/src/rdma_sender.cpp` 實機驗通（CRC 20/20）。IP 端 rdma-validate / rdma_receiver 仍 L0。 |

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
- **GB10 容量：1 台 Spark 足夠**：實測正常面板 ~7.4ms/張（cudaEvent，於固定 block_dim 16×16），1110×7.4ms=8.2s/面板 < 30s 節拍（餘裕 ~73%）。vs reference 4.9ms 慢 1.5× 屬 CCL 收斂迴圈+zero-copy+canonical 排序的決定性代價，非 bug
- **block_dim 固定 16×16**：`zone_config_adapter::from_ini` 硬寫死 16×16，INI 的 `[GPU] block_dim`（32×32）為死設定從未生效；RAG 建議的 16×16 已是現狀，改 INI 不影響 GPU block 維度（勘誤：原「16 vs 32 無差異」實為同一設定的重跑）
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

## Step 3 N-slot RDMA 實機驗證（2026-06-17 **全通 → L3**）

> 驗證環境：`damac`（Ryzen7700 + ConnectX-5）↔ `spark-c16f`（GB10/sm_121 + ConnectX-7），100G DAC，RoCE v2。
> 最終 commit：`de047a3`（ip: rdma_source 偵測 Grab 斷線）

**已實作且驗通：**

| 元件 | 檔案 | 說明 |
|------|------|------|
| `MrInfoEx` 握手結構 | `grab/src/rdma_common.h` | 256B wire format，含 `n_slots`/`slot_size` |
| Grab N-slot 定址 | `grab/src/rdma_sender.cpp` | `slot_id = frame_seq % n_slots`，`poll_one` = backpressure |
| IP N-slot ring buffer | `ip/src/image_source/rdma_source.cpp` | cudaHostAlloc N×slot，初始 N post_recv，釘點 1 順序 |
| `push_blocking` | `ip/src/image_source/image_source.h` | FrameQueue 滿時阻塞（背壓鏈） |
| `rdma-validate` 模式 | `ip/src/main.cpp` | `#ifdef CFAOI_HAS_RDMA`，CRC 二次驗，seq 序驗 |
| `rdma_nslot_test` | `grab/src/rdma_nslot_test.cpp` | 合成幀送器，不需相機 |
| CMake | `ip/CMakeLists.txt`, `grab/CMakeLists.txt` | ibverbs+rdmacm 條件連結 |

**背壓鏈**：`process_image 慢 → FrameQueue 滿 → push_blocking 阻塞 → 不 post_recv → Grab RNR（rnr_retry_count=7=∞）→ Grab poll_one 阻塞`

**驗證結果（所有項目通過）：**

| 驗證項目 | 結果 |
|---------|------|
| Phase 1：連續 120 幀 CRC 全對 | ✅ ok=120 err=0，1375fps/86MB/s，slot 0→3 繞回正確 |
| Phase 1：CM DISCONNECTED 乾淨退出 | ✅ `[rdma_source] 偵測到 Grab 斷線（CM DISCONNECTED）` |
| Phase 2：credit 耗盡 → Grab 阻塞（非斷線） | ✅ 9.6fps（200ms 延遲限速），ok=20 err=0，QP 未進 error state |
| Phase 2：IP 消費恢復後 Grab 自然繼續 | ✅ 全 20 幀完成，無重連 |

**本次 bug fix（驗證過程中修正）：**

1. `d2a42b0`：`rdma_nslot_test.cpp` FrameHeader include 路徑
2. `b7b13e3`：`recv_thread_fn` 退出後必須呼叫 `queue_->close()`
3. `de047a3`：RoCE v2 `WR_FLUSH_ERR` 不保證立即出現 → 新增 CM event 輪詢（`check_cm_disconnect()`）

---

## 權威 Gap 表（2026-06-17）：舊版 Reference → 現狀 → gap#

> 來源：2026-06-17 三套 Reference 逐功能考古（`Reference/PrjCfAoi`=legacy 單體、`Reference/Demo`=GPU 核心、
> `Reference/cfaoi_phase1`=取像套件）+ 對源碼核實。file:line 為實際讀到。詳見各模組「程式完整說明.md」。
>
> **編號原則**：既有有意義編號（#1/#2/#5/#6/#7/#8/#9）不重編、不換意義；幽靈號（交接僅記「已做/工具」無具體定義）
> 標「無對應」不留空槽；真正全新自 **#16** 起；完整性掃描漏網項自 **#22** 起。L-level 對齊本檔分級。

### 表一：既有編號 #1–#15（含幽靈號標註）

| # | 意義（既有定義）| 現狀 / L-level | 證據（file:line）|
|---|---|---|---|
| **#1** | 對位 pipeline（單 CCD 已做；多 CCD 預留）| 單 CCD **L3**；多 CCD 對位 **L0（預留）**| IP `align_engine`+`golden_maker`+CHECK/SET_ALIGN（取代 MIL `CamProc.cs:307-485`）；多 CCD 對位未實作 |
| **#2** | CCD 參數 UI（曝光/增益，跨 Control+Grab）| 參數控制 **L3**；光度 **L3**；Control UI **L1（待 Mac 目視）**| Grab 側全 L3（見 Grab 表）；光度加光源後 exp ratio=2.314 / gain ratio=2.267 全 PASS；Control TabControl + 相機 tab L1，待 Addis Mac 目視後才升。|
| **#3** | （交接僅記「已做」，無具體定義）| **無對應** | 考古查無對應功能；不留幽靈號，僅記錄此編號存在但定義缺失 |
| **#4** | （交接僅記「已做」，無具體定義）| **無對應** | 同 #3 |
| **#5** | 座標換算 pixel→μm（單 CCD 已做；多 CCD 預留）| 單 CCD **L3**；多 CCD 座標 **L0（預留）**| IP `GlobalPosX_um/Y_um`+`CcdIndex`（INI [Optical]→OpticalParams）；多 CCD 拼接（`CamToStageAngle/CcdPitch/CcdOverlap`, `Configuration.cs:80-88`）未實作、公式分歧待確認 |
| **#6** | 多 IP 配方編輯（per-CCD tab）| **L0/部分（未做）**| legacy `frmAoiSettingEditor.cs:358-690`（多 IP/Align/SaveAs）；Control `RecipeStore` 只處理單 IP0 |
| **#7** | 配方批次複製 | **L0（未做）**| legacy `frmCopyRecipeParamToRecipe.cs`（跨配方複製參數）；Control 只「找不到自動生成預設」，無批次複製 |
| **#8** | 互動 ROI 繪製 | **L0（未做）**| legacy `frmAlgorithmTestTools.cs:474-643`（滑鼠增刪/拖矩形）；Control `ZoneParamEditor` 只數值位移 x±/y± |
| **#9** | 2/4-way kernel | **L0（未做）**| ip `zone_config_adapter.cpp:162` `AlgorithmWay` 忽略；Demo `cuda_kernels_fast.cu` 只有 8-way（`kernelFast8Way{Texture,Comparison,Shared}` @123/239/346），無 2/4-way 變體 |
| **#10–#15** | （交接僅記「工具 6 個」，未逐項定義）| **無對應（未逐一定位）**| 考古無法逐一對位 6 個工具；不逐個留幽靈號。候選工具見表二註（`mac_ip_binder`/多通道 log 可能屬此範圍但無定義佐證）|

### 表二：新增編號 #16–#21（考古確認的真正全新缺口）

| # | 缺功能 | 舊版位置(file:line) | 現狀 / L-level | 備註 |
|---|---|---|---|---|
| **#16** | **Rule 改判**（ImageRuleEnable/MeanLowThreshold/HdivWThreshold/NgSizeThreshold）| `CamProc.cs:816-847` | **L0（完全缺）**| AI 又停用 → 現行**無任何自動 OK 改判**，全進人工複核（風險：過殺）|
| **#17** | **online / image-capture 模式**（線上收圖主迴圈）| Demo `rivermax_receiver.h` / `inline_controller.cpp` / `frame_assembler.h` | **L0**| = Step 4/5 里程碑；GB10 須改 cudaHostAlloc（不變式 11）；STATUS 已列 L0 但無 gap# |
| **#18** | **多尺度 + LSC auto-calibrate 接線**（kernel 複製進 ip 但無 caller）| Demo `cuda_kernels_fast.cu:641-770,999`；ip 同 kernel 但 `gpu_pipeline.cpp::run()`(218-306) 不呼叫 | **L0（死碼/執行路徑）**| 接線缺非演算法缺；`enable_multiscale` 帶入 ZoneConfig 但不讀；**Demo 本身亦未接**（RAG 文件 > 程式碼）|
| **#19** | **Sobel 二次檢測**（vSobel DetectReason）| `CamProc.cs:668-725` | **L0（缺）**〔已釐清〕| Control 有欄位（SobelEnable/Dark/Bright）；**ip 主檢測路徑 `gpu/` 無 Sobel**（`ai_kernels.cu:128-160` 的 sobel 是 AI 特徵抽取，非缺陷二次檢測）→ 由「待確認」確認為**缺** |
| **#20** | **多通道 log**（7→3，NetRec/NetSend/Prc/Msk/GetImg 缺）| `LogMgr.vb:11-17` | **L2（部分）**| Control `LogService` 3 通道；IP 另有 flight_recorder。**可能屬原 #10–#15「工具」之一**（無定義佐證，暫給新號）|
| **#21** | **MAC Persistent IP 綁定**（mac_ip_binder）| grab/CLAUDE.md 列 `t01_pylon_mac_setup`，但 Reference 樹下**無此原始檔** | **L0（雙缺）**| 來源懸空引用、grab 未建檔。**可能屬原 #10–#15「工具」之一**（無定義佐證，暫給新號）|

### 表三：完整性掃描 #22–#28（NEW①–⑩ 之外的漏網項）

> 規則：legacy 有、新系統零/部分覆蓋、且**既不在 #1–#15 也不在前述 NEW①–⑩**。寧多列勿漏。

| # | 漏網缺功能 | 舊版位置(file:line) | 現狀 / L-level | 備註 |
|---|---|---|---|---|
| **#22** | **MaskGen 掩碼生成** | `LibAoiSetting/frmMaskGen.cs` | **L0（缺）**| 掩碼 ROI 繪製/遮罩無對應（與 #8 互動繪製相關但功能獨立）|
| **#23** | **Interest ROI（IOI）存圖** | `CamProc.cs:1547-1614`（DetectIoiList）| **L0（缺）**| ip 輸出 `<IoiInfoList/>` 為空殼；興趣區存圖無對應 |
| **#24** | **AI 模型管理 UI**（掃 .onnx/刪除/配方關聯）| `frmAiModelManager.cs:36-72` | **L0（缺）**| AI 停用 → 管理 UI 未遷移；Control 只有 AiRootPath 設定欄 |
| **#25** | **CF_STOP**（上位機中斷取像命令）| `MainProc.cs:999-1015` | **缺**| Control `UpstreamServer` 無 CF_STOP 分支（offline 無停止對象）|
| **#26** | **BypassAlignment review**（review_offset 機制）| `CamProc.cs:1688-1812` | **L0**| ShareSetting 旗標存在但停用；review_offset 寫檔機制無對應 |
| **#27** | **file replay（檔案→RDMA 送器）** | phase1 `t40_e2e_client_file.cpp:35-168` | **缺（grab）**| grab 無檔案 RDMA 送器（ip `file_source` 是 offline-tcp 非 RDMA 回放，不等價）|
| **#28** | AutoFlash 待機閃頻 / 登入權限（frmLogin）| `LibAoiSetting/AutoFlash.cs` / `frmAoiSettingEditor.cs:1487-1577` | **缺（次要）**| 產線/操作周邊；多數場景可不補（與 won't-do 邊界）|

### 表四：二次考古補登 #29–#31（2026-06-17，用 Reference 源碼回頭驗證 #1–#28 時發現的漏網項）

> 來源：2026-06-17 二次考古——以 4 個 reader 逐項核實 #1–#28 與 ip/grab/control 源碼一致性（結論：**100% 一致**），
> 並比對 legacy `PrjCfAoi` 全功能清單,撈出**既缺、又不在 #1–#28** 的漏網項。寧多列勿漏。

| # | 漏網缺功能 | 舊版位置(file:line) | 現狀 / L-level | 備註 |
|---|---|---|---|---|
| **#29** | **DetectRoi 前後處理參數未接線**（Remap / Smooth / Blob 合併距離 / EdgePass 濾除）| 見下四子項 | **L0（缺）**| 四項皆 legacy `DetectRoi` 欄位,`zone_config_adapter.cpp:162` 註解明確「Blob* 已忽略」,Remap/Smooth 連欄位都無。⚠️ **檢測精度 caveat**：26 張跨架驗證(25/26 一致)**未暴露**其影響 → 推測測試配方未啟用,或 ground truth 亦由不含這些步驟的 Demo 產生;**是否影響生產取決於正式配方是否啟用,待真機/真實配方定論**。|
| ⤷ 子項 a | Remap 影像前處理（`M_ImagePreproc=Ip_Remap`，MIL `MimRemap`）| `ClibCf/Recipe.cs:54` / `CamProc.cs:591-598` | **L0** | gpu_pipeline 無幾何 remap 等價 |
| ⤷ 子項 b | Smooth 平滑前處理（`SmoothTimes`/`SmoothTimes2`，5×5 卷積疊加）| `ClibCf/Recipe.cs:58,61` / `CamProc.cs:619-641` | **L0** | ZoneConfig 無欄位,檢測直接進 8-Way,跳過卷積平滑 |
| ⤷ 子項 c | Blob 合併距離（`Blob{Dark,Bright,All}MergeDistance`）| `ClibCf/Recipe.cs:126-132` / `CamProc.cs:553-562` | **L0** | gpu Blob analysis 無合併邏輯 |
| ⤷ 子項 d | EdgePass 邊界濾除（`EdgePassRatio`/`EdgePassThreshold`）| `ClibCf/Recipe.cs:108,110` | **L0** | gpu 缺陷後處理無邊界通過濾除 |
| **#30** | **建立/重命名/另存配方 UI**（`frmNewRecipe`）| `PrjAoiSettingEditor/frmNewRecipe.cs:15-62` | **L0（缺）**| Control `RecipeService` 只「找不到→自動生成預設」,無新建/重命名/另存對話框,主視窗 Recipe 區僅下拉選單。（與 #7 配方批次複製相關但功能獨立）|
| **#31** | **frmViewDefect 缺陷影像移到 OK/NG 資料夾**| `PrjAoiSettingEditor/frmViewDefect.cs:213-295` | **部分缺**| Control `DefectSortView` 涵蓋 legacy `frmSortDefect`（標 TrueDefect/Particle）,但**未涵蓋 frmViewDefect 的「移動/複製影像檔到 OK/NG 資料夾」**檔案歸檔操作。|

### 里程碑（非 gap，已在 STATUS 模組表追蹤）

- **多相機匯聚（cam_manager / cam_ebus / --cam-count ALL）**：`grab` 多相機全陣列 **L0**（Step 3，待 SN2201 Switch + 相機陣列）。
  legacy `CamProc[]`(`MainProc.cs:238-258`) + phase1 `t31_ebus_grab.cpp`。依使用者裁示**併入既有多相機里程碑，不單列 gap#**。

### 刻意不搬 / won't-do（記錄，不追蹤為 gap）

- **Demo 測試/單機範式**：合成缺陷注入 `DefectGenerator`、auto-tune BTH/DTH、6 模組 TDD 框架、`spark_scheduler`（分散式由 Control 分派）、`gui_config`、Result.csv/result.bmp（已改 JSON/XML+PNG）。
- **legacy 硬體/產線周邊**：FrameGrabber 7 後端（Matrox/Dalsa/Silicon → pylon/eBUS+RDMA）、frmVariance 模糊度統計（呼叫 Python）、多工位部署 .bat。
  （註：Basler 串口「曝光/增益」功能本身 = **#2**，非 won't-do；won't-do 的是其「MIL 串口」實作機制。）
- **phase1 機況腳本**：00/10/11/20/30（保留為機況確認 Agent；10/11 L4，20/30 在 GB10 有效性存疑）。

### 本次與「建議對位」的分歧（依考古信實調整）

1. **#2（CCD 參數 UI 曝光/增益）**：上一輪暫放「won't-do」，本輪改列**正式 gap #2**——核實 grab/control 源碼 0 命中 exposure/gain，功能確為「未做 tracked gap」。
2. **#19 Sobel（原 NEW⑤「待確認」）**：核實 ip `gpu/` 主檢測路徑無 Sobel（`ai_kernels.cu` 的 sobel 僅 AI 特徵）→ 由「待確認」**確認為缺**。
3. **#20/#21（原 NEW⑧多通道log/⑨mac_ip_binder）**：建議「對映進 #10–#15 工具」；但 #10–#15 **無任何定義**可佐證該對映 → 依「不留幽靈號」改給新號 #20/#21，並註明可能即原工具清單之二（待日後若查出 #10–#15 定義再回填）。
4. **NEW① 多 CCD 拼接**：併入 **#5（座標）+ #1（對位）的多 CCD 預留**，不單列。
5. **NEW⑩ 三合一拆分**：ROI 繪製→**#8**、MaskGen→**#22**、IOI→**#23**（三者功能獨立，分列）。

6. **二次考古結論（2026-06-17）**：用 Reference `PrjCfAoi` 源碼回頭逐項驗證 #1–#28 → **與 ip/grab/control 源碼 100% 一致**（標「缺/L0」者源碼確實無對應實作,無標定錯誤）。同時比對 legacy 全功能清單,補登 **#29–#31**（DetectRoi 前後處理 Remap/Smooth/Blob合併/EdgePass / 建配方 UI / frmViewDefect 影像歸檔）;依使用者裁示 Remap+Smooth+Blob合併+EdgePass 合併為 **#29**（四子項）。
7. **UI 設定專項複查（2026-06-17）**：逐控制項對照 legacy 15 個 WinForms ↔ 新版 5 個 View（見「UI 設定專項複查」表五/表六）→ legacy 絕大多數 UI 設定新版**都有對應 UI**（DetectRoi 全參數在 ZoneParamEditor 27 列,IP 未接者標「IP待接」佔位）;真正缺的 UI 補登 **#32**（RecipeSetting BypassEdgeX/Y/OfflineLoadImageFolder/Kernal 值無 UI）、**#33**（配方 Delete/SaveAll/開資料夾無 UI）。無「有 UI 但漏記」的反向落差。

> **殘留考古缺口（誠實列出）**：
> ① **μm 契約**：legacy 缺陷一律 pixel、CF_GET_RESULT 只回 XML 路徑+缺陷數 → 無法確認舊上位機要不要 μm（#5 為片面提議，與 UpstreamServer 接真機同一條 follow-up）。
> ② **#10–#15「工具」定義**：交接無逐項清單，考古無法逐一還原；#20/#21 可能屬此但無佐證。
> ③ **#18 多尺度**：RAG_TRAINING.md 稱生效，但 Demo `batch_detector` 與 ip `run()` 皆無 launch 呼叫 → 文件 > 程式碼接線（兩邊 L0）。

---

## UI 設定專項複查（2026-06-17）：legacy WinForms 逐表單 ↔ 新版 Control 對照

> 來源：3 個 reader 逐控制項盤點 legacy 15 個 WinForms（每個 TextBox/CheckBox/ComboBox/Button）+ 新版 Control 5 個 View（MainWindow/Step1/ZoneParamEditor/DefectSort/SystemSettings），逐一對照「每個 UI 設定項是否有對應功能」。
>
> **總結論**：legacy **絕大多數 UI 設定在新版都有對應 UI**——24+ 個 `DetectRoi` 參數 → ZoneParamEditor **27 列**（含 Smooth/Sobel/EdgePass/Blob 等,IP 未消費者標「IP待接」佔位,**UI 存在**）；ShareSetting/RecipeSetting → MainWindow 兩區；曝光/增益 → SystemSettings 相機 tab；缺陷分類 → DefectSort。真正缺的 UI 集中在「少數 RecipeSetting 欄位 + 配方管理操作」,補登 #32–#33。

### 表五：legacy UI 表單覆蓋對照

| legacy 表單 | 用途 | 新版對應 | 狀態 |
|---|---|---|---|
| `frmCfAoi` | 主控視窗（CF 鈕/log/status/Recipe 預覽）| `MainWindow` | ✅ 完整（CF_STOP/Grab/Align 鈕停用,見 #25/#1）|
| `frmIpParamEditor` | 24+ DetectRoi 參數批次編輯 | `ZoneParamEditorView`（27 列+多 ROI 批次）| ✅ **UI 完整**（參數列全在,IP 未接者標「IP待接」→ 接線缺 = #29,非 UI 缺）|
| `frmIpParamFullView` | 全參數 DataGrid 檢視+批次寫 | `ZoneParamEditorView` 批次套用 | ⚠️ 功能可替代（無全欄 DataGrid,但有逐參數/多 ROI 批次）|
| `frmAoiSettingEditor` | 配方管理（New/Delete/Save/SaveAs/SaveAll/Copy/OpenFolder）| `MainWindow` Recipe 下拉+Save | ⚠️ **部分**（Save/選配方 ✅;New/SaveAs=#30、Copy=#7、**Delete/SaveAll/OpenFolder 無 UI = #33**）|
| `frmSetting`（ShareSetting）| TuningRecipe/SaveSourceImage/SaveFullImage/DebugAlgorithm/BypassAlignment/AiRootPath | `MainWindow` ShareSetting 區 | ✅ 6 欄全在（TuningRecipe/SaveFullImage/BypassAlignment 停用顯示,新流程不適用）|
| `frmSetting`（RecipeSetting）| MaxSave*/SaveDefect*/AiDefect*/Kernal*/BypassEdge*/ImageRule*/M_AiGroup/OfflineLoadImageFolder | `MainWindow` RecipeSetting 區 + `RecipeSavingModel` | ⚠️ **部分**（11 欄有;**BypassEdgeX/Y + OfflineLoadImageFolder 完全無 = #32**；KernalValue/File2/Value2 有 model 無 UI = #32；ImageRule*=#16；M_AiGroup=#24）|
| `frmNewRecipe` | 新增/重命名/另存配方 | — | ❌ 缺 = **#30** |
| `frmCopyRecipeParamToRecipe` | 配方間參數複製 | — | ❌ 缺 = **#7** |
| `frmAiModelManager` | 掃/刪 .onnx | — | ❌ 缺 = **#24** |
| `frmMaskGen` | Mask ROI 互動繪製 | — | ❌ 缺 = **#22**（互動繪製=#8）|
| `frmBaslerCom` | 曝光/增益（COM 串口）| `SystemSettingsView` 相機 tab | ✅ 完整 = **#2**（串口機制 won't-do,功能已用 Grab pylon SET/GET_CAM_PARAMS）|
| `frmViewDefect` | 缺陷影像檢視/分類/移 OK/NG | `DefectSortView`（Layer2 小圖分類）| ⚠️ 部分（分類 ✅;**移影像檔到 OK/NG = #31**;Cut 裁剪 minor）|
| `frmSortDefect` | 缺陷檔案排序歸檔 | `DefectSortView`（Layer1 LIST/SORT）| ✅ 完整 |
| `frmAlgorithmTestTools` | 離線演算法驗證/ROI 繪製/影像變換 | `Step1View` | ✅ 核心完整（MIL 專屬 Add/Del Rect/Cut/Remap/HistEq/Reload 停用 = #8/#29）|
| `frmVariance` | 模糊度統計（呼叫 Python）| — | ⏸️ won't-do（已記錄）|
| `frmLogin`（×2）| 密碼/登入等級 | — | ❌ 缺 = **#28**（次要）|

### 表六：UI 專項複查新發現 gap #32–#33

| # | 漏網缺功能 | 舊版位置(file:line) | 現狀 / L-level | 備註 |
|---|---|---|---|---|
| **#32** | **RecipeSetting 部分欄位無對應 UI/model**（BypassEdgeX/Y 邊界略過 + OfflineLoadImageFolder + KernalValue/File2/Value2）| `RecipeSetting.cs:55-68,90-91`(BypassEdge/OfflineFolder) + `:52-60`(Kernal*) | **L0/部分** | **BypassEdgeX/Y**（邊界略過距離,影響檢測有效區）`RecipeSavingModel.cs` **完全無欄位**、無 UI、IP 不消費(grep 0 命中);**OfflineLoadImageFolder** 無（Step1 直接 browse 取代,可接受）;**KernalValue/KernalFile2/KernalValue2** 有 model 無 MainWindow UI（KernalFile 僅停用顯示「MIL 前處理,IP 未接」→ 本質即 #29 Smooth 的 MIL Gaussian kernel 機制）。`M_AiGroup`/`ImageRule*` 另計 #24/#16。|
| **#33** | **配方管理操作不全**（Delete / SaveAll / 開資料夾）| `frmAoiSettingEditor.cs:154-168` | **L0（缺）** | 新版 `MainWindow` 只有配方下拉+Save;legacy 的 **刪除配方 / 全部另存 / 開配方資料夾** 無對應 UI。（新建/重命名/另存 = #30、跨配方複製 = #7,功能獨立分列。）|

> **UI 複查結論**：標「缺」的 UI 全部對得上既有/新增 gap#（無「有 UI 但漏記」的反向落差）。新版**未引入** legacy 沒有的多餘 UI 設定。
> 多數「停用顯示」（IsEnabled=False + tooltip）屬刻意保留版面 1:1、標明 MIL/新流程不適用,**非缺漏**。

---

## 下一階段：Step 3（Grab 全陣列 + Switch）→ Step 4

Step 2 已完成（2026-06-15），**N-slot RDMA（合成幀路徑）實機驗通（2026-06-17 L3）**：

**Step 3 剩餘（需相機陣列）：**
1. **SN2201 交換器到貨** + 37 台 raL8192 接線
2. `cfaoi_grab --cam-count ALL`（cam_manager 全陣列多相機，目前 L0）
3. Control↔Grab 8100 完整接線（目前 hardcode nc，待真 Control UI）

**Step 4 前置（已具備）：**
- N-slot RDMA ring buffer + 背壓驗通（L3）
- IP rdma-validate 模式可直接升級為 image-capture 模式
- 使用者有單台 Basler raL8192 相機，可做 Step 4 單相機端到端測試

**需接線啟動（L1 → L4）：**
- UpstreamServer（CF_/8787）：需 `.Start()` + 綁 On* 回呼 + 真實上位機
