# 全部相機不見 → 交換機上行到 Grab 斷線

- 日期：2026-10-05　來源：實驗室故障注入（tools/triage/fault_inject.py；RDMA 線斷為 rdma_cable_test.sh）　機台：user-IMB-M47 + spark-c16f + HPE 5945（借用機）
- 分類：switch　注入方式：交換機上行埠 HGE1/0/25 shutdown　已還原：是（還原後健檢：正常）

## 線上人員看到的（原話）
開機後 Control 顯示所有相機都沒有影像，Grab 燈號是綠的。

## 證據：一鍵健檢結論
## 結論：❌ 3 項異常、⚠️ 1 項注意、✅ 3 項正常

1. ❌ **[相機] 全部 6 台相機都不見了 → 問題在共用的那一段，不是個別相機**
   - 可能原因：Grab ↔ 交換機 100G 上行線斷/模組鬆脫、交換機沒電或當機、或交換機設定被重置
   - 請這樣做：① 看交換機電源燈與 Grab 後面 100G 線燈號 ② 重插 Grab ↔ 交換機 100G 線兩端 ③ 相機總電源 ④ 仍不行找工程看交換機設定（見交換機項目）（設備工程）
2. ❌ **[交換機] 交換機 → Grab 上行埠 HGE1/0/25 斷線（ADM）**
   - 可能原因：100G 線/模組鬆脫或損壞、或交換機埠被關閉 → 所有相機都會不見
   - 請這樣做：重插 Grab 與交換機之間的 100G 線兩端；仍不行換線（設備工程）
3. ❌ **[交換機] 交換機設定和基準不同（1 行）—很可能就是 CCD01、CCD02、CCD03、CCD04、CCD05、CCD06 不見的原因**
   - 可能原因：有人改過交換機設定，或交換機更換/重置/升級後設定沒還原（例：相機埠少了 speed 1000 → 1G 相機永遠連不上）
   - 請這樣做：工程人員對照差異還原（相機埠需 speed 1000、上行 100G、jumbo），再 save force；換新交換機見 docs/troubleshooting_線上異常處理.md「交換機更換」（設備工程）
4. ⚠️ **[交換機] 交換機有埠被手動關閉（shutdown）：HGE1/0/25**
   - 可能原因：有人下了 shutdown，或換交換機後設定不同 → 相機插在這些埠會沒有訊號
   - 請這樣做：相機改插其他埠；或工程人員 undo shutdown（設備工程）

（完整報告：`docs/troubleshooting/experiments/20261005_fault_injection/uplink_down.md`、JSON：`uplink_down.triage.json`）

## 根因
Grab ↔ 交換機 100G 上行中斷（現場同類：100G 線/模組、交換機沒電/當機/被重置）

## 處置
1. 看交換機電源燈、Grab 後方 100G 線燈號
2. 重插/換 100G 線兩端
3. 上行被關閉 → 工程 undo shutdown

## 學到的 / 預防
健檢把「全部不見」歸成一個根因（共用段），不逐台列；只靠症狀助手會往綁定/ARM 方向猜

## 機況助手表現（0–2 分）
- 只給症狀（A，舊知識庫）：**0** — 往綁定、ARM、cam_map 方向猜，完全沒提上行
- 症狀 + 健檢報告（B，新知識庫）：**2** — 正確指出上行被 shutdown（另多編了一條「設 speed 100000」）

<details><summary>助手 A 回答原文</summary>

## 🔍 結論

**Grab 燈號綠**表示 `cfaoi_grab` 程式有正常執行、與 Control 的 8100 TCP 連線正常，但**所有相機無影像**，問題不在程式層，而在**相機端或傳輸路徑**。

---

## 可能原因（依檢查順序排列）

| 原因 | 說明 |
|------|------|
| 1️⃣ **相機未列舉到（LIST_CAMERAS 回傳為空）** | 相機電源未開、pylon 驅動未載入、或 GigE 網路不通 [8] |
| 2️⃣ **CCD 未綁定（cam_map.json 未建立或不正確）** | 即使相機有列舉到，也要執行 SET_CAM_MAP 把 MAC → cam_id / ccd_id 綁定，Control 的相機陣列總覽才會顯示影像 [5] |
| 3️⃣ **未執行 GRAB_ARM / GRAB_START** | Grab 狀態機必須走 IDLE → GRAB_ARM（開陣列+RDMA 預熱）→ GRAB_START（觸發）才開始送幀 [1][3] |
| 4️⃣ **GevSCPSPacketSize / MTU 不匹配** | 相機封包大小設 9000，若交換機或 NIC 未開 jumbo frame（9416），封包會被丟棄 [3] |
| 5️⃣ **線材或交換機埠位異常** | 線材上的 CCD 標示可能與交換機埠位不再對應；交換機重新拔插後埠位會變 [5] |

---

## 👷 現場照做步驟（可由線上人員操作）

### Step 1：確認相機列舉
在 damac 終端機執行：
```bash
cfaoi_grab --cam-count 0
# 或直接看 pylon Viewer 能不能找到相機
```
👉 如果 **pylon Viewer 也看不到相機** → 檢查相機電源（12V 指示燈）與 GigE 網線

### Step 2：執行 LIST_CAMERAS 看列舉結果
在 Control 的 SystemSettings → 相機 tab，點「重新整理」：
- 看**實體陣列**一欄有沒有 CCD 出現（已綁定=綠色、待綁定=琥珀色、離線=灰色）[8]
- 如果全部空白 → 相機網路不通

### Step 3：綁定 CCD（若列舉到但未綁定）
在 **SystemSettings → 相機 tab** 中，選取待綁定的 CCD，按「綁定」[5]。
> 綁定後會自動執行 `SET_CAM_MAP`，寫入 `cam_map.json`。

### Step 4：確認 Grab 狀態機
在 Control 端檢查 Grab 連線狀態：
- 若 Grab 燈號綠、但無影像，從 Control 發 **GRAB_ARM**（讓 Grab 提前開相機+連 RDMA）→ 再發 **GRAB_START**[1][3]

### Step 5：檢查交換機與 MTU
- 確認交換機埠 **Jumbo Frame** 設定為 **9416**（非 1514 或 1500）
- 確認 damac NIC MTU = 9000（`ip link show | grep mtu`）[3]

---

## ✅ 怎麼確認修好

1. **LIST_CAMERAS** 回傳的相機數量 = 實際接線的 CCD 數量
2. **每台 CCD 在 Control 相機總覽中顯示為「已綁定（綠色）」**[8]
3. 執行 `GRAB_START` 後，Control 收到影像，`grabbed` 計數持續增加 [3]
4. 若以上都對但還是沒影像 → 檢查 `--pkt-size`、`--line-rate` 等 CLI 參數是否正確設定 [3]

</details>

<details><summary>助手 B 回答原文</summary>

## 結論

**交換機上行埠 HGE1/0/25 被手動關閉（shutdown）**，導致 Grab ↔ 交換機之間的 100G 連線中斷，所以全部 6 台 CCD01–CCD06 相機都看不到。

---

## 可能原因

1. **有人對交換機下了 `shutdown` 指令** – 設定了 `interface HundredGigE1/0/25` 然後 `shutdown`，導致該埠不送訊號 [1]。
2. **交換機更換/重置後設定沒還原** – 新交換機或重置後，上行埠處於預設關閉狀態，或相機埠缺少 `speed 1000` 設定 [1]。
3. **100G 線/光模組鬆脫或損壞** – 雖然健檢顯示埠狀態是 `ADM`（被管理關閉）而非 `DOWN`（斷線），但不排除硬體也有問題。

---

## 現場照做步驟

### 👷 設備工程（或懂交換機的人）

1. **確認交換機電源燈有亮**，Grab 後面接交換機的那條 100G 線兩端燈號有亮。
2. **對交換機下 `undo shutdown`**，讓 `HGE1/0/25` 恢復啟用。
3. **檢查設定**：確認相機埠（建議 WGE1/0/33-38）有設 `speed 1000`，上行埠 `HGE1/0/25` 有設 `speed 100000` 及 `jumbo` [1]。
4. **執行 `save force`** 儲存設定，避免下次重開機又跑掉。
5. **重插 Grab ↔ 交換機 100G 線兩端**（光模組也重插）；若仍不行，換一條線試試。

---

## 怎麼確認修好

- Grab 燈號維持綠色（表示與交換機連線正常）。
- Control 畫面恢復顯示 CCD01–CCD06 的即時影像。
- 再次按「一鍵健檢」，相機與交換機項目應顯示 ✅ 正常。

</details>
