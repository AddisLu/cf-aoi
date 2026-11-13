# upstream_tester — 上位機對測工具（CF_ 協議）

在 Grab 桌面（Linux x64）跟 **Control** 或 **真上位機（Controller）** 對測 `CF_` 交握的小程式。
只用 Python 標準函式庫，免裝套件；介面是本機網頁（`http://127.0.0.1:8790/`）。

依據文件：
- `L8A CF IP Control Handshake Spec_20240704.xlsx`（命令/參數表）
- `20250217_CF_AOI_IP_說明.pptx`「CF Command Flow (Alignment)」（命令順序）

---

## 一鍵安裝

在 Grab 上（免 sudo）：

```bash
bash tools/upstream_tester/install.sh
```

或在檔案管理員對 `install.sh` 按右鍵 →「以程式執行」。

執行後會：
1. 檢查 python3
2. 把程式複製到 `~/.local/share/cfaoi/upstream_tester/`
3. 在**桌面**放「上位機對測（CF_ 協議）」圖示，應用程式選單也會有
4. **直接開啟工具**

之後雙擊桌面圖示即可。更新時先 `git pull`，再執行一次 `install.sh`，設定會保留。

| 選項 | 用途 |
|------|------|
| `bash install.sh --no-launch` | 只安裝、不開啟（SSH 遠端安裝用） |
| `bash install.sh --uninstall` | 移除程式與圖示（保留設定與紀錄） |

---

## 兩種模式（畫面頂端切換）

| 模式 | 工具扮演 | 連線方向 | 用在 |
|------|----------|----------|------|
| **Client** | 上位機 Controller | 工具 → Control `192.168.10.1:8787` | 驗 Control 的 UpstreamServer |
| **Server** | AOI 端（IP 為 Server） | 真上位機 → 工具 `<Grab IP>:8787` | 驗真上位機送的命令/格式 |

> Server 模式若同一台也有 Control 在聽 8787，port 會衝突。請改用別的 port，或先關 Control。

---

## 功能

### 1. 設定 IP（「連線設定」頁）
- **Client**：Control IP / Port、常用位址清單（可新增、刪除；預設有「Control 控制網 192.168.10.1」「本機」）、回應逾時、
  斷線自動重連、開啟時自動連線、心跳（閒置 N 秒送一次指定指令，0 = 關閉）。
- **Server**：監聽位址（0.0.0.0 或本機各 IP）、Port、開啟時自動監聽；畫面會顯示「上位機請連 <本機 IP>:port」。
- 格式錯誤（IP、port 超出範圍）會擋下並提示。設定存在 `~/.config/cfaoi/upstream_tester.json`，下次開啟沿用。

### 2. 連線指示（畫面頂端）
| 燈號 | 意義 |
|------|------|
| 🟢 綠 | 已連線（Client）／上位機已連入（Server） |
| 🟡 黃閃 | 連線中／自動重連中（第 N 次）／監聽中、等待上位機 |
| 🔴 紅 | 連線失敗／斷線（顯示原因） |
| ⚪ 灰 | 未連線／未監聽 |

旁邊會顯示對端位址、連線時間、最後送出時間、最後收到時間、心跳結果。
背景每 0.5 秒偵測對方是否關閉連線；斷線會立刻轉紅並寫進 Log，有開自動重連就每 3 秒重試。

### 3. Log 監控視窗
- 每筆收發都有時間（到 ms）和方向（送出 / 收到 / 回覆）、延遲 ms，並拆出 P1..P9。
- 每筆都對照規格檢查，✓ 表示符合，⚠ 表示不符；不符也不會擋送出。
- 可依種類篩選（送出/回覆、收到、資訊、警告、錯誤、心跳），也可以搜尋。
- 有暫停捲動、清畫面、**下載今日紀錄**、**開啟紀錄資料夾**等按鈕，並統計各類筆數。
- 紀錄檔存在 `~/cfaoi_upstream_tester/logs/YYYYMMDD.log`，對測完可附給對方。

### 4. 指令新增（「指令管理」頁）
- **新增指令**：名稱限大寫英數與底線（例 `CF_MY_CMD`）。可寫說明、Request P1..P9 名稱、Response P1..P9 名稱。
- 自訂指令可改名、刪除。改名或刪除時，流程裡的步驟會一起更新。
- 規格內建的 5 個指令不能刪除或改名，但參數可以改。
- 「還原預設指令」會把所有指令回到出廠狀態。
- 新增的指令會自動出現在「操作」頁，也可以加進流程；Server 模式收到它時，會依它的回覆設定回應。

### 5. 參數更新與監控（「操作」頁 + 右上「參數監控」表）
- **參數更新**：
  - Client 模式在每個指令卡片上直接改 Request P1..P9 的值；Server 模式則改回覆的 Result（OK/Error）、P1..P9 和延遲 ms。
  - 改了會自動儲存。
  - Client 模式卡片下方會即時預覽「將送出」的完整字串。
- **變數**（填在參數值裡，送出時才代入）：
  - `{now}`：送出當下時間 `yyyy-MM-dd-HH-mm-ss`
  - `{shiftX}` / `{shiftY}`：上次 CHECK_ALIGN 回傳的 Shift。SET_ALIGN 預設就帶這兩個
  - `{round}`：流程第幾輪
  - `{p1}`..`{p9}`：Server 模式專用，把收到的參數帶回回覆
- **參數監控表**：
  - 每個指令兩列。Client 模式是「送出 → 收到」，Server 模式是「收到 → 回覆」。
  - 欄位為 Cmd/Result、P1..P9（附參數名稱）、時間、延遲 ms、次數、⚠ 規格不符數。
  - **值有變動的格子會亮黃**。

### 一鍵流程（「流程」頁，Client 模式）
- 預設順序照 pptx：`CF_LOAD_RECIPE → CF_GRAB_START → CF_CHECK_ALIGN → CF_SET_ALIGN → CF_GET_RESULT`。
- 步驟可以增刪、上下調整，也有「還原規格順序」。
- 可設輪數（0 = 連續跑到按停止）、步驟間隔、遇錯即停。
- 畫面會顯示目前第幾輪、跑到哪一步，以及 PASS / FAIL 統計。

---

## 命令列（不開畫面，SSH 用）

```bash
python3 tools/upstream_tester/upstream_tester.py --host 192.168.10.1 --flow   # 依設定檔流程跑（exit 0 = 全 OK）
python3 tools/upstream_tester/upstream_tester.py --send "CF_LOAD_RECIPE|R1|P001|2026-11-04-10-00-00||||||0"
python3 tools/upstream_tester/test_upstream_tester.py                         # 工具自測（假 Control / 假上位機，不需網路）
```

---

## 兩種線上框格式（Server 模式自動判斷）

| 來源 | 框格式 | 結尾 |
|---|---|---|
| 規格書 / 我們的 Control `UpstreamServer.cs` | `CF_LOAD_RECIPE\|p1\|…\|p9` | `\r\n` |
| **真上位機（AUO `SubSystemControl.dll`, 2012）** | `<Request><Command>CF_LOAD_RECIPE</Command><Param1>…</Param1>…<Param9>…</Param9></Request>` | **沒有任何結尾字元** |

真上位機走的是 XML，**不是**規格書上的 `\|` 分隔文字（2026-11-04 實測，見
[案例](../../docs/troubleshooting/cases/20261104_other_upstream_xml_frame.md)）。
工具在 Server 模式會看第一個位元組自動判斷：`<` 開頭就走 XML（以 `</Request>` 當邊界），
回覆用 `<Response><Result>OK</Result><Param1>…</Param1>…</Response>`，同樣不加結尾字元。
log 會先印一行「對方走 XML 框格式 → 自動切換」，收發都顯示原始內容。

> Client 模式仍送 `\|` 分隔文字，因為它的對象是我們自己的 Control。

---

## 卡住時怎麼查

**症狀：上位機那端顯示「已連線」，但工具頂端一直是黃色「等待上位機連入」。**

先在 Grab 上看 TCP 實況（最準，不用猜）：

```bash
ss -tnp | grep 8787
```

| 看到 | 意思 | 怎麼辦 |
|---|---|---|
| 沒有 `LISTEN` | 工具根本沒在聽 | 看「連線設定」頁有沒有按「重新監聽」；port 被 Control 占用會在 log 報錯 |
| 有 `LISTEN`，`Recv-Q` > 0 | 連線排在 accept queue 沒被收走 | 工具的 accept 執行緒掛了 → 按「重新監聽」；若常發生請回報（2026-11-04 已修一個這類 bug）|
| 有 `ESTAB`，但畫面沒反應 | 連線成立但沒送命令 | 上位機那端沒發命令，或送的不是 `\r\n` 結尾 |
| 什麼都沒有 | 根本沒連進來 | 上位機的目標 IP/port 設錯、或中間防火牆擋掉 |

監聽位址選了某個網卡 IP（例 `192.168.1.136`）時，**只有那張網卡收得到**；不確定就選 `0.0.0.0`。

---

## ⚠ 規格 20240704 與 Control 現行實作的差異

對測時這幾處會出現 ⚠：

| 項目 | 規格表 | Control `UpstreamServer.cs` |
|------|--------|-----------------------------|
| CF_CHECK_ALIGN 回應 | Param1=ShiftX、Param2=ShiftY | Param1=`Cs_AlignSet`、Param2=ShiftX、Param3=ShiftY |
| CF_SET_ALIGN 請求 | Param1=ShiftX、Param2=ShiftY | 讀 Param2/Param3（Param1 當 result） |
| 失敗 Result 字串 | `OK/Error` | `ERR` |
| CF_GRAB_START | 無參數 | 可帶 Param1=timeoutMs（空 = 40000） |
| CF_READY / CF_STOP | 無 | Control 擴充 |

- 工具**收**回應時，兩種 Shift 位置都能解析，也都會自動更新 `{shiftX}`/`{shiftY}`。
- 要用 Control 現行格式送 SET_ALIGN，在「操作」頁把參數改成 `P1=Cs_AlignSet`、`P2={shiftX}`、`P3={shiftY}`。
- 規格表的 `Time=`、`DefectCnt=`、`Result file path=` 字樣，**收**的時候有沒有 `key=` 前綴都能解析；**送**的時候照參數欄填的值原樣送出。
- 差異要以真上位機為準，再決定改哪一邊。

---

## 檔案

| 檔案 | 說明 |
|------|------|
| `install.sh` | 一鍵安裝（桌面圖示 + 開啟） |
| `upstream_tester.py` | 後端：TCP client/server、規格檢查、設定、紀錄、本機網頁 API |
| `ui.html` | 前端畫面 |
| `test_upstream_tester.py` | 自測 |
