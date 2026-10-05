# CF-AOI 一鍵健檢 2026-10-05 18:20（機台 user-IMB-M47）

## 結論：❌ 1 項異常、⚠️ 1 項注意、✅ 0 項正常

1. ❌ **[RDMA] Grab ↔ Spark 直連線沒有 link（線的問題，或 Spark 沒開機）**
   - 可能原因：這條線兩端都沒訊號：① Spark 關機/當機（看 Spark 電源燈）② 線或光模組鬆脫/損壞 ③ 網卡故障
   - 請這樣做：① 確認 Spark 電源燈亮、風扇有轉 ② 重插 Grab 與 Spark 之間的線（兩端）③ 換備用線 → 若換線就好 = 線壞；換線仍無 link 且 Spark 有開機 = 網卡/機器問題（設備工程）
2. ⚠️ **[GPU] 無法登入 Spark 查 GPU**
   - 可能原因：見 RDMA 項目

## 各項明細（給工程師）

### RDMA
- ❌ Grab ↔ Spark 直連線沒有 link（線的問題，或 Spark 沒開機）
  - 證據：Grab enp1s0f0np0：link 無、?Mb/s、MTU 9000、RoCE DOWN；ping Spark 不通、大封包 不通；Spark 端：連不上（無法查詢）

### GPU
- ⚠️ 無法登入 Spark 查 GPU
  - 證據：ssh: connect to host 192.168.3.1 port 22: Connection timed out

