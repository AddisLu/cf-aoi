# CF-AOI 系統狀態盤點 (STATUS.md)

> 本文件用 meta 不變式 #0 的 L0–L4 分級，誠實標註每個模組的真實完成度。
> 規則：**標低不標高；有疑慮時標保守級別。「寫好 ≠ 驗證過」。**
> 每一列的級別皆**逐項核實程式碼 / selftest 後**標定；與初版草稿不同者於該列加註。
> 最後更新：**2026-06-18**（① Gap #6 多 IP 配方單一入口 L2 + #8 視覺 ROI L1 完成。② 主視窗加 Grab/上位機連線燈;GigE 機器層參數 GET_CAM_NODES UI 可見 L3。③ **per-camera ROI 考古 + 設計定案 = 新 gap #34**:每台相機不同起始點 → legacy = 本地 ROI + 每台對位 Mark;已選模型 A + 底圖兩來源都支援,見表六。④ **#34 A2 完成**:per-IP 對位 Mark(M_AlignRoi) 編輯 UI 進 ZoneParamEditor + `RecipeIps` 多 CCD 宣告(修 config List 附加致 IP0 重複),`--selftest store` 驗 AlignRoi per-IP 隔離 PASS(L2,版面待 Mac 目視);Step1 ROI 框選加四角四邊把手精修+數值微調+左鍵拖曳平移。A1(底圖綁實拍)待相機。⑤ 多 CCD 三層模型(運算單元/CCD/per-CCD 配方)扶正進 docs/CLAUDE.md §2 + Phase 1 拆塊(塊1/2/3,關聯 #6/#34/#21,docs-only;容量數字守誠實分級:7.4ms 實測、37 CCD 餘裕 73% 為投影)。前次：① 二次考古 #1–#28 100% 一致,補登 #29–#31;② UI 專項複查補登 #32–#33。⑥ **2026-06-19 docs 全面對齊**：上位機 CF_ 已接線 L2/L3(端到端跑通,真上位機/μm 維 L4 不混)；補 RoiImageView/SingleCcdSetupView/ArrayTopology/UpstreamWiring 進 control 說明 §2/5/6/16；模組表補相機陣列/RoiImageView/單 CCD 工作台；清理 cruft(.pyc/測試 recipes untrack)。⑦ **2026-06-21 三模組獨立重驗**：#1–#34 一致無漏列；校正 **#9 範圍**（legacy 9 個 AlgorithmWay string，缺失 7 非-8-way 幾何模式，8-Way 已做）；**#19** 加備註（AlgorithmWay 的 EdgeDetect(51000) 邊緣模式同根因併入）；**#24** 加「SaveAiTrain→DefectSort **部分替代**」備註；新增「已知新碼缺陷」短表（F1 全幅對位 1px bug）。⑧ **2026-06-21 doable-now 收口 sprint**（Mac/Control + Linux x86 RTX2080，offline）：24 gap triage（workflow，含對抗複查）→ 收掉 **#6/#7/#16/#23/#25/#32/#33**（IP 演算法 #16/#23/#32 RTX2080 **L3**；Control 邏輯 #7/#25/#33 selftest **L2**）；**#31 covered-by-substitution**、**#22/#26/#34-A1 等 batch-later/blocked**（見表七後「收口 sprint」段）。）

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
| SystemSettings 相機陣列（運算單元帶 / 宣告陣列 / 偵測相機）| **L2(selftest)/L1(目視)** | `--selftest topology`+`camera` PASS（拓樸載入/依 compute_unit 分群/處理 N 真/負載估算公式/連線規則；LIST_CAMERAS 分群+KPI、離線不假造）。塊1/2；版面待 Mac 目視。宣告(config) 與 偵測(runtime) 分開、不假 merge（約束②）。|
| RoiImageView（影像/ROI 共用控制項，塊3-3a）| **L1** | 從 Step1View 抽出，行為一致（StyledProperty 介面，EditZone 可注入/AllZones 畫全部 ROI）；`--selftest singleccd` 驗 EditZone 連動。縮放/平移/框選/導航/量 Pitch **互動待 Mac 目視**。|
| 單 CCD 工作台（SingleCcdSetupView，塊3-3c）| **L2(selftest)/L1(目視)** | `--selftest singleccd` 5 case PASS（組合既有 Step1+ZoneEditor 實例 / LoadSlot 設 SelectedIp / header 顯 CCD 名 / EditZone=選中 ROI + AllZones=DetectRoiList）。大影像 + 右精簡欄、選 ROI 影像高亮定位 **待 Mac 目視**。對位 Mark 視覺定位 deferred（現數值卡）。|
| UpstreamServer（CF_/8787/9 參數） | **接線+回呼+模擬器 L2(selftest)/L3 ✓(2026-06-19 端到端跑通)；真上位機 L4；μm(#5) L4** | **2026-06-19 接線**：`AppServices.Build` new UpstreamServer(8787) + `MainWindowViewModel` ctor `UpstreamWiring.Bind`+`Start()`（Optional 失敗不阻塞）；回呼重用既有 IP 流程——`OnLoadRecipe`→`IpClient.LoadRecipeAsync`、`OnGetResult`→`ListDefectFoldersAsync` 組「路徑,逗號+缺陷數,逗號」(非 JSON)、`OnConnectedChanged`→`SetUpstreamConnected`(燈轉綠)。**★A 誠實失敗**：GRAB_START/CHECK_ALIGN/SET_ALIGN offline 不綁 → 回 **ERR(非假 OK)**（CHECK_ALIGN 不再回假 `OK\|0\|0`）。`--selftest upstream` 6 case PASS（READY/LOAD_RECIPE接IP/GET_RESULT路徑+數/CHECK·SET_ALIGN誠實失敗/燈轉綠）=**L2**；`scripts/upstream_simulator.py` 端到端 = **L3 ✓ 跑通（2026-06-19，模擬器 ↔ 真 Control(Mac 8787) ↔ 真 IP(8200)）**：`CF_GET_RESULT` 回真實 IP 結果夾+缺陷數（例 `OK\|IP04_Origin000001_DEFAULT,…\|0,0,0,0,1,1`，回呼 `ListDefectFoldersAsync` 端到端通）、CHECK/SET_ALIGN 回誠實失敗 ERR、上位機燈轉綠。⚠️ **真上位機協議認帳（欄位/序列/μm 是否如實機預期）= L4 做不了**；**μm 契約(#5)= IP 片面提議 = L4**（不混）。#25 CF_STOP/#26 BypassAlignment 未動。 |
| 從 IP 載入影像（遠端檔案瀏覽 + 縮小預覽 + 全解析度檢測）| **Control L2(selftest)；IP L3(2026-06-22 Spark)；端到端 L3 ✓(2026-06-22)** 〔HARD verify 通過：`scripts/verify_remote_image.py` 7/7 — REVIEW_LOCAL_IMAGE(磁碟 PGM) == SEND_IMAGE_FOR_REVIEW(上傳同 raw) 逐缺陷欄位 bit-exact(n=5)、REVIEW 兩跑 bit-exact、不存在路徑→ERR、GET_IMAGE_PREVIEW 回全解析度 8192×5000+縮圖；於 spark-c16f GB10 offline-tcp 跑通〕 | Mac 遠端時圖太大搬不動 → IP 讀自己磁碟、只回縮小預覽 PNG + 全解析度寬高（network-clean，不搬全圖）。`--selftest remoteimg` 3 case PASS（假 IP server：① RemoteImageBrowser 列舉/導航(.. 在前/雙擊目錄進入·影像回路徑) ② Step1 遠端載入：預覽 PNG 有效(ImageSharp round-trip)+`ImageWidth/Height`=全解析度8192×5000+`PixelData=null`(像素值遠端關「-」)+`IsRemoteImage` ③ Test 路由 `REVIEW_LOCAL_IMAGE`→2 缺陷疊預覽·縮圖牆遠端略過）=**L2**。IP 端 `control_server.cpp` 加 `LIST_DIR`/`GET_IMAGE_PREVIEW`(預覽 resize/灰階 **display-only 絕不進檢測**)/`REVIEW_LOCAL_IMAGE`(讀全解析度 IMREAD_UNCHANGED→**與 SEND_IMAGE_FOR_REVIEW 同一 process_image 入隊 → bit-exact**)=**L1，待 Spark/RTX Linux build**。**HARD verify 待 L3**：`REVIEW_LOCAL_IMAGE(某圖)` 缺陷結果 == `SEND_IMAGE_FOR_REVIEW(同圖上傳)`。框 ROI 於 2048-寬預覽=視覺 ±4 全解析度 px，精修走數值框（全解析度）。|
| Step 2-3 / Step 4-5 操作 UI（RDMA 監控 / 存圖瀏覽 / 生產） | **L0** | 無對應 View（現有 MainWindow/Step1/ZoneParamEditor/DefectSort/SystemSettings/SingleCcdSetup + RoiImageView 控制項）。|

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
| 行車紀錄（flight recorder：結構化診斷 JSONL/incident） | **L3**（src 欄位 **L3 ✓ 2026-06-22 Spark**） | **2026-07-12 盲區收口 v2 = L3 ✓（RTX 2080 `verify_flight_v2.py` 11/11 + `verify_flight_src.py` 回歸 9/9 + 決定性 bit-exact + bench no-op + 真圖基準 2606 不變）**：① ZoneSnap 補 SUB/融合欄位（algo_mode/multiscale/blob/pitch_times/choose_amount/lsc/remap）② LOAD_RECIPE 成功留痕（type=recipe jsonl）③ defect_flood 觸發器（GPU **過濾前**計數——真圖+錯pitch30 → 觸頂10000 → incident；BlobMinSize=5 洗到 0 顆仍觸發=不被遮蔽）④ 週期 stats（每200張 fps/gpu_ms p50/p95/queue_peak）⑤ incident 節流（同kind 30s一檔；5×bad_json→1檔+suppressed摘要）+ record_incident race 修復（深拷入鎖，fallback POD）。commit a441005/5a3350b+。⚠️ 附帶發現（follow-up）：與 pitch 同週期的合成點陣圖偵測不到（被視為正常網格）、噪點被 best-match 搜尋抵消——合成 CI 圖須用 verify_recipe_roundtrip 的「網格紋理+缺陷塊」形態，純點陣/噪點圖無效。 `diag/flight_recorder` 環形緩衝+只記出事；2026-06-15 RTX 2080 端到端驗證五種 incident kind（cuda_fatal 經人為 OOM 觸發、frame_validation/bad_json/recipe_load/uncaught_exception）+ JSON 全可解析 + 決定性不破 + bench 無 `_diag`（recorder no-op，gpu_ms 零擾動）。見 ip/CLAUDE.md 不變式 16。**2026-06-18 新增 incident `src`（出錯源碼 `檔名:行號`，repo 相對）欄位 + `FR_RECORD_INCIDENT` 巨集（`__FILE__:__LINE__` 編譯期常數，零成本、不破 bit-exact）+ `docs/html/incident-viewer.html`（log→VS Code `vscode://file` 跳轉，可攜）。`src` 欄位 **L3 ✓（2026-06-22 Spark GB10 重編 + 觸發）**：`scripts/verify_flight_src.py` 9/9 — 非破壞性觸發 bad_json/frame_validation/recipe_load 三種 incident，讀 `<output>/_diag/*.jsonl`+`incident_*.json` 確認每筆都帶 repo 相對 `src` 且行號落在實際 FR_RECORD_INCIDENT 呼叫點（bad_json→`control_server.cpp:407`、frame_validation→`:534`、recipe_load→`main.cpp:466`）；session 開機紀錄無 src 屬正常。** |
| 收圖入口 magic/version/CRC32 + 尺寸驗證 | **L3（offline-tcp）/ L1（RDMA wire）** | offline-tcp：尺寸防呆 + client 宣告 `crc32` 比對於 RTX 2080 實測拒收+記 incident（L3）。**RDMA wire header 的 magic/version/CRC 驗證分支待 `rdma_source` 實作後才生效（L1）**。見 ip/CLAUDE.md 不變式 17。 |
| 玻璃前緣/尾緣健檢（edge_check：Align Fail 警告 + 傳送片速度 drift）| **L3（Stage 1 合成 + 失敗路徑，RTX 2080）** | **2026-07-18 RTX 2080**：`edge_verify` **13/13 PASS**（前/尾緣行號誤差 0 行 @±3 容差：leading=1200/1200、tail=13800/13800、measured=12600/12600；drift 數值 **-1.190%** 精確命中；Align Fail（前緣不在第一張）/尾緣缺失/expected±tol 範圍外/低對比 Δ12<20 不誤報/亮暗方向反轉/重疊窗/INI 解析全過）。pipeline 整合煙霧（cfaoi_ip offline-file 真圖無玻璃邊）：`[EdgeCheck] ⚠ Align Fail`+`傳送異常` → **`align_fail`/`transport_anomaly` incident 落地 `_diag/`** + ResultInfo.json `edge_check` 欄位（**XML 不動**保 CF_GET_RESULT 鏈）+ GET_STATUS counters；**預設停用（INI `[EdgeCheck]` enabled=0）→ 0 條 log、JSON 無欄位 = 行為不變**（實測確認）。定位法：列平均剖面＋帶間隔差分判「存在」、兩側基準**中值跨越**定「位置」（差分平頂上 argmax 有 ±6 行雜訊歧義，合成驗證抓到已修）。掛點：offline-file/stitch/offline-tcp/rdma-process 四路徑（GPU 計時區外，bench 不掛）。**待實機**：真玻璃邊對比度→min_contrast 調參、expected_panel_lines 依產品換算、Control 警告分頁 Mac 目視 → 真面板 L3。commit `56c1d65`/`1cb5375`/`5368af9` |

---

## Grab 端（Linux / C++）

> **2026-06-15 Step 2 更新**：正式 `cfaoi_grab` 單相機路徑已實機驗通（L4）。
> 見 [Step 2 驗證報告](verification/verification_report_step2_20260615.md)。
> 多相機全陣列（--cam-count ALL）+ Switch + N-slot ring buffer 屬 Step 3，尚未實作（L0）。

| 模組 | 級別 | 驗證方式 / 缺什麼 |
| ---- | ---- | ----------------- |
| 正式 cfaoi_grab 單相機→RDMA→Spark（cam_pylon + rdma_sender + control_server + main） | **L4** | 2026-06-15 Step 2 實機：raL8192-12gm → pylon → FrameHeader(0xA01CF00D)+CRC32 → RDMA 18515 → Spark GB10 pinned memory → CRC 20/20，FAIL=0（見 [Step 2 報告](verification/verification_report_step2_20260615.md)）|
| 多相機全陣列（cam_manager / --cam-count ALL / N-slot ring buffer） | **L1** | **2026-07-18 cam_manager 落地（37 CCD 軟體觸發設計：GRAB_START=觸發，逐台平行 arm，skew 由 IP 玻璃前緣對位吸收）**：`open_all`（--cam-count N\|ALL，fail-fast 不半開）/`start_all` 逐台 arm/收滿 `frames_per_panel` 自動停（cam_pylon `set_max_frames`，= 舊 M_FRAMES_PER_TRIGGER(N) 語意）/GRAB_START params 加 `frames_per_panel`（0=連續 legacy）/**FrameHeader sliceIndex/totalSlice 填真值**（原三處硬編碼 1）/N 相機 thread 共用單 QP 以 send_mtx 序列化/cam_config.json 每台一筆。單台 legacy 路徑保留（--cam-count 1 預設 + --cam-id/--serial 語意不變）。cam_manager/control_server **語法驗證 ✓**（RTX2080 g++ -fsyntax-only）；**完整編譯待 damac**（pylon+rdma dev headers；本日 damac 離線）；**實機多台 = Step 3 待 Switch+相機到貨**。commit `28bf4fb` |
| rdma_nslot_test（合成幀送器，驗 N-slot ring + 背壓，不需相機） | **L3** | **2026-06-17 damac↔Spark 實機**：120 幀連送 CRC=OK；背壓 20 幀（IP 200ms 延遲）ok=20 err=0；commit `de047a3` |
| Control↔Grab 8100 完整接線 | **L1** | CHECK_HEALTH/LOAD_RECIPE/GRAB_START/GRAB_STOP 命令解析正確；以 nc hardcode 觸發；未接真正 Control UI |
| ⤷ Gap #2：參數控制（SET/GET_CAM_PARAMS）| **L3** | **2026-06-17 damac raL8192-12gm 實機**：Stage 0（ExposureTimeAbs 2~10000µs；GainRaw 256~2047；TLParamsLocked=0）；Stage 1（SET actual 誤差 0%：exp 200/500µs actual 完全一致；gain 256/512 actual 完全一致）；Stage 4（4 ERR 路徑全 PASS）；cam_config.json 持久化。close() 空 QP bug fix：重現 connect 127.0.0.1 route 失敗 → `rdma_destroy_qp(nullptr)` SIGSEGV；修後 `if (id && id->qp)` guard + null-clear，乾淨退出。⚠️ 假設：曝光/增益為機器層（cam_config.json），若日後隨產品調 → 補 recipe-override。|
| ⤷ Gap #2：光度效果（Stage 2+3 mean gray 單調性）| **L3** | **2026-06-17 damac 加光源後 cam_mean_gray_test 全 PASS**：Stage 2 曝光 exp=70µs mean=3.30 / exp=500µs mean=7.63 → **ratio=2.314（>1.4）PASS**；Stage 3 增益 gain=256 mean=4.65 / gain=1024 mean=10.54 → **ratio=2.267（>1.2）PASS**。證 set_params 確實驅動 sensor 積分（曝光/增益皆單調遞增）。（暗環境基線：mean≈2.5/255 noise floor，ratio≈0.95，加光源後響應正確。）|
| ⤷ Gap #2：Control UI（相機 tab + SystemSettings TabControl 改版）| **L1（待 Mac 目視）** | TabControl（連線設定/相機）；相機 tab：Grab Ellipse 指示器、ExposureUs NumericUpDown 2~10000µs + actual 回顯行、GainRaw 256~2047 + actual 回顯行、Apply（IsEnabled=IsGrabConnected）/讀取 btn + CamStatus。MVVM `[ObservableProperty]/[RelayCommand]` 對齊現有面板；0 警告 0 錯誤。**連線設定 tab = 原有內容搬入 TabItem，待 Mac 重新目視確認版面無誤**；相機 tab 互動亦待 Addis Mac 目視。**（2026-06-18 此相機 tab 已演進為「相機陣列總覽」，見下兩列。）**|
| ⤷ Gap #2+：LIST_CAMERAS 唯讀列舉（cam_pylon enumerate + control_server）| **L3（idle）** | **2026-06-18 damac raL8192-12gm 實機（idle，未 GRAB_START）**：`{"cmd":"LIST_CAMERAS"}` → 回 1 台:`mac=00:30:53:53:19:41 model=raL8192-12gm serial=25445953 ip=192.168.5.1 persistent=true device_class=BaslerGigE`（`CTlFactory::EnumerateDevices`+`CDeviceInfo`,不開相機）。⚠️ **GRABBING 中並存呼叫未驗**：本次 Spark RDMA 收端 `rdma_bind_addr: Cannot assign 192.168.3.1`（RDMA 鏈路未就緒）→ 無法讓相機真的串流 → 「列舉 vs 取像並存不掉幀」**待 RDMA 鏈路就緒後補測**；在驗證前建議 LIST_CAMERAS 僅於 idle 呼叫（程式未加守門，誠實列為 follow-up）。`ip_config` 目前回原始碼值（如 "5"），persistent bool 為權威狀態。**2026-06-22 公司現場補測（部分）**：RDMA 鏈路 IP 層帶起（`sudo ip addr add 192.168.3.1/24 dev enp1s0f0np0`，ping damac 192.168.3.2 0% loss、RoCEv2 GID 存在、port ACTIVE）；idle LIST_CAMERAS 回 1 台 ✓；GRAB_START 開相機 ✓（raL8192 SN25445953, payload 24.48MB）。**但 RDMA-CM connect 被 REJECT（`rdma_common.h:139` expected 9 got 8）— 合成 `rdma_nslot_test` 與 `cfaoi_grab` 皆然 → 非程式問題，是重開機後鏈路 RoCE 設定未完整還原（jumbo MTU：Spark 可 sudo 設 9000，**damac sudo 需密碼無法設** → 兩端 MTU 不一致）。** ∴「列舉 vs 取像並存不掉幀」仍 **未驗（deferred）**：待 damac sudo 還原雙端 jumbo MTU/RoCE 鏈路後，跑 `scripts/verify_list_during_grab.py`（已備好：GRAB_START→串流中 LIST_CAMERAS×6→GRAB_STOP，搭配 Spark rdma-validate err=0 佐證不掉幀）。|
| ⤷ Gap #2+：Control 相機陣列總覽 view（KPI/實體陣列/分群/明細）| **L2（邏輯）/ L1（版面待 Mac 目視）** | SystemSettings 相機 tab 演進為總覽（資訊架構照 `camera_overview_mockup.html`）：KPI（配置/上線/已綁定/待綁定/離線）+ 實體陣列色碼（綠/琥珀/灰）+ 分群清單 + 明細面板（重用 Gap #2 曝光/增益,改 `SelectedCamera.CamId`）。**2026-06-18 `dotnet build` 0 警告 0 錯誤 + `--selftest camera`（假 server 多台 bound+unbound）全 PASS**：列舉 2 台 / KPI 配置2上線2綁1待綁1離0 / 分群 bound[CCD00]+unbound[CCD01]+offline 空 / 預選第一台 / 欄位解析。**離線群維持 0（無 config↔CCD 映射,不假造）**。版面待 Addis Mac 目視 → L1。**defer（= Gap #21）**：綁定動作（指派 IP/位置映射,按鈕停用標 #21）/ 配置 vs 偵測映射（現配置數=偵測數）/ cam_id↔MAC 穩定映射（現以列舉 index 暫派,重啟可能對到別台,cam_config per-cam 存檔亦繼承此不穩,多台改 MAC keying）。多相機（37 台）/離線格待 SN2201 Switch + 相機陣列。|
| ⤷ Gap #2+：GigE 機器層參數補齊（PixelFormat/Auto/Trigger）| **L3** | **2026-06-18 damac 實機**：加強版 probe_cam_nodes 查 raL8192:`PixelFormat` RW(Mono8/Mono12/YUV)、`ExposureAuto/GainAuto` RW、`TriggerMode=Off`/`TriggerSelector` 有 LineStart/`TriggerSource` 有 ShaftEncoderModuleOut、`Width 8160 Height 3000(max3587)`、`GevSCPSPacketSize 9000`/`GevSCPD 0`、persistent IP 節點 RW。考古確認 legacy 只 runtime 設曝光(µs)+增益(dB)、其餘烤在 .dcf;GigE 無 .dcf → `open()` 顯式補 `PixelFormat=Mono8`+`ExposureAuto/GainAuto=Off`+`TriggerMode=Off`,開機 log 實機確認生效。**注:此相機無 `AcquisitionLineRate`,線掃時序靠 Trigger(可接 encoder)**;`Gain` 只有 GainRaw(256~2047) 無 dB。**2026-06-18 加 `GET_CAM_NODES` + Control 明細「讀取機器層參數」鈕 → 看得到(實機回 PixelFormat=Mono8/ExposureAuto=Off/GainAuto=Off/TriggerMode=Off(FrameStart/Line3)/ROI 8160×3000/PacketSize 8192/SCPD 0)。**|
| ⤷ Gap #2+：調參效果確認 mean gray（TUNE_MEAN）| **L3** | **2026-06-18 damac 實機**:`TUNE_MEAN{exp,gain}` → 開相機(免 RDMA)+set+抓 1 幀算 uint8 平均 → 回 `mean_gray`(`cam_pylon::grab_one_mean`,timeout 隨曝光×Height 自適應 3~15s,exp=2000µs 不再逾時)。機制驗通:exp 70/500/2000、gain 256/1024 皆正常回真實 mean。Control 明細加「套用並驗證(抓幀看 mean)」鈕顯示 X→Y Δ。**⚠️ 現為暗場 → mean 都 ~2.5(noise floor),變化不明顯;打光後可見(同 Gap #2 Stage2:加光源 exp70→3.30/exp500→7.63 ratio 2.31)。機制完成,「看到變化」需打光,非程式問題。** |
| ⤷ Gap #2+：encoder 行觸發 / GevSCPD / ROI Height / persistent IP 綁定 | **L0（defer 陣列時）** | 現況 1 台、已綁定(persistent 192.168.5.1)、free-run 正常 → 依使用者裁示 **3/4 階段待陣列**:encoder 行觸發(TriggerSelector=LineStart+Source=Encoder)待接產線 encoder;GevSCPD/ROI Height 待多相機;persistent IP 綁定動作(寫 GevPersistentIP,節點已確認 RW)= Gap #21,待 Switch+陣列(風險:設錯失聯,需 ForceIP 救援,故帶安全網)。 |
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
| 配方 round-trip（Mac 改→IP 套用） | **L3 ✓（2026-06-22 Spark）** | `scripts/verify_recipe_roundtrip.py` 4/4 — 同一合成圖只改 LOAD_RECIPE 的 recipe_xml DarkThreshold：0.6→偵測 N=5、0.2→暗缺陷被濾 N=3（改值生效）、改回 0.6→N=5（可逆 round-trip，非單向飄移）、同配方兩跑 bit-exact。於 spark-c16f offline-tcp。**(原 L2 缺乾淨可復現單一佐證，本次補上)** |
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
| **#6** | 多 IP 配方編輯（per-CCD 單一入口）| **L2（單機驗）**| **2026-06-18 完成**：考古確認 legacy 多 IP（一份配方 = PC/Recipe/IP1~IP4，`frmIpParamEditor.cs:493-588` 分頁+GroupBox、`frmAoiSettingEditor.cs:938-969` per-IP PropertyGrid）。新版做成**單一入口**:`RecipeStore` IP-aware（`IpNames`/`SelectedIp`/`Select(recipe,ip)` 載 `{recipe}/{ip}/RecipeInfo.xml`，切 IP 重載；`Save` per-IP），ZoneParamEditor 加 IP/CCD 下拉。可擴充:預設單台 IP0,`appsettings RecipeIps` 加 IP1/IP2… 即多台（預留 GPU 給外圍 AI 區運算的分散式設計）。`--selftest store` 驗:IP0.PitchX=11/IP1.PitchX=22 per-IP 隔離 + 切回重載 PASS。版面待 Mac 目視 → L2/L3。|
| **#7** | 配方批次複製 | **L2（2026-06-21 selftest；UI 鈕待 Mac 目視）**| `RecipeService.CopyRecipeParams/CopyRecipeParamsToMany` + `RecipeStore.CopyParamsToMany`（跨配方複製偵測參數+對位 Mark）；`--selftest recipemgmt` PASS（SRC→DST1/DST2 PitchX=77 隔離）。MainWindow 批次目標選取 UI = 視覺殘留待 Mac。commit `13c0c95` |
| **#8** | 互動 ROI 繪製（影像上框選）| **L1（待 Mac 目視）**| **2026-06-18 完成**：考古確認 legacy 影像框選在 `frmAlgorithmTestTools.cs:474-531`(addRect/拖矩形),但有 bug(DrawGroupList 沒寫回 DetectRoiList)。新版做進**有影像的 Step1View**:「框 ROI」鈕 → 影像上拖矩形(重用既有 matrix 座標轉換)→ 寫回 `RecipeStore.PrimaryZone` StartX/Y/EndX/Y(夾邊界、單一資料來源,ZoneParamEditor 數值即時同步)+ 畫現有 ROI 藍框/拖曳黃框;「ROI 全幅(-1)」清除。修了 legacy 沒接的「畫→寫回配方」。build 0 警告;互動待 Addis Mac 目視 → L1。|
| **#9** | 非-8-way AlgorithmWay 幾何偵測模式（2-Way-UD/RL · 4-Way-Arrow · 2D-Diamond · 2D-Rect · 2D-Polygon-Avg/Way，共 7 模式）| **L0（未做）**| legacy 9 模式 string 派發於 `CudaCore/CoreProcessor.cpp:100-182`（2-Way-UD 1000 / 2-Way-RL 2000 / 4-Way-Arrow 10000 / 8-Way-Star 20000 / 2D-Diamond 30000 / 2D-Rect 31000 / 2D-Polygon-Avg 32000 / 2D-Polygon-Way 32100(SUB-only) / EdgeDetect 51000）；欄位 `Recipe.cs:86`。新版僅 8-Way-Star（≡ `ip/src/gpu/cuda_kernels.cu:123/239/346`，dispatch 1114-1144），AlgorithmWay 整體忽略 `zone_config_adapter.cpp:162`。缺失 7 非-8-way 幾何模式；**EdgeDetect(51000)=邊緣/Sobel 併入 #19**（見 #19 備註） |
| **#10–#15** | （交接僅記「工具 6 個」，未逐項定義）| **無對應（未逐一定位）**| 考古無法逐一對位 6 個工具；不逐個留幽靈號。候選工具見表二註（`mac_ip_binder`/多通道 log 可能屬此範圍但無定義佐證）|

### 表二：新增編號 #16–#21（考古確認的真正全新缺口）

| # | 缺功能 | 舊版位置(file:line) | 現狀 / L-level | 備註 |
|---|---|---|---|---|
| **#16** | **Rule 改判**（ImageRuleEnable/MeanLowThreshold/HdivWThreshold/NgSizeThreshold）| `CamProc.cs:816-847` | **IP 演算法 L3（2026-06-21 RTX2080）；Control 送出端 L3 ✓（2026-06-22）**| `ip/src/defect_rules.h`（CUDA-free 後處理）：MeanLow(patch均值<→OK)/HdivW(H/W>→OK)/NgSize(size>→強制NG)；`recipe_saving` 新欄位由 LOAD_RECIPE 傳入。驗：`rules_verify` 6/6（各分支+passthrough）+ `verify_rules_edge.py` e2e 同路徑。預設停用→不破 bit-exact。✅ **2026-06-22：Control→IP `recipe_saving` 送出端已建構** — `RecipeSavingModel` 補 ImageRuleEnable/MeanLow/HdivW/NgSize + `BuildRecipeSavingJson()`，3 處 LoadRecipe 站送出，`--selftest recipesaving` unit+e2e PASS（見 doable-now 後「公司現場 sprint」段）。剩操作員編輯 Rule 欄位的 UI 輸入框 = Mac 目視。commit `d9fdcf0` |
| **#17** | **online / image-capture 模式**（線上收圖主迴圈）| Demo `rivermax_receiver.h` / `inline_controller.cpp` / `frame_assembler.h` | **L0**| = Step 4/5 里程碑；GB10 須改 cudaHostAlloc（不變式 11）；STATUS 已列 L0 但無 gap# |
| **#18** | **多尺度 + LSC auto-calibrate 接線**（kernel 複製進 ip 但無 caller）| Demo `cuda_kernels_fast.cu:641-770,999`；ip 同 kernel 但 `gpu_pipeline.cpp::run()`(218-306) 不呼叫 | **L0（死碼/執行路徑）**| 接線缺非演算法缺；`enable_multiscale` 帶入 ZoneConfig 但不讀；**Demo 本身亦未接**（RAG 文件 > 程式碼）|
| **#19** | **Sobel／邊緣二次檢測**（vSobel DetectReason；含 AlgorithmWay 的 EdgeDetect 模式）| `CamProc.cs:668-725` | **L0（缺）**〔已釐清〕| Control 有欄位（SobelEnable/Dark/Bright）；**ip 主檢測路徑 `gpu/` 無 Sobel**（`ai_kernels.cu:128-160` 的 sobel 是 AI 特徵抽取，非缺陷二次檢測）→ 由「待確認」確認為**缺**。**2026-06-21 補**：legacy 邊緣/Sobel 有兩入口——① 二次 pass `SobelDetectEnable`（`CamProc.cs:668`→`DetectReason="vSobel"` `:720`，Sobel*Threshold `Recipe.cs:79-82`）② AlgorithmWay 模式 `EdgeDetect`→51000（`CudaCore/CoreProcessor.cpp:180-182` → kernel `Algo_isEdge_8bits`(`CUDA_Kernel.cu`)/`Check_isEdge_8bits`(`CUDA_KernelFunction.cu:3`)）。兩者入口不同但同屬邊緣/Sobel、新 ip `gpu/` 全無 → **同根因併入 #19，不另開號**（#9 幾何模式不含 EdgeDetect，避免重複計數） |
| **#20** | **多通道 log**（7→3，NetRec/NetSend/Prc/Msk/GetImg 缺）| `LogMgr.vb:11-17` | **L2（部分）**| Control `LogService` 3 通道；IP 另有 flight_recorder。**可能屬原 #10–#15「工具」之一**（無定義佐證，暫給新號）|
| **#21** | **MAC Persistent IP 綁定**（mac_ip_binder）| grab/CLAUDE.md 列 `t01_pylon_mac_setup`，但 Reference 樹下**無此原始檔** | **L0（雙缺）**| 來源懸空引用、grab 未建檔。**可能屬原 #10–#15「工具」之一**（無定義佐證，暫給新號）|

### 表三：完整性掃描 #22–#28（NEW①–⑩ 之外的漏網項）

> 規則：legacy 有、新系統零/部分覆蓋、且**既不在 #1–#15 也不在前述 NEW①–⑩**。寧多列勿漏。

| # | 漏網缺功能 | 舊版位置(file:line) | 現狀 / L-level | 備註 |
|---|---|---|---|---|
| **#22** | **MaskGen 掩碼生成** | `LibAoiSetting/frmMaskGen.cs` | **L0（缺）**| 掩碼 ROI 繪製/遮罩無對應（與 #8 互動繪製相關但功能獨立）|
| **#23** | **Interest ROI（IOI）存圖** | `CamProc.cs:1547-1614`（DetectIoiList）| **L3（2026-06-21 RTX2080）**| `ZoneConfigAdapter::parse_ioi_list` 解析 `<DetectIoiList>`；`InspectionResult.ioi_list` 經 LOAD_RECIPE(offline-tcp)/recipe 檔(offline-file)；`result_saver` IoiInfoList 由空殼→填矩形+中心全域座標(XML+JSON)+裁切存 `Ioi_<IpName>_<idx>_X_Y.png`。驗：`verify_rules_edge.py` Stage D = IoiInfoList 2 筆+中心(1100,1100)+2 張 Ioi PNG 落地。commit `bdc1795` |
| **#24** | **AI 模型管理 UI**（掃 .onnx/刪除/配方關聯）| `frmAiModelManager.cs:36-72` | **L0（缺）**| AI 停用 → 管理 UI 未遷移；Control 只有 AiRootPath 設定欄。**2026-06-21 補**：訓練資料匯出（legacy SaveAiTrain `RecipeSetting.cs:44-45`；資料夾 `AiTrain/{IpName}/` `MainProc.cs:444`；gate `if(SaveAiTrain)` `CamProc.cs:952/964/975` → export `MbufExport(aiDefectImagePath,…)` `:953/965/976`，路徑 `:923`）在新版為 **部分替代(partial)**：DefectSort `classification.json`+TrueDefect/Particle（`control_server.cpp:800-837`）係人工事後標，非 legacy 之線上自動匯出（且新版 AI 停用+Rule 改判=#16 L0，自動來源不存在）|
| **#25** | **CF_STOP**（上位機中斷取像命令）| `MainProc.cs:999-1015` | **L2（2026-06-21 selftest）；真取像停止 L4**| `UpstreamServer` 加 `CF_STOP` const + `OnStop` callback + case；offline 無取像對象 → 誠實失敗 ERR（決策 A，不假 OK）。`--selftest upstream` 加斷言 PASS。真實「中斷取像」需 Grab/相機 = L4。commit `13c0c95` |
| **#26** | **BypassAlignment review**（review_offset 機制）| `CamProc.cs:1688-1812` | **L0（batch-later，2026-06-21 triage）**| ShareSetting 旗標存在但停用；review_offset 寫檔機制無對應。**triage 判定 batch-later**：與 MIL 時代「對位 bypass + 人工 review offset」工作流綁定，新 offline 架構無清楚對應/消費者、spec 模糊、價值低 → 不在本次 doable-now 收口，待真上位機/Step4 釐清需求。|
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
| **#31** | **frmViewDefect 缺陷影像移到 OK/NG 資料夾**| `PrjAoiSettingEditor/frmViewDefect.cs:213-295` | **covered-by-substitution（2026-06-21 triage）**| Control `DefectSortView` 第二層分類（`SAVE_DEFECT_CLASSIFICATION` → IP `control_server.cpp:807-843` 複製小圖到 `{folder}/TrueDefect\|Particle/` + `classification.json`）**即** legacy「把缺陷影像移到分類資料夾」的同類操作——僅軸名不同（Particle≈OK/誤判、TrueDefect≈NG/真缺陷）。→ 視為以替代方案涵蓋，不另實作 OK/NG 命名軸。|

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

### 表七：三模組獨立重驗（2026-06-21）

> 3 reader 重盤 legacy 全功能 ↔ control/ip/grab；**#1–#34 一致，無「legacy 有、新版零覆蓋、且不在既有號」的全新缺口**。
> 校正（不新增號）：
> 1. **#9** legacy 實為 9 個 AlgorithmWay string 派發（`CudaCore/CoreProcessor.cpp:100-182`），新版僅 8-Way-Star，缺失 7 非-8-way 幾何模式；其中 EdgeDetect(51000) 屬邊緣/Sobel，**經查與 #19 vSobel 入口機制不同（zone-mode vs 二次 pass）、無法證明單一同源 code path，但同根因（新 ip `gpu/` 無邊緣/Sobel）→ 併入 #19**（#9 不含 EdgeDetect，避免重複）。
> 2. **#24** SaveAiTrain（`RecipeSetting.cs:44-45` / `CamProc.cs:952-976`）→ DefectSort 為「**部分替代**」。
> F1–F4（對位新碼複查）= 新碼缺陷/契約，**不進 gap#**，列下方「已知新碼缺陷」短表。

## 已知新碼缺陷／待修（非缺功能 gap；新碼自身問題）

> 來源：`docs/roi_pattern_matching_座標轉換複查.html`（2026-06-21 對位座標轉換複查；已納版控）。**與上方「缺功能 gap#」表分離追蹤。**
> **2026-06-21 收口 sprint（commit `b21e15d`，RTX 2080 SUPER `addis-b850m-ds3h` 實機驗）**：F1 修+驗 → **fixed/L3**；F4 done；F2/F3 文件契約 L1（F3 守門經實證）。

| 編號 | 問題 | 位置(file:line) | 狀態 | 修法 / 驗證數據 |
|---|---|---|---|---|
| **F1** | 全幅 zone 套對位被毀成 ~1px（`roi_start_x=-1` + shift>0 → `-1+7=6(≥0)` → `is_full_frame()` 翻 false → `zone_rect` 縮成 1px）| `ip/src/main.cpp:462`；`config/zone_config_adapter.h:90`（`apply_align_shift`）| **fixed / L3** | SET_ALIGN 套位移抽成共用 `apply_align_shift()`，全幅 zone 先 `if (z.is_full_frame()) continue;`（main 與 align_verify 共用）。**RTX 2080 驗（2026-06-21）**：align_verify **18/18**（Stage3C ★全幅套對位後 `aligned_start_x=-1 eff_start_x=-1 is_full=1`）；verify_alignment.py **12/12**（Stage2D 全幅 `n_base=37 → SET_ALIGN(7,3) → n_after=37`，未塌；同圖兩跑 37==37；既有 8/8 不回歸，Stage2C `n0=7=n_aligned`）|
| F2 | 中心裁切契約（窗中心=Refer、近邊補零不 clamp）；生產裁切端未實作 | `ip/src/align_engine.h`（run_align doc 不變式）| **L1(doc)** | 不變式已寫進 align_engine.h（窗中心=Refer、補零不 clamp）；實際從實拍幀中心裁切的生產 wiring 待 Step4 |
| F3 | 旋轉只回報不套用、範圍僅 ±3° | `ip/src/align_engine.h`（限制說明）；`align_engine.cpp:45-148` | **L1(doc) + 守門實證** | 限制已文件化（model A 純平移、±3°）。**score_threshold 擋大角度經實證**：align_verify Stage3A 15° → `ok=false score=0.279 < thr=0.55` → 回 ERR（不回錯誤 shift）|
| F4 | `parabolic_fit_1d` 死碼（從未呼叫）| `ip/src/align_engine.cpp:23`（已刪）| **done** | 刪除（0 caller，真 fit 內聯於 run_align）；align_verify 重編 18/18 無破 |

---

## doable-now 收口 sprint（2026-06-21）：Mac/Control + Linux x86 RTX2080（offline）能做到的都收掉

> 方法：24 個 gap 經 workflow triage（每 gap 一個分析 agent + 對抗複查 agent）判定「現有兩機（Mac Control + 家用 Linux x86 RTX2080，**無相機/Grab/Spark/RDMA/陣列**）可否實作+驗證」。
> 驗證機：`addis-b850m-ds3h`（RTX 2080 SUPER，CUDA 12.6 / OpenCV 4.6）。Control 邏輯走 `dotnet --selftest`（headless）；IP 走 offline-tcp 合成影像 + 單元測。**UI 版面 L3 仍需 Mac 目視**（headless 測不到）。

**✅ 已收掉（7）：**

| # | 功能 | 級別 | 驗證 | commit |
|---|---|---|---|---|
| #6 | 多 IP 配方單一入口 | L2 | `--selftest store` 6/6（功能完整，餘 Mac 目視）| 既有 |
| #7 | 配方批次複製 | L2 | `--selftest recipemgmt`（SRC→DST1/DST2）| 13c0c95 |
| #25 | CF_STOP | L2 | `--selftest upstream`（offline 誠實失敗 ERR）| 13c0c95 |
| #33 | 配方管理 Delete/SaveAll/開資料夾 | L2 | `--selftest recipemgmt`（全 PASS）| 13c0c95 |
| **#16** | **Rule 改判**（Mean/HdivW/NgSize）| **L3** | `rules_verify` 6/6 + `verify_rules_edge.py` | d9fdcf0 |
| **#23** | **IOI 興趣區存圖** | **L3** | `verify_rules_edge.py` Stage D（IoiInfoList 2 筆+2 PNG）| bdc1795 |
| **#32** | **BypassEdgeX/Y**（IP 演算法）| **L3** | `verify_rules_edge.py`（bypass=100 → 5→3）| d9fdcf0 |

**⏸ batch-later / covered / blocked（triage 結論）：**
- **covered-by-substitution**：#31（DefectSort TrueDefect/Particle = legacy 移 OK/NG 同類，見 #31）。
- **blocked（對抗複查推翻 doable）**：**#22 MaskGen**（legacy mask = pattern-match don't-care，非偵測濾除；且與無遮罩 `matchTemplate` 對位引擎衝突，spec 未定）、**#34-A1**（per-CCD 來源 TIFF 夾/格式不存在，塌回需相機的 live-capture 半邊）。
- **batch-later（doable 但大/需 spec/需相機）**：#1·#5 多 CCD（單進程多工 vs N 進程待決）、#8（UI 視覺；數學可 L2）、#9（僅 ~5/7 模式可 DIV；無 golden 對 legacy bit-exact）、#17（online；.bin↔TIFF 不變式衝突+RDMA）、#18（未接 kernel 無 golden）、#19 Sobel（需 3 新 kernel + SUB/DIV 決策）、#20 log（Dispatcher 硬化）、#21·#24·#26·#29·#30·#2。

**✅ follow-up（2026-06-22 公司現場 已收掉）：Control→IP `recipe_saving` 送出端已建構並 e2e 驗通。** `RecipeSavingModel` 補 #32 BypassEdgeX/Y + #16 ImageRuleEnable/MeanLowThreshold/HdivWThreshold/NgSizeThreshold + `BuildRecipeSavingJson()`（鍵名逐字對齊 IP `control_server.cpp` 解析端 10 鍵）；送出端 3 處接線：`MainWindowViewModel.CfLoadRecipe`(live Store)、`UpstreamWiring.OnLoadRecipe`(svc.RecipeStore)、`OfflineReviewService` 3 個 LoadRecipe 站(per-recipe `LoadRecipeSetting`)。`--selftest recipesaving` JSON 契約 unit 11/11 + **e2e（真 Control IpClient.LoadRecipeAsync(recipeSaving:..)→真 Spark IP，經 SSH tunnel）：N(bypass=0)=5 → N(bypass_edge_x=100)=3，近邊界 2 顆被丟 → PASS**。剩「操作員在 RecipeSetting 面板編輯 BypassEdge/Rule 欄位」的 UI 輸入框 = Mac 目視（見下）。

---

## 公司現場 sprint（2026-06-22）：Mac(Control) + Spark(IP) + 截取中心(1 相機) 能驗的都升 L3

> 環境：公司 Mac（Control，dotnet 10）+ spark-c16f（GB10/sm_121，IP）+ damac（截取中心，raL8192 ×1，pylon）。
> 目標：除「Switch + 37 CCD 到料後才能測」的全陣列項，其餘 gap 都推到 L3。**演算法 L0 缺口（#9/#19/#22/#29/#30/#24）依使用者裁示維持 deferred（需新 code+spec，非驗證受阻；有相機亦不解）。**
> 三機皆 clone 同一 repo @ 本 commit；IP 驗證腳本走 spark offline-tcp 合成影像，Control 走 `dotnet --selftest`。

**✅ 已升 L3（4 項，皆貼數據）：**

| 項 | 模組 | L | 驗證（script / 數據）|
|---|---|---|---|
| A | 從 IP 載入影像（IP 端 + e2e）| **L1/待驗 → L3** | `verify_remote_image.py` 7/7：REVIEW_LOCAL_IMAGE == SEND_IMAGE_FOR_REVIEW 逐缺陷 bit-exact(n=5) + 決定性 + ERR 路徑 + 預覽全解析度 8192×5000 |
| B | 行車紀錄 `src` 欄位 | **L1 → L3** | `verify_flight_src.py` 9/9：bad_json/frame_validation/recipe_load 三 incident，`src`=repo 相對 file:line 正確 |
| C | 配方 round-trip（Mac 改→IP 套用）| **L2 → L3** | `verify_recipe_roundtrip.py` 4/4：DarkThreshold 0.6→0.2→0.6，N=5→3→5 可逆 + 決定性 |
| D | #16/#32 recipe_saving 送出端 | **follow-up → L3** | `--selftest recipesaving` unit 11/11(JSON 契約) + e2e（真 Control IpClient→真 Spark IP）N(bypass=0)=5→N(=100)=3 |

驗證腳本納版控：`scripts/verify_remote_image.py` / `verify_recipe_roundtrip.py` / `verify_flight_src.py` / `verify_list_during_grab.py`。
Control 改動：`RecipeSavingModel`(+6 欄位+BuildRecipeSavingJson) / `MainWindowViewModel`·`UpstreamWiring`·`OfflineReviewService`(送 recipe_saving) / `SelfTest`(recipesaving case)。dotnet build 0 警告 0 錯誤。

**⚠️ 部分完成 / 受阻（誠實，未升 L3）：**
- **G — LIST_CAMERAS-during-grab（#2+ follow-up）**：RDMA 鏈路 IP 層帶起 ✓（Spark 192.168.3.1 ↔ damac 192.168.3.2 ping 0%、RoCEv2 GID 存在、port ACTIVE）、idle LIST_CAMERAS 回 1 台 ✓、相機開得起 ✓；但 **RDMA-CM connect 被 REJECT（合成 nslot_test 與 cfaoi_grab 皆然）** → 重開機後鏈路 RoCE 設定未完整還原（jumbo MTU 等），**Spark 可 sudo、damac sudo 需密碼無法設** → 兩端 MTU 無法一致。∴ 串流中列舉不掉幀 **未驗（deferred，待 damac sudo 還原雙端 jumbo MTU 後跑 `verify_list_during_grab.py`）**。

**⏭ 需 Mac 目視（使用者眼睛，無法 headless 驗）：** MainWindow / 影像 Viewer 互動 / 缺陷 overlay / ZoneParamEditor / DefectSort UI / SystemSettings 相機陣列 / RoiImageView / SingleCcdSetup / #6·#7·#8·#25·#33·#34-A2 鈕卡 / Grab 相機 tab / 塊1·2·3 / 連線心跳 green↔red / RecipeSetting 面板 BypassEdge·Rule 輸入框。→ 逐頁目視確認後個別升 L3（見各列「待 Mac 目視」備註）。

**🚫 維持 deferred（使用者裁示，只記錄）：** 演算法 L0 缺口 #9（7 非-8-way 幾何）/#19(Sobel)/#22(MaskGen)/#29(Remap·Smooth·Blob合併·EdgePass)/#30(建配方UI)/#24(AI模型管理)/#26(BypassAlignment review) + #17(image-capture/online mode，需新 mode+RDMA) + 多相機全陣列(Step 3，待 Switch+37CCD)。皆「需新 code+spec」非「驗證受阻」。

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
| **#32** | **RecipeSetting 部分欄位無對應 UI/model**（BypassEdgeX/Y 邊界略過 + OfflineLoadImageFolder + KernalValue/File2/Value2）| `RecipeSetting.cs:55-68,90-91`(BypassEdge/OfflineFolder) + `:52-60`(Kernal*) | **BypassEdge：IP 演算法 L3（2026-06-21 RTX2080）；Control 欄位+送出 L3 ✓（2026-06-22）。其餘維持。** | **BypassEdgeX/Y**：IP 已消費——`recipe_saving.bypass_edge_x/y` → `defect_rules`（全域中心落影像邊緣內→丟，對齊 legacy），`verify_rules_edge.py` e2e bypass=100 → 5→3 驗通（commit `d9fdcf0`）。✅ **2026-06-22：Control `RecipeSavingModel` 已補 BypassEdgeX/Y 欄位 + `BuildRecipeSavingJson()` 送出**，e2e（真 Control IpClient→真 Spark IP）N(bypass=0)=5→N(bypass_edge_x=100)=3 PASS（見「公司現場 sprint」段）。剩 RecipeSetting 面板 BypassEdge 輸入框 = Mac 目視。**OfflineLoadImageFolder** 無（Step1 browse 取代,可接受）；**KernalValue/File2/Value2** 有 model 無 UI（= #29 Smooth MIL kernel 機制）。`M_AiGroup`/`ImageRule*` 另計 #24/#16。|
| **#33** | **配方管理操作不全**（Delete / SaveAll / 開資料夾）| `frmAoiSettingEditor.cs:154-168` | **L2（2026-06-21 selftest；UI 鈕待 Mac 目視）** | `RecipeService.DeleteRecipe/RecipeFolder` + `RecipeStore.SaveToAllIps`(存所有 IP 分區)/`DeleteRecipe`(刪→退 DEFAULT)/`RecipeFolder`(開資料夾路徑)。`--selftest recipemgmt` PASS（SaveAll→IP0+IP1、Delete→資料夾消失+清單移除、刪目前→退 DEFAULT）。MainWindow 按鈕（含 Process.Start 開資料夾）= 視覺殘留待 Mac。（新建/重命名/另存 = #30 batch-later、跨配方複製 = #7。）commit `13c0c95` |
| **#34** | **per-camera ROI 底圖 + per-IP 對位 Mark 編輯**（每台相機不同起始點 → 各自 ROI）| `ClibCf/Recipe.cs:36-49`(本地 ROI)、`MainProc.cs:441`(per-IP 配方)、`CamProc.cs:386-485`+`Recipe.cs:143-151`(per-camera 對位 `AlignedStartX=StartX+ShiftX`)、`LibAoiSetting/Configuration.cs:29-95`(CcdPitch/Overlap=全域拼接,與 ROI 無關) | **A2 L2(selftest)/L1(待Mac目視);A1·A3 L0** | **考古結論**:legacy = **每台相機本地像素 ROI + 每台自己的對位 Mark(M_AlignRoi)吸收起始點差異**;ROI 不用全域、不靠 CcdPitch 換算。**已選模型 A(legacy 原汁)**+**底圖兩來源都支援(接相機即時抓幀 + Step4 存的該台 TIFF)**。骨架已具(#6 per-IP 配方 + #1 per-IP 對位)。三塊:**A2 ✓ 2026-06-18 完成**——per-IP M_AlignRoi 編輯「對位 Mark」card 進 ZoneParamEditor(綁 `Store.Recipe.AlignRoi`:AlignEnable/ReferX/Y/SearchW/H/PatternPath+樣板檔選擇),切 IP 各自編輯、儲存寫回 `{recipe}/{IP}/RecipeInfo.xml`;`appsettings RecipeIps` 宣告多 CCD(修正 .NET config 對 `List<T>` 附加致 IP0 重複:`SystemConfigModel.RecipeIps` 預設改空、由 RecipeStore fallback);`--selftest store` 驗 per-IP AlignRoi 隔離+存回+切回重載 PASS、磁碟 MULTIIP_TEST/IP0·IP1 `<M_AlignRoi>` 內容確認分歧;**版面待 Mac 目視→L1**。**A1(核心,待相機)** #8 視覺框 ROI 底圖綁選中 IP/CCD 實拍幀仍 **L0**(現為載入 TIFF)。**A3(可選,回報用)** per-CCD 全域偏移=#5 多 CCD,與 ROI 解耦可延後。模型 B(全域定義自動分配)legacy 沒做,暫不採。|

> **UI 複查結論**：標「缺」的 UI 全部對得上既有/新增 gap#（無「有 UI 但漏記」的反向落差）。新版**未引入** legacy 沒有的多餘 UI 設定。
> 多數「停用顯示」（IsEnabled=False + tooltip）屬刻意保留版面 1:1、標明 MIL/新流程不適用,**非缺漏**。

---

## 多 CCD 陣列 UI — Phase 1 拆塊（塊1/2/3；關聯 #6 / #34 / #21，不重編既有號）

> 三層模型見 docs/CLAUDE.md §2「多 CCD 陣列三層模型」（運算單元 / CCD / per-CCD 配方）。
> Phase 1 **在家可驗**（無相機，底圖先用載入 TIFF）；綁定動作 / A1 實拍底圖 / 離線偵測 = **Phase 2（待相機/Switch）**。
> 約束①(不做 IP0→CCD 改名)、約束②(拓樸宣告≠live 綁定) 全程適用。

| 塊 | 內容 | 關聯/依賴 | L（目標） | 在家可驗 |
|---|---|---|---|---|
| **塊1** | ArrayTopology 資料模型 + `array_topology.json` 載入 + 陣列 render（**宣告狀態**；運算單元帶骨架） | 新；約束②「宣告≠綁定」；per-CCD 分區沿用 **#6** 的 per-IP RecipeStore | **L2 ✓(2026-06-19 selftest)** / L1(待 Mac 目視) | ✅ |
| **塊2** | 運算單元帶細節（每台 Spark 卡：連線燈 / 處理 N 顆 CCD / 負載%） | 關聯 `ActiveIpNode`；**負載% 標「估算」**（家裡 1 台量不到 37 CCD 吞吐，非實測；與 CLAUDE.md §2 容量「投影」一致） | **L2 ✓(2026-06-19 selftest) / L1(待目視)** | ✅（數字=估算） |
| **塊3** | 單 CCD 設定整合頁（大影像 + 右精簡欄：ROI 清單 / 27 參數 / 對位 Mark） | **抽共用 `RoiImageView`(3a)** + 重排為影像為主工作台(3c)；複用 `Step1ViewModel`(影像) + `ZoneParamEditorViewModel`(ROI/參數) VM | **3a+3c L2 ✓(2026-06-19 selftest) / L1(待目視)** | ✅（底圖載入 TIFF） |

**`array_topology.json` schema（機台層；版控模板 `array_topology.example.json`，本機值不版控，比照 `cam_config.json`）**

- `ccd_total_count` (int)：陣列 CCD 總數（37）。
- `compute_units[]`：`id`(顯示名 "Spark1") + `node`(對映 `appsettings.Nodes` 鍵 "IpOffline"… = 連線目標) + `role`("aoi"/未來 "ai")。
- `slots[]`（37 槽）：`ccd_id`(CCD 概念/UI 名 "CCD00"…) + `compute_unit`(指向某 `compute_units[].id`) + `expected_mac`(string|null，**可 null=TBD**，實際 MAC↔CCD 綁定 = #21/Phase 2) + `recipe_partition`(**現行儲存鍵=IpName** "IP0"… → `{recipe}/{recipe_partition}/RecipeInfo.xml`，約束①與 ccd_id 解耦並存)。
- 只**宣告結構**（拓樸）；MAC 多 TBD；列舉相機↔槽的 live 綁定 = #21（約束②，勿 merge）。

```json
{
  "ccd_total_count": 37,
  "compute_units": [
    { "id": "Spark1", "node": "IpOffline", "role": "aoi" }
  ],
  "slots": [
    { "ccd_id": "CCD00", "compute_unit": "Spark1", "expected_mac": null, "recipe_partition": "IP0" },
    { "ccd_id": "CCD01", "compute_unit": "Spark1", "expected_mac": null, "recipe_partition": "IP1" },
    { "ccd_id": "CCD36", "compute_unit": "Spark1", "expected_mac": "00:30:53:2A:0B:24", "recipe_partition": "IP36" }
  ]
}
```

**關聯註腳（誠實，不糊）：**
- **#34**：A2 **數值** M_AlignRoi 卡已完成（`control/src/Views/ZoneParamEditorView.axaml:81-100`，L2/L1）→ 塊3 **複用**；塊3 補 A2 的「影像上視覺定位對位 Mark」(現缺) + A1 底圖綁實拍(=Phase 2)。
- **#21**：`array_topology.json` 宣告 `expected_mac` 槽位 = #21 的資料前置；實際 MAC↔CCD **live 綁定動作仍 = #21/Phase 2**（約束②）。
- **#6**：per-CCD 配方分區沿用 #6 已建 per-IP RecipeStore（`RecipeStore.cs:43-74` IpNames/SelectedIp、`{recipe}/{IpName}/RecipeInfo.xml`）。
- **命名**：本拆塊**不做** IP0→CCD 路徑改名（約束①，follow-up 待 Addis）；CCD=UI 名、IP0=儲存鍵並存。

**塊1 完成（2026-06-19，L2）**：`Models/ArrayTopologyModel.cs`(ComputeUnits/Slots + Load 本機優先回退 .example) + `config/array_topology.example.json`(37 槽 CCD00–36→IP0–36，expected_mac 全 null=TBD，本機檔 .gitignore) + `SystemSettingsViewModel.ApplyTopology`(依 compute_unit 分群成運算單元帶) + `SystemSettingsView` 相機 tab：上半「運算單元·宣告陣列」(宣告槽=黃點未綁) / 下半「偵測到的相機(runtime)」分開呈現。`--selftest topology` 4 case PASS（① 載入 3 槽/2 單元+欄位 ② 分群 Spark1[2]/Spark2[1]+DeclaredSlotCount ③ 全槽「已宣告·未綁」無人線上 ④ 假 LIST_CAMERAS 1 台獨立、宣告槽未因列舉而變=不假 merge）；`--selftest camera` 回歸全 PASS（無 regression）。版面待 Mac 目視→L1。塊3(單 CCD 整合頁)、綁定(#21) 未動。

**塊2 完成（2026-06-19，L2）**：運算單元卡補三項——① **連線燈(真)** = `ComputeUnitGroup.UnitConnected(Node==ActiveIpNode && IsIpConnected)`，`BoolToGreenRed`+`.live` 呼吸燈，預設未連顯灰**不假綠**、連線變化即時刷新（`RefreshUnitConnectivity`，結構未假設永遠單台 active）；② **處理 N(真)** = SlotCount（拓樸槽數，不寫死）；③ **負載%(估算投影)** = `SlotCount×30×7.4ms/30000`（37 CCD→~27%/餘裕~73%；LoadText 標「估算」、tooltip 寫投影算式「7.4ms 實測、37CCD 吞吐未實機跑滿」，與連線無關）。`--selftest topology` 加塊2-a/b/c PASS（處理 N=槽數、負載公式非寫死且含估算旗標、連線預設不假綠+active 才綠）；camera 回歸不破。**負載%=估算非即時量測**；不碰約束②（偵測 section 未動）。版面待 Mac 目視→L1。

**塊3 子塊1 完成（2026-06-19，L2；reuse=A 薄殼）**：新增第 6 螢幕 `SingleCcdSetupView`(薄殼) + `SingleCcdSetupViewModel`——**組合既有 Step1ViewModel + ZoneParamEditorViewModel 獨立新實例**（影像視埠獨立、recipe/zone/align 經共用 RecipeStore 同步），左嵌 `Step1View`、右嵌 `ZoneParamEditorView`(**兩既有檔不改**)；header 顯 `slot.CcdId`("CCD05")+運算單元(唯讀)，`LoadSlot` 設 `RecipeStore.SelectedIp=slot.RecipePartition`("IP5" 儲存鍵,約束①不改名)；master→detail = `SystemSettings` 宣告槽 chip `Tapped`→`SelectedSlot`→MainWindowVM 訂閱→進頁。MainWindow **僅 +1 nav/Panel**(既有 5 個未改)。`--selftest singleccd` 4 case PASS（組合既有實例/進頁前 HasSlot=false/SelectedIp=IP5/header 顯 CCD05）；store/topology/camera 回歸全綠。**deferred（未來 B/Phase 2）**：對位 Mark 視覺定位、嵌入編輯器內 IP0→CCD0 顯示、重複 chrome 收斂（皆需動 View）；A1 實拍底圖、#21。

**塊3 子塊3a+3c 完成（2026-06-19，L2；重排為影像為主工作台）**：① **3a**：把 Step1 的影像/ROI code-behind 抽成共用 `Controls/RoiImageView`(StyledProperty `Source/EditZone/AllZones/Defects/…`，**EditZone 注入=可編任一 ROI、AllZones 畫全部 ROI**)；`Step1View` 改用它（`EditZone=PrimaryZone`、單一 ROI → 行為不變）。② **3c**：`SingleCcdSetupView` 重寫為 **大影像(`*`寬，RoiImageView) + 右精簡欄(420px)**——右欄複用 `ZoneParamEditorViewModel` 的 `Rois`/`ParamRows`/`AlignRoi`，影像 `EditZone={ZoneEditor.EditZone}`(選中 ROI)、`AllZones={DetectRoiList}`(畫全部)→ **選 ROI 即在影像上對照定位/框選**；拿掉重複 chrome（不再嵌整個 Step1View/ZoneParamEditorView）+ **移除單 CCD 頁的 IP/CCD 下拉**(header 顯 CCD 名,解先前選 CCD36 下拉空白)。`--selftest singleccd` 5 case PASS（含 ⑤ EditZone=選中 ROI + AllZones=DetectRoiList）；store/topology/camera/upstream 回歸全綠；build 0 警告。**既有 Step1View / ZoneParamEditorView / MainWindow 既有 5 入口未改 = 零 regression**（行為待 Mac 目視）。對位 Mark 視覺定位仍 deferred（現數值卡）。

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
- UpstreamServer（CF_/8787）：**2026-06-19 接線完成 + 回呼接 IP + python 模擬器 → L2(selftest)/L3 ✓ 端到端跑通(模擬器↔真Control↔真IP，GET_RESULT 回真實 path+count)**；
  align/grab offline 誠實失敗(非假 OK)。**真上位機協議認帳 = L4（做不了）；μm 契約(#5)= IP 片面提議 = L4**（不混）。

---

## 上線前驗證清單：雙 Spark 產能與 mode2（2026-07-16）

> **新決策事實**：已購 **2 台 DGX Spark**，要處理 **上 37＋下 18＝55 CCD**（30s 節拍）。生產基準維持 DIV mode0（7/12 決策，7.4ms/張 L4 實測）；
> mode2（DIV-voting 融合）為候選升級——設計上較準（暗區棄權/16 路投票/多尺度），但**效能未 bench、準確度無 A/B、缺 DTH 防呆**。
> 台數分水嶺：mode2 倍率 **r≤2.7 → 37/18 自然分；2.7–4.8 → 改均衡 28/27；>4.8 → 2 台不夠**（實務到不了）。mode0 下單台可扛 55 CCD（41%），第二台可規劃熱備。
> **2 台上線真正 blocker = Control 只支援單一 `ActiveIpNode`**——gap #1/#5（IP 多 CCD 多工待決）與審計 B6 因 2 台採購**由 batch-later 升為上線前必做**（gap 表原行不改，帳目以此節為準）。

→ 可勾選驗收單（V1 效能 bench / V2 準確度 A/B / V3 P0-5 防呆 / V4 雙 Spark 接入 / V5 拓樸頻寬＋門檻速查表＋G7 新機台估算附錄）：
**[verification/capacity_2spark_checklist.md](verification/capacity_2spark_checklist.md)**
