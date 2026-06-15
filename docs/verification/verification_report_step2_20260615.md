# CF-AOI 正式 Grab 驗證報告（Step 2）

**日期**：2026-06-15  
**執行主機**：damac（截取中心 x86）↔ spark-c16f（DGX Spark）  
**驗證範圍**：正式 cfaoi_grab（單相機）→ pylon 擷取 → FrameHeader + CRC32 → RDMA → Spark GB10 pinned memory → CRC 驗證

---

## 一、Step 2 目標

5 步驟驗證流程的第 2 步：

| 角色 | 設定 |
|------|------|
| Grab | `cfaoi_grab --cam-count 1`（單台 raL8192） |
| IP 模式 | rdma-validate（本次以 t40_e2e_server 代）|
| Control | 本次 hardcode 直觸（LOAD_RECIPE + GRAB_START via nc）|
| 目的 | 正式 Grab 主程式的單相機 RDMA 傳輸端到端驗證 |

---

## 二、硬體配置

### 截取中心 damac
| 項目 | 規格 |
|------|------|
| CPU | AMD Ryzen 7 7700 |
| RDMA NIC | Mellanox ConnectX-5 MCX516A（100GbE），IP 192.168.3.2/24（enp1s0f0np0）|
| 板載 NIC | Intel I219（1GbE，接相機，192.168.5.x）|
| OS | Ubuntu 22.04 |

### DGX Spark spark-c16f
| 項目 | 規格 |
|------|------|
| GPU | NVIDIA GB10（Blackwell，NVLink-C2C SoC，sm_121）|
| RDMA NIC | ConnectX-7（內建），IP 192.168.3.1/24（enp1s0f0np0）|
| CUDA | 13.0（/usr/local/cuda）|

### 相機
| 項目 | 規格 |
|------|------|
| 型號 | Basler raL8192-12gm（line-scan）|
| 序號 | 25445953 |
| SDK | Basler pylon 26.05.0 |
| 取像尺寸 | 8160 × 3000（Mono8，每幀 ~24.5 MB）|

### 鏈路
```
[raL8192-12gm] ──1GbE RJ45──> [damac enp0s31f6 / 192.168.5.x]
[damac enp1s0f0np0 / 192.168.3.2] ──100G DAC MCP1600──> [spark-c16f enp1s0f0np0 / 192.168.3.1]
```

---

## 三、使用軟體

| 軟體 | 版本 / 位置 | 用途 |
|------|------------|------|
| cfaoi_grab | `grab/build/cfaoi_grab`（本次開發，grab/src/ 全新實作）| 正式 Grab 主程式 |
| t40_e2e_server | `/home/auo001/download/Grab/build/t40_e2e_server`（本次 patch 後重建）| IP 端驗證接收（rdma-validate 代用）|
| Basler pylon SDK | 26.05.0（/opt/pylon）| 相機擷取 |
| libibverbs / librdmacm | 39.0-1 | RDMA userspace verbs |
| NVIDIA CUDA | 13.0 | Spark 端 pinned memory 配置 |
| nlohmann/json | 3.10.x | Control JSON 協議解析 |

---

## 四、正式 Grab 主程式架構（本次新實作）

`grab/src/` 本次從零建立四個模組：

| 檔案 | 功能 |
|------|------|
| `cam_pylon.cpp` / `.h` | pylon 相機封裝（GevSCPSPacketSize=8192、GrabStrategy_OneByOne、drop 偵測）|
| `rdma_sender.cpp` / `.h` | RDMA 發送封裝（connect + MrInfo 交換 + per-frame write + poll_one）|
| `control_server.cpp` / `.h` | TCP JSON server（port 8100，接受 Control 命令）|
| `main.cpp` | CLI + 狀態機（IDLE → GRABBING → IDLE）|

**設計決策（本次確認）：**
- Grab 開 8100 等 Control 連（與 IP 端 8200 一致）
- RDMA port 18515（沿用 t40 驗證值）
- Single-slot RDMA（逐幀同步 post_write_imm + poll_one）

---

## 五、測試流程

```
# Spark 端
[spark-c16f] $ t40_e2e_server 192.168.3.1 18515 67108864 0 20

# damac 端
[damac] $ cfaoi_grab --rdma-dest 192.168.3.1:18515 --cam-id 0
[damac] $ echo '{"cmd":"LOAD_RECIPE","seq":1,"params":{"recipe":"DEFAULT","panel_id":"T001"}}' | nc 127.0.0.1 8100
[damac] $ echo '{"cmd":"GRAB_START","seq":2,"params":{"timeout_ms":40000}}' | nc 127.0.0.1 8100
（等 20 幀）
[damac] $ echo '{"cmd":"GRAB_STOP","seq":3,"params":{}}' | nc 127.0.0.1 8100
```

---

## 六、結果

### Spark 端（t40_e2e_server log）

```
[rdma] listening on 192.168.3.1:18515
[PASS] gpu_mr — GPU MR 就緒
[rdma] connection established (server)
  ·  已通知 client GPU buffer；開始接收幀...
[PASS] e2e — 正確幀=20, 錯誤幀=0（共 20 幀） — 全程資料正確
==== t40_e2e_server 結果：FAIL=0 ====
```

### Grab 端（CHECK_HEALTH 回應）

```json
{
  "data": {
    "grabbed":    33,
    "dropped":    0,
    "sent_frames": 20,
    "sent_bytes":  489605120,
    "grabbing":   true
  },
  "status": "OK"
}
```

### 關鍵數據

| 指標 | 數值 | 說明 |
|------|------|------|
| 正確幀 | **20 / 20** | 0 錯誤，FAIL=0 |
| 每幀大小 | ~24.5 MB | 256B Header + 8160×3000 Mono8 |
| 傳輸總量 | ~467 MB | 20 幀合計 |
| 掉幀數 | 0 | GevSCPSPacketSize=8192 穩定 |
| server 斷線後 | grabbed=33，sent_frames=20 | 13 幀優雅跳過（不 crash）|

**結論：正式 cfaoi_grab 單相機 → RDMA → Spark 端到端驗證 PASS。**

---

## 七、本次修改記錄

### 1. Spark t40_e2e_server.cpp：`cudaMalloc` → `cudaHostAlloc(Portable|Mapped)`

**問題**：t40_e2e_server 原本使用 `cudaMalloc` 配置 GPU buffer，再用 `ibv_reg_mr` 直接註冊。
在 GB10 上因為 GPU Bus ID 非標準 PCIe（NVLink-C2C SoC），`nvidia_peermem` 無法載入，
導致 `ibv_reg_mr` 回傳 EFAULT（Bad address）。

**修改**（`/home/auo001/download/Grab/t40_e2e_server.cpp`）：

```cpp
// 前：直接 GPU 記憶體（GB10 上 ibv_reg_mr EFAULT）
CUDA_OK(cudaMalloc(&gpu_buf, cap));
ibv_mr* gpu_mr = c.reg(gpu_buf, cap, IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE);

// 後：pinned host memory（GB10 RDMA 路徑）
CUDA_OK(cudaHostAlloc(&gpu_buf, cap, cudaHostAllocPortable | cudaHostAllocMapped));
ibv_mr* gpu_mr = c.reg(gpu_buf, cap, IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE);

// GPU 讀寫改用 NVLink-C2C DevicePointer（~900GB/s），host 端直接用 gpu_buf 指標
// 框架驗證後，cudaMemcpy DeviceToHost 替換為直接存取 pinned pointer
```

同步更新：`cudaFree(gpu_buf)` → `cudaFreeHost(gpu_buf)`。

**原因**：此為 GB10 不變式（CLAUDE.md 不變式 11，2026-06-11 驗證時已記錄）；
t40_e2e_server 仍沿用舊版 cudaMalloc，Step 2 執行時再次觸發，補 patch。

---

### 2. `grab/src/rdma_sender.cpp`：`send_frame()` 加 try/catch

**問題**：server 斷線後（t40_e2e_server 收到 20 幀自動退出），Grab 的取像 thread 仍嘗試
呼叫 `conn_.poll_one()`，因 WR flush 拋出 `std::runtime_error`，未被捕捉導致
`std::terminate`（abort crash）。

**修改**：

```cpp
// 斷線後優雅跳過，不 crash grab thread
try {
    conn_.post_write_imm(...);
    conn_.poll_one();
} catch (const std::exception& e) {
    if (connected_) {
        fprintf(stderr, "[rdma_sender] 發送失敗（seq=%llu）：%s\n", frame_seq, e.what());
        connected_ = false;  // 後續幀跳過
    }
    return;
}
```

**效果**：server 斷線後 grabbed 繼續到 33，sent_frames 停在 20，不 crash。

---

## 八、架構決策（本次確認）

| 決策 | 採用值 | 理由 |
|------|--------|------|
| Grab 端口方向 | Grab 開 8100，Control 主動連入 | 比照 IP 端 8200 模式 |
| RDMA port | 18515 | 沿用 t40_e2e 驗證值 |
| Ring buffer | single-slot（逐幀 poll_one）| Step 2 簡化；N-slot 留 Step 3 |
| FrameHeader | 手填（不用 make_frame_header）| frameSeq 需 uint64，make_frame_header 只接 uint16 |
| GB10 記憶體 | cudaHostAlloc(Portable\|Mapped) | nvidia_peermem 在 GB10 不可用（不變式 11）|

---

## 九、狀態分級更新（meta #0）

| 路徑 | 更新前 | 更新後 | 證據 |
|------|--------|--------|------|
| 正式 cfaoi_grab 單相機 → RDMA → Spark | L0 | **L4** | 本次實機驗證，CRC 20/20，FAIL=0 |
| shared/FrameHeader.h 真機 RDMA 收發 | L2（編譯驗證）| **L4** | 本次 cfaoi_grab 實際以此 Header 傳輸，Spark 端 magic/CRC 全對 |
| RDMA 收發實作（正式主程式 RdmaSender）| L0 | **L4** | rdma_sender.cpp 單相機路徑實機驗通 |

---

## 十、下一步：Step 3（Switch 到貨後）

- 條件：SN2201 交換器到貨 + 多台 raL8192 接線
- `cfaoi_grab --cam-count ALL`（37 台相機全陣列）
- N-slot ring buffer（配合連續多相機流量）
- Control↔Grab 8100 完整接線（目前本次 hardcode 觸發）
- IP 端加入 rdma-validate 模式（目前由 t40_e2e_server 代）
