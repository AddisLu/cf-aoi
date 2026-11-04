# 上位機說「已連線」，對測工具卻一直「等待上位機連入」→ accept 執行緒被暫時性錯誤殺掉

- 日期：2026-11-04　來源：現場（Grab 桌面對測）　機台：user-IMB-M47
- 分類：other（工具 upstream_tester）　嚴重度：提示（對測卡住，不影響產線）
- 關鍵字：upstream_tester、8787、等待上位機連入、Server 模式、accept、Recv-Q、ECONNABORTED

## 線上人員看到的（原話）
我用了一台 switch 就可以了，目前我的 IP 是 192.168.1.136 是 server。windows control 那台是 192.168.1.135。
它的程式有連線到，但我的軟體呈現等待連線中？

## 證據
Grab 上看 TCP 實況：

```
$ ss -tnp | grep 8787
ESTAB   0  0  192.168.1.136:8787   192.168.1.135:56902
LISTEN  6  5  192.168.1.136:8787   0.0.0.0:*   users:(("python3",pid=1780717,fd=5))
             ↑Recv-Q=6（6 條連線排隊沒被 accept）  ↑Send-Q=5（backlog 上限）

$ ls /proc/1780717/task | wc -l
2            ← 只有主執行緒 + UI，accept 執行緒已經不在
```

- 工具 log 從頭到尾只有一行 `Server 監聽 192.168.1.136:8787`，**沒有任何錯誤**。
- 網路本身沒問題：`ping 192.168.1.135` 0% loss。
- 對向的 `192.168.1.135:8787` 是「連線被拒絕」= Control 的 UpstreamServer 沒在聽，
  連進來的是 Windows 上的**真上位機程式** → 使用者選 Server 模式是對的。

## 根因
`upstream_tester.py` 的 `_accept_loop` 寫成 `except OSError: break`。
`accept()` 對**單一條**有問題的連線會丟暫時性 errno（上位機連進來又馬上斷 → `ECONNABORTED` 等），
舊版把它當成致命錯誤 → 執行緒 break 結束，但 **listen socket 還開著**。
之後核心自己完成三次握手、把連線排進 accept queue（所以**上位機那端看起來「已連線」**），
應用程式卻永遠不會收走 → UI 停在「等待上位機連入」，而且因為 break 前沒 log，畫面上一點線索都沒有。

## 處置
1. 修 `_accept_loop`：暫時性 errno（ECONNABORTED/EINTR/EAGAIN/EPROTO/ECONNRESET…）→ 記一行 warn 後**繼續等**；
   真的致命才 break，而且 break 前把狀態設成「監聽中斷」並 log「請按重新監聽」。
2. 加回歸測試 `test_accept_survives_transient_error`（讓第一次 accept 丟 ECONNABORTED，之後必須還能服務）。
   **已驗證：這個測試對舊版程式會失敗、對修好的版本會過。**
3. 重新 `install.sh` + 重啟工具，實測 `CF_READY|||||||||` → 回 `OK|||||||||`，
   斷線後 accept 執行緒仍在（3 threads）、`Recv-Q` 回到 0。

## 學到的 / 預防
- **「對方說連上了、我這邊說沒連上」第一招就是 `ss -tnp | grep <port>`**：
  `LISTEN` 的 `Recv-Q` > 0 就是「連線排隊沒被 accept」，直接指向應用程式沒收連線，不用再查網路或防火牆。
- `/proc/<pid>/task` 數執行緒，可以看出背景執行緒是不是默默死掉了。
- **背景執行緒 break 前一定要 log + 改狀態**：沒 log 的 break 會讓 UI 顯示的狀態與現實相反，比直接壞掉更難查。
- accept() 的錯誤要分「這條連線壞了」與「監聽壞了」，不能一視同仁。
