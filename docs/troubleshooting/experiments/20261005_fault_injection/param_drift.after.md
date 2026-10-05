# CF-AOI 一鍵健檢 2026-10-05 17:39（機台 user-IMB-M47）

## 結論：✅ 全部正常（6 項）


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

### GPU
- ✅ GPU 運作中：NVIDIA GB10, 50, 0 %, 2405 MHz, 10.77 W
  - 證據：Spark 可用記憶體 112 GB

