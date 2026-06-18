# Step 1 Control UI — Mac 目視驗收清單

> 目的：把 STATUS.md 裡因「未經 Mac 目視 / 無自動 selftest」而被誠實降級的 Control UI 項目，整理成可逐項勾選的驗收單。
> 在 **Mac 上跑 Control 連 Linux IP（offline-tcp）** 逐項確認；每項通過後即可把 STATUS.md 對應列升級。
> 範圍：**Step 1（offline 演算法驗證）**。Step 2（單 CCD 傳輸 rdma-validate）功能本體已 L3/L4，無待目視項。
>
> 對應源碼：[Step1View.axaml](../../control/src/Views/Step1View.axaml) / [Step1View.axaml.cs](../../control/src/Views/Step1View.axaml.cs) /
> [ZoneParamEditorView.axaml](../../control/src/Views/ZoneParamEditorView.axaml) / [MainWindow.axaml](../../control/src/Views/MainWindow.axaml)
>
> 圖例：☐ 待驗 · ✅ 通過 · ❌ 有問題（記下現象）

---

## A. 影像 Viewer 互動（STATUS L43 · 現 L1 → 目標 L3）

開「離線分析工具 (Algorithm Test)」→ Load Image 載一張 TIFF。

| # | 操作 | 預期結果 | 結果 |
|---|------|---------|------|
| A1 | 載圖後 | 影像自動 Fit 置中於黑底區；底部狀態列顯示 `ImageSize`、`Zoom : 1.xx x` | ☐ |
| A2 | 滑鼠滾輪上/下 | 以游標為中心縮放（游標下的像素點不動）；`Zoom` 數字跟著變 | ☐ |
| A3 | 按 `F` 鍵 或 點 `Fit` 鈕 | 影像回到 Fit 置中 | ☐ |
| A4 | 中鍵拖曳，或 按住空白鍵+左鍵拖曳 | 影像平移，放開停住 | ☐ |
| A5 | 滑鼠移到影像上 | 狀態列 `Axis : (x,y)` 顯示原始像素座標、`Value : v` 顯示該點灰階值；移出影像顯示 `-` | ☐ |
| A6 | 按住 `M` 鍵，左鍵點兩點 | 兩紅圈+連線；狀態列 `Region` 顯示 `dx= dy= d=`（量 Pitch 用）；點第三點會清空重量 | ☐ |

> 全 6 項過 → STATUS「影像 Viewer 互動」**L1 → L3**。

---

## B. 缺陷標示 overlay 圓圈/導航（STATUS L45 · 現 L1 → 目標 L3）

延續上圖，按 `Test` 跑 IP（看到 DefectCnt > 0、縮圖牆出現小圖）。

| # | 操作 | 預期結果 | 結果 |
|---|------|---------|------|
| B1 | Test 完成 | 大圖上每個缺陷畫圓圈（亮紅/暗藍）；底部 `DefectCnt` 顯示數量（觸頂 10000 變色） | ☐ |
| B2 | 點縮圖牆某張小圖 | 大圖跳轉置中該缺陷(~5x)、該圈高亮綠、縮圖同步選中 | ☐ |
| B3 | 按 `←` / `→` 鍵 | 在缺陷間前後導航，大圖置中+縮圖捲動同步 | ☐ |
| B4 | 直接點大圖某個缺陷框內 | 選中該缺陷（縮圖牆捲到它、圈高亮），大圖不亂跳 | ☐ |
| B5 | 點「刷新標示」鈕 | 不重跑 Test，依現有缺陷清單重畫圓圈 | ☐ |
| B6 | 關掉視窗再重開 | 不重跑 Test，圓圈標示自動從記憶體重建 | ☐ |

> 全 6 項過 → STATUS「缺陷標示 overlay」**L1 → L3**。

---

## C. 視覺 ROI 框選 #8（STATUS L241 / L42 · 現 L1 → 目標 L3）

延續有圖狀態。ROI 寫回的是 `RecipeStore.PrimaryZone` 的 StartX/Y/EndX/Y（單一資料來源）。

| # | 操作 | 預期結果 | 結果 |
|---|------|---------|------|
| C1 | 點「框 ROI」鈕 | 鈕變綠（進入框選模式）；狀態列 `ROI 框選中：拖矩形` | ☐ |
| C2 | 在影像上左鍵拖一個矩形 | 拖曳時黃框跟隨；放開後變藍框；狀態列顯示 `ROI (x0,y0)-(x1,y1)` | ☐ |
| C3 | 拖一個太小的框（<2px） | 忽略，不寫回 | ☐ |
| C4 | 框完後縮放/平移 | 藍框跟著影像一起變換，貼齊同一像素範圍 | ☐ |
| C5 | 點「ROI 全幅(-1)」鈕 | 藍框消失；StartX/Y/EndX/Y 歸 -1（全幅）；狀態列 `ROI 全幅(-1)` | ☐ |
| C6 | 框 ROI 後開 ZoneParamEditor 看 StartX/Y/EndX/Y | 數值 = 剛框的範圍（雙向同步）；反過來在 Editor 改數值，Step1 藍框即時跟著動 | ☐ |
| C7 | 再按一次「框 ROI」 | 退出框選模式（鈕恢復、可正常縮放/點選缺陷） | ☐ |

> 全項過 → STATUS #8 / Step1View ROI **L1 → L3**。

---

## D. 多 IP/CCD 配方單一入口 #6（STATUS L239 · 現 L2 → 目標 L3）

開「配方編輯 (IP Param Editor)」。

| # | 操作 | 預期結果 | 結果 |
|---|------|---------|------|
| D1 | 頂列看 IP/CCD 下拉 | 預設有 IP0（多台時 appsettings `RecipeIps` 擴充）；旁邊有配方下拉 | ☐ |
| D2 | 切換 IP/CCD 下拉 | 參數重載成該台 `{recipe}/{IP}/RecipeInfo.xml`（不同 IP 值互相隔離） | ☐ |
| D3 | 改某 IP 的值→儲存配方→切到別 IP 再切回 | 值持久且 per-IP 隔離（已有 `--selftest store` 佐證，此處目視確認 UI 流程） | ☐ |

> 全項過 → STATUS #6 **L2 → L3**。

---

## E. ZoneParamEditor 27 列表單版面（STATUS L46 · 現 L2 → 目標 L3）

同「配方編輯」視窗。資料同步已有 `--selftest store`，此處只目視版面。

| # | 操作 | 預期結果 | 結果 |
|---|------|---------|------|
| E1 | 看中欄參數列 | 參數列完整顯示，每列 = 勾選框 + 名稱 + 輸入(text/開關/下拉) + IP待接標記 + Update 鈕，不重疊不被切 | ☐ |
| E2 | 右欄 ROI 清單 | 列出各 ROI，可勾選(套用目標)、點選(載入該 ROI 參數) | ☐ |
| E3 | 左欄 Region | StartX/Y/EndX/Y 數字框 + 「套用範圍到勾選 ROI」+ 批次位移 X±/Y±（步進可調） | ☐ |
| E4 | 勾幾列參數 → Update Params；勾「批次更新前確認」再 Update | 黃色確認列彈出（確認套用/取消）；Clear All / Select All 正常 | ☐ |
| E5 | 改值後「儲存配方」 | 底部狀態列回報成功 | ☐ |

> 全項過 → STATUS ZoneParamEditor **L2 → L3**。

---

## F. MainWindow 主視窗版面（STATUS L41 · 現 L2 → 目標 L3）

| # | 操作 | 預期結果 | 結果 |
|---|------|---------|------|
| F1 | 主視窗開啟 | 左欄 Status/Reserve/系統log/Error-Warning 分頁；右欄 Config/Recipe/ShareSetting/RecipeSetting 2×2，皆不被切 | ☐ |
| F2 | 看 Status 區三顆連線燈 | IP / Grab / 上位機 三燈，連上=綠、未連=紅（IP 連上後應綠） | ☐ |
| F3 | 點「顯示/隱藏進階 (Ctrl+F)」 | 進階 CF 按鈕列展開/收合；停用鈕(Grab/Check/Set Align/Stop) 灰且有 tooltip | ☐ |
| F4 | ShareSetting 區改 SaveSourceImage/DebugAlgorithm/AiRootPath → 儲存 | 寫回 appsettings.json（已有 `--selftest settings`，此處目視 UI） | ☐ |
| F5 | RecipeSetting 區（標「IP 待接」）改 MaxSaveDefectCount 等 → 儲存 | 寫回 `{recipe}/RecipeSetting.xml`；MaxDefectCountPass 顯示唯讀（IP固定） | ☐ |

> 全項過 → STATUS MainWindow **L2 → L3**。

---

## G. 連線心跳 / DefectSort UI（STATUS L50 / L51 · 現 L2 → 目標 L3）

| # | 操作 | 預期結果 | 結果 |
|---|------|---------|------|
| G1 | 起 IP 後再停 IP，看 Status IP 燈 | 綠 → 紅（CHECK_HEALTH 心跳偵測）；重啟 IP 後自動回綠 | ☐ |
| G2 | 開「缺陷整理 (Sort Defect)」Layer1 | LIST 列出 panel 夾；SORT 正常（已有 selftest，目視跨機真實 IP） | ☐ |
| G3 | 雙擊 panel 夾進 Layer2 小圖分類 | 縮圖牆顯示；T/P 標 TrueDefect/Particle、←→ 切換、即時持久化（離開重開續標未標的） | ☐ |
| G4 | 切 filter（全部/未分類/TrueDefect/Particle）；確認中文不亂碼 | filter 正確、UTF-8 正常、統計數字對 | ☐ |

> G2–G4 過 → DefectSort(Control UI) **L2 → L3**；G1 過 → 連線心跳 **L2 → L3**。

---

## 驗收後動作

逐項打勾後，把通過的列在 [STATUS.md](../STATUS.md) 對應行把級別改成 **L3**，並在「驗證方式」欄補一句「2026-MM-DD Mac 目視通過」。若某項 ❌，記下現象（截圖/說明）回報即可。
