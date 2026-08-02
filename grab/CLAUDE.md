# Grab 程式 — CLAUDE.md

> 先讀 `../CLAUDE.md` 了解遷移策略，再讀本文件。
> **核心原則：從 `Reference/cfaoi_phase1/` 的已驗證測試工具升級為生產等級。**

---

## 1. 從 Reference 遷移的檔案對照表

| Reference 來源 | grab/src/ 目標 | 處理方式 |
|---------------|--------------|---------|
| `cfaoi_phase1/shared/FrameHeader.h` | `../shared/FrameHeader.h` | ✅ 直接複製（唯一真相）|
| `cfaoi_phase1/src/t31_pylon_grab/` | `cam_pylon.cpp` | 🔧 升級：多相機陣列、MAC 綁定、cam_count 控制 |
| `cfaoi_phase1/src/t31_ebus_grab/` | `cam_ebus.cpp` | 🔧 升級：多 iPORT 管理 |
| `cfaoi_phase1/src/t30_pylon_probe/` | `cam_pylon.cpp` init 部分 | 🔧 整合 |
| `cfaoi_phase1/src/t30_ebus_probe/` | `cam_ebus.cpp` init 部分 | 🔧 整合 |
| `cfaoi_phase1/src/t40_e2e_client_pylon/` | `rdma_sender.cpp` | 🔧 升級：async、多幀、生產等級 |
| `cfaoi_phase1/src/t01_pylon_mac_setup/` | `mac_ip_binder.cpp` | 🔧 整合 |
| — | `cam_manager.cpp` | 🆕 全新（統一管理 pylon/eBUS）|
| — | `control_client.cpp` | 🆕 全新（TCP client to Control）|
| — | `frame_assembler.cpp` | 🔧 從 t40_e2e_client 抽出 |

> ⚠️ **實作現況（2026-08-02 更新，取代 2026-06-17 考古註記）**：本表為**原始遷移規劃**，與現況已分岔。
> grab/src 現有（14 檔 ~2748 行）：`main.cpp`、**`cam_manager.{h,cpp}`（✅ 已建，2026-07-18 起：多相機陣列 +
> Gap #21 cam_map MAC 綁定，2 台實機 L3）**、`cam_pylon.{h,cpp}`、`rdma_sender.{h,cpp}`、
> `control_server.{h,cpp}`（非 client）、`rdma_common.h`、`rdma_nslot_test.cpp`，
> 另有工具三檔 `image_replay_sender.cpp`／`probe_cam_nodes.cpp`／`cam_mean_gray_test.cpp`。
> **規劃未建**：`cam_ebus`（eBUS 整路徑，L0）；`frame_assembler`（組幀邏輯已內聯於 main.cpp `frame_cb` +
> rdma_sender，不另建檔）；`control_client`（實作方向相反：Grab 是 TCP **server**，Control 連入 8100）。
> **`t01_pylon_mac_setup` 是懸空引用**——`Reference/cfaoi_phase1/` 與整個 Reference 樹下**無此原始檔**，
> MAC Persistent IP 綁定能力從未存在於程式碼；**該需求已由 `cam_map.json`（MAC↔cam_id 映射，見不變式 8）
> 以另一機制取代**（persistent IP 設定現用 runbook 手動流程 + GVCP 廣播工具）。
> phase1 來源實為**扁平單檔**（`t31_pylon_grab.cpp`，非 `t31_pylon_grab/` 子目錄）。詳見 [docs/grab_程式完整說明.md](../docs/grab_程式完整說明.md) §10。

---

## 2. 第一步：複製 FrameHeader

```bash
cp ../Reference/cfaoi_phase1/shared/FrameHeader.h ../shared/FrameHeader.h
```

這份 FrameHeader.h 已在 Phase-1 測試中驗證，Grab 和 IP 兩端必須使用同一份。

---

## 3. 第二步：從測試工具升級到生產相機管理

### 升級要點（t31_pylon_grab → cam_pylon.cpp）

```
t31_pylon_grab（測試工具）的功能：
  ✅ 保留：Pylon 初始化、GigE 傳輸最佳化、回呼機制、FPS/drop 統計
  🔧 改進：從單相機 → 多相機陣列（CInstantCameraArray 或獨立執行緒陣列）
  🔧 改進：從 main() 腳本 → 類別封裝（PylonCamPath）
  ~~🔧 加入：MAC-based Persistent IP 綁定（來自 t01_pylon_mac_setup）~~ → 來源懸空（見 §1 註）；
     改由 cam_map.json MAC↔cam_id 映射落地（不變式 8）
  ✅ 加入：--cam-count 控制（實作讀 cam_map.json/列舉，非 CSV）
  ✅ 加入：每幀回呼 → main.cpp frame_cb → RdmaSender（規劃中的 FrameAssembler 未另建檔，內聯處理）
  ❌ 移除：.raw 檔案儲存、單次 500 幀測試邏輯

t31_ebus_grab（測試工具）的功能：（⚠️ 整段為規劃，cam_ebus 未建檔＝L0，eBUS SDK 未裝）
  ✅ 保留：PvDevice/PvStream 初始化、buffer queue、grab loop
  🔧 改進：從單 iPORT → 多 iPORT 並行執行緒
  🔧 加入：cam_count 控制
  🔧 加入：每幀回呼 → RdmaSender
  ❌ 移除：.raw 儲存
```

---

## 4. cam_manager.cpp（✅ 已建 2026-07-18；實作與原規劃不同，此節以現況為準）

實作：`grab/src/cam_manager.{h,cpp}`（94+335 行；2 台實機 L3，見 STATUS「Switch 到貨日」章節）。
與原始規劃的差異：**設定來源是 `cam_map.json`（MAC↔cam_id 穩定映射，Gap #21），不是 CSV**（`CsvLoader` 未建）；
無 `FrameAssembler`／`EbusCam`（僅 pylon，組幀內聯於 main.cpp frame_cb）；持有 `RdmaSender` 的是 main，不是 CamManager。

核心 API（詳見 cam_manager.h 檔頭註解）：

| API | 行為 |
|---|---|
| `load_map(path, err, warn)` | 載 cam_map.json。檔案不存在=合法舊行為（warn）；格式錯/MAC 重複/cam_id 重複=**回 false 啟動中止**（不變式 8）|
| `write_map(path, entries_json, err)` | SET_CAM_MAP 落地：寫暫存檔→**用 load_map 同一套規則驗**→過了才 rename（原子），驗證單一標準 |
| `annotate(infos)` | LIST_CAMERAS 用：依映射填 cam_id/ccd_id/bound；未綁定誠實標 bound=false |
| `open_all(want, serial, pkt_size, err)` | want<=0=ALL；**有映射=嚴格模式**（未列於映射的相機拒開）；任一台失敗全關（fail-fast 不半開）；冪等重用（台數符合直接重用；`primary_only_` 旗標防 idle 單台被當整陣列＝★7 修法）|
| `start_all(max_frames, cb)` | 逐台平行 arm（skew 由 IP 端玻璃前緣對位吸收）；cb 被 N 個 thread 併發呼叫，呼叫端負責序列化 |
| `stop_all()` / `get(cam_id)` / `get_or_open_primary(...)` | 停+清列表／依 cam_id 路由／idle 調參路徑開單台（設 `primary_only_`）|

<details><summary>原始規劃碼（2026-06-17 前；保留考古——CsvLoader / FrameAssembler / EbusCam / ICam 均未建）</summary>

```cpp
// grab/src/cam_manager.cpp
// 無直接前身，全新撰寫，但邏輯從 t31_* 測試工具的 main() 提取

class CamManager {
public:
    bool init(const Config& cfg) {
        auto layout = CsvLoader::load(cfg.layout_csv);

        for (auto& row : layout) {
            if (!is_cam_enabled(row.cam_id, cfg)) continue;

            if (row.sdk == "pylon") {
                auto cam = std::make_unique<PylonCam>(row, frame_assembler_, rdma_sender_);
                cameras_.push_back(std::move(cam));
            } else if (row.sdk == "ebus") {
                auto cam = std::make_unique<EbusCam>(row, frame_assembler_, rdma_sender_);
                cameras_.push_back(std::move(cam));
            }
        }
        LOG_INFO("啟用 {}/{} 台相機 (cam_count={})",
                 cameras_.size(), layout.size(), cfg.cam_count);
        return !cameras_.empty();
    }

private:
    bool is_cam_enabled(int id, const Config& cfg) {
        if (!cfg.cam_ids.empty())   return std::count(cfg.cam_ids.begin(), cfg.cam_ids.end(), id);
        if (cfg.cam_count > 0)      return id < cfg.cam_count;
        return true;
    }
    std::vector<std::unique_ptr<ICam>> cameras_;
    FrameAssembler frame_assembler_;
    RdmaSender     rdma_sender_;
};
```
</details>

---

## 5. rdma_sender.cpp（升級自 t40_e2e_client）

```
t40_e2e_client_pylon（測試工具）的功能：
  ✅ 保留：RDMA CM 連線建立、ibv_post_send、completion queue 輪詢
  ✅ 保留：MrInfoEx 握手（addr/rkey 仍在 wire 上交換；SEND 資料路徑已不用遠端位址，見不變式 7）
  ✅ 改進：單幀同步 → N-buffer 非同步串流（≤ n_slots 筆 in-flight、lazy FIFO poll）
  ✅ 改進：發送統計 sent_frames/sent_bytes；app-CRC 由呼叫端在 send_mtx 之外先算（crc_of，37 台不佔鎖）
  ❌ 未實作：重連機制（原規劃「IP 端重啟後自動重連」）——現況 send/poll 失敗後 connected_=false，
     之後所有幀靜默丟棄、且 disconnect() 因 connected_==false 早退不清 QP/MR；
     恢復唯一路徑 = GRAB_STOP → 重 GRAB_ARM。已列審計 P0-7「上線前必修」（rdma_sender.cpp 檔頭註記）。
  ❌ 移除：CRC 對比驗證（移到 IP 端做）
```

---

## 6. 程式碼結構（2026-08-02 對齊實際檔案）

```
grab/
├── CLAUDE.md
├── CMakeLists.txt                5 個目標：cfaoi_grab / rdma_nslot_test / image_replay_sender /
│                                 probe_cam_nodes / cam_mean_gray_test
├── cam_config.example.json       每台曝光/增益模板（本機副本 cam_config.json 不版控）
├── cam_map.example.json          MAC↔cam_id 綁定模板（Gap #21；本機副本 cam_map.json 不版控）
└── src/
    ├── main.cpp                  ← ✅ 進入點/狀態機（IDLE⇄ARMED⇄GRABBING）、frame_cb、11 個 8100 回呼接線
    ├── cam_manager.h/.cpp        ← ✅ 多相機陣列 + cam_map MAC 綁定（原規劃「統一管理 pylon/eBUS」，現僅 pylon）
    ├── cam_pylon.h/.cpp          ← ✅ 升級自 t31_pylon_grab（grab thread、曝光/增益、enumerate、grab_one_mean）
    ├── rdma_sender.h/.cpp        ← ✅ 升級自 t40_e2e_client（N-buffer SEND pipeline，見 §5）
    ├── rdma_common.h             ← ✅ 沿用 phase1 RcConn + MrInfoEx（⚠️ 與 ip/src/image_source/ 有同源副本，
    │                                 post_recv 等 wire 相關改動必須兩份同步，見不變式 7 配套註記）
    ├── control_server.h/.cpp     ← ✅ TCP JSON server @8100（規劃中的 control_client 方向反轉：Grab 為 server）
    ├── rdma_nslot_test.cpp       ← 合成幀送器（免相機；threads>1 模擬 N 相機共用單 QP）
    ├── image_replay_sender.cpp   ← 檔案回放送器（Gap #27；stdin 餵 Mono8 raw）
    ├── probe_cam_nodes.cpp       ← GenICam 節點探測（Gap #2 Stage 0）
    └── cam_mean_gray_test.cpp    ← 曝光/增益→mean gray 單調性驗證（Gap #2 Stage 2+3）
```

> **規劃未建（保留考古脈絡）**：`cam_ebus.h/.cpp`（eBUS 路徑，L0）；`mac_ip_binder.h/.cpp`（來源 t01 懸空，
> 能力由 cam_map.json 取代）；`frame_assembler.h/.cpp`（內聯於 main.cpp frame_cb）；
> `control_client.h/.cpp`（反轉為 control_server）。

---

## 7. 啟動命令（實際 CLI；未知參數直接 exit 1）

```bash
# Step 2：單台 pylon（legacy 語意：--cam-id 自訂 FrameHeader.camId、--serial 指定序號）
./build/cfaoi_grab --rdma-dest 192.168.3.1:18515 --cam-count 1 [--cam-id 0] [--serial auto]

# Step 3+：多台 / 全陣列（ALL = 列舉到的全部；有 cam_map.json 即嚴格模式，未列於映射拒開）
./build/cfaoi_grab --rdma-dest 192.168.3.1:18515 --cam-count ALL [--frames-per-panel N]
```

全部旗標（main.cpp 檔頭同步維護）：

| 旗標 | 預設 | 說明 |
|------|------|------|
| `--rdma-dest IP:PORT` | （必填）| Spark IP 端 RDMA server（如 192.168.3.1:18515）|
| `--cam-count N\|ALL` | 1 | 啟用台數；ALL = 列舉到的全部 |
| `--frames-per-panel N` | 0 | 每片每台張數（0=連續；GRAB_START params 可覆蓋）|
| `--cam-id N` | 0 | 單台模式的 FrameHeader.camId（legacy）|
| `--serial STR` | auto | pylon 序號；auto = 第一台（單台模式）|
| `--pkt-size N` | 8192 | GevSCPSPacketSize |
| `--ctrl-port N` | 8100 | 等 Control 連入的 TCP port |
| `--cam-config PATH` | exe 上一層/cam_config.json | 曝光/增益 JSON（路徑錨定 grab/，不隨 CWD 漂移）|
| `--cam-map PATH` | exe 上一層/cam_map.json | MAC↔cam_id 映射（Gap #21；同上錨定）|

> 舊版此節的 `--cam-ids`／`--sdk ebus`／`--config config/system_config.json` 均**不存在於程式**
> （eBUS 路徑 L0 未建；設定檔僅 cam_config.json / cam_map.json 兩份，無 system_config.json）。

---

## 8. 不變式

1. `cam_pylon.cpp` 和 `cam_ebus.cpp` 禁止互相 include
2. FrameHeader.h 必須與 IP 端使用相同版本（從 shared/ 引用）
3. Grab 無 UI，所有設定從 Control 命令或 system_config.json 來
4. RDMA NIC link_layer 必須是 `Ethernet`（RoCE v2）
5. 相機 NIC MTU 必須 9000；RDMA NIC 不需要 jumbo
6. **GB10（DGX Spark）不可用 `nvidia_peermem`，改 `cudaHostAlloc`（2026-06-11 實機驗證）**：
   DGX Spark 是 GB10 NVLink-C2C SoC，GPU Bus ID 非標準 PCIe 空間（`0000000F:01:00.0`），
   `modprobe nvidia_peermem` 回 **EINVAL**（其 PCIe P2P 拓樸檢查正確拒絕）。
   **正式 IP 端 RDMA 接收（RdmaReceiver）必須**：用
   `cudaHostAlloc(cudaHostAllocPortable | cudaHostAllocMapped)` 配 pinned host memory →
   `ibv_reg_mr` 註冊給 RDMA NIC 直接 DMA → GPU 經 `cudaHostGetDevicePointer` 透過
   NVLink-C2C(~900GB/s) 讀寫（等效甚至優於 PCIe P2P）。
   ⚠️ `Reference/cfaoi_phase1/rdma_common.h::RcConn::reg()` 的註解「GPU 記憶體失敗多半是
   nvidia_peermem 未載入」在 GB10 上**不適用**；移植到 IP 時該註解要改成上述 cudaHostAlloc 方案。
   證據：`docs/verification/verification_report_20260611.md` §五問題1 + `t40_e2e_server.cpp`。
   （此即 `t40_e2e_server` 已採用的作法：`cudaHostAlloc(...Portable|Mapped)` 配 `gpu_buf`。）
7. **N-slot ring + MrInfoEx 握手；資料路徑必須是 `SEND`，不可用 `RDMA_WRITE_WITH_IMM`**
   （2026-07-30 實機抓到資料損毀後改正）：
   - `connect()` 收 256B `MrInfoEx`，驗 `n_slots != 0 && slot_size >= frame_cap`
   - `send_frame()` 用 **`post_send`**（不指定遠端位址）；資料落點由 IP 端的 recv WQE 決定
   - `poll_one()` 完成等待 = 背壓點：IP 端 WQE 用完 → SEND 收 RNR（`rnr_retry_count=7=∞`）→ 此處阻塞
   IP 端（`rdma_source.cpp`）N 個 `post_recv` **各指向一個 slot**（`wr_id`=slot 編號），
   處理完該 slot 才重掛它的 WQE。
   **⚠️ 為何不能用 WRITE_WITH_IMM**：RNR credit 只擋 immediate 遞送，**擋不住 payload 落地**——
   送端一拿到前一筆 send completion 就 post 下一筆 write，payload 照樣寫進 slot，且 RNR 期間
   持續重試、每次重寫該 slot ⇒ IP 端正在讀的 slot 被覆寫。實測（2 台相機+背壓）
   slots=2/4/16 → err=11/10/0（純由 ring 深度決定）。改 SEND 後三種深度全 err=0，
   ring 深度只影響吞吐不影響正確性。**縮短收端讀取窗口治不了此問題**（試過，err 只從 10 降到 7）。
   **實測數據**：2026-06-17 `rdma_nslot_test` 120 幀 CRC=OK（WRITE 版）；
   2026-07-30 SEND 版 4 片×3 張×2 台=24 幀 CRC/seq 錯誤 0、背壓下 slots=2 亦 ok=20 err=0。

8. **cam_id 必須來自 MAC 穩定映射，不可用列舉順序（Gap #21，2026-07-30）**：
   `cam_map.json`（每機本地，模板 `cam_map.example.json`）以 `{mac, cam_id, ccd_id}` 綁定。
   - 有映射 → **嚴格模式**：列舉到但未列於映射的相機**直接報錯拒開**，不默默佔用槽位
     （docs/CLAUDE.md 約束②：宣告狀態與偵測狀態不可假 merge）
   - 映射檔格式錯 / `cam_id` 重複 / MAC 重複 → **啟動即中止（exit 1）**，
     不可默默退回列舉順序（那會在無人察覺下把槽位對錯台）
   - 無映射檔 → 退回舊行為 + 明確 WARN（僅限開發；正式陣列必須有映射）
   **為何**：cam_id 決定 `cam_config.json` 的曝光/增益、`FrameHeader.camId`、
   IP 端輸出夾 `CCD{camId}`。2026-07-30 實測：接上第二台後 raL8192 由 cam_id 0 變成 1。
