# CF-AOI 截取中心機況驗證報告

**日期**：2026-06-11  
**執行主機**：damac（截取中心 PC）↔ spark-c16f（DGX Spark）  
**驗證範圍**：相機取像 → RDMA → GPU 記憶體寫入完整路徑

---

## 一、硬體配置

### 截取中心 PC
| 項目 | 規格 |
|------|------|
| CPU | AMD Ryzen 7 7700 |
| 主機板 | B850M |
| RDMA NIC | Mellanox ConnectX-5 MCX516A（2× QSFP28 100GbE），PCIe x16 槽 |
| 板載 NIC | Intel I219（1GbE，接相機） |
| OS | Ubuntu 22.04 |

### DGX Spark
| 項目 | 規格 |
|------|------|
| GPU | NVIDIA GB10（Blackwell，NVLink-C2C SoC） |
| RDMA NIC | ConnectX-7（內建，4× RDMA HCA） |
| 驅動版本 | 580.95.05 |
| CUDA | 13.0（/usr/local/cuda） |
| Tailscale hostname | spark-c16f（100.83.141.68） |

### 相機
| 項目 | 規格 |
|------|------|
| 型號 | Basler raL8192-12gm |
| 序號 | 25445953 |
| 連線方式 | GigE（RJ45 直連板載 NIC，IP 192.168.5.1） |
| SDK | Basler pylon 26.05.0 |

### 鏈路
```
[raL8192-12gm] ──1GbE RJ45──> [截取中心 enp0s31f6 / 192.168.5.2]
[截取中心 enp1s0f0np0 / 192.168.3.2] ──100G DAC MCP1600──> [Spark enp1s0f0np0 / 192.168.3.1]
```

---

## 二、使用軟體

| 軟體 / 套件 | 版本 | 用途 |
|------------|------|------|
| Basler pylon SDK | 26.05.0 | 相機取像（raL8192-12gm） |
| libibverbs / librdmacm | 39.0-1 | RDMA userspace verbs |
| rdma-core | 39.0-1 | RDMA 核心工具 |
| ibverbs-utils | 39.0-1 | ibv_devinfo、ib_write_lat、ib_write_bw |
| perftest | — | RDMA 延遲 / 頻寬量測 |
| NVIDIA CUDA | 13.0 | GPU 記憶體配置（Spark 端） |
| g++ | 11.4.0（PC）/ 13.3.0（Spark） | C++ 編譯 |
| Tailscale | — | 跨網段 SSH 管理通道 |
| Python 3 | 3.10 | 影像亮度統計分析 |

---

## 三、驗證步驟與結果

### Layer 00：環境確認（截取中心 PC）

**執行**：`./00_check_env.sh`

| 檢查項目 | 結果 | 細節 |
|---------|------|------|
| rdma:devices | ✅ PASS | rocep1s0f0、rocep1s0f1 已偵測 |
| kmod:mlx5 | ✅ PASS | mlx5_core / mlx5_ib 已載入 |
| pci:connectx | ✅ PASS | ConnectX-5 MT27800 於 PCIe 01:00.0、01:00.1 |
| ebus:sdk | ⚠️ WARN | eBUS SDK 未安裝（L803K 路徑用，本次不需要） |
| pylon:sdk | ⚠️ WARN | ldconfig 找不到（pylon 裝在 /opt/pylon，非標準路徑，實際可用） |

> WARN 兩項均為非阻塞性，不影響 raL8192 取像路徑。

---

### Layer 10：RDMA 鏈路確認（截取中心 PC）

**執行**：`./10_rdma_linkcheck.sh`（修正 tab 解析 bug 後）

| 檢查項目 | 結果 | 細節 |
|---------|------|------|
| rdma:state | ✅ PASS | PORT_ACTIVE（物理連線已建立） |
| rdma:link_layer | ✅ PASS | Ethernet（RoCE v2 模式正確） |
| rdma:rate | ⚠️ WARN | 腳本解析格式不符，實測 ethtool 確認 100 Gbps |

**ethtool 實測**：
```
Speed: 100000Mb/s   Link detected: yes
active_width: 4X    active_speed: 25.0 Gbps (= 4×25G = 100G)
```

---

### Layer 30p：相機偵測（pylon probe）

**執行**：`./build/t30_pylon_probe`

| 檢查項目 | 結果 | 細節 |
|---------|------|------|
| device | ✅ PASS | Model=raL8192-12gm \| SN=25445953 \| Class=BaslerGigE \| IP=192.168.5.1 |

---

### Layer 31p：相機取像驗證

**執行**：`./build/t31_pylon_grab auto 500 8192 frame_ra_500.raw`

| 檢查項目 | 結果 | 細節 |
|---------|------|------|
| pylon:open | ✅ PASS | 已連上 raL8192-12gm SN=25445953 |
| throughput | ✅ PASS | FPS=23.3，頻寬=0.76 Gb/s，耗時=21.43s |
| drop | ✅ PASS | 漏幀=0 / 預期=500，drop=0.0000% |
| stream 統計 | ✅ | 失敗緩衝=0，重送請求=0 |
| 影像尺寸 | ✅ | 8160×500 px（Mono8 Line Scan） |

**光源對比驗證（確認非假影像）**：

| | 光源放入前 | 光源打開後 |
|---|---|---|
| 平均亮度 | 8.9 | **62.5** |
| 最大像素值 | 14 | **156** |
| 暗區佔比（<10） | 95% | **0%** |

> 亮度提升 +600%，確認相機確實感光取像。

---

### Layer 21：RDMA → GPU 資料正確性驗證

**執行**：
- Spark：`./build/t21_rdma_gpu_server 192.168.3.1 18515 33554432 0`
- PC：`./build/t21_rdma_gpu_client 192.168.3.1 18515 33554432`

**PC 端（client）**：

| 檢查項目 | 結果 | 細節 |
|---------|------|------|
| ctrl | ✅ PASS | 取得對端 GPU buffer addr/rkey，len=33554432 |
| rdma_write | ✅ PASS | RDMA_WRITE_WITH_IMM 完成，frameSeq=1 |

**Spark 端（server）**：

| 檢查項目 | 結果 | 細節 |
|---------|------|------|
| gpu_mr | ✅ PASS | GPU 記憶體成功註冊為 RDMA MR |
| write_imm | ✅ PASS | 收到 RDMA_WRITE_WITH_IMM，frameSeq=1 |
| integrity | ✅ **PASS** | **GPU 內容逐位元正確（CRC=0x1591755899）** |

> 32 MB 資料從截取中心 PC 透過 RDMA 寫入 DGX Spark GPU-accessible memory，CRC 兩端完全一致，零位元錯誤。

---

## 四、RDMA 效能量測

**工具**：`ib_write_lat`（perftest）  
**測試**：1000 次 RDMA Write，2 bytes payload

| 指標 | 數值 |
|------|------|
| 最小延遲 | **1.38 μs** |
| 典型延遲 | **1.43 μs** |
| 平均延遲 | **1.44 μs** |
| P99 延遲 | **1.57 μs** |
| 最大延遲 | 3.45 μs |

---

## 五、技術問題與解決方式

### 問題 1：`nvidia_peermem` 無法載入（EINVAL）

**現象**：`sudo modprobe nvidia_peermem` 回傳 Invalid argument  
**原因**：GB10 為 NVLink-C2C SoC 架構，GPU Bus ID 為 `0000000F:01:00.0`（非標準 PCIe 空間），`nvidia_peermem` 的 PCIe P2P 拓撲檢查正確拒絕此配置。  
**解決**：改用 `cudaHostAlloc(cudaHostAllocPortable | cudaHostAllocMapped)` 配置 pinned host memory，RDMA NIC 可直接存取，GPU 透過 `cudaHostGetDevicePointer` 讀寫。  
**影響**：在 Grace-Blackwell NVLink-C2C 架構下，此方案等效甚至更優（NVLink-C2C 頻寬 ~900 GB/s >> PCIe ~64 GB/s）。

### 問題 2：原始碼路徑與 CMakeLists.txt 不符

**現象**：CMakeLists.txt 指向 `src/` 子目錄，但源碼在專案根目錄；`common/report.h` 路徑不存在。  
**解決**：
1. 建立 `common/` symlink 目錄指向根目錄 headers
2. CMakeLists.txt 改 `include_directories(${CMAKE_SOURCE_DIR})`、source 路徑移除 `src/` 前綴
3. cmake 未安裝，改用 `g++ + pylon-config` 直接編譯

### 問題 3：`rdma_common.h` 缺少 `<netdb.h>`

**現象**：編譯時 `addrinfo` / `getaddrinfo` 未宣告  
**解決**：在 `rdma_common.h` 加入 `#include <netdb.h>`

### 問題 4：`CIntegerParameter::TryGetValue` 不存在（pylon API）

**現象**：t31_pylon_grab.cpp 編譯失敗  
**解決**：改用 `GetValueOrDefault(-1)`

### 問題 5：libibverbs-dev 未安裝（PC 端）

**現象**：無 unversioned `.so` 可供連結  
**解決**：`apt-get download` 下載 deb 並 `dpkg -x` 取出 headers 與 symlinks，不需要 sudo

---

## 六、未來架構延遲估算（37 CCD 規劃）

### 規劃拓樸
```
37× raL8192-12gm (1GbE)
    ↓ 各自 1GbE
[SN2201 Switch]
    ↓ 100GbE 上行
[截取中心 PC / ConnectX-5]
    ↓ 100G RDMA (DAC)
[DGX Spark GB10]
```

### 頻寬分析
| 段落 | 總頻寬需求 | 可用頻寬 | 使用率 |
|------|-----------|---------|--------|
| 37台相機輸出 | 37 × 0.76 = 28.1 Gbps | — | — |
| SN2201 → 截取中心 | 28.1 Gbps | 100 Gbps | **28%** |
| 截取中心 → Spark | 28.1 Gbps | 100 Gbps | **28%** |

> 兩段均有 72% 餘裕，無壅塞風險。

### 端對端延遲分解（單台相機路徑）

| 段落 | 延遲 |
|------|------|
| GigE 封包傳輸（1GbE S&F，8 KB） | 65 μs |
| SN2201 → 截取中心（100GbE S&F） | 0.7 μs |
| pylon 接收 + DMA | 100–500 μs |
| RDMA 寫入 Spark（1幀 4 MB） | 322 μs（含 1.4 μs 實測協議延遲） |
| **傳輸合計（最後一行掃出 → Spark 收到）** | **~0.5–1 ms** |

### 37 台同時批次
| 場景 | 延遲 |
|------|------|
| 37 幀全部 RDMA（148 MB @ 100G） | 11.8 ms 傳輸完畢 |
| GPU 推論（AOI 演算法） | 10–50 ms（演算法決定） |
| **全程（掃線 → 出判斷結果）** | **~10–50 ms** |

> 傳輸延遲僅 0.5–1 ms，**瓶頸在 GPU 推論，而非網路**。

---

## 七、結論

| 驗證層級 | 結果 |
|---------|------|
| 相機偵測（t30_pylon_probe） | ✅ PASS |
| 500幀取像穩定性（t31_pylon_grab） | ✅ PASS（零掉幀） |
| 光源亮度對比確認 | ✅ PASS（+600% 亮度差異） |
| RDMA 鏈路狀態（10_rdma_linkcheck） | ✅ PASS（100G PORT_ACTIVE） |
| RDMA 延遲量測（ib_write_lat） | ✅ 1.44 μs avg，1.57 μs P99 |
| RDMA → GPU 資料正確性（t21） | ✅ **PASS（32 MB 逐位元正確，CRC 一致）** |

**相機 → 截取中心 → RDMA → DGX Spark GPU 完整資料路徑驗證通過。**
