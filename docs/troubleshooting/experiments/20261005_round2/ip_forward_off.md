# CF-AOI 一鍵健檢 2026-10-05 20:25（機台 user-IMB-M47）

## 結論：❌ 1 項異常、⚠️ 0 項注意、✅ 1 項正常

1. ❌ **[主機] Grab 的轉送功能關閉（ip_forward=0）→ Control 連不到 Spark 的 IP**
   - 可能原因：Grab 系統設定被改或重灌後沒設（Control 到 Spark 要經過 Grab 轉送）
   - 請這樣做：找工程開啟（/etc/sysctl.d 的 net.ipv4.ip_forward=1）；暫時可重開 Grab（設備工程）

## 各項明細（給工程師）

### RDMA
- ✅ Grab ↔ Spark RDMA 正常（線、兩端網卡、IP 程式都正常）
  - 證據：Grab enp1s0f0np0：link 有、100000Mb/s、MTU 9000、RoCE ACTIVE；ping Spark 通、大封包 通；Spark 端：link 有、MTU 9000、RoCE ACTIVE、IP 生產服務 active、調參服務 inactive、RDMA 埠 18515 有在聽；IP 控制埠 有回應

### 主機
- ❌ Grab 的轉送功能關閉（ip_forward=0）→ Control 連不到 Spark 的 IP

