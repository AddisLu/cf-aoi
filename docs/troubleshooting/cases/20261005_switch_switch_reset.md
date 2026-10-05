# 換交換機/重置後 CCD05、CCD06 連不上 → 相機埠少了 speed 1000（port-group 連動）

- 日期：2026-10-05　來源：實驗室故障注入（tools/triage/fault_inject.py；RDMA 線斷為 rdma_cable_test.sh）　機台：user-IMB-M47 + spark-c16f + HPE 5945（借用機）
- 分類：switch　注入方式：CCD05 所插埠 undo speed（33–36 整組連動）　已還原：是（還原後健檢：正常）

## 線上人員看到的（原話）
交換機壞掉換了一台新的（或被重置過），接回去後有兩台相機（CCD05、CCD06）一直連不上，線都換過了還是一樣。

## 證據：一鍵健檢結論
## 結論：❌ 3 項異常、⚠️ 0 項注意、✅ 4 項正常

1. ❌ **[相機] CCD05 不見了：線路沒有訊號**
   - 可能原因：CCD05 的網路線/光纖或交換機那端的模組鬆脫、損壞；或 CCD05 相機沒電（電源線/電源供應器）
   - 請這樣做：請找線材標示「CCD05」的那一條：① 看 CCD05 相機背面燈號是否亮（不亮 = 沒電，查電源線）② 重插這條線兩端（相機端、交換機端模組） ③ 換一條線或交換機換一個空埠（埠位不必固定）（設備工程）
2. ❌ **[相機] CCD06 不見了：線路沒有訊號**
   - 可能原因：CCD06 的網路線/光纖或交換機那端的模組鬆脫、損壞；或 CCD06 相機沒電（電源線/電源供應器）
   - 請這樣做：請找線材標示「CCD06」的那一條：① 看 CCD06 相機背面燈號是否亮（不亮 = 沒電，查電源線）② 重插這條線兩端（相機端、交換機端模組） ③ 換一條線或交換機換一個空埠（埠位不必固定）（設備工程）
3. ❌ **[交換機] 交換機設定和基準不同（4 行）—很可能就是 CCD05、CCD06 不見的原因**
   - 可能原因：有人改過交換機設定，或交換機更換/重置/升級後設定沒還原（例：相機埠少了 speed 1000 → 1G 相機永遠連不上）
   - 請這樣做：工程人員對照差異還原（相機埠需 speed 1000、上行 100G、jumbo），再 save force；換新交換機見 docs/troubleshooting_線上異常處理.md「交換機更換」（設備工程）

（完整報告：`docs/troubleshooting/experiments/20261005_fault_injection/switch_reset.md`、JSON：`switch_reset.triage.json`）

## 根因
25G 埠插 1G 模組必須 speed 1000，重置後整組 auto-neg 永不 link；看起來像壞線（換線也沒用）

## 處置
1. 工程對該 port-group 任一埠下 speed 1000，跳 [Y/N] 一定回 Y
2. save force
3. 重跑健檢、--save-baseline

## 學到的 / 預防
設定與基準共識比對（console 有雜訊）列出 4 行 speed 1000 消失，並關聯到不見的相機

## 機況助手表現（0–2 分）
- 只給症狀（A，舊知識庫）：**2** — 知識庫原有 runbook，答得好
- 症狀 + 健檢報告（B，新知識庫）：**2** — 正確（但猜錯 CCD06 的埠號——埠位會變，不該猜）

<details><summary>助手 A 回答原文</summary>

知識庫中沒有關於交換機更換後特定相機連不上的完整硬體故障排除步驟，但根據現有資料，我來分析可能的原因。

## 結論

**交換機埠設定遺失**是最可能的原因。更換或重置交換機後，所有埠的專屬設定（尤其是 `speed 1000`）都會消失，CCD05、CCD06 插的埠因缺少此設定而無法 link up。線已換過，問題不在線材。

## 可能原因

1. **交換機埠未下 `speed 1000` 指令**：25G SFP28 埠插 1G 銅纜模組時，auto-negociation 永遠無法 link up，必須手動鎖定速度 [4]。
2. **port-group 連動未被注意**：若 CCD05、CCD06 插的埠落在同一 port-group（33–36 一組、37–40 一組…），對其中一埠下 speed 指令會連動整組，且會跳 `[Y/N]` 確認提示，腳本化時若沒處理提示，後續指令會被當答案吃掉 [4]。
3. **只插模組沒下 speed**：雖然 `display transceiver` 看得到模組，但 link 會 DOWN、`Input 0 packets`，看起來像壞線，實際是 auto-neg 問題 [4]。
4. **MAC 位址未配對**：若交換機重置後 MAC 表清空，且 `grab/cam_map.json` 的 MAC 對應尚未更新，bootstrap 時可能無法正確綁定相機 [1][6]（低機率，因線已換過）。

## 現場照做步驟（可由線上人員操作）

1. **進交換機 console，對 CCD05 與 CCD06 所插的埠下 speed 指令**：
   ```
   system-view
   interface WGE1/0/<CCD05_port>
     speed 1000
     description CCD05-raL8192
     stp edged-port
     quit
   interface WGE1/0/<CCD06_port>
     speed 1000
     description CCD06-raL8192
     stp edged-port
     quit
   save force
   ```
   ▶ 注意：若出現 `[Y/N]` 提示，必須先回應 Y 或 N，不能繼續下指令。

2. **確認 link 狀態**：
   ```
   display interface WGE1/0/<port>
   ```
   應看到 **UP 1000Mbps/F** [4]。

3. **如果還是不行，檢查 port-group 歸屬**：
   - 若 CCD05 和 CCD06 的埠在不同 port-group，需分別對兩埠都下 speed 指令。
   - 若在同一 port-group，只要對其中一埠下，整組就會生效，但要注意提示。

## 如何確認修好

- 交換機端：`display interface WGE1/0/<port>` 顯示 **UP 1000Mbps/F** [4]。
- 軟體端：ConnectionManager 的 heartbeat 機制會在 ~2.5 秒內偵測到連線恢復，log 會出現「IP 已連線 / 已重新連線」[5]（如果你的系統有啟用 heartbeat）。
- 相機端：在設備操作畫面上應能看到 CCD05、CCD06 狀態轉為綠色連線。

</details>

<details><summary>助手 B 回答原文</summary>

好的，我先用工具查一下目前有沒有相關的任務或已知問題。

## 結論

**交換機設定遺失**導致 CCD05 和 CCD06 連不上。這兩台相機插在交換機的 **WGE1/0/35（CCD05）** 和 **WGE1/0/36（CCD06）**（對照回推 33–36 這組 port-group），以及相鄰的 **WGE1/0/33、WGE1/0/34** 也少了 `speed 1000` — 新交換機（或被重置的交換機）這四埠預設是 auto-neg，但 25G SFP28 埠插 1G 銅纜模組必須手動設 `speed 1000`，否則永不 link up [5]。

---

## 可能原因

1. **交換機更換／重置後，相機埠沒補 `speed 1000`**（最常見）[5]
   - 25G SFP28 埠插 1G 銅纜模組，auto-neg 永遠 link DOWN，插了模組也像壞線。
   - `speed 1000` 有 **port-group 連動**（33–36 四埠一組），下一個就全組生效，但沒下的話整組都 link down。

2. **若曾對 port-group 下 `speed 1000` 但沒回答 `[Y/N]` 確認**，指令被吃掉，設定沒寫進去 [5]。
   - 2026-09-17 實例：37–40、41–44 組沒回到 Y → 顯示仍 `auto`，4 台相機全失聯。

3. **線或模組本身損壞**（但已換過線，可能性較低）

---

## 現場照做（設備工程人員可做）

### 步驟一：確認交換機目前設定

用 console 線連交換機（damac `/dev/ttyUSB0`，9600 8N1），下指令：
```
display interface brief | include WGE
```
檢查 WGE1/0/33、34、35、36 的 speed 是否為 **1G**（而非 `auto`）。

### 步驟二：補下 speed 1000

對 33–36 這組 port-group 下一次即可（會連動全組，但會跳 `[Y/N]` 確認）：
```
interface Twenty-FiveGigE1/0/33
 speed 1000
```
**看到 `[Y/N]` 時一定要輸入 `Y`**，否則後續指令會被吞掉。  
也可以對 35 或 36 下，效果相同（同一個 port-group）。

### 步驟三：儲存並確認

```
save force
```
再跑一次 `display interface brief | include WGE`，確認四埠都顯示 **UP 1G**。

### 步驟四（若以上無效）

- 把 CCD05 和 CCD06 的線改插到**同一 port-group 內的其他空埠**（例如 WGE1/0/37–56 已預設 `speed 1000`，直接插就能用 [5]）。
- 檢查相機背面燈號（不亮 = 沒電，查電源線）。

---

## 怎麼確認修好

1. 交換機上 `display interface brief` 看到 WGE1/0/33–36 都 **UP 1G**。
2. 再跑一次「一鍵健檢」→ CCD05 和 CCD06 **不再出現「不見了」**，MAC 00:30:53:54:e3:16（CCD05）和 00:30:53:54:e3:15（CCD06）出現在交換機 MAC 表中 [1][報告]。
3. 取像測試：4 台以外的 CCD05、CCD06 也能正常抓畫面。

</details>
