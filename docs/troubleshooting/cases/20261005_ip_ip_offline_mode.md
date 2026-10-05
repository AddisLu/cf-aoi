# 調參後隔天送料沒結果、IP 燈是綠的 → IP 停在調參模式

- 日期：2026-10-05　來源：實驗室故障注入（tools/triage/fault_inject.py、host_sudo_test.sh）　機台：user-IMB-M47 + spark-c16f + HPE 5945（借用機）
- 分類：ip　注入：{}　已還原：是（還原後健檢：正常）

## 線上人員看到的（原話）
昨天工程師在調參數，今天開線送料後一直沒有結果，Control 的 IP 燈是綠的。

## 證據：一鍵健檢結論
## 結論：❌ 1 項異常、⚠️ 0 項注意、✅ 6 項正常

1. ❌ **[RDMA] Spark 上的 IP 停在「調參模式」，生產用的收圖沒開（不是線的問題）**
   - 可能原因：調機/調參後沒切回生產模式（生產與調參互斥）
   - 請這樣做：Control 系統狀態 → Spark →「IP 生產」按「切到此模式」（線上人員可先做）

（完整報告：`docs/troubleshooting/experiments/20261005_round2/ip_offline_mode.md`）

## 根因
Spark 的 IP 在 cfaoi-ip-offline（調參），生產用 rdma-process 沒開；調參模式也回應控制命令所以 IP 燈綠

## 處置
1. Control 系統狀態 → Spark →「IP 生產」→「切到此模式」
2. 重跑健檢確認 RDMA ✅

## 學到的 / 預防
「IP 燈綠但沒結果」不是連線問題，看是哪個模式在跑

## 機況助手表現（0–2 分）
- 只給症狀（A）：**1** — 大致方向（生產服務沒跑）但叫線上人員在 Spark 打 sudo systemctl
- 症狀 + 健檢報告（B）：**2** — 正確，一步到位

<details><summary>助手 A 回答原文</summary>

**結論：上位機（Spark）上的 IP 服務（`cfaoi-ip-production`）可能沒有正常運作**，但 Control 這端的 IP 燈是綠的——這表示 Control 與 Spark 之間的連線正常，問題出在 **Spark 上的 IP 程式沒有在跑**（inactive），所以 RDMA 埠 18515 沒在聽、IP 控制埠也沒回應，導致上位機送料後沒有結果回來 [8]。

## 可能原因

1. **IP 服務（`cfaoi-ip-production`）意外停止或當掉** [2] — 昨天調參數時可能關閉或改動了相關服務，重開機後沒自動啟動。
2. **Control 設定指向的 IP 節點不正確** — 例如之前有人改過 `control/src/appsettings.json` 中的 `IpOffline` 指向，導致 Control 連到錯誤的位置 [3]。

## 現場照做（線上人員可操作）

### 步驟 1：確認 IP 服務狀態
到 **Spark 主機**（上位機那台）開啟終端機，輸入：
```bash
systemctl status cfaoi-ip-production
```
- 如果看到 **inactive / dead** → 服務已停止。
- 如果看到 **active (running)** → 服務還在跑，請通知工程師。

### 步驟 2：重啟 IP 服務（若上一步顯示停止）
```bash
sudo systemctl restart cfaoi-ip-production
```
等一下觀察 Control 是否正常連上。

### 步驟 3：若服務正常但問題仍在 → 通知工程師
檢查 `control/src/appsettings.json` 中的 `IpOffline` 設定是否正確，以及昨天的調參是否有改動到其他設定 [3]。

## 怎麼確認修好

- **重新送料後，上位機能正常收到結果回來** [1][2]。
- 如果 Control 的 IP 燈是綠的但上位機仍沒結果，代表 IP 服務本身沒在跑，重啟後應可恢復。

</details>

<details><summary>助手 B 回答原文</summary>

健檢報告很清楚了，結論就是 **Spark 上的 IP 停在「調參模式」，生產用的收圖沒開**，不是線路或硬體的問題。

---

### 可能原因

昨天工程師調參後，IP 切到了「調參服務」（active），但**沒有切回「生產服務」**就關機下班了。今天開線時 IP 仍然在調參模式，不會收圖送料，所以一直沒結果 [8]。

證據：Spark 端 `IP 生產服務 inactive`、`調參服務 active`，RDMA 埠 18515 沒在聽，但線、網卡、RoCE 全都正常 [4]。

---

### 現場照做步驟（線上人員可先做）

1. 到 **Control 系統狀態 → Spark**。
2. 找到「**IP 生產**」。
3. 按「**切到此模式**」[8]。

---

### 怎麼確認修好

等約 10–20 秒，再按一次**一鍵健檢**，看 RDMA 項目變 ✅ 綠色，正常送料後就會開始出結果了 [8]。

</details>
