# IP 燈紅、Grab 綠、Spark 有開、重開 Control 也一樣 → Grab 轉送（ip_forward）被關

- 日期：2026-10-05　來源：實驗室故障注入（tools/triage/fault_inject.py、host_sudo_test.sh）　機台：user-IMB-M47 + spark-c16f + HPE 5945（借用機）
- 分類：host　注入：{"cmd": "sudo sysctl -q -w net.ipv4.ip_forward=0"}　已還原：是（還原後健檢：正常）

## 線上人員看到的（原話）
Control 上 IP 的燈一直是紅的，Grab 的燈是綠的；Spark 有開機，重開 Control 也一樣。

## 證據：一鍵健檢結論
## 結論：❌ 1 項異常、⚠️ 0 項注意、✅ 1 項正常

1. ❌ **[主機] Grab 的轉送功能關閉（ip_forward=0）→ Control 連不到 Spark 的 IP**
   - 可能原因：Grab 系統設定被改或重灌後沒設（Control 到 Spark 要經過 Grab 轉送）
   - 請這樣做：找工程開啟（/etc/sysctl.d 的 net.ipv4.ip_forward=1）；暫時可重開 Grab（設備工程）

（完整報告：`docs/troubleshooting/experiments/20261005_round2/ip_forward_off.md`）

## 根因
Control → Spark 要經 Grab 轉送；Grab ip_forward=0 → Control 連不到 IP

## 處置
1. 暫時：重開 Grab（從設定檔重新套用）
2. 永久：工程確認 /etc/sysctl.d 有 net.ipv4.ip_forward=1

## 學到的 / 預防
IP 燈紅不一定是 Spark 或線：控制網拓樸是 Control → Grab（轉送）→ Spark

## 機況助手表現（0–2 分）
- 只給症狀（A）：**0** — 說成 IP 服務沒跑（錯，重啟 IP 不會好）
- 症狀 + 健檢報告（B）：**2** — 正確，含暫時/永久解法

<details><summary>助手 A 回答原文</summary>

**結論：問題不是硬體線路，是 Spark 上的 IP 生產服務（cfaoi-ip-production）沒有在跑。**  
Control IP 燈紅 + Grab 燈綠 + Spark 有開機、重開 Control 無效，代表實體線路和 Grab 程式都正常，但 Spark 端的 IP 服務未啟動，導致 Control 收不到 IP 心跳 [2][6]。

---

### 可能原因
1. **cfaoi-ip-production 服務未啟動（inactive）** — 這是健檢報告指出的直接原因 [6]。
2. 服務因某種原因崩潰或被手動停止，沒有自動重啟。

---

### 現場照做步驟（線上人員可操作）

1. **到 Control 系統狀態 → Spark → 找到「IP 生產」服務。**
2. **按「重新啟動」，啟動 cfaoi-ip-production 服務。** [2]
3. **等約 10–20 秒。**

---

### 怎麼確認修好
- Control 主視窗 **Status 區的 IP 燈轉為綠色**，log 出現「IP 已連線」 [6]。
- 重新送料後，上位機能正常收到結果回來 [6]。

---

若上述步驟做完 IP 燈仍紅，請通知工程師進一步檢查 Spark 端的程式與 ControlServer Listen 狀態 [4]。

</details>

<details><summary>助手 B 回答原文</summary>

**結論：問題出在 Grab 主機，不是 Spark 也不是線路。** 健檢報告說 Grab 的 `ip_forward` 被關掉了（`ip_forward=0`），所以 Control（Windows）送給 Spark 的 IP 封包無法經由 Grab 轉送過去，Control 就收不到 Spark 的心跳，因此 IP 燈恆紅 [1]。

---

### 可能原因

1. **Grab 系統設定被改或重灌後沒設** — `ip_forward` 原本應為 `1`，但可能因為更新、重整或被其他程式改回 `0` [1]。
2. **臨時性的核心參數失效** — 重開機後沒套用到 `/etc/sysctl.d` 的設定檔。
3. **Control 重開無效是正常的** — 因為問題在 Grab 端，Control 重開不會影響 Grab 的轉送功能。

---

### 現場照做步驟（誰可以做）

**設備工程（可操作）：**

1. **暫時解法：重開 Grab 主機** — 重開後 `ip_forward` 會從設定檔重新套用，暫時恢復轉送 [1]。
2. **永久解法：確認 `/etc/sysctl.d` 裡有 `net.ipv4.ip_forward=1`** — 若沒有，請系統/軟體工程補上並執行 `sudo sysctl -p` 套用 [1][7]。

---

### 怎麼確認修好

- Control 上 **IP 燈轉為綠色**。
- 健檢報告第 1 項從 ❌ 變成 ✅（`ip_forward` 顯示 `1`）。
- 上位機重新送料後，結果正常回來 [1]。

</details>
