# Grab 程式 — CLAUDE.md

> 先讀 `../CLAUDE.md` 了解遷移策略，再讀本文件。
> **核心原則：從 `Reference/phase1_tests/` 的已驗證測試工具升級為生產等級。**

---

## 1. 從 Reference 遷移的檔案對照表

| Reference 來源 | grab/src/ 目標 | 處理方式 |
|---------------|--------------|---------|
| `phase1_tests/shared/FrameHeader.h` | `../shared/FrameHeader.h` | ✅ 直接複製（唯一真相）|
| `phase1_tests/src/t31_pylon_grab/` | `cam_pylon.cpp` | 🔧 升級：多相機陣列、MAC 綁定、cam_count 控制 |
| `phase1_tests/src/t31_ebus_grab/` | `cam_ebus.cpp` | 🔧 升級：多 iPORT 管理 |
| `phase1_tests/src/t30_pylon_probe/` | `cam_pylon.cpp` init 部分 | 🔧 整合 |
| `phase1_tests/src/t30_ebus_probe/` | `cam_ebus.cpp` init 部分 | 🔧 整合 |
| `phase1_tests/src/t40_e2e_client_pylon/` | `rdma_sender.cpp` | 🔧 升級：async、多幀、生產等級 |
| `phase1_tests/src/t01_pylon_mac_setup/` | `mac_ip_binder.cpp` | 🔧 整合 |
| — | `cam_manager.cpp` | 🆕 全新（統一管理 pylon/eBUS）|
| — | `control_client.cpp` | 🆕 全新（TCP client to Control）|
| — | `frame_assembler.cpp` | 🔧 從 t40_e2e_client 抽出 |

---

## 2. 第一步：複製 FrameHeader

```bash
cp ../Reference/phase1_tests/shared/FrameHeader.h ../shared/FrameHeader.h
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
  🔧 加入：MAC-based Persistent IP 綁定（來自 t01_pylon_mac_setup）
  🔧 加入：--cam-count 控制（從 CSV 讀取，選擇性啟用）
  🔧 加入：每幀呼叫 FrameAssembler + RdmaSender
  ❌ 移除：.raw 檔案儲存、單次 500 幀測試邏輯

t31_ebus_grab（測試工具）的功能：
  ✅ 保留：PvDevice/PvStream 初始化、buffer queue、grab loop
  🔧 改進：從單 iPORT → 多 iPORT 並行執行緒
  🔧 加入：cam_count 控制
  🔧 加入：每幀呼叫 FrameAssembler + RdmaSender
  ❌ 移除：.raw 儲存
```

---

## 4. cam_manager.cpp（全新，統一介面）

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

---

## 5. rdma_sender.cpp（升級自 t40_e2e_client）

```
t40_e2e_client_pylon（測試工具）的功能：
  ✅ 保留：RDMA CM 連線建立、ibv_post_send、completion queue 輪詢
  ✅ 保留：rkey/remote_addr 交換機制
  🔧 改進：從單幀測試 → 持續非同步串流
  🔧 改進：加入發送統計（bytes/sec、latency）
  🔧 加入：重連機制（IP 端重啟後自動重連）
  ❌ 移除：CRC 對比驗證（移到 IP 端做）
```

---

## 6. 程式碼結構

```
grab/
├── CLAUDE.md
├── CMakeLists.txt
└── src/
    ├── main.cpp                  ← 🆕 進入點（解析 --cam-count/--sdk）
    ├── cam_manager.h/.cpp        ← 🆕 統一管理 pylon/eBUS
    ├── cam_pylon.h/.cpp          ← 🔧 升級自 t31_pylon_grab
    ├── cam_ebus.h/.cpp           ← 🔧 升級自 t31_ebus_grab
    ├── mac_ip_binder.h/.cpp      ← 🔧 升級自 t01_pylon_mac_setup
    ├── frame_assembler.h/.cpp    ← 🔧 從 t40_e2e_client 抽出
    ├── rdma_sender.h/.cpp        ← 🔧 升級自 t40_e2e_client
    └── control_client.h/.cpp     ← 🆕 TCP JSON client（連 Control）
```

---

## 7. 啟動命令

```bash
# Step 2：單支 pylon 相機
./build/cfaoi_grab --cam-count 1 --cam-ids 0 --rdma-dest 10.0.0.2:18515

# Step 2：單支 eBUS 相機
source /opt/pleora/ebus_sdk/.../set_puregev_env.sh
./build/cfaoi_grab --cam-count 1 --sdk ebus --rdma-dest 10.0.0.2:18516

# Step 3-5：全陣列
./build/cfaoi_grab --config config/system_config.json
```

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
   ⚠️ `Reference/phase1_tests/rdma_common.h::RcConn::reg()` 的註解「GPU 記憶體失敗多半是
   nvidia_peermem 未載入」在 GB10 上**不適用**；移植到 IP 時該註解要改成上述 cudaHostAlloc 方案。
   證據：`docs/verification/verification_report_20260611.md` §五問題1 + `t40_e2e_server.cpp`。
   （此即 `t40_e2e_server` 已採用的作法：`cudaHostAlloc(...Portable|Mapped)` 配 `gpu_buf`。）
