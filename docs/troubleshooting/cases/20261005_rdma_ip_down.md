# IP 燈紅、沒結果 → 不是線，是 Spark 上 IP 程式沒在跑

- 日期：2026-10-05　來源：實驗室故障注入（tools/triage/fault_inject.py；RDMA 線斷為 rdma_cable_test.sh）　機台：user-IMB-M47 + spark-c16f + HPE 5945（借用機）
- 分類：rdma　注入方式：Spark 停止 cfaoi-ip-production　已還原：是（還原後健檢：正常）

## 線上人員看到的（原話）
上位機送料後一直沒有結果回來，Control 的 IP 燈是紅的，Grab 燈是綠的。是不是 Grab 跟 Spark 中間的線壞了？

## 證據：一鍵健檢結論
## 結論：❌ 1 項異常、⚠️ 0 項注意、✅ 5 項正常

1. ❌ **[RDMA] 線和網路都正常，是 Spark 上的 IP 程式沒在跑（機器/程式問題，不是線）**
   - 可能原因：IP 服務停止或反覆當掉
   - 請這樣做：Control 系統狀態 → Spark →「IP 生產」重新啟動；反覆失敗看 log／收診斷包（線上人員可先做）

（完整報告：`docs/troubleshooting/experiments/20261005_fault_injection/ip_down.md`、JSON：`ip_down.triage.json`）

## 根因
IP 生產服務停止（線、link、ping、大封包、RoCE 全正常）

## 處置
1. Control 系統狀態 → Spark →「IP 生產」重新啟動
2. 反覆當掉 → 收診斷包給工程

## 學到的 / 預防
健檢同時看兩端 link/ping/RoCE/服務 → 分辨「線」與「機器/程式」；RDMA CM 監聽要用 rdma resource（ss 看不到）

## 機況助手表現（0–2 分）
- 只給症狀（A，舊知識庫）：**1** — 「不是線」碰巧對，但叫人重插 Control 網線、重開 Control，沒處理 IP 服務
- 症狀 + 健檢報告（B，新知識庫）：**2** — 正確；但把無關的開發程式 Xid 牽扯進來（誤導）

<details><summary>助手 A 回答原文</summary>

**結論：不是 Grab 跟 Spark 之間的線路問題。** 燈號顯示 **IP 連線中斷**（IP 燈紅），Grab 正常（燈綠），問題出在 Control ↔ IP（Spark）之間的連線，不是 Grab ↔ Spark。[1][2]

---

### 可能原因

1. **網路抖動或瞬斷**：若網路封包遺失率高或 RTT 過大（例如跑在手機熱點或無線環境），心跳可能被觸發斷線。舊版曾經 2s 逾時＋單次失敗就斷，目前已改為 **5s 逾時 × 連續 2 次失敗** 才判定斷線（最慢約 12.5s 判定）[2][4]

2. **IP 端的 ControlServer 忙碌或未啟動**：IP 的 ControlServer 是單客戶端序列處理，若有人佔住連線（例如診斷腳本），Control 就會連不上 [7]

3. **實體線路（Control ↔ 交換機 ↔ IP）接觸不良或鬆脫**

4. **IP 端程式當掉或未正常運作**

---

### 現場照做步驟（誰可以做）

**現場人員（可操作）：**

1. **先檢查網路線**：確認 Control 主機到交換機、交換機到 Spark（IP 機）的網路線是否有鬆脫或燈號不亮
2. **檢查 Control App 的 Status 區三盞燈**：IP（紅）、Grab（綠）、上位機——目前 IP 紅代表 Control 收不到 IP 心跳 [1]
3. **重插網路線**：兩端拔起重插，確認交換機埠燈號正常
4. **重新啟動 Control App**：關掉重開，看 IP 燈是否變綠
5. **若持續紅燈**：請通知工程師檢查 IP（Spark）端程式是否正常運作、ControlServer 是否有 Listen

---

### 怎麼確認修好

- Control 主視窗 **Status 區的 IP 燈轉為綠色**，log 出現「IP 已連線」[2]
- 重新送料後，上位機能正常收到結果回來 [1]

</details>

<details><summary>助手 B 回答原文</summary>

報告告訴你答案了：**不是線壞掉。**

健檢結果第 1 項明確寫著：

> ❌ **線和網路都正常，是 Spark 上的 IP 程式沒在跑（機器/程式問題，不是線）**[1]

Grab ↔ Spark 之間的實體線（Grab enp1s0f0np0：link 有、100000Mb/s、RoCE ACTIVE；ping Spark 通、大封包也通）全部正常，問題出在 Spark 那一端的 **IP 生產服務（cfaoi-ip-production）沒有在跑（inactive）**，所以 RDMA 埠 18515 沒在聽、IP 控制埠也沒回應 —— 這就是為什麼 Control 的 IP 燈是紅的，而上位機送料沒有結果回來。

---

### 可能原因
1. **IP 生產服務停止或反覆當掉** — 可能是程式 crash、記憶體不足被 OOM killer 終止，或 GPU 異常（健檢報了 105 筆 GPU Xid 錯誤，包含 Xid 13/31/43）導致服務被系統停掉。

---

### 現場照做步驟（線上人員可先做）

1. 到 Control 系統狀態 → Spark → 找到「IP 生產」服務。
2. 按「重新啟動」啟動 cfaoi-ip-production 服務。
3. 等約 10–20 秒後，跑一次一鍵健檢確認 IP 燈變綠。

---

### 怎麼確認修好？
- 重新跑一次一鍵健檢：RDMA 那一項變成「✅ Grab ↔ Spark RDMA 正常（線、兩端網卡、IP 程式都正常）」[1]。
- Control 的 IP 燈從紅轉綠。
- 上位機再送一片料應該就會有結果回來。

---

如果重新啟動後又馬上掛掉（反覆當掉），請收 log 和診斷包交給設備工程，因為健檢也發現最近 7 天有 105 筆 GPU 程式錯誤紀錄（Xid 13/31/43），可能跟 GPU 那邊有關。

</details>
