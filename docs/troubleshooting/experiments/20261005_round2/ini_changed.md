# CF-AOI 一鍵健檢 2026-10-05 19:44（機台 user-IMB-M47）

## 結論：❌ 1 項異常、⚠️ 1 項注意、✅ 8 項正常

1. ❌ **[GPU] 參考圖測圖結果與標準答案不同—IP 參數檔被改過，很可能就是原因**
   - 可能原因：IP 預設參數檔和版本不同（見上一項）
   - 請這樣做：確認 ip/config/default_zone.ini 未被改（誤改 → git checkout 還原）；重開 Spark 重測；仍不同找工程（軟體工程）
2. ⚠️ **[GPU] Spark 上的 IP 參數檔被改過（和版本不同）：ip/config/default_zone.ini**
   - 可能原因：有人手動改了預設參數/預設配方（下次 IP 重啟就會生效，影響檢測結果）
   - 請這樣做：工程確認是否有意修改；誤改 → 在 Spark 上 git checkout 該檔還原，再重啟 IP（軟體工程）

## 各項明細（給工程師）

### 相機
- ✅ 相機 6 台都在（CCD01–CCD06）

### 取像
- ℹ️ 全部相機畫面都很暗（亮度中位數 2.8）
- ✅ 取像測試 6 台：影像完整、無掉封包
  - 證據：每台 5.0 秒；CCD01 4張、CCD02 4張、CCD03 4張、CCD04 4張、CCD05 4張、CCD06 4張

### 交換機
- ℹ️ 交換機時鐘未設定（顯示 2001 年）
- ℹ️ 交換機 console 讀取有雜訊（同一份設定兩次讀出不同字元）
- ✅ 交換機上行 HGE1/0/25 正常（100G(a)）
- ✅ 交換機設定與基準相同

### RDMA
- ✅ Grab ↔ Spark RDMA 正常（線、兩端網卡、IP 程式都正常）
  - 證據：Grab enp1s0f0np0：link 有、100000Mb/s、MTU 9000、RoCE ACTIVE；ping Spark 通、大封包 通；Spark 端：link 有、MTU 9000、RoCE ACTIVE、IP 生產服務 active、調參服務 inactive、RDMA 埠 18515 有在聽；IP 控制埠 有回應

### 主機
- ✅ 主機設定正常（MTU 9000、轉送、Spark 回程路由、磁碟、時間）
  - 證據：Spark 時間差 0.181 秒

### GPU
- ❌ 參考圖測圖結果與標準答案不同—IP 參數檔被改過，很可能就是原因
  - 證據：應為 [(5894, 725, 'PointDark')]；實得 []
- ⚠️ Spark 上的 IP 參數檔被改過（和版本不同）：ip/config/default_zone.ini
  - 證據：M ip/config/default_zone.ini
- ℹ️ GPU 程式錯誤紀錄（最近 7 天 105 筆，來自 ccl_bench, dbg；日期 2026-09-29, 2026-09-30）
  - 證據：Xid 13：程式執行錯誤（非法指令/越界）；Xid 31：程式存取非法記憶體（MMU fault）；Xid 43：程式被 GPU 停止（多半跟在 13/31 後）
- ✅ GPU 運作中：NVIDIA GB10, 50, 0 %, 2405 MHz, 10.80 W
  - 證據：Spark 可用記憶體 112 GB
- ✅ IP 離線單元測試 5 組全過（crc, align, coord, edge, rules）

