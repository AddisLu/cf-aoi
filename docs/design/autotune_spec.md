# 自動調參規格（2026-10-05 草案）

> 目標：人員只設「幾 up」，其他參數（檢測區/bypass、上下邊界、pitch、門檻…）由程式從實際影像自動算出；
> 人員只確認「檢出的是不是缺陷」，再選擇寫入範圍。介面設計稿：Claude Design「CF-AOI 自動調參」（4 個畫面）。

## 1. 流程（4 步，對應設計稿 4 個畫面）

| 步驟 | 人員做的事 | 系統做的事 |
|---|---|---|
| ① 收集 | 選「幾 up」、收集幾片（預設 3）、按開始 | Control → IP 進**存圖模式**（不檢測）→ Grab 取像 → IP 依 IP04 命名存檔 |
| ② 分析 | 看整片玻璃圖（圖層：邊界/晶片/bypass/偏暗 CCD）| 每台 CCD：pitch（FFT）、檢測區與 bypass、上下邊界、門檻；跨 CCD 檢查「欄 × 列 = 幾 up」；找偏暗 CCD |
| ③ 確認 | 對檢出的缺陷小圖按「真缺陷 / 誤判」（T/F 鍵）| 用新參數對收集的影像試檢；統計誤判集中在哪台 → 建議放寬 |
| ④ 寫入 | 選：全部寫入 / 只寫選定 CCD / 放寬選定 CCD（5 段：0、+10、+20、+35、+50%）| 預估「真缺陷抓到 / 誤判剩下」→ 備份 → 寫入各 CCD 配方分區；可一鍵還原 |

**簡易模式原則**：使用者看到的只有圖、勾選與 5 段放寬，沒有原始數字輸入；數值只在「會改的地方」唯讀列出。
要手動微調數字的工程人員走既有的「相機工作台 → 調參」（進階）。

## 2. 存圖與命名（IP04 規則）

- 資料夾：`/srv/cfaoi/50_raw/tuning/<yyyyMMdd_HHmm>_<配方>/IPnn/IPnn_Origin000001.tif`
  - `nn` = CCD 編號（CCD01 → IP01；舊機台 IPnn 即相機編號，見 `scripts/verify_rdma_replay.py`）
  - 序號：該 CCD 收到的第幾張（跨片連續），8-bit 灰階 TIF（與參考圖相同，可直接給 Step1 / REVIEW_LOCAL_IMAGE）
  - 另存 `session.json`（幾 up、片數、配方、每張的片號/slice、時間），供分析與追溯
- `tuning/` 不受 14 天原始圖清理影響（歸檔工具需加例外），調參完成後由人員決定保留或刪除。

## 3. 演算法（`tools/autotune/autotune_engine.py`，已實作並測試）

| 參數 | 方法 | 實測 |
|---|---|---|
| Pitch | 每台取 4 個 pattern 區塊，去低頻後 X/Y 剖面 FFT，**諧波疊加（HPS）**挑基本週期，拋物線內插到小數 | IP04：X **25.84**、Y **18.3**（直接取最大峰會抓到子畫素倍頻 6.0）|
| 門檻 | pattern 區每點與 ±pitch 四鄰平均比（DIV 比值 / SUB 灰階差），每張取 1e-6 分位 = 雜訊底線、跨張取中位數，再加 15% 邊際 | IP04 DIV：底線 0.70/1.25 → **0.60/1.44**（人工 0.60/1.40）；已知暗缺陷 0.41 仍抓得到 |
| 檢測區 / bypass | 局部紋理能量（門檻 = 90 百分位 × 30%）→ pattern 遮罩 → Y/X 投影分段 → 每顆晶片一個 DetectRoi；其餘 = bypass | 合成 6 up：每條 CCD 2 列晶片、位置 ±60 px |
| 上下邊界 | 列平均亮度剖面的大躍變（同 IP edge_check 想法）| 合成：±60 px |
| 幾 up 檢查 | 晶片列數（各 CCD 眾數）× 整片 X 投影分段數 = 幾 up | 合成 6 up ✓；輸入 4 → 判不一致 |
| 放寬 | 個別 CCD 邊際 × (1+放寬%)：DIV 暗↓亮↑、SUB 絕對值↑ | ✓ |

測試：`python3 tools/autotune/test_autotune.py`（15 項；真實 IP04 + 合成整片玻璃）。

## 4. 各模組要改的地方（盤點結果）

| 模組 | 現況 | 要做 |
|---|---|---|
| IP | **沒有「只存圖不檢測」**：rdma-process 每幀都檢測；`SourceImageWriter` 只在 offline-tcp、寫無檔頭 `.bin` | 新命令 `SET_CAPTURE {mode:"save_only", dir, naming:"ip04"}`：rdma-process 收到幀直接寫 `IPnn/IPnn_Origin%06d.tif`、跳過檢測；`{mode:"inspect"}` 還原 |
| Grab | 無存檔（全走 RDMA，這是對的）| 不用改；收集時 `GRAB_START frames_per_panel` 照配方 |
| Control | `share_flags` 從未送出；工作台「套用」只會整份配方複製 | 新頁「自動調參」（4 步）；寫入引擎：逐 CCD 分區寫 ROI/pitch/門檻、放寬係數、備份與還原 |
| 配方 | `recipe_saving`（含 bypass_edge）是整份配方共用，不能每台不同；沒有「排除區」概念 | bypass 以「多個 DetectRoi（每顆晶片一個）」表達（IP 已支援多 ROI）；不需新欄位 |
| 分區命名 | `IP0`（topology）與 `IP01`（make_t550_recipe）並存 | 寫入一律經 `array_topology.json` 的 CCD → 分區對照，不自己拼名字 |
| 回饋 | DefectSort 的 TrueDefect/Particle 標記從未回饋參數 | ③ 確認結果用來：統計每台誤判 → 建議放寬；預估寫入後的效果 |

## 5. 分期

1. ✅ 引擎核心 + 測試 + 設計稿（本次）
2. IP `SET_CAPTURE save_only`（C++，Spark 編譯；以 `image_replay_sender` 回放 IP04 驗證命名與張數）
3. 自動調參服務：Spark 節點代理 `AUTOTUNE` 命令跑引擎（影像就在 Spark，cv2 4.13），結果 JSON + 預覽圖
4. Control「自動調參」頁（依設計稿）+ 寫入引擎（備份/還原）+ 試檢（REVIEW_LOCAL_IMAGE 帶新配方 XML）
5. 實機：6 台相機實際收集（需玻璃 + 光源）→ 與人工調參比對

## 6. 待決（需 Addis 確認）

1. **Pitch Y**：IP04 實測 18.3，ini 寫 19。kernel 吃整數 pitch → 自動調參會建議 18。舊機台人工值 19 是否有其他原因？
2. **生產配方是 SUB 模式**（T550 `Awc_8_Way_Star_Sub`，門檻為灰階差 +17/−16），自動門檻要以 SUB 為主還是 DIV？（引擎兩種都支援）
3. 命名 `IPnn` 的 nn 用 CCD 編號（CCD01→IP01）是否正確？
4. 收集幾片的預設值（目前 3）與每片張數（依配方 frames_per_panel）。
