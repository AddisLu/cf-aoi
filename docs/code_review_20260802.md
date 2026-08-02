# 2026-08-02 全面複查報告：6 相機到貨前 bug 總掃

> 方法：4 路平行深度複查——grab（14 檔 ~2748 行逐行）、ip（41 檔 ~10841 行，核心逐行）、
> control（65 檔 ~7462 行）、跨模組契約（8100×11 命令／8200×15 命令逐鍵比對、scripts×15、
> ResultInfo 輸出契約、設定檔契約、docs 漂移）。
> 已修的 ★1–★8 與既知開放項（8200/8100 單客戶端、GET_FRAME_PREVIEW、演算法 deferred gap）不重報；
> 每條附 file:line 證據；查證後排除的疑點共 **40 項**（各節末），不列為 bug。
> **本報告為發現記錄：所有項目均「未修」**，修繕順序待 Addis 裁示。風險分級以「6 相機（8/M）到貨日」視角。
>
> 編號前綴：B=grab、I=ip、K=control、X=8100 契約、G=8200 契約、R=輸出契約、S=scripts、C=設定檔。
> 重合另註（如 G5=I5）。

---

## P0 — 到貨日標準動線直接會撞（建議到貨前修）

| # | 端 | 問題 | 位置 | 觸發情境 |
|---|---|------|------|---------|
| ~~**B1**~~ **已修 L2** | grab | `grab_loop` 對 pylon 例外零防護：一台拔線/斷電 → `GenericException` 逸出 thread → `std::terminate` **全行程死亡**（其餘 5 台陪葬、8100/RDMA 全斷）。`TimeoutHandling_Return` 只吞逾時不吞裝置移除 | cam_pylon.cpp:142-201 | 任一台線材/供電/交換機出狀況（×6 機率）|
| **B4** | grab | `--cam-count ALL` 無「應到幾台」概念：cam_map 6 筆、只列舉到 5 台 → ARM 回 OK **靜默跑 5 台**（fail-fast 只保護 want>0）。與已修 ★7 不同根因 | cam_manager.cpp:247-251 | 到貨日某台 link 未起（忘下 speed 1000 / 未上電）。**運維面先改 runbook 用 `--cam-count 6`** |
| **X1** | control | `GET_CAM_NODES` **不送 cam_id** → 永遠讀 cam0 且無錯誤提示（Grab 端 ★4 已修另一半：cam_id 必填+回聲，Control 送 null、回聲也不讀）| GrabClient.cs:129-131,133-147；呼叫點 SystemSettingsViewModel.cs:361 | runbook §5 逐台健檢：選 CCD05 按「讀取機器層參數」顯示的是 cam0 |
| **B6** | grab | idle 路徑 `GET_CAM_NODES`/`TUNE_MEAN` 的 `get_or_open_primary` fallback **無視 cam_map**：idle 第一發 cam_id=3 查詢 → 開列舉第一台、回聲卻是 3 = 靜默錯台；TUNE_MEAN 更把錯台量測**寫進 cam_id=3 的 cam_config 槽** | main.cpp:451,496,502；cam_manager.cpp:299-311 | runbook §5「逐台健檢（idle）」正是此情境 |
| **B7** | grab | `--cam-count 1` + cam_map 並存：MAC 綁定查回的 cam_id 被 CLI `--cam-id`（預設 0）**無條件蓋掉** → 單台隔離測試時實體 CCD03 送 `camId=0` + 套 CCD00 曝光 | main.cpp:294-295 | 到貨日單台排障（常用手段）|
| **K4** | control | 工作台 Step5「套用到其他相機」**複製磁碟舊檔**：`ApplyToTargets` 前無 `Store.Save()`，`CopyParamsToIps` 讀磁碟 → 未存檔的 Step4 調參不會被複製、卻回報「✓ 參數 N 分區」。selftest 自己先 Save 把坑遮住。同族：切槽時 dirty 調參默默丟棄 | CameraWorkbenchViewModel.cs:297-336→RecipeService.cs:120-138 | 五步驟標準動線（調完 Step4 直接按 Step5）|
| **K2** | control | 觸發鏈命令**全無逾時** + `IsBusy` 心跳跳過 → 對端 wedge 時 `_lock` 永不釋放、心跳恆判存活**假綠燈**、後續 CF_ 全卡死只能重啟 Control（對照組：工作台相機操作都有 CTS）| IpClient.cs:222-237；ConnectionManager.cs:73-77；UpstreamWiring.cs 全部預設 CT | 上位機聯調日 GRAB_ARM/START 重活時 |
| **K3** | control | 上位機 `CF_LOAD_RECIPE` 配方名拼錯 → `EnsureRecipeExists` **自動生成預設 DIV 配方**照送 IP → 回上位機 OK = 假成功進生產（教材 P0-4 已標、程式未加閘）。另 recipe 名未消毒可 `../` 逃逸 | UpstreamWiring.cs:31→RecipeService.cs:174-192 | 上位機與 Control 配方名不同步時 |
| **I8** | ip | **拼接座標 recipe × rdma-process 逐 slice 無座標換算**：zone（StartY 可達 146k）超出 slice 範圍被 clamp 塌成 1px 條照跑 → 恆 0 缺陷 = **該 zone 靜默零覆蓋**。生產 recipe（T550QVN10 全 24 CCD = SUB+拼接座標）直接踩 | main.cpp:189-196 | 相機到貨後把生產 recipe 餵 rdma-process |
| **I7** | ip | 連續模式（totalSlice≤1）`loss_by_cam` **永不歸零**：一次 CRC 失敗後該 cam 每幀都標 `panel_incomplete` + 每幀 incident（30s 節流擋洪水）。`fpp=0` 是 appsettings 無 Grab 節點時的預設 = 8/M 大機率第一次上線就是連續模式 | main.cpp:1110（唯一歸零點）,1189-1197 | 8/M 連續模式 + 任一次傳輸損壞 |
| **I5**=G5 | ip+契約 | **守門路由語意待裁示**：legacy enum 值全為 `Awc_*_Way_*_Div`（皆含 "Way"、無 `Awc_8_Way_Star_Div`）→ 任何帶 awc 的 legacy DIV recipe 命中 `is_fused` **路由 mode2（非已驗 mode0）**，且 mode2 連帶吃 `EnableMultiscale` ini 預設 1、`MeanLowThreshold` 預設 40。純 mode0 幾乎只剩「無 awc + stale `AlgorithmCompare="DIV"`」可達。Control enum 又開放 `Awc_4_Way_Arrow_Div` 等看似幾何模式實則全進 mode2 | zone_config_adapter.cpp:131-135；ZoneSettingModel.cs:12-14 | 載入任何 legacy DIV recipe 即發生。**裁示選項**：(a) `is_fused` 收緊（star+div 同現才算），legacy `*_Div` 走 mode0；(b) 維持路由但文件全改 + record_recipe 明示 + multiscale 預設 0 |

## P1 — 靜默漏檢／假 PASS 族（本專案最忌；機制「以為有保險其實沒有」）

| # | 端 | 問題 | 位置 |
|---|---|------|------|
| **G1**=R1 | control | `frame_loss`（panel_incomplete）**無消費端**：`IsPass => DefectCnt==0`、無 FrameLoss 屬性 → 7/30 做的「消除靜默漏檢」只做到 IP 端，Control/上位機仍把不完整片當乾淨 PASS | DefectResultModel.cs:132；IP 端 result_saver.cpp:241-250 |
| **I1** | ip | 尾 slice 遺失 → 標記**蒸發**（frame_loss 只向後傳播；先歸零①後吸收②順序也反了 → 遺失可歸屬到錯的片）。7/30 注入驗證（slice3/7）恰好都是片中段、沒蓋到尾張 | main.cpp:1108-1130 |
| **I2** | ip | seq 跳號備援對帳錯：`gap > total_lost` 拿**區間量比累計量** → 早期 CRC 失敗會遮蔽後續真跳號；`total_lost=gap` 毀累計帳 | main.cpp:1134-1147 |
| **B2** | grab | RDMA 送失敗後**全相機幀進黑洞**：`connected_=false` 後每幀靜默蒸發、`dropped` 仍 0、CHECK_HEALTH 無 rdma/error 欄位、無重連（檔頭自承 P0-7「上線前必修」，未修）| rdma_sender.cpp:91,100-107,123-129 |
| **K1** | control | `CF_GET_RESULT` 假 OK：`OnGetResult` catch 吞例外回 `("","0")`、UpstreamServer 無 ERR 路徑恆 `Resp(true)` → IP 斷線被上位機讀成「0 個結果夾」（★A 誠實失敗原則的漏網；`OnStop` catch 也無 log = ★6 同型殘留）| UpstreamWiring.cs:99-115；UpstreamServer.cs:171-176 |
| **I3** | ip | GPU 收集階段**硬編碼過濾**（wrapper 傳 `1, 300, 5.0`）：>300px 大缺陷、aspect>8 線狀刮傷**靜默丟**；recipe `BlobMaxSize` 形同虛設；mode2 多尺度大缺陷補強被自我抵銷。兩跑一致地錯 → `--verify-deterministic` 抓不到。修 wrapper 參數化即可，不觸 kernel 禁改 | cuda_kernels.cu:1554-1562（wrapper）,789-831（kernel 本體）|
| **I4** | ip | `bindTextureObject` 只比指標不比尺寸：重配同址 → stale texture 幾何**決定性地錯**（錯位/越界讀）。觸發：mode0 寬≥8000 多尺寸切換 | cuda_kernels.cu:915-919；gpu_pipeline.cpp:77-79 |
| **B18** | grab | `LOAD_RECIPE`/`GRAB_STOP` 無 handler 時仍回 OK（其他命令回 ERR）——靜默成功反模式 | control_server.cpp:140-146,183-186 |

## P2 — 穩定性／資源／操作正確性

| # | 端 | 問題 | 位置 |
|---|---|------|------|
| **B3** | grab | RDMA 失敗後 `disconnect()` 短路（`if(!connected_)return`）→ QP/MR/event channel 全遺留；重 ARM 疊髒重連 = pinned 洩漏 + 懸空 MR | rdma_sender.cpp:136-137 |
| **B5** | grab | 背壓中收端 wedge（IP 活著但卡死）→ `poll_one` busy-poll 無逾時 → `stop_all` join 永卡 → state_mtx 永久持有 → 8100 全死 kill -9（★1 姊妹死鎖，修法未覆蓋此環）| rdma_sender.cpp:99；rdma_common.h:180-186；main.cpp:363-366 |
| **B10**+I13 | 兩端 | ControlServer 溫和退出卡死：`stop()` 只關 listen fd，`handle_client` 卡 `recv(client_fd)` → join 永不返回（與 62a2bbb 溫和退出部署方針直接衝突）| grab control_server.cpp:94-98,27-31；ip control_server.cpp:360-372,398-401 |
| **B11** | grab | `accept` 失敗即 `break`：ECONNABORTED/EMFILE/EINTR → 8100 **靜默死亡**、行程假活 | control_server.cpp:104-108 |
| **B12** | grab | `cam_config.json` parse 失敗 `catch(...){}` 全吞 → 6 台**靜默回 70µs/256 出廠值**；寫檔失敗也全吞（與 `load_map` fail-fast 雙標）| main.cpp:66-83,103-106 |
| **I6** | ip | LOAD_RECIPE 失敗**狀態撕裂**：saving/ioi/share_flags/align **先寫後驗**、zones 不換 → 守門拒絕後用「舊 zones + 新配套」繼續跑 | control_server.cpp:424-494 |
| **I9** | ip | 尺寸切換逐幀/逐 zone 觸發 GPU 全套 cudaFree/cudaMalloc 重配（含 pinned）→ 異型混編/多 zone throughput 崩 | gpu_pipeline.cpp:77-79,306-311 |
| **I10** | ip | recv WC error（幀>slot 的 LOC_LEN_ERR 等）→ 例外 → recv_thread 死 → session 全停，且該幀不進 lost_ 帳、無 incident 歸類 | rdma_common.h:179-188；rdma_source.cpp:135-141 |
| **I11** | ip | CHECK_ALIGN 缺 `kMaxDim` + uint32 乘法迴繞防呆（SEND_IMAGE_FOR_REVIEW 有這裡沒有）→ 構造尺寸 → 4GB Mat 越界 crash | control_server.cpp:886（對照 537-543）|
| **I12** | ip | no_wait 串流 `results_` map 只進不出 → 慢性 OOM | control_server.cpp:620-627,374-380 |
| **K5** | control | 上位機回呼在 TCP 執行緒直改 UI 綁定狀態（`Rois.Clear/Add` ObservableCollection 非 UI 執行緒）＋「上位機載配方 vs 操作員編輯」競態 | UpstreamServer.cs:89→UpstreamWiring.cs:31→ZoneParamEditorViewModel.cs:192-200 |
| **K7** | control | 系統設定「測試連線」把**共用 IpClient 永久切到測試位址**（心跳只在 !IsConnected 才重連）；且 6 個設定欄位可編輯但無儲存命令、不回寫 Config | SystemSettingsViewModel.cs:156-168；SystemSettingsView.axaml:26-45 |
| **K10** | control | UpstreamServer 接受多客戶端：第二條（診斷腳本）斷線把燈關掉、CF_ 併發互踩無鎖 RecipeStore（放大 K5）| UpstreamServer.cs:84-95,201 |
| **K8/K9** | control | 工作台：Step2 曝光/增益滑桿與 Engine 值脫鉤（未轉發 PropertyChanged，切槽顯示上一槽值）；重列舉 KPI 同值不 raise → 左欄/候選不重建、SelectedCandidate 指向已不存在相機 | CameraWorkbenchViewModel.cs:185-186,41-47,62-72 |
| **K11** | control | 34 欄表文字解析失敗**默默寫 0**（TryParse fallback 0、未用 InvariantCulture）→「1,4」變 0 送 IP | ZoneParamEditorViewModel.cs:56-59,81-89 |
| **X4** | 契約 | SET_CAM_MAP cam_id 語意分歧：Control 新綁定用拓樸槽索引、既存條目回送 Grab 值、未預檢 cam_id 碰撞（Grab 強制唯一）→ 手編過 cam_map 後 UI 綁定整包被拒 | SystemSettingsViewModel.cs:261-265,298-310；cam_manager.cpp:70-72 |
| **C1** | control | Grab 節點名 `"GrabA"` **硬編碼**（無 ActiveGrabNode 機制）：改名/加 GrabB → 心跳目標 null → 永遠離線無錯誤 | ConnectionManager.cs:42；appsettings.json:17 |
| **C2**=G4 | control | ShareSetting 六旗標**五個死鍵**：面板無 View 掛載（導覽收斂孤兒）、`share_flags` 從未送 IP → `SaveSourceImage`（Step4 存原圖）/`TuningRecipe` 從 Control 不可達 | ShareSettingModel.cs；IpClient.cs:85-86 零呼叫端 |
| **C3**=K16 | control | appsettings 缺檔靜默回退：`ListenPort` 預設 **8000**≠8787、Nodes 空 → 全離線無提示（`optional:true`）| ConfigLoader.cs:29；SystemConfigModel.cs:19 |
| **C4**=K13 | control | `ArrayTopologyModel.Load` catch 吞：37 筆手編 JSON 打錯一逗號 → UI 0 槽、無原因（語法錯 vs 檔不存在不可分辨）| ArrayTopologyModel.cs:62-69 |
| **G9** | control | rdma 模式誠實 ERR Control 端無處理：指向 rdma 節點按 Test → 先上傳 40MB 再收裸例外對話框（`IpOnline.Mode` 未用來擋）| OfflineReviewService.cs:72-141；ip 端拒絕清單 control_server.cpp:512/558/1002 |
| **G2**=K6 | control | **對位鏈斷頭**：`golden_png_base64` 零呼叫端 → IP 收不到 golden 恆 `align_enable=false`；Control 只把本機 PatternPath 寫進 XML（IP 不讀）；工作台 tooltip「LOAD_RECIPE 自動 base64 送 IP」與程式不符。CHECK/SET_ALIGN 兩端都實作但 Control 零呼叫（決策 A 刻意，Step4 前必接）| IpClient.cs:87-88；RecipeModel.cs:17；CameraWorkbenchView.axaml:218 |

## P3 — 低危／衛生／理論項

| # | 一句話 | 位置 |
|---|--------|------|
| B13 | 統計欄位跨執行緒裸讀寫（UB；建議 atomic relaxed）| cam_pylon.h:102-103；rdma_sender.h:53-56 |
| B14=I17 | `getaddrinfo` 未檢查回傳＋洩漏（hostname 打錯 → segfault）——兩份 rdma_common 同病 | grab rdma_common.h:104,130；ip rdma_common.h:86 |
| B15 | `--rdma-slots >32` 超 `max_send_wr` → post_send ENOMEM → 墜入 B2 | rdma_sender.cpp:28-33 應驗上限 |
| B16 | ConnReader.buf 無上限（不含 `\n` 的流無限累積）| grab control_server.cpp:27-33 |
| B17 | slice 截斷不對稱（totalSlice 有 clamp、sliceIndex 直接截斷；理論項）| main.cpp:258-261,275 |
| I14 | slice0 遺失 → edge_leading_state 殘留上片前緣、尾緣 drift 錯基準；tail_global 假設等高 slice | main.cpp:289-336 |
| I15=R3 | 缺陷檔名 `Slice` 恆 00：`frame_height` 全 repo 無人賦值、`hdr.sliceIndex` 未傳入 result_saver | result_saver.h:54、.cpp:371 |
| I16 | SourceImageWriter n_slots=1 恆滿全 drop（容量=n-1）；swap 掏空 slot 預配置 | source_image_writer.h:71-74,124 |
| I18 | `apply_blob` O(n²)：爆量 10000×merge>0 → ~10^8 迭代佔死消費線程 → 背壓全鏈 | defect_rules.h:106-111 |
| I19 | flight_recorder 多執行緒 append 同 jsonl 無鎖（行可能交錯）| flight_recorder.cpp:222,301,342,389 |
| I20 | RdmaImageSource::init 失敗路徑不釋放 conn_/MR | rdma_source.cpp:107-111 |
| K14 | RefreshNames Clear → ComboBox two-way 可能回寫 null → `Select(null)` 無防護（需 Mac 實測；guard 零成本）| RecipeStore.cs:110-121 |
| K15 | DefectSort 第二層用當前 DatePicker 非 Parse 時快照 → 改日期後雙擊舊列表打錯資料夾 | DefectSortViewModel.cs:72,192,235,320 |
| K17 | Step1 遠端影像 OK/NG 歸檔默默無動作 | Step1ViewModel.cs:381-384 |
| K18 | Step5 目標勾選每次換步驟/槽重置 | CameraWorkbenchViewModel.cs:109-120,290-295 |
| K19 | selftest 污染真實配方目錄（WB_TEST/SYNC_TEST/MULTIIP_TEST 留在生產下拉）| SelfTest.cs:1110-1113,1306-1341 |
| K20 | RoiImageView `CenterOnDefect` Clamp 寫法恆 5.0（誤導）| RoiImageView.axaml.cs:310 |
| X5 | Grab 三命令 params 無存在保護（nc/腳本會 parse error 而非欄位錯）| grab control_server.cpp:141,189,307 |
| X6 | SelfTest 假 Grab server 回應形狀 ≠ 真 Grab → X1/X3 類缺漏 selftest 蓋不到 | SelfTest.cs:1078,1094-1095,1099-1100 |
| G7 | `date=""` 全掃丟日期維度；IP `locate_panel` 取第一個命中日期夾 → 同名 panel 可能操作到舊日期 | UpstreamWiring.cs:103-110；ip control_server.cpp:188-193 |
| G8 | `crc32` 宣告比對（offline-tcp 唯一傳輸損壞偵測）Control 從不送；`system_id`/`no_wait` 未用；`pixel_format` 文件幽靈 | IpClient.cs:163-167；ip control_server.h:18 |
| G10 | `IoiInfoList`（#23）Control 不解析 → UI 看不到 | DefectResultModel.cs 無屬性 |
| G11 | μm/CcdIndex 無 8200 契約（IP 由本機 INI 單方決定，Control 不能設 opt_res 也不讀三欄）——多 CCD 拼接前要補 | result_saver.cpp:160-161；8200 無鍵 |
| G12 | recipe_saving 缺席預設兩端不一致（IP 100/100/-1/-1 vs model 64/64/250/10000）；RecipeSavingModel 9 欄可編輯但不進 `BuildRecipeSavingJson` 永不生效 | control_server.cpp:428-431；RecipeSavingModel.cs:15-27,43-57 |
| R2 | `ImagePath` 恆空字串（legacy 有值）→ DefectInfo↔patch 檔連結退化成 (RoiIndex,RunIndex) 手工配對 | result_saver.cpp:41,152,170 |
| R4 | `GC_X/GC_Y` 同名兩義：ResultInfo=ROI-local 重心 vs LIST_DEFECT_PATCHES=檔名全域座標 → 多 ROI 配方錯位 | result_saver.cpp:52-53 vs control_server.cpp:785 |
| R5 | `defect_count` vs patch 檔數不一致：預設 `debug=false` 不存 patch → DefectSort 列 N、雙擊進去 0 張 | control_server.cpp:678；IpClient.cs:147 |
| R6 | XML 已非嚴格 legacy JudgeResult schema（插入 _um/CcdIndex；IoiInfo 結構不同；XmlSerializer 靜默跳過所以能動）| result_saver.cpp:121-126,346-357 |
| R7 | CF_GET_RESULT 實回資料夾名（legacy 期望 ResultInfo.xml 完整路徑）——真上位機 L4 對表項 | UpstreamWiring.cs:107 |
| 小 | `Ioi_*.png` 不被清舊/不被 SORT 複製 → 跨次殘留；OfflineReviewService docstring 與實作不符 | result_saver.cpp:280,465-466 |
| X2=B8 | GRAB_START `timeout_ms` 死參數（兩端接了、handler 不用）＋收滿無 watchdog（encoder 行觸發後掉一幀=永久等待）| grab main.cpp:334,338 |
| X3 | CHECK_HEALTH 三方不一致（header 文件扁平 frames/drops vs 實作巢狀 data vs Control 只讀 status）＋ **B9** 無 per-cam 對帳欄位 → 6 台哪台少幀不可見 | grab control_server.h:6、main.cpp:506-535；ConnectionManager.cs:90 |
| X8 | rdma_common.h 兩份：wire byte-identical ✓、**行為層已分歧**（CQ 64 vs 128、max_recv_wr 32 vs 64、IP 端多 nonblock/CM 輪詢）→「同源副本要同改」敘述改為「wire 凍結共用、行為各自演化」＋維護風險註記 | grab/src/ vs ip/src/image_source/ |
| X9=S4 | CF_LOAD_RECIPE detectMode 差一格：docs 範例/模擬器/SelfTest 全 repo 一致地放 token10、程式讀 P(9)；legacy 9 參數制推算 6 根管線；`_detectMode` 未用故假性通過——真上位機對表時定案 | upstream_simulator.py:67；UpstreamServer.cs:116,22；SelfTest.cs:531,623 |
| S1 | `scripts/control_test.py` 整支陳舊（命令語意/結果欄位全對不上現行協議）→ 建議刪或重寫 | control_test.py:37-48,63-69,96-114,165 |
| S2 | `verify_alignment.py` 用 `--port`（不存在 → IP 直接退出）應為 `--control-port`（其餘 6 支腳本都對）| verify_alignment.py:8,511 |
| S3 | upstream_simulator 未涵蓋已接線的 CF_GRAB_START/CF_STOP（verify_step3_trigger.py:30 卻宣稱有）| upstream_simulator.py:64-73 |
| S5 | 「9 參數回應」實為 10 段（`OK|p1..p8|errMsg`）——多處文件措辭 | UpstreamServer.cs:205-208 |
| **S6** | ~~`verify_flight_src.py` EXPECT 行號已過時~~ → **✅ 本輪已修並自測**：原寫死 bad_json{412}/frame_validation{539,569}/recipe_load{426,593}，實際源碼為 412 / **544,580(+rdma_source 195,216,258)** / **559,728,1061** → 重跑必 FAIL（L3 徽章失效）。**修法：改 `scan_call_sites()` 即時掃描 `ip/src` 的 `FR_RECORD_INCIDENT("kind"` 呼叫點**，比對 src 是否正好命中（不再需 ±3 容差，且日後改碼／加註解永不再漂移）；掃不到源碼時退回「只驗格式」並印 warn。自測：掃出 11 種 kind、三個目標 kind 呼叫點正確 | scripts/verify_flight_src.py:44-70,104-120 |
| C5 | cam_map.ccd_id 與 array_topology.ccd_id 同名異管、無交叉檢核（填 37 筆 MAC 時兩檔人工同步）| 兩 example 檔 |
| C6 | `IpOnline` 節點純占位（無切換路徑）| appsettings.json:12-16 |
| K12 | 主控台 GET_RESULT 鈕實送 GET_STATUS（顯示名不符）；GRAB/STOP tooltip「MIL 專屬未啟用」已不真 | MainWindowViewModel.cs:176-185；MainWindow.axaml:187-193 |
| 死碼 | FrameHolder 全類；SlotBinding.StatusLabel/Tooltip；ComputeUnitGroup 整卡；CamStatusToBrush/SlotBindToBrush 轉換器；SystemSettingsView camStrip 樣式；CanBind | control/src 多處 |
| **T1** | golden_maker **GUI 模式尺度錯誤**：裁切取自縮放後顯示圖 `g_gray(sel)`（>2000px 必縮，線掃 8192 縮至 ~0.24×），XML 座標卻以 `inv_scale` 還原全解析度 → GUI 產的 golden 與搜尋窗不同尺度、對位低分/失敗。CLI `--mark-rect` 路徑正確（`gray_full(rect)` 原圖裁切；Gap #1 L3 驗的就是 CLI）。主線複查補抓（2026-08-02） | golden_maker.cpp:191（GUI crop）vs :148（CLI crop）|

---

## P4 — UI／usage／log 字串與實作不符（補註解階段發現，**刻意未改**）

> 這些是 code token（改動需重編譯＋重跑 selftest），本輪「只改註解」的鐵則下一律保留原樣、僅在
> 旁邊補註解說明。皆為一行可修，建議與相關功能修繕同一個 commit 處理。

| # | 位置 | 現有字串 | 問題 |
|---|------|---------|------|
| U1 | `control/src/Views/CameraWorkbenchView.axaml:218` | 「golden 範本影像…**LOAD_RECIPE 時自動 base64 送 IP**」 | 與實作不符（G2/K6：`goldenPngBase64` 零呼叫端，從未送出）→ 操作員會以為對位已備妥 |
| U2 | `control/src/Views/MainWindow.axaml:191-193` | GRAB/ALIGN/STOP tooltip「**MIL 專屬，新版未啟用**」 | GRAB/STOP 已非 MIL 專屬（CF_ 觸發鏈 7/21 已接真 Grab，只是本地鈕未綁）→ 誤導成「系統不能取像」 |
| U3 | `control/src/ViewModels/MainWindowViewModel.cs:178` | 狀態列顯示 `CF_GET_RESULT` | 實送 `GET_STATUS`（K12 名實不符） |
| U4 | `control/src/Controllers/UpstreamServer.cs:125,137,161,182` | 「GRAB_START 未支援：**offline 無取像對象**（待 Step4+/相機）」等 4 則 | GRAB_START/STOP 已接線，走到此處實為「Grab 未連線」→ 訊息歸因錯誤 |
| U5 | `control/src/Views/SystemSettingsView.axaml:11` | 副標「appsettings.json · **GrabClient**」 | 該頁已無 GrabClient 相關 UI（相機參數整併進工作台） |
| U6 | `control/src/Views/SingleCcdSetupView.axaml:127`＋`CameraWorkbenchView.axaml:250` | 「進階參數（**27 欄**全表）」 | 實為 **34 列**（`BuildParamRows` 34 個 Add） |
| U7 | `control/src/ViewModels/SingleCcdSetupViewModel.cs:24` | 「未選 CCD（**從系統設定的宣告陣列**點一顆進入）」 | 導覽收斂後入口已改為相機工作台左欄 |
| U8 | `ip/src/main.cpp:111` | usage「`--recipe <xml>` legacy RecipeInfo.xml（**只接受 DIV 模式**）」 | 三域守門後 SUB/DIV-voting 皆合法 |
| U9 | `ip/src/main.cpp:107-108,135` | usage 主行只列 `offline-tcp\|offline-file`；rdma 段標題只寫 rdma-validate | 未列 bench/rdma-process；該段參數 rdma-process 同樣適用 |
| U10 | `grab/src/rdma_common.h` `reg()` 的 RC_CHECK 訊息 | 「GPU 記憶體失敗多半是 **nvidia_peermem 未載入**」 | GB10 不適用（應指向 cudaHostAlloc，不變式 11）；已在旁補註解 |
| U11 | `grab/src/control_server.cpp` 例外回應前綴 | `"parse error: "` | 缺 `params` 時實為 nlohmann type_error，非 parse error → 歸類誤導 |
| U12 | `grab/src/rdma_nslot_test.cpp:102` | `printf("完成：ok=%u err=0 …")` | `err=0` 是硬編碼字面值（該工具從不計錯）→ 恆印 0 有誤導空間 |

---

## 查證後排除（40 項摘要——查過沒問題的邊界，避免日後重查）

- **grab（12）**：mac_map 並發（8100 單執行緒序列化）；slice_seq 無鎖（ARM 時無 cb 在飛+各寫自槽）；★1 修法本體正確；`++frame_seq` 已在 send_mtx 內；enumerate 與取像並存（實測 PASS）；params null → parse error 不 crash；析構吞例外合理；magic static；snprintf 有界；want>0 冪等重用正確；auto-stop 後 grabbing=true 是刻意設計；TUNE_MEAN 曝光殘留 runbook 已載。
- **ip（16）**：seq 起始 1 安全；SEND/RECV slot 生命週期正確（★2 根治確認）；NOCRC 兩端同步；min_rnr_timer=12 在；8200 先於 init；CCL 收斂+canonical 排序在；smooth scratch 有歸零；remap 僅 mode1/多尺度僅 mode2；AI stream 同步順序；RDMA 入口驗證已生效；截斷 bit-exact；record_incident race 已修；注入守門先讀完 payload；wr_id 防呆；FrameQueue 緩衝池 happens-before；slots>64 誠實失敗。
- **control（12）**：seq 錯位配對有 Dispose 保護；UTF-8 整行解碼；★8 已修確認；bound 語意已修確認；SET_CAM_MAP 完整表+搶槽守門；MacUtil 對齊 grab；CopyParams 逐目標深拷；CanBind 死碼降級；DispatcherTimer selftest 不觸；int→bool 綁定合法；SaveAlign 分區正確；detectMode P(9) 已標 P3-7。

## 契約面「查證後一致」重點（可信任基線）

- FrameHeader 單一來源（ip 經 CMake include 指 `shared/`，無第三份）；MrInfo/MrInfoEx wire byte-identical。
- 8100 `LIST_CAMERAS` 11 欄逐字全對；8200 `recipe_saving` 10 鍵逐字對齊（selftest 鎖「無漏無多」）；
  DefectSort 五命令、遠端影像三命令（LIST_DIR/GET_IMAGE_PREVIEW/REVIEW_LOCAL_IMAGE）全對；
  SUB/融合契約鍵（經 recipe_xml）兩端逐一對上。
- 輸出契約：三層資料夾/panel 夾名/ResultInfo 檔名/缺陷檔名 6-token/(RoiIndex,RunIndex) 配對/
  classification.json/`_diag` 不誤認日期夾——全部兩端+docs 三方一致。
- scripts：verify_step3_trigger/list_during_grab/alignment(線上部分)/flight*/remote_image/coord/
  roundtrip/rules_edge/compare_results/存圖控制 與現行協議全一致。
- 設定檔：appsettings↔SystemConfigModel、array_topology.example↔Model、cam_config/cam_map.example↔grab
  解析端全鍵對上（含 `_behavior` 敘述逐條屬實）。

## 本次文件更新記錄（2026-08-02）

- `docs/CLAUDE.md`：全面對齊（目錄樹/約束②#21 已落地/五步驟表模式勘誤/CF_ 常數+detectMode 警語+
  CF_GET_RESULT 實回內容/決策 A 範圍/移除 SET_MODE+補兩端命令集/輸出段 --ip-name·frame_loss·Ioi·
  incident kinds/三域守門重寫含 mode2 路由警語/遷移表新 kernel 註記/不變式 1·5 修訂/§9 加讀表）。
- `docs/STATUS.md`：標頭日期、IP 表 GPU 引擎列、不變式節 DIV-only→三域守門、image-capture/online 勘誤、
  本報告入帳（見 STATUS「2026-08-02 全面複查」節）。
- `grab/CLAUDE.md`+`docs/grab_程式完整說明.md`、`ip/CLAUDE.md`+`docs/ip_程式完整說明.md`、
  `control/CLAUDE.md`+`docs/control_程式完整說明.md`：依各自漂移清單全面修正（詳見各檔 2026-08-02 註記）。
- `docs/tools_程式完整說明.md`+`tools/README.md`：查證幾乎零漂移，未改。

## 修復記錄

### B1 — 拔線導致全行程死亡（2026-08-02 修，**L1**）

**修法**（`grab/src/cam_pylon.{h,cpp}`、`cam_manager.{h,cpp}`、`main.cpp`）：

1. `grab_loop()` 拆成 **try/catch 薄殼 + `grab_loop_body()` 本體**。薄殼攔
   `Pylon::GenericException` / `std::exception` / `...` 三層，例外**絕不逸出 thread 進入點**
   → 不再 `std::terminate`。收尾的 `StopGrabbing()` 也另包 try/catch（斷線後它自己會擲）。
2. 攔到例外 → `note_fault()` 豎 `faulted_` + 存訊息 + stderr 警告；**該台 thread 乾淨退出，
   其餘相機的 thread 不受影響繼續送幀**。
3. `CamPylon::is_faulted()/fault_message()`、`CamManager::faulted_count()/faults()` 上拋。
4. 8100 `GET_STATUS`/`CHECK_HEALTH` 新增 **`faulted`（台數）+ `faulted_cams`（cam_id/ccd_id/err）**。

**刻意不做**：斷線後**不自動重連**。重連要重跑 open→參數→RDMA 全鏈，靜默重連會把「線鬆了」
變成無人察覺的間歇掉幀（與本專案「不靜默假成功」原則衝突）。恢復路徑仍是 GRAB_STOP → 排除線路 → GRAB_ARM。

**留下的坑（新增，須列入判讀紀律）**：故障後該台 `is_running()==false`，與「收滿 N 張正常停」
**外觀完全相同**。任何「是否收完」的判斷都必須先看 `faulted==0`，否則斷線會被當成正常收完。
⚠️ **Control 端尚未消費 `faulted` 欄位**（只用 CHECK_HEALTH 的 OK/ERR 判燈）→ 目前掉線只在
Grab stdout 與 8100 回應可見，UI 不會亮警示。屬 K 系列待接項。

**驗證狀態 L2**：`grab/test/b1_fault_containment/`（pylon stub 注入例外，任何機器可跑、不需相機）
6 情境 20 項斷言全過，macOS clang + Ubuntu 24.04 gcc 13.3 兩套 toolchain 皆然。
反向對照：拿掉 try/catch 重編 → 測試在第 1 項即被 terminate 以 SIGABRT(134) 殺掉（確認抓得到）。
⚠️ stub 只驗例外圍堵結構，**不驗真 pylon API 契約與真相機行為 → L3 不可略過**。

**L2→L3 補驗步驟**（damac，需 ≥2 台相機）：
```bash
./build/cfaoi_grab --rdma-dest <ip:port> --cam-count 2   # GRAB_ARM + GRAB_START 後
# 拔掉其中一台的網線／關該台 PoE
pgrep -x cfaoi_grab                       # 期望：行程仍在（修前會消失）
# 8100 送 CHECK_HEALTH，期望 data 內：
#   faulted=1、faulted_cams[0].cam_id = 被拔的那台、err 含 pylon 訊息
#   另一台 grabbed 持續增加、sent_frames 持續增加
```
