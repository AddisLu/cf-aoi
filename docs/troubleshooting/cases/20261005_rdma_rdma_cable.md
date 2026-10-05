# IP 燈紅、Spark 燈亮 → Grab↔Spark 直連線沒有 link（線的問題）

- 日期：2026-10-05　來源：實驗室故障注入（tools/triage/fault_inject.py；RDMA 線斷為 rdma_cable_test.sh）　機台：user-IMB-M47 + spark-c16f + HPE 5945（借用機）
- 分類：rdma　注入方式：Grab RDMA 網卡 link down（sudo）　已還原：是（還原後健檢：正常）

## 線上人員看到的（原話）
上位機送料後沒有結果，Control 的 IP 燈是紅的，Spark 前面燈有亮。到底是線還是 Spark 壞了？

## 證據：一鍵健檢結論
## 結論：❌ 1 項異常、⚠️ 1 項注意、✅ 0 項正常

1. ❌ **[RDMA] Grab ↔ Spark 直連線沒有 link（線的問題，或 Spark 沒開機）**
   - 可能原因：這條線兩端都沒訊號：① Spark 關機/當機（看 Spark 電源燈）② 線或光模組鬆脫/損壞 ③ 網卡故障
   - 請這樣做：① 確認 Spark 電源燈亮、風扇有轉 ② 重插 Grab 與 Spark 之間的線（兩端）③ 換備用線 → 若換線就好 = 線壞；換線仍無 link 且 Spark 有開機 = 網卡/機器問題（設備工程）
2. ⚠️ **[GPU] 無法登入 Spark 查 GPU**
   - 可能原因：見 RDMA 項目

（完整報告：`docs/troubleshooting/experiments/20261005_fault_injection/rdma_cable.md`、JSON：`rdma_cable.triage.json`）

## 根因
直連線兩端沒訊號（現場：線/光模組鬆脫或損壞；Spark 有開機）

## 處置
1. 重插 Grab ↔ Spark 線兩端
2. 換備用線：換線就好 = 線壞；仍無 link = 網卡/機器

## 學到的 / 預防
線斷時 Spark 也連不上，健檢只能從 Grab 端判斷；「Spark 燈有亮」這句線上人員的觀察很關鍵

## 機況助手表現（0–2 分）
- 只給症狀（A，舊知識庫）：**0** — 叫人查 appsettings 的 Tailscale 位址（錯且離題）
- 症狀 + 健檢報告（B，新知識庫）：**2** — 正確，給出換線判斷流程

<details><summary>助手 A 回答原文</summary>

**結論：Spark 本身應該是好的（前燈有亮代表它活著），問題出在 Control 與 Spark 之間的連線（IP 燈紅）。** 這有兩個可能：

1. **CCD 線材或交換機埠不穩**（例如封包遺失、抖動過大），造成心跳逾時 [1][4]。
2. **Control 設定指向的 IP 節點已離線或不存在**（例如之前有 commit 把 IpOffline 改指向家中驗證機，如果在公司環境就會連不上）[6]。

---

### 現場照做步驟（可由線上人員執行）

**第一步：檢查 Control 指向哪個 IP**
- 打開 Control 所在電腦的 `control/src/appsettings.json`，看 `IpOffline` 或 `ActiveIpNode` 的 IP 位址和埠號。
- 如果是 `addis-b850m-ds3h.tailffdb68.ts.net:8200` 之類的 Tailscale 域名，代表是指到 Addis 家裡的機器——**在公司環境一定連不上**，要改回正確的 Spark IP。

**第二步：測試通訊品質（如果 IP 是對的）**
- 從 Control 電腦對 Spark 的 IP 執行 `ping -t`，看有無封包遺失、RTT 是否穩定。
- 如果封包遺失 >1% 或 RTT 破數百毫秒，是網路問題（線、交換機、或臨時用手機熱點）[1][4]。

**第三步：檢查心跳 log**
- 看 Control 的 log 是否有反覆「連線中斷/已重新連線」——如果是，代表網路抖動但沒完全斷（新版需連續 2 次失敗才判定斷線，判定時間約 12.5 秒）[1][4]。
- 如果 log 只有一條「IP 連線中斷」且沒自動恢復，是完全斷線。

---

### 怎麼確認修好

1. **IP 燈轉綠**，且 log 不再洗「連線中斷/已重新連線」。
2. Control 能對 Spark 送圖取回結果（`DefectCnt` 正常）[6]。
3. 上位機重新送料後能正常收到結果回傳。

</details>

<details><summary>助手 B 回答原文</summary>

**結論：問題出在 Grab 與 Spark 之間的直連線（RDMA 線），不是 Spark 整台壞掉。**  
Control IP 燈紅 = 控制面沒收到 Grab 的狀態（因為 RDMA 不通），但 Spark 前面燈有亮表示它電源正常、有開機。[1][2]

---

## 可能原因（依機率）

1. **Grab↔Spark 直連線鬆脫或光模組沒插緊** — 健檢報 Grab 端 `enp1s0f0np0` 完全沒有 link（link 無、速率 ? Mb/s）[1]
2. **線材或光模組損壞** — 兩端都沒訊號
3. **Spark 關機/當機** — 但使用者已確認 Spark 燈有亮，所以機率低
4. **Grab 或 Spark 的 RDMA 網卡故障** — 如果線換過仍無 link 才考慮

---

## 現場照做（順序做）

### 誰可以做：線上人員（不用等設備工程）

1. **到機台後方，找到 Grab 主機（截取中心 PC）與 Spark（上方那台）之間的 RDMA 直連線**（通常是光纖或銅纜，兩端接在 100G 網卡）
2. **先把兩端插頭拔出來，再重新插到底**，確認卡榫有卡緊
3. **看 Grab 端網卡的 link 燈**（在 `enp1s0f0np0` 這個埠）：
   - 燈亮了 → 線接觸問題，已排除
   - 燈不亮 → 下一步
4. **換一條備用 RDMA 線**（兩端都換）：
   - 換線後 link 燈亮 → 原線壞了，用備用線繼續生產
   - 換線後仍不亮 → 通知設備工程（可能是網卡或機器問題）

---

## 怎樣算修好

- Control 的 **IP 燈變綠**
- 上位機送料後正常出結果（不再有「沒有結果」的情況）
- 或者現場再跑一次「一鍵健檢」，RDMA 項目變 **✅ PASS**（`PORT_ACTIVE`、ping 通、大封包通）[4]

</details>
