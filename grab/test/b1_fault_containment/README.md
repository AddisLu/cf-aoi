# B1 例外圍堵測試（grab thread 拔線不殺行程）

驗證 `docs/code_review_20260802.md` **B1** 的修法：相機拔線/斷電時 pylon 例外
**不得逸出 grab thread 進入點**，否則 `std::terminate` 會殺掉整個 `cfaoi_grab`
（6 台相機陪葬 + 8100/RDMA 全斷）。

## 為什麼需要 stub

真正的功能驗證要 damac + 真相機。但那條路太貴、也不是每次改 `grab_loop` 都做得到，
而這個 bug 的性質是「平常完全看不出來，出事就是全滅」——最需要迴歸保護。
所以這裡用一份**極小的 pylon stub** 把例外注進去，在任何機器上都能跑。

**stub 驗的是**：例外圍堵結構（catch 順序、`note_fault` 不做配置、收尾 `StopGrabbing`
也包 try、故障旗標的生命週期）。
**stub 不驗的是**：真 pylon API 契約、真相機行為、RDMA。這些仍需 damac。

## 跑法

不接進 CMake（避免 stub 有任何機會混進生產建置）。一行編譯即可：

```bash
g++ -std=c++17 -Wall -Wextra -pthread \
    -Igrab/test/b1_fault_containment/pylon_stub -Igrab/src \
    grab/test/b1_fault_containment/b1_fault_test.cpp grab/src/cam_pylon.cpp \
    -o /tmp/b1_fault_test && /tmp/b1_fault_test
```

預期 `全數通過`、exit 0。已驗過的環境：macOS clang（Apple libc++）、
Ubuntu 24.04 gcc 13.3（libstdc++），各 20 項全過。

## 這支測試最重要的性質

**修法失效時，它不是回報 FAIL，而是整支程式被 `std::terminate` 殺掉**——
跟產線上的失效模式一模一樣。所以「有印出最後那行結果」本身就是主張成立的證據。

反向對照做過：把 `grab_loop()` 的 try/catch 拿掉重編，測試在第 1 項就以
`terminate called after throwing an instance of 'GenICam::GenericException'`
被 SIGABRT(134) 殺掉。確認這支測試真的抓得到，不是怎樣都綠。

## 涵蓋的情境

| # | 情境 | 對應現場 |
|---|------|---------|
| 1 | `StartGrabbing` 擲例外 | ARM 當下該台已斷線 |
| 2 | `RetrieveResult` 擲例外 | 取像途中被拔線／交換機掉埠 |
| 3 | `frame_cb` 自己擲 | RDMA 送幀失敗等非 pylon 例外 |
| 4 | 取像例外 **+ 收尾 `StopGrabbing` 也擲** | 斷線後最常見；漏包這層等於沒修 |
| 5 | 正常收滿 | 不可誤標故障 |
| 6 | 故障後重新 `start()` | 排除線路後旗標要清，否則永遠是紅的 |

## 改 `grab_loop` 之後請重跑

見 `grab/CLAUDE.md` 不變式 9（thread 進入點例外不得逸出）。
