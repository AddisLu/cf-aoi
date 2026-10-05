# CF-AOI 機台資料夾 `{{CFAOI_HOME}}`（機台 {{MACHINE_ID}}）

> 這份檔案由 `tools/archive/cfaoi_archive.py` 自動放置，**改規則請改 repo 裡的 `tools/archive/README_CFAOI_HOME.md`**。
> 自動歸檔：`cfaoi-archive.timer` 每 10 分鐘一次；最近一次結果看 `STATUS.json`。
> 這個資料夾也是 LoopEngineering（Spark 上的本地模型 + 知識圖譜）的資料來源：它以遠端路徑 `grab:{{CFAOI_HOME}}` 經 SSH 直接讀取（不複製），
> 登錄方式見 repo 的 `tools/archive/loop_register.sh`。

## 資料夾

| 資料夾 | 內容 | 來源 | 保留 |
|---|---|---|---|
| `00_software/` | 相關軟體：`installers/`（pylon、eBUS、CodeMeter .deb）、`control_windows/`（Windows Control 免安裝包）、`updates/`（離線更新包）、`firmware/`（相機/交換機韌體，人工放） | 自動 + 人工 | 更新包各留最新 5 版 |
| `10_logs/` | 機台 log（Grab / Spark 的 journald、Grab 前景 log、kernel、IP 行車紀錄 `_diag` 與 incident） | 自動 | 365 天 |
| `20_docs/` | 參考資料：`cf-aoi/`（repo 全部說明文件鏡像，**勿手改**）、`vendor/`（廠商手冊）、`sop/`（現場 SOP、異常處理） | 自動 + 人工 | 永久 |
| `30_tests/` | `tests_catalog.md/.json`（每支 unit test / 驗證腳本的用途、執行方式、需要的硬體）、`results/`（測試輸出） | 自動 | 結果 365 天 |
| `40_defects/` | 檢測結果：ResultInfo（json/xml）、**defect 小圖**、overlay | 由 Spark 拉回 | 90 天 |
| `50_raw/` | **原始圖檔**（IP SaveSourceImage）、`align/`（相機工具快照）、`reference/`（驗證用參考圖） | 由 Spark 拉回 / 相機工具 / 人工 | 14 天（`reference/` 永久） |
| `60_config/` | 參數快照：`current/`（目前值）、`history/`（每次變更的舊值） | 自動 | 365 天 |
| `70_knowledge/` | 每日摘要（檢測統計、事件、參數變更），給 RAG 檢索 | 自動 | 730 天 |

磁碟超過 85% → 從最舊的原始圖、再到最舊的檢測結果刪到 80%（今天的資料不刪）。

## 命名規則

**通用**
- 日期 `yyyyMMdd`、時間 `HHmmss`（24 小時制、機台本地時間；Grab 是全機校時主機）。
- 欄位之間用 `_`，欄位內用 `-`；自動產生的名稱只用英數 `A-Z a-z 0-9 _ - .`（避免 Windows/USB/工具相容問題）。
- 節點名稱：`grab`（本機）、`spark1`（上方 Spark）、`spark2`（下方 Spark）、`control`（Windows 上位機）。
- 相機：`CCDnn`（兩位數，01 起；上方 raL8192 = CCD01–37、下方 L803K = CCD38–55），與相機內的 DeviceUserID 一致。
- 機台編號 `{{MACHINE_ID}}`：檔案帶出這台機器（USB / 寄給別人）時，打包檔名用 `<機台>_<yyyyMMdd>_<資料夾>.tar`。

**各資料夾**

| 類別 | 格式 | 範例 |
|---|---|---|
| 機台 log | `10_logs/<yyyyMMdd>/<節點>_<來源>.log` | `10_logs/20261005/grab_cfaoi-grab.log`、`spark1_cfaoi-ip-production.log` |
| 行車紀錄 | `10_logs/<yyyyMMdd>/<節點>_diag.jsonl`、`<節點>_incident_<HHmmss>_<ms>.json` | `spark1_incident_155022_335.json` |
| 檢測結果 | `40_defects/<yyyyMMdd>/<panelId>_<recipe>/<panelId>_<recipe>_ResultInfo.{json,xml}` | （與 Spark 輸出、上位機契約同名，**不改名**） |
| defect 小圖 | `…/<panelId>_<recipe>/Defect_<IP名>_Slice<ff>_Roi<rr>_Run<nn>_X<xxxx>_Y<yyyyyy>_Dr<原因>.png` | 座標為全域像素 |
| overlay | `…/<panelId>_<recipe>/<panelId>_<recipe>_result.png` | |
| 原始圖檔 | `50_raw/<yyyyMMdd>/<panelId>_source.bin`（Mono8，寬×高 bytes，尺寸見同 panel 的 ResultInfo） | |
| 相機快照 | `50_raw/align/<yyyyMMdd_HHmmss>/CCDnn.{png,raw,json}` | 相機工具「存快照」 |
| 參考圖（人工） | `50_raw/reference/<yyyyMMdd>_<來源>_<說明>/`（日期 = 影像拍攝/取得日；夾內放 README.md 寫來源與用途） | `20251202_IP04_GPU-Only-TestImage/` |
| 測試結果 | `30_tests/results/<yyyyMMdd>/<yyyyMMdd>_<HHmmss>_<節點>_<項目>.log` | `20261005_143000_grab_verify-grab-host.log` |
| 參數快照 | `60_config/current/<節點>_<名稱>`；`history/<yyyyMMdd>/<HHmmss>_<節點>_<名稱>` | `grab_cam_config.json`、`spark1_default_zone.ini` |
| 每日摘要 | `70_knowledge/<yyyyMMdd>_daily.md` | |
| 廠商文件（人工） | `20_docs/vendor/<廠商>_<型號>_<文件類型>_<版本或日期>.<ext>` | `Basler_raL8192-12gm_UserManual_v3.pdf`、`Pleora_iPORT-CL-GigE_DataSheet_2024.pdf` |
| SOP（人工） | `20_docs/sop/<主題>_<yyyyMMdd>.<ext>`（同主題新版改日期，不覆蓋） | `相機更換_20261005.md` |
| 安裝檔 | 保留廠商原檔名 | `pylon_26.05.0-deb0_amd64.deb` |
| Control 包 / 更新包 | `cfaoi-control-win-x64-<git短碼>.zip`、`cfaoi-update-<git短碼>/` | |
| 韌體（人工） | `00_software/firmware/<廠商>_<型號>_<版本>.<ext>` | |

## 手動操作

```bash
python3 ~/Addis/cf-aoi/tools/archive/cfaoi_archive.py            # 立即全部歸檔
python3 ~/Addis/cf-aoi/tools/archive/cfaoi_archive.py logs       # 只更新 log
python3 ~/Addis/cf-aoi/tools/archive/cfaoi_archive.py --dry-run  # 看會做什麼
systemctl status cfaoi-archive.timer
```

設定（保留天數、Spark 位址、限速）：`/etc/default/cfaoi-archive`。
