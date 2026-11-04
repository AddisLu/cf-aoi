# Windows 控制網插交換機 port 55 不通 → SFP 光籠裡的 RJ45 轉換器讀不到 EEPROM

- 日期：2026-11-04　來源：實驗室（控制網改走交換機的嘗試）　機台：user-IMB-M47 + HPE 5945（借用機 `CFAOI-SW1`）
- 分類：switch　嚴重度：提示（調機期，未影響產線）
- 關鍵字：port 55、WGE1/0/55、RJ45 SFP、轉換器、UNKNOWN_SFP_PLUS、transceiver failed、控制網、8787

## 線上人員看到的（原話）
我有在 hpe port55 插入 windows pc 的網路線，請幫忙設定。

## 證據
交換機 console（`/dev/ttyUSB0` 9600 8N1，`tools/triage/switch_console.py`）：

```
display interface WGE1/0/55
Twenty-FiveGigE1/0/55
Current state: DOWN           Line protocol state: DOWN
Media type is not sure, port hardware type is UNKNOWN_SFP_PLUS
Input (total): 0 packets, 0 bytes
Last link flapping: Never     Last time when physical state changed to up: -

display transceiver interface Twenty-FiveGigE1/0/55
Reading information from the transceiver failed.
```

關鍵對照（同一批埠）：
| 埠 | hardware type | 判讀 |
|---|---|---|
| WGE1/0/53、54、56 | `No connector` | 空籠 |
| **WGE1/0/55** | **`UNKNOWN_SFP_PLUS`** | **有插模組，但 EEPROM 讀不到** |
| HGE1/0/25 | Mellanox MCP1600-C003（有讀到，附「not a supported transceiver」警告）| 非原廠模組只要讀得到就能用 |

- `display logbuffer reverse`：**1/0/55 一筆 PHY_UPDOWN 都沒有**（相機埠 33/35/37/39/41/43 有完整 up/down 歷史）→ 這個埠從開機到現在從未成立過 link。
- 這台 5945 **58 個埠全是光籠**，一個 RJ45 都沒有：`WGE1/0/1-24`＋`WGE1/0/33-56`（48×25G SFP28）、`HGE1/0/25-32`（8×100G QSFP28）、`GE1/0/57-58`（空籠）、管理埠 `M-GE0/0/1`（`no connector`、DOWN）。

## 試過但無效的（軟體面已窮盡）
1. `undo speed`（解除相機用的強制 `speed 1000` → 自動協商；銅質 SFP 常需要）：改成 autonegotiation 後等 20 s，仍 DOWN。
   ⚠️ 這個指令有 **port-group 連動**：「This command will take effect on Twenty-FiveGigE1/0/53 to 1/0/56」。
2. `shutdown` / `undo shutdown` 強迫重新偵測模組：`transceiver failed` 不變。

## 根因
**推測（未確認）**：模組本身、或模組與 25G 光籠的接觸/相容性問題。EEPROM 讀不到代表交換機連 I2C 都問不到模組，
不是設定問題 → 常見為①沒插到底（卡榫未壓下）②模組故障或髒污 ③模組型別不對（需 **RJ45 1000Base-T SFP**；
10GBase-T SFP+ 耗電高、在 25G 籠常不被接受；QSFP28↔SFP28 轉接座或外接式媒體轉換器不算）。

## 處置（待現場做）
1. 把模組拔出重插到底（卡榫壓下），再看 `display transceiver interface Twenty-FiveGigE1/0/55`。
2. 換到別的埠試（例 WGE1/0/1）：**讀得到 = 55 號籠有問題；還是讀不到 = 模組或型別問題**。
3. 確認模組側標型號，必須是 1000Base-T RJ45 SFP。
4. 怎麼確認修好：`display transceiver` 讀得到廠牌/料號，且 `display interface` 出現 `Current state: UP`、
   logbuffer 有 1/0/55 的 PHY_UPDOWN up。

## 還原事項（借用機，務必處理）
`speed 1000` 已從 **WGE1/0/53–56**（port group）移除 → 與 triage baseline 有 drift。
放棄這條路時要還原：`system-view` → `interface WGE1/0/55` → `speed 1000`（會連動 53–56）→ `return` → `save force`。

## 學到的 / 預防
- **區分空籠與讀不到模組**：`No connector` = 空；`UNKNOWN_SFP_PLUS` + `Reading information from the transceiver failed`
  = 有模組但讀不到。只看 `display interface brief` 的 DOWN 看不出差別，會誤判成「線沒插」。
- **logbuffer 查「這個埠有沒有成立過 link」**比看當下狀態有用：一筆 PHY_UPDOWN 都沒有 = 從來沒通過，先查實體不要查設定。
- 控制網要走交換機，**需要兩個** RJ45 SFP（Windows 一個、Grab `enp3s0` 一個）；只有一個時 Grab 的控制網仍在交換機外，
  通了也沒用。設計上的定案做法是 Windows ↔ Grab `enp3s0` **直連**（見 docs/CLAUDE.md 線位表），交換機零設定。
- 相機埠的 `speed 1000` 是 port-group 連動設定，改一個埠會動到四個；借用機上任何改動都要記錄還原。
