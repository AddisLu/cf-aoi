# CF-AOI 一鍵健檢 2026-10-05 20:26（機台 user-IMB-M47）

## 結論：✅ 全部正常（2 項）


## 各項明細（給工程師）

### RDMA
- ✅ Grab ↔ Spark RDMA 正常（線、兩端網卡、IP 程式都正常）
  - 證據：Grab enp1s0f0np0：link 有、100000Mb/s、MTU 9000、RoCE ACTIVE；ping Spark 通、大封包 通；Spark 端：link 有、MTU 9000、RoCE ACTIVE、IP 生產服務 active、調參服務 inactive、RDMA 埠 18515 有在聽；IP 控制埠 有回應

### 主機
- ✅ 主機設定正常（MTU 9000、轉送、Spark 回程路由、磁碟、時間）
  - 證據：Spark 時間差 0.159 秒

