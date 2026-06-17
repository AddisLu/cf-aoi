# Grab 程式完整說明

> 版本：2026-06-17 整理（逐檔靜態分析 grab/src ~1018 行 + cfaoi_phase1 考古對照）
> 專案：`grab/CMakeLists.txt`（C++17）→ 可執行檔 `cfaoi_grab` + `rdma_nslot_test`
> 技術棧：C++ + pylon SDK（Basler GigE）+ libibverbs / librdmacm（RoCE v2）+ nlohmann_json
> 平台：截取中心 PC `damac`（x86 Linux，Ryzen 7700 + ConnectX-5）

---

## 目錄

1. [系統概述](#1-系統概述)
2. [架構對比：cfaoi_phase1 測試套件 → Grab](#2-架構對比cfaoi_phase1-測試套件--grab)
3. [整體分層架構](#3-整體分層架構)
4. [軟體流程圖](#4-軟體流程圖)
5. [主要模組職責](#5-主要模組職責)
6. [執行模式與命令列參數](#6-執行模式與命令列參數)
7. [RDMA N-slot ring buffer + 背壓握手](#7-rdma-n-slot-ring-buffer--背壓握手)
8. [TCP 協議（Control ↔ Grab，port 8100）](#8-tcp-協議control--grab-port-8100)
9. [FrameHeader wire format](#9-frameheader-wire-format)
10. [功能對照表：phase1 → grab（含尚未升級項）](#10-功能對照表phase1--grab含尚未升級項)
11. [關鍵不變式](#11-關鍵不變式)
12. [建置與平台支援](#12-建置與平台支援)
13. [驗證狀態](#13-驗證狀態)
14. [關鍵檔案索引](#14-關鍵檔案索引)

---

## 1. 系統概述

Grab 是 CF-AOI 分散式架構的**取像節點**，跑在截取中心 PC（`damac`，x86 Linux），負責：

- 用 **pylon SDK** 驅動 Basler **raL8192-12gm** 線掃相機取像（GigE）。
- 把每幀組成 **FrameHeader（256B, magic 0xA01CF00D）+ payload**，算 **CRC32**。
- 經 **RDMA（RoCE v2, WRITE_WITH_IMM）** 直送 IP 端（DGX Spark GB10）的 N-slot ring buffer。
- 提供 **TCP JSON server（port 8100）** 給 Control：載入配方（panel_id）、開始/停止取像、回報狀態。

核心策略：**從 `Reference/cfaoi_phase1/` 的已驗證測試工具升級為生產等級**。
單相機路徑（pylon → RDMA → Spark）2026-06-15 Step 2 實機驗通（CRC 20/20，L4）。

與 phase1 測試套件最大不同：
- 測試腳本（單次 500 幀、存 .raw）→ **持續 grab thread + callback**，移除檔案儲存。
- 單一大 buffer 逐幀同步收 → **N-slot ring buffer + credit 背壓握手（MrInfoEx）**（Step 3 新增）。
- 無控制面 → **control_server（TCP JSON 8100）+ IDLE/GRABBING 狀態機**。

> ⚠️ **本程式目前只實作單相機 pylon 路徑**。多相機陣列（`cam_manager`）、eBUS（`cam_ebus`）、
> MAC Persistent IP 綁定（`mac_ip_binder`）、frame_assembler、control_client 等
> grab/CLAUDE.md §1/§6 規劃的檔案**均未建檔**（見 §10）。Step 3 待 SN2201 Switch + 相機陣列到貨。

---

## 2. 架構對比：cfaoi_phase1 測試套件 → Grab

```
Reference/cfaoi_phase1（取像+RDMA 硬體驗證測試套件，原名 phase1_tests）
┌──────────────────────────────────────────────────┐
│ 相機偵測   t30_pylon_probe / t30_ebus_probe        │
│ 相機取像   t31_pylon_grab（單次 500 幀，存 .raw）   │  ← FPS/drop 統計
│            t31_ebus_grab（iPORT）                   │
│ RDMA→GPU  t21_rdma_gpu_{client,server}（CRC 驗）   │
│ 端到端     t40_e2e_{client_pylon,client_file,server}│
│ wire       FrameHeader.h（magic 0xA01CF00D）        │  ← 唯一真相
│            rdma_common.h（RcConn 連線樣板 + MrInfo） │
│ 機況腳本   00_check_env / 10_rdma_linkcheck / ...   │
│ 單一 buffer 逐幀同步、無背壓、無控制面               │
└──────────────────────────────────────────────────┘
                      ↕ 升級（保留 pylon/RDMA 樣板，加生產外殼）
CF-AOI 分散式架構 Grab 程式（本文件）
┌──────────────────────────────────────────────────┐
│ main.cpp（IDLE/GRABBING 狀態機，CLI 解析）          │
│   CamPylon（持續 grab thread + callback + FPS/drop）│
│   RdmaSender（N-slot 定址 + MrInfoEx 握手 + 背壓）  │
│   ControlServer（TCP JSON @8100：CHECK_HEALTH/      │
│                  LOAD_RECIPE/GRAB_START/GRAB_STOP） │
│   RcConn（rdma_common.h，沿用 phase1 + MrInfoEx）   │
│   shared/FrameHeader.h（= phase1 版 + 相容層）      │
│ rdma_nslot_test（合成幀送器，免相機，驗 N-slot）    │
└──────────────────────────────────────────────────┘
        ↑ TCP JSON 8100                ↓ RDMA RoCE v2（18515）
   ┌────┴─────┐                   ┌────┴──────────────┐
   │ CONTROL  │                   │ IP（DGX Spark GB10）│
   └──────────┘                   │ rdma_source N-slot │
                                  └────────────────────┘
```

---

## 3. 整體分層架構

```
╔═══════════════════════════════════════════════════════╗
║ 進入點 / 狀態機 (main.cpp)                            ║
║   parse CLI → 建 CamPylon / RdmaSender / ControlServer║
║   狀態：IDLE ⇄ GRABBING（mutex 保護，跨 thread）      ║
║   每幀 callback：++frame_seq → sender.send_frame()    ║
╚═══════════════════════════════════════════════════════╝
        ↑ 命令                       ↓ 影像幀
╔══════════════════════════╗  ╔══════════════════════════╗
║ control_server.{h,cpp}   ║  ║ cam_pylon.{h,cpp}        ║
║  TCP JSON server @8100   ║  ║  pylon 開相機/grab thread║
║  CHECK_HEALTH/LOAD_RECIPE║  ║  GrabStrategy_OneByOne   ║
║  GRAB_START/GRAB_STOP    ║  ║  FPS/drop 統計、callback ║
╚══════════════════════════╝  ╚══════════════════════════╝
                                        ↓ uint8 Mono8 payload
╔═══════════════════════════════════════════════════════╗
║ RDMA 發送層 (rdma_sender.{h,cpp})                    ║
║  connect()：收 MrInfoEx（N-slot 握手）               ║
║  send_frame()：填 FrameHeader+CRC32 → N-slot 定址     ║
║               → post_write_imm → poll_one（背壓點）   ║
╚═══════════════════════════════════════════════════════╝
        ↓ RcConn（rdma_common.h：RC QP / WRITE_WITH_IMM）
╔═══════════════════════════════════════════════════════╗
║ wire (shared/FrameHeader.h — 兩端共用 256B)          ║
║  magic 0xA01CF00D / crc32_ieee（0xEDB88320）         ║
╚═══════════════════════════════════════════════════════╝
```

---

## 4. 軟體流程圖

### 4.1 啟動 / 狀態機（main.cpp:47-191）

```
main()
  ├─ parse CLI（--rdma-dest 必填；--cam-id/--serial/--pkt-size/--ctrl-port）
  ├─ 建 CamPylon cam / RdmaSender sender / ControlServer ctrl(ctrl_port)
  ├─ cam.set_frame_callback(...)   每幀：seq=++frame_seq → sender.send_frame(...)
  ├─ ctrl.set_load_recipe(...)     更新 panel_id + panel_hash（FNV）
  ├─ ctrl.set_grab_start(...)      開相機 + RDMA connect + cam.start() → grabbing=true
  ├─ ctrl.set_grab_stop(...)       cam.stop() + sender.disconnect() → grabbing=false
  ├─ ctrl.set_status_provider(...) 回 {grabbing,grabbed,dropped,sent_frames,sent_bytes}
  ├─ ctrl.start()                  監聽 0.0.0.0:8100（失敗 → 1）
  └─ 主迴圈：每 200ms 檢查 g_shutdown（SIGINT/SIGTERM）→ 清理退出
```

### 4.2 GRAB_START 路徑（main.cpp:109-130）

```
Control 送 GRAB_START
  → ctrl.set_grab_start callback（state_mtx 鎖）
      ├─ 已 grabbing → err="already grabbing"，回 ERR
      ├─ cam.open(serial, pkt_size)        pylon 開相機，讀 PayloadSize（失敗→ERR）
      ├─ sender.connect(host, port, payload_size)  RDMA 連 + 收 MrInfoEx（失敗→ERR）
      ├─ frame_seq=0；cam.start(cam_id)     啟動 grab thread
      └─ grabbing=true，回 OK
```

### 4.3 取像迴圈（cam_pylon.cpp:79-131，跑在 grab thread）

```
grab_loop()
  MaxNumBuffer=16；StartGrabbing(GrabStrategy_OneByOne)   持續，不限幀數
  while (!stop_flag && IsGrabbing):
    RetrieveResult(2000ms, TimeoutHandling_Return)
    GrabSucceeded:
       ++grabbed
       drop 偵測：BlockID 不連續 + GetNumberOfSkippedImages（取兩者）
       cb_(cam_id, buffer, ImageSize, Width, Height)   → main callback → sender.send_frame
    每 5 秒印 FPS/grabbed/dropped
```

### 4.4 發送一幀（rdma_sender.cpp:47-99）

```
send_frame(cam_id, seq, panel_hash, payload, bytes, w, h):
  ├─ 手填 FrameHeader（frameSeq=u64，故不用 make_frame_header）
  │    magic/version/headerBytes/frameSeq/panelId/camId/width/height/bitDepth=8/pixelFormat=0
  │    crc32 = crc32_ieee(payload, bytes)
  ├─ memcpy [FrameHeader(256B) || payload] → txbuf_
  ├─ slot_id    = frame_seq % n_slots
  │  write_addr = remote_.addr + slot_id × slot_size      ← N-slot 定址
  ├─ post_write_imm(txbuf, total, write_addr, rkey, imm=frame_seq)
  └─ poll_one()    ← 背壓點：IP credit 耗盡時此處阻塞（RNR, rnr_retry=7=∞）
     失敗 → connected_=false（停止後續嘗試）
```

---

## 5. 主要模組職責

| 模組 | 檔案 | 職責 |
|------|------|------|
| `main` | `src/main.cpp` | CLI 解析、IDLE/GRABBING 狀態機、每幀 callback 串接、信號處理清理 |
| `CamPylon` | `src/cam_pylon.{h,cpp}` | pylon 開相機（auto/SN）、`GevSCPSPacketSize` 設定、持續 grab thread、FPS/drop 統計、frame callback |
| `RdmaSender` | `src/rdma_sender.{h,cpp}` | RDMA 連線、收 `MrInfoEx` 握手、填 FrameHeader+CRC32、N-slot 定址 `post_write_imm`、背壓 `poll_one` |
| `ControlServer` | `src/control_server.{h,cpp}` | TCP JSON server @8100；newline-JSON；CHECK_HEALTH/LOAD_RECIPE/GRAB_START/GRAB_STOP；callback 注入 |
| `RcConn` / `MrInfoEx` | `src/rdma_common.h` | librdmacm RC 連線樣板（make_qp/reg/connect/post_write_imm/poll_one）；256B N-slot 握手結構（沿用 phase1 + Step 3 擴充）|
| `rdma_nslot_test` | `src/rdma_nslot_test.cpp` | 合成幀送器（256×256 小幀），免相機驗 N-slot ring 繞回 + 背壓（delay_ms 模擬慢消費）|

> **wire format**：`shared/FrameHeader.h`（grab 從上一層 include，與 IP 端同一份）。

---

## 6. 執行模式與命令列參數

### 6.1 cfaoi_grab（生產主程式，main.cpp:47-74）

| 旗標 | 預設 | 效果 |
|------|------|------|
| `--rdma-dest IP:PORT` | （必填）| Spark IP 端 RDMA server（如 `192.168.3.1:18515`）|
| `--cam-id N` | 0 | 相機 cam_id（寫入 FrameHeader.camId）|
| `--serial STRING` | auto | pylon 序號；`auto`=第一台 |
| `--pkt-size N` | 8192 | `GevSCPSPacketSize`（GigE jumbo）|
| `--ctrl-port N` | 8100 | 等 Control 連入的 TCP port |

回傳碼：0 OK｜1 參數錯/缺 `--rdma-dest`/ControlServer 啟動失敗。

> ⚠️ grab/CLAUDE.md §7 啟動範例提到的 `--cam-count`/`--cam-ids`/`--sdk ebus`/`--config`
> **目前 main.cpp 未實作**（單相機 pylon 寫死）。屬 Step 3 多相機規劃，尚未建。

### 6.2 rdma_nslot_test（合成幀送器，免相機）

```bash
rdma_nslot_test <server_ip> <port> <num_frames> [width] [height] [delay_ms]
# delay_ms > 0 → 每幀後延遲，模擬慢送驗背壓
```

---

## 7. RDMA N-slot ring buffer + 背壓握手

> Step 3 核心（2026-06-17 damac↔Spark 實機驗通 L3，commit `de047a3`）。grab/CLAUDE.md 不變式 7 / ip/CLAUDE.md 不變式 23。

### 7.1 握手：MrInfoEx（256 bytes，rdma_common.h:45-54）

連線時 IP server **SEND** 一個 `MrInfoEx` 給 Grab，取代 phase1 的單一 `MrInfo`：

| 欄位 | 說明 |
|------|------|
| `addr` | N-slot ring buffer 基底位址（IP 端 cudaHostAlloc pinned）|
| `rkey` | 整塊 buffer 的 RDMA 授權金鑰（一個 MR 涵蓋全部 N slot）|
| `len` | 整塊大小 = `n_slots × slot_size` |
| `n_slots` | slot 數量（RDMA ring 深度，預設 4）|
| `slot_size` | 每 slot 大小 = `sizeof(FrameHeader) + max_payload` |
| `pad[228]` | 對齊至 256 bytes |

Grab `connect()` 收到後驗證 `n_slots != 0 && slot_size >= frame_cap`（rdma_sender.cpp:27），否則拒絕連線。

### 7.2 N-slot 定址 + credit 背壓

```
Grab 端（rdma_sender.cpp:80-87）：
  slot_id    = frame_seq % n_slots          連續 N 幀佔 N 個不同 slot
  write_addr = addr + slot_id × slot_size
  post_write_imm(... imm=frame_seq) → poll_one()   ← 背壓阻塞點

IP 端（ip/src/image_source/rdma_source.cpp）：
  預掛 N 個 post_recv = N 個初始 credit
  recv_thread 順序（不可換）：memcpy slot → push_blocking → post_recv（補 credit）

背壓鏈：
  IP 消費慢 → FrameQueue 滿 → push_blocking 阻塞 → 不 post_recv
  → credit 耗盡 → Grab 下一幀 WRITE_WITH_IMM → RNR（rnr_retry_count=7=∞）
  → Grab poll_one() 阻塞 → 自然背壓（無需額外控制通道）
```

### 7.3 斷線偵測

RoCE v2（非 IB）下 Grab 斷線後 IP 端 `IBV_WC_WR_FLUSH_ERR` **不保證即時出現** → IP `rdma_source` 在 no-event 分支輪詢 CM event channel（`check_cm_disconnect()`，commit `de047a3`），否則 recv_thread 永不退出。

**實測（2026-06-17）**：Phase 1 連續 120 幀 CRC=OK（1375fps/86MB/s，slot 0→3 繞回正確）；Phase 2 背壓（IP `--test-consumer-delay-ms 200`）20 幀 ok=20 err=0，Grab 降至 9.6fps（非斷線），QP 未進 error state。

---

## 8. TCP 協議（Control ↔ Grab，port 8100）

格式：`{"cmd":..,"seq":..,"params":{..}}\n`，回應 `{"seq":..,"status":"OK"|"ERR",...}\n`（newline-delimited JSON，與 IP 端 control_server 同模式）。單一 accept thread、一次服一個 client。

| `cmd` | 輸入 params | 動作 / 回應 |
|-------|------------|------------|
| `CHECK_HEALTH` | 無 | 回 `status=OK`；若有 status_provider，附 `data`={grabbing, grabbed, dropped, sent_frames, sent_bytes} |
| `LOAD_RECIPE` | `recipe`, `panel_id` | 更新 panel_id + panel_hash（不取像）；回 `status=OK` |
| `GRAB_START` | `timeout_ms`（預設 40000）| 開相機+RDMA+grab thread；成功 `OK`，失敗 `ERR`+`error` |
| `GRAB_STOP` | 無 | 停相機+斷 RDMA；回 `status=OK` |
| 未知 cmd | — | `status=ERR`, `error="unknown command: ..."` |

> ⚠️ STATUS.md：Control↔Grab 8100 完整接線為 **L1**（control_server 寫好、命令解析正確，但實機以 `nc` hardcode 觸發，**未接真正 Control UI**）。Control 端 `GrabClient` 目前只有 CHECK_HEALTH（取像命令 Step 2+ 待擴充）。

---

## 9. FrameHeader wire format

`shared/FrameHeader.h`（256 bytes，magic `0xA01CF00D`，version 2）= cfaoi_phase1 實機驗證版，兩端共用。
**逐欄與 `Reference/cfaoi_phase1/FrameHeader.h` 完全相同**（考古抽查確認）；差異僅在 shared 版「附加」的非-wire 相容層 `frame_panel_hash()`（FNV-1a）/ `make_frame_header()`（phase1 版無）。

- Grab `send_frame` **手填**（不用 `make_frame_header`，因 frameSeq 需 u64），`crc32 = crc32_ieee(payload, bytes)`（多項式 0xEDB88320）。
- `panelId` = `frame_panel_hash(panel_id 字串)`；`bitDepth=8`、`pixelFormat=0`（Mono8）；`sliceIndex=0`/`totalSlice=1`（Phase-2 單幀，未切 slice）；`ptpTimestampNs=0`（無 PTP）。

詳見 docs/CLAUDE.md §5 FrameHeader 定義（不變式 2/3）。

---

## 10. 功能對照表：phase1 → grab（含尚未升級項）

> ✅=已升級到生產 / 📦=停在測試套件 / ❌=規劃但未建檔。L-level 對齊 STATUS.md。

| phase1 測試工具/能力 | phase1 位置 | 現行 grab 對應 | 狀態 | L-level |
|---|---|---|---|---|
| FrameHeader 256B wire + crc32_ieee | `FrameHeader.h:19-62` | `shared/FrameHeader.h`（+相容層）| ✅ | L4 |
| RcConn RDMA-CM 連線樣板 | `rdma_common.h:38-171` | `grab/src/rdma_common.h:56-189`（逐行沿用 + MrInfoEx）| ✅ | L4 |
| pylon 相機偵測（t30_probe）| `t30_pylon_probe.cpp:22-61` | `cam_pylon.cpp:14-51`（整合進 open）| ✅ | L4 |
| pylon 取像 + FPS/drop（t31_grab）| `t31_pylon_grab.cpp:32-159` | `cam_pylon.cpp:79-131`（持續 thread，移除 .raw）| ✅ | L4 |
| RDMA 發送（t40_client_pylon）| `t40_e2e_client_pylon.cpp:64-93` | `rdma_sender.cpp` + `main.cpp`（N-slot 定址、重連旗標）| ✅ | L4（單相機）|
| RDMA→GPU 收圖 cudaHostAlloc（t21/t40_server）| `t21_rdma_gpu_server.cpp:48-95` | IP `rdma_source.cpp:40`（沿用手法）| ✅ | L4 |
| **N-slot ring + MrInfoEx 握手 + credit 背壓** | phase1 **無** | `rdma_common.h:45-54` + `rdma_sender.cpp:80-87` + IP `rdma_source` | 🆕 grab 新增 | L3 |
| **control_server（TCP JSON 8100）** | phase1 **無** | `control_server.cpp:65-182` | 🆕 grab 新增 | L1（未接真 Control）|
| **rdma_nslot_test 合成幀送器** | phase1 **無** | `rdma_nslot_test.cpp` | 🆕 grab 新增 | L3 |
| **eBUS 相機（iPORT，L803K）** | `t30/t31_ebus_grab.cpp` | **無 cam_ebus.cpp** | ❌ 未建檔 | L0（eBUS SDK 未裝）|
| **多相機陣列 / cam_manager** | t31 main() 邏輯 | **無 cam_manager.{h,cpp}** | ❌ 未建檔 | L0（Step 3 待 Switch）|
| **MAC Persistent IP 綁定** | grab/CLAUDE.md §1 列為 `t01_pylon_mac_setup` | **無 mac_ip_binder** | ❌ 雙缺* | L0 |
| **file replay（檔案→RDMA）** | `t40_e2e_client_file.cpp:35-168` | **無**（grab 無檔案送器）| 📦 測試套件 | — |
| **frame_assembler / control_client** | grab/CLAUDE.md §6 規劃 | **無**（main.cpp 內聯 callback；grab 是 control_**server** 非 client）| ❌ 未建檔 | — |
| 機況腳本（00/10/11/20/30）| `*.sh` | 無遷移（保留為機況確認 Agent）| 📦 測試套件 | 10/11 L4；20/30 無 GB10 數據 |

> \* **MAC Persistent IP 綁定「雙缺」**：grab/CLAUDE.md §1 把它列為遷移來源 `phase1_tests/src/t01_pylon_mac_setup/`，
> 但 `Reference/cfaoi_phase1/` 目錄與整個 Reference 樹下 `find` **不到任何 t01/mac_setup 檔**——此能力從未存在於程式碼（文件懸空引用）。

**重點：grab 相對 phase1 的最大新增 = N-slot ring buffer**（phase1 server 是單一大 buffer、逐幀同步、無背壓、無 recv_thread 解耦）。

---

## 11. 關鍵不變式

> 完整 8 條見 [grab/CLAUDE.md §8](../grab/CLAUDE.md)。重點：

1. `cam_pylon.cpp` 與（未來）`cam_ebus.cpp` **禁止互相 include**（SDK 嚴格分離）。
2. `FrameHeader.h` 必須與 IP 端同版（從 `shared/` 引用），`sizeof==256`、magic `0xA01CF00D`。
3. Grab 無 UI，所有設定從 Control 命令或 CLI 來。
4. RDMA NIC link_layer 必須是 `Ethernet`（RoCE v2）。
5. 相機 NIC MTU 9000；RDMA NIC 不需 jumbo。
6. **GB10 不可用 `nvidia_peermem`，IP 端改 `cudaHostAlloc(Portable|Mapped)`**（2026-06-11 實機驗證，見 docs/CLAUDE.md 不變式 11）。
   ⚠️ **已知文件債**：`grab/src/rdma_common.h:87` 的 `reg()` 註解仍寫「GPU 記憶體失敗多半是 nvidia_peermem 未載入」（phase1 殘留）——在 GB10 上不適用。**不影響功能**（grab 端註冊的是一般 host txbuf，不觸發該路徑；GPU 記憶體是 IP 端的事），但移植/閱讀時應留意。
7. **N-slot ring + MrInfoEx 握手 + credit 背壓**（§7，2026-06-17 L3）：`slot_id = frame_seq % n_slots`，`poll_one` = 背壓點。
8. （IP 端配對）credit 補充順序 `memcpy → push_blocking → post_recv` 不可換序。

---

## 12. 建置與平台支援

### 12.1 依賴與建置（`CMakeLists.txt`）

- **REQUIRED**（缺則 `FATAL_ERROR`）：pylon SDK（/opt/pylon 或 `PYLON_ROOT`）、libibverbs、librdmacm、nlohmann_json。
- pylon 偵測三法：`find_package(pylon)` → `PYLON_ROOT` 環境變數 → `pylon-config`。
- 兩個目標：`cfaoi_grab`（main+cam_pylon+rdma_sender+control_server）、`rdma_nslot_test`（rdma_nslot_test+rdma_sender，**不需 pylon**）。

```bash
cd grab
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j$(nproc)        # 產出 build/cfaoi_grab + build/rdma_nslot_test
```

### 12.2 平台

| 角色 | 主機 | NIC | 說明 |
|------|------|-----|------|
| 截取中心 | `damac`（Ryzen 7700, x86 Linux）| ConnectX-5 MCX516A | pylon 取像 + RDMA 發送端 |
| 相機 | Basler raL8192-12gm（SN 25445953）| GigE 192.168.5.1 | 8192 寬線掃；pylon 26.05 |
| 鏈路 | damac↔Spark 100G DAC（192.168.3.x）| RoCE v2 | 目前 1 台相機直連、無 SN2201 Switch |

---

## 13. 驗證狀態

| 項目 | 狀態 | 證據 |
|------|------|------|
| 單相機 pylon→RDMA→Spark（cam_pylon+rdma_sender+control_server+main）| **L4** | 2026-06-15 Step 2 實機：raL8192-12gm → FrameHeader(0xA01CF00D)+CRC32 → RDMA 18515 → Spark GB10 pinned → CRC **20/20 FAIL=0**（見 [Step 2 報告](verification/verification_report_step2_20260615.md)）|
| rdma_nslot_test（N-slot ring + 背壓）| **L3** | 2026-06-17 damac↔Spark：120 幀 CRC=OK；背壓 20 幀 ok=20 err=0；CM 斷線乾淨退出（commit `de047a3`）|
| Control↔Grab 8100 完整接線 | **L1** | control_server 寫好、命令解析正確；以 `nc` hardcode 觸發，未接真 Control UI |
| 底層能力（相機/RDMA/端到端，Phase-1 測試套件）| **L4** | 見 [Phase-1 驗證報告](verification/verification_report_20260611.md)（零掉幀 500 幀、CRC 一致、1.44μs）|
| 多相機全陣列 / eBUS / mac_ip_binder | **L0** | 未實作；Step 3 待 Switch + 相機陣列 |

詳見 [docs/STATUS.md](STATUS.md)。

---

## 14. 關鍵檔案索引

| 主題 | 檔案 |
|------|------|
| 進入點 / 狀態機 | [src/main.cpp](../grab/src/main.cpp) |
| pylon 相機 | [src/cam_pylon.cpp](../grab/src/cam_pylon.cpp) / [.h](../grab/src/cam_pylon.h) |
| RDMA 發送 | [src/rdma_sender.cpp](../grab/src/rdma_sender.cpp) / [.h](../grab/src/rdma_sender.h) |
| RDMA 連線樣板 + MrInfoEx | [src/rdma_common.h](../grab/src/rdma_common.h) |
| TCP 命令 server | [src/control_server.cpp](../grab/src/control_server.cpp) / [.h](../grab/src/control_server.h) |
| N-slot 合成測試 | [src/rdma_nslot_test.cpp](../grab/src/rdma_nslot_test.cpp) |
| wire format | [shared/FrameHeader.h](../shared/FrameHeader.h) |
| 建置 | [CMakeLists.txt](../grab/CMakeLists.txt) |
| 不變式 | [grab/CLAUDE.md](../grab/CLAUDE.md) |

---

*本文件由 grab/src 逐檔靜態分析整理（~1018 行），對照 `Reference/cfaoi_phase1/` 考古（FrameHeader 逐欄比對、t30/t31/t40 對映）+ STATUS.md L-level。格式對齊 [ip_程式完整說明.md](ip_程式完整說明.md) / [control_程式完整說明.md](control_程式完整說明.md)。*
