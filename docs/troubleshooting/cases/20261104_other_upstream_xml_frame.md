# 真上位機連上 8787 卻「沒反應」→ 它走 XML 框格式，不是規格書的 `|` 分隔文字

- 日期：2026-11-04　來源：現場（Grab ↔ 真上位機 192.168.1.135）　機台：user-IMB-M47
- 分類：other（上位機協議）　嚴重度：提示（對測階段）
- 關鍵字：8787、CF_LOAD_RECIPE、SubSystemControl.dll、XML、Request、Response、沒有結尾字元、Connection reset

## 線上人員看到的（原話）
有連線到了，但都沒有 log？且 control 送出指令後我這邊也都沒有回應……

## 證據
工具加了「收到位元組但一直沒有換行結尾」的警告之後，原始內容直接現形：

```
18:16:01.095 上位機已連入：192.168.1.135:62842
18:16:06.454 收到 241 bytes 但一直沒有換行結尾（CF_ 命令要以 \r\n 收尾）：
  b'<Request><Command>CF_LOAD_RECIPE</Command><Param1>T546HW02</Param1><Param2>Z55H187C6</Param2>
    <Param3>2026-10-16-15-04-00</Param3><Param4></Param4><Param5></Param5><Param6></Param6><Param7></Pa'
18:16:45.208 上位機 192.168.1.135:62842 斷線：[Errno 104] Connection reset by peer
```

241 bytes 剛好是完整一個 frame（9 + 33 + 25 + 26 + 36 + 17×6 + 10 = 241）→ **它不送任何結尾字元**，
送完就等回覆；我們這端在等 `\n`，永遠湊不成一行 → 不回覆 → 上位機等約 40 秒逾時後 reset。

佐證（上位機共用的連線程式庫 `SubSystemControl.dll`，AUO 2012，`strings -el`）：
字串表只有 `Request` / `Command` / `Param1`…`Param9` / `Response` / `Result` / `UTF-8`
→ 請求與回覆都是這組元素名組出來的 XML。

同目錄 `DgxIpSimulator`（AOI 端模擬器，同一支 DLL）的 log 另外釐清了三件事：
- **AOI 端是 Server**（`[啟動] 監聽 127.0.0.1:8787`），上位機主動連進來 → 工具用 Server 模式是對的。
- `CF_LOAD_RECIPE` 的 **P4 是 6 個鏡組旗標 `1,1,1,1,1,1`**（逗號在參數值裡面，不是欄位分隔）。
- **P9 = detectMode**（實測 `P9=0`）→ `UpstreamServer.cs` 讀 `P(9)` 是對的，
  docs/CLAUDE.md §5「detectMode 欄位差一格」的疑慮可以結案（錯的是文件範例，不是程式）。
- `CF_CHECK_ALIGN` 回 `P1=Cs_AlignSet, P2=ShiftX, P3=ShiftY`；
  `CF_GET_RESULT` 回 `P1=6 條 xml 路徑逗號串`、`P2=6 個缺陷數逗號串`。

## 根因
真上位機的線上框格式是 **XML 且無結尾字元**：
`<Request><Command>…</Command><Param1>…</Param1>…<Param9>…</Param9></Request>`，
回覆對應 `<Response><Result>OK</Result><Param1>…</Param1>…<Param9>…</Param9></Response>`。
規格書與我們的實作假設的是 `CF_X|p1|…|p9\r\n`，兩者對不上。

## 處置
1. `upstream_tester` Server 模式改成**看第一個位元組自動判斷**框格式：`<` 開頭走 XML、以 `</Request>` 當邊界。
2. 回歸測試 `test_xml_frame_from_real_upstream` 直接**原封重播現場那 241 bytes**，
   並驗證兩個 frame 黏在同一段 TCP 也要拆得開。
3. 實機驗證：送現場那包 → 回 `<Response><Result>OK</Result>…</Response>`，log 先印「自動切換」。

## 學到的 / 預防
- **「連得上但沒反應」要先看原始位元組**。只看「有沒有收到命令」會一直以為是對方沒送。
  工具現在會在收到資料卻湊不成一個完整命令時，把原始 bytes 印出來 —— 這次就是靠它一眼看出是 XML。
- **規格書寫的格式不等於線上實際跑的格式**。`docs/CLAUDE.md` 不變式 4 的 `CF_` 命令名與 9 參數沒錯，
  錯的是「`|` 分隔 + `\r\n`」這個**傳輸封裝**的假設。
- ⚠ **連帶影響（尚未處理）**：`control/src/Controllers/UpstreamServer.cs` 目前只解析 `|` 分隔文字，
  依本案例的證據，**它接不上真上位機**。要嘛 Control 也支援 XML 框，要嘛確認產線上位機另有文字模式。
  這是 L4「真上位機協議認帳」的第一筆實證。
