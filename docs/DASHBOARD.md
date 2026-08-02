# CF-AOI 現況儀表板

> 生成於 **2026-08-02 20:03**（commit `b344617`，branch `claude/worktree-checkbox-chinese-input-9ebc98`）。
> ⚠️ **這個檔是 `scripts/gen_dashboard.py` 生成的，不要手改**——重跑腳本即更新。
> 它的存在理由：回答「現在做到哪、下一步做什麼」不該需要翻 14 萬字的 STATUS.md。

## 一句話

該修的（P0–P2）還有 **40 條**，其中 **P0（到貨日標準動線直接會撞）10 條**、**P1（靜默漏檢／假 PASS）8 條**。另有 P3 低危 42 條、P4 刻意未改 12 條（不列待辦）。

## 缺陷收斂進度

| 級別 | 未修 | 已修 | 總數 | 這一級是什麼 |
|---|---:|---:|---:|---|
| **P0** | 10 | 1 | 11 | 到貨日標準動線直接會撞（建議到貨前修） |
| **P1** | 8 | 0 | 8 | 靜默漏檢／假 PASS 族（本專案最忌；機制「以為有保險其實沒有」） |
| **P2** | 22 | 0 | 22 | 穩定性／資源／操作正確性 |
| **P3** | 42 | 0 | 42 | 低危／衛生／理論項 |
| **P4** | 12 | 0 | 12 | UI／usage／log 字串與實作不符（補註解階段發現，刻意未改） |

<small>來源：`docs/code_review_20260802.md`。P4 是「刻意未改」的字串／文件不符項，不列入待辦。</small>

## P0 未修（10 條）— 到貨日標準動線直接會撞（建議到貨前修）

| # | 端 | 一句話 |
|---|---|---|
| `B4` | grab | --cam-count ALL 無「應到幾台」概念：cam_map 6 筆、只列舉到 5 台 → ARM 回 OK 靜默跑 5 台（fail-fast … |
| `X1` | control | GET_CAM_NODES 不送 cam_id → 永遠讀 cam0 且無錯誤提示（Grab 端 ★4 已修另一半：cam_id 必填+回聲，Contr… |
| `B6` | grab | idle 路徑 GET_CAM_NODES/TUNE_MEAN 的 get_or_open_primary fallback 無視 cam_map：id… |
| `B7` | grab | --cam-count 1 + cam_map 並存：MAC 綁定查回的 cam_id 被 CLI --cam-id（預設 0）無條件蓋掉 → 單台隔離… |
| `K4` | control | 工作台 Step5「套用到其他相機」複製磁碟舊檔：ApplyToTargets 前無 Store.Save()，CopyParamsToIps 讀磁碟 … |
| `K2` | control | 觸發鏈命令全無逾時 + IsBusy 心跳跳過 → 對端 wedge 時 _lock 永不釋放、心跳恆判存活假綠燈、後續 CF_ 全卡死只能重啟 Con… |
| `K3` | control | 上位機 CF_LOAD_RECIPE 配方名拼錯 → EnsureRecipeExists 自動生成預設 DIV 配方照送 IP → 回上位機 OK =… |
| `I8` | ip | 拼接座標 recipe × rdma-process 逐 slice 無座標換算：zone（StartY 可達 146k）超出 slice 範圍被 cl… |
| `I7` | ip | 連續模式（totalSlice≤1）loss_by_cam 永不歸零：一次 CRC 失敗後該 cam 每幀都標 panel_incomplete + 每… |
| `I5=G5` | ip+契約 | 守門路由語意待裁示：legacy enum 值全為 Awc_*_Way_*_Div（皆含 "Way"、無 Awc_8_Way_Star_Div）→ 任何… |

<small>已修：`B1`</small>

## P1 未修（8 條）— 靜默漏檢／假 PASS 族（本專案最忌；機制「以為有保險其實沒有」）

| # | 端 | 一句話 |
|---|---|---|
| `G1=R1` | control | frame_loss（panel_incomplete）無消費端：IsPass => DefectCnt==0、無 FrameLoss 屬性 → 7/3… |
| `I1` | ip | 尾 slice 遺失 → 標記蒸發（frame_loss 只向後傳播；先歸零①後吸收②順序也反了 → 遺失可歸屬到錯的片） |
| `I2` | ip | seq 跳號備援對帳錯：gap > total_lost 拿區間量比累計量 → 早期 CRC 失敗會遮蔽後續真跳號 |
| `B2` | grab | RDMA 送失敗後全相機幀進黑洞：connected_=false 後每幀靜默蒸發、dropped 仍 0、CHECK_HEALTH 無 rdma/er… |
| `K1` | control | CF_GET_RESULT 假 OK：OnGetResult catch 吞例外回 ("","0")、UpstreamServer 無 ERR 路徑恆 … |
| `I3` | ip | GPU 收集階段硬編碼過濾（wrapper 傳 1, 300, 5.0）：>300px 大缺陷、aspect>8 線狀刮傷靜默丟 |
| `I4` | ip | bindTextureObject 只比指標不比尺寸：重配同址 → stale texture 幾何決定性地錯（錯位/越界讀） |
| `B18` | grab | LOAD_RECIPE/GRAB_STOP 無 handler 時仍回 OK（其他命令回 ERR）——靜默成功反模式 |

## 最近做了什麼

| commit | 日期 | 內容 |
|---|---|---|
| `b344617` | 2026-08-02 | refactor(教材) 看碼改索引版：拿掉 1.74MB 內嵌源碼，改開真實檔案（4.95→3.01MB） |
| `f5aaacd` | 2026-08-02 | test(grab) B1 例外圍堵迴歸測試（L1→L2）：pylon stub 注入例外 + 反向對照 |
| `b83fc55` | 2026-08-02 | fix(grab) B1：拔線不再殺整個行程——grab thread 例外三層攔截 + faulted 狀態上報 8100 |
| `53c0e20` | 2026-08-02 | feat(training) Cursor 化 P5+P6：右側 dock（看碼／AI／診斷）+ auto debug 知識庫 |
| `6612074` | 2026-08-02 | feat(training) Cursor 化 P4：Tab 引導式閱讀（ghost text） |
| `02eb254` | 2026-08-02 | feat(training) Cursor 化 P3：⌘P / ⌘K / ⌘⇧F 三分工（sigil 前綴切模式） |
| `b363fcf` | 2026-08-02 | feat(training) Cursor 化 P2：編輯器分頁列 + 修暗色語法色可讀性 |
| `9a35dec` | 2026-08-02 | feat(training) Cursor 化 P1：暗色為預設主題（亮色仍可一鍵切換） |

## 規模：程式碼 vs 文件

| 模組 | 路徑 | 檔 | 行 |
|---|---|---:|---:|
| grab | `grab/src` | 14 | 2,933 |
| ip | `ip/src` | 41 | 11,009 |
| control | `control/src` | 50 | 7,659 |
| **合計** | | | **21,601** |

| 文件 | 行 | 字 |
|---|---:|---:|
| `docs/STATUS.md` | 1,193 | 98,042 |
| `docs/ip_程式完整說明.md` | 915 | 49,348 |
| `docs/control_程式完整說明.md` | 835 | 40,483 |
| `ip/CLAUDE.md` | 478 | 26,134 |
| `docs/grab_程式完整說明.md` | 520 | 25,873 |
| `docs/CLAUDE.md` | 426 | 23,835 |
| `docs/code_review_20260802.md` | 210 | 19,939 |
| `control/CLAUDE.md` | 280 | 13,458 |
| `grab/CLAUDE.md` | 262 | 11,683 |
| `docs/6cam_setup_runbook.md` | 124 | 5,622 |
| **合計** | | **314,417** |

<small>約每行程式碼配 **15 個中文字**的散文（另有教材 HTML 約 93,000 字未計入）。維護成本目前由一個人付，帶新人的收益還沒到帳——擴充文件前先想清楚這筆帳。</small>

## 下一步（P0 未修，由上而下）

1. **B4**（grab）--cam-count ALL 無「應到幾台」概念：cam_map 6 筆、只列舉到 5 台 → ARM 回 OK 靜默跑 5 台（fail-fast 只保護 want>0）
1. **X1**（control）GET_CAM_NODES 不送 cam_id → 永遠讀 cam0 且無錯誤提示（Grab 端 ★4 已修另一半：cam_id 必填+回聲，Control 送 null、回聲也不…
1. **B6**（grab）idle 路徑 GET_CAM_NODES/TUNE_MEAN 的 get_or_open_primary fallback 無視 cam_map：idle 第一發 cam_id=…

<small>完整清單與修法建議見 `docs/code_review_20260802.md`。</small>

---

**其他入口**：架構契約 `docs/CLAUDE.md`｜誠實帳本 `docs/STATUS.md`｜現場架設 `docs/6cam_setup_runbook.md`｜互動教材 `docs/html/cf-aoi-training.html`。
