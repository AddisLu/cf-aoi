# Grab 程式完整說明

> 版本：**2026-08-02 全面對齊現行程式**（逐檔靜態分析 grab/src 14 檔 ~2748 行；取代 2026-06-17 版——
> 該版的「單相機 only／WRITE_WITH_IMM／cam_manager 未建檔」敘述已全部過期）
> 專案：`grab/CMakeLists.txt`（C++17）→ `cfaoi_grab` + `rdma_nslot_test` + `image_replay_sender` + `probe_cam_nodes` + `cam_mean_gray_test`
> 技術棧：C++ + pylon SDK（Basler GigE）+ libibverbs / librdmacm（RoCE v2）+ nlohmann_json
> 平台：截取中心 PC `damac`（x86 Linux，Ryzen 7700 + ConnectX-5）；相機經 HPE FlexFabric 5945 交換機接入

---

## 目錄

1. [系統概述](#1-系統概述)
2. [架構對比：cfaoi_phase1 測試套件 → Grab](#2-架構對比cfaoi_phase1-測試套件--grab)
3. [整體分層架構](#3-整體分層架構)
4. [軟體流程圖](#4-軟體流程圖)
5. [主要模組職責](#5-主要模組職責)
6. [執行模式與命令列參數](#6-執行模式與命令列參數)
7. [RDMA N-slot ring buffer + SEND/RECV 背壓](#7-rdma-n-slot-ring-buffer--sendrecv-背壓)
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

- 用 **pylon SDK** 驅動 Basler 線掃相機陣列取像（GigE；已實測 raL8192-12gm + raL4096-24gm 混流）。
- **多相機管理（CamManager）**：`--cam-count N|ALL`、`cam_map.json` MAC↔cam_id 穩定映射（Gap #21 嚴格模式）、
  逐台平行 arm、每台收滿 `frames_per_panel` 張自動停。
- 把每幀組成 **FrameHeader（256B, magic 0xA01CF00D）+ payload**，算 **app-CRC32**（在 send_mtx 之外）。
- 經 **RDMA（RoCE v2，SEND/RECV 資料路徑）** 送 IP 端（DGX Spark GB10）的 N-slot ring buffer；
  N 相機 thread 共用單一 QP，以 `send_mtx` 序列化。
- 提供 **TCP JSON server（port 8100，11 個命令）** 給 Control：ARM/START/STOP、每台曝光/增益、
  相機列舉/綁定、GigE 節點查詢、調參預覽。

核心策略：**從 `Reference/cfaoi_phase1/` 的已驗證測試工具升級為生產等級**。

**軟體觸發架構（docs plan「37 CCD 觸發設計」）**：`GRAB_ARM` = 預熱（開陣列＋套參數＋RDMA connect，冷啟秒級），
`GRAB_START` = 觸發本體（僅 start_all，實測命令往返 0.3ms）。啟動 skew（2 台實測 1.45–1.72ms）由 IP 端
玻璃前緣對位吸收（40mm/張窗口，餘裕 ~250×）。

與 phase1 測試套件最大不同：
- 測試腳本（單次 500 幀、存 .raw）→ **持續 grab thread + callback**，移除檔案儲存。
- 單一大 buffer 逐幀同步收 → **N-slot ring + MrInfoEx 握手 + SEND/RECV 背壓**（2026-07-30 由 WRITE_WITH_IMM 改 SEND 根治 slot 覆寫，見 §7）。
- 無控制面 → **control_server（TCP JSON 8100）+ IDLE/ARMED/GRABBING 狀態機**。
- 單相機 → **CamManager 多相機陣列 + MAC 綁定**（1 台經 5945 全鏈 L3、2 台實機 L3；6/37 台待到貨）。

> ⚠️ 已知開放項（記錄於 STATUS.md／審計，未修）：8100 單客戶端序列處理；`GET_FRAME_PREVIEW` 未做；
> **RDMA 斷線後不自動重連、失敗後幀靜默丟棄（審計 P0-7，上線前必修）**——恢復需 GRAB_STOP → 重 GRAB_ARM。

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
│ 單一 buffer 逐幀同步、無背壓、無控制面、單相機        │
└──────────────────────────────────────────────────┘
                      ↕ 升級（保留 pylon/RDMA 樣板，加生產外殼）
CF-AOI 分散式架構 Grab 程式（本文件）
┌──────────────────────────────────────────────────┐
│ main.cpp（IDLE/ARMED/GRABBING 狀態機、frame_cb、    │
│           11 個 8100 回呼接線、設定檔路徑錨定）      │
│   CamManager（多相機陣列 + cam_map MAC 綁定 #21）   │
│     └ CamPylon × N（每台一條 grab thread）          │
│   RdmaSender（MrInfoEx 握手 + N-buffer SEND 管線）  │
│   ControlServer（TCP JSON @8100，11 命令）          │
│   RcConn（rdma_common.h，沿用 phase1 + MrInfoEx）   │
│   shared/FrameHeader.h（= phase1 版 + 相容層）      │
│ rdma_nslot_test / image_replay_sender（免相機送器） │
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
║   parse CLI → 設定檔路徑錨定(exe 上一層=grab/) →       ║
║   load_map(fail-fast) → 建 CamManager/RdmaSender/Ctrl ║
║   狀態：IDLE ⇄ ARMED ⇄ GRABBING（state_mtx 保護）     ║
║   frame_cb：CRC(鎖外) → send_mtx{seq=++frame_seq →    ║
║             sender.send_frame}（panel_hash 為 atomic） ║
╚═══════════════════════════════════════════════════════╝
        ↑ 11 命令                     ↓ 影像幀（N thread 併發）
╔══════════════════════════╗  ╔══════════════════════════╗
║ control_server.{h,cpp}   ║  ║ cam_manager.{h,cpp}      ║
║  TCP JSON server @8100   ║  ║  cam_map MAC 綁定（#21） ║
║  單 accept thread、       ║  ║  open_all（嚴格/fail-fast║
║  單客戶端序列處理         ║  ║  /冪等）+ start_all 平行 ║
╚══════════════════════════╝  ║  cam_pylon.{h,cpp} × N   ║
                              ║   grab thread + FPS/drop ║
                              ╚══════════════════════════╝
                                        ↓ uint8 Mono8 payload
╔═══════════════════════════════════════════════════════╗
║ RDMA 發送層 (rdma_sender.{h,cpp})                    ║
║  connect()：收 MrInfoEx（n_slots/slot_size 協商）     ║
║  send_frame()：填 FrameHeader → N-buffer →            ║
║               post_send（SEND，不指定遠端位址）→      ║
║               poll_one（背壓點：RNR 時阻塞）          ║
╚═══════════════════════════════════════════════════════╝
        ↓ RcConn（rdma_common.h：RC QP / SEND / poll）
╔═══════════════════════════════════════════════════════╗
║ wire (shared/FrameHeader.h — 兩端共用 256B)          ║
║  magic 0xA01CF00D / crc32_ieee（0xEDB88320）         ║
╚═══════════════════════════════════════════════════════╝
```

---

## 4. 軟體流程圖

### 4.1 啟動 / 狀態機（main.cpp:156-568）

```
main()
  ├─ parse CLI（--rdma-dest 必填；全旗標見 §6.1；未知參數 → exit 1，main.cpp:183）
  ├─ 設定檔路徑錨定（main.cpp:194-213）：cam_config/cam_map 預設 = exe 上一層（grab/），
  │    印絕對路徑；CWD 有同名檔且非採用那份 → WARN 點名（防 2026-07-30 的雙副本漂移）
  ├─ mgr.load_map(cam_map_path)（main.cpp:226-233）：檔在但格式錯/重複 → exit 1（不變式 8 fail-fast）
  ├─ 狀態：state_mtx 保護 grabbing/armed/panel_id；panel_hash 為 atomic（★1 修法：
  │    frame_cb 不取 state_mtx，避免與 GRAB_STOP→join 成鎖環，main.cpp:240-243）
  ├─ frame_cb（main.cpp:252-276，N 相機 thread 併發）：
  │    slice_seq[cid]++（各 thread 只動自己那格）→ 收滿殘幀不送
  │    → crc = crc_of(payload)（send_mtx 之外算，37 台不佔鎖）
  │    → send_mtx{ seq = ++frame_seq（鎖內指派：seq 順序==wire 順序）→ sender.send_frame }
  ├─ 11 個 8100 回呼接線（§8）
  ├─ ctrl.start()  監聽 0.0.0.0:8100（失敗 → 1）
  └─ 主迴圈：每 200ms 檢查 g_shutdown（SIGINT/SIGTERM）→ 清理退出
```

### 4.2 GRAB_ARM 預熱（main.cpp:291-329，冪等）

```
Control 送 GRAB_ARM（或 GRAB_START 未 ARM 時自動先 ARM）
  → do_arm（state_mtx 內）
      ├─ mgr.open_all(cam_count, serial, pkt_size)
      │    冪等：台數符合直接重用（每片重 ARM 零冷啟）；primary_only_（idle 調參開的單台）→ 一律重開（★7）
      │    有 cam_map → 嚴格模式：列舉→依 MAC 查 cam_id→依 cam_id 由小到大取前 want 台；
      │                 未列於映射的相機 → 整體報錯拒開；任一台 open 失敗 → 全關（fail-fast）
      ├─ 每台套 cam_config.json 條目（曝光/增益，read-back actual 後 re-save；失敗僅警告續行）
      ├─ sender 未連 → sender.connect(host, port, mgr.max_payload())（失敗 → 全關 + ERR）
      └─ armed=true（實測冷啟：1 台 542ms／2 台 1257ms ≈ 線性；37 台外推 ≈23s → 產線 ARM 須遠早於觸發）
```

### 4.3 GRAB_START 觸發（main.cpp:334-361）

```
Control 送 GRAB_START（params: timeout_ms?, frames_per_panel?）
  → state_mtx 內：
      ├─ grabbing 且 running_count()>0 → ERR "already grabbing"（收滿自動停後 running=0 可直接開下一片）
      ├─ 未 ARM → 自動 do_arm（相容 nc 手測；冷啟秒級，產線應先 ARM）
      ├─ slice_seq.assign(max_cam_id+1, 0)；total_slice=frames_per_panel（main.cpp:350-353）
      │    ⚠️ frame_seq 不歸零（★3 修法：全域唯一單調，跨片歸零會撞 slot/報 seq 跳躍/覆蓋輸出夾）
      └─ mgr.start_all(n_frames, frame_cb) → grabbing=true（觸發往返實測 0.3ms）
  ⚠️ timeout_ms 目前被 handler 忽略（main.cpp:334 `int /*timeout_ms*/`）——Grab 端無 per-panel watchdog，
     逾時控管完全依賴上位機/Control。
```

### 4.4 取像迴圈（cam_pylon.cpp，每台一條 grab thread）

**B1 修法（2026-08-02，L1）**：`grab_loop()` 現在是 **try/catch 薄殼**，實際迴圈在
`grab_loop_body()`。薄殼攔 `GenericException` / `std::exception` / `...` 三層 →
拔線/斷電的例外**不再逸出 thread 進入點**（修前 = `std::terminate` 全行程死亡、6 台陪葬）。
攔到後 `note_fault()` 豎 `faulted_` + 存訊息，**該台退出、其餘相機續跑**；不自動重連。
⚠️ 故障後 `is_running()==false`，與「收滿自動停」外觀相同 → 判斷收完與否要先看 `faulted`。

```
grab_loop()                       ← thread 進入點，只有 try/catch + StopGrabbing 收尾
  try { grab_loop_body() }
  catch GenericException / std::exception / ...  → note_fault()（faulted_=true）
  try { StopGrabbing() } catch(...)              ← 斷線時它自己也會擲，必須吞
  running_ = false

grab_loop_body()                  ← 原本的迴圈，允許擲例外
  MaxNumBuffer=16；StartGrabbing(GrabStrategy_OneByOne)
  while (!stop_flag && IsGrabbing):
    RetrieveResult(2000ms, TimeoutHandling_Return)
    GrabSucceeded:
       ++grabbed
       drop 偵測：BlockID 缺口 + GetNumberOfSkippedImages 相加（cam_pylon.cpp:160-166）
       cb_(cam_id, buffer, ImageSize, Width, Height)   → main frame_cb → sender.send_frame
       max_frames_>0 且 grabbed>=max_frames_ → 自動停（thread 自然退出；= 舊 M_FRAMES_PER_TRIGGER(N) 語意）
    每 5 秒印 FPS/grabbed/dropped
```

### 4.5 發送一幀（rdma_sender.cpp:61-134）

```
send_frame(cam_id, seq, panel_hash, payload, bytes, w, h, slice, total_slice, crc):
  ├─ 手填 FrameHeader（frameSeq=u64 故不用 make_frame_header；crc32 由呼叫端鎖外先算）
  ├─ buf_idx = frame_seq % n_buf_（N-buffer 環，≤ n_buf_ 筆 in-flight，rdma_sender.cpp:96）
  ├─ while (posted_ >= n_buf_) poll_one()   ← 背壓點：IP 端 recv WQE 用完 → SEND 收 RNR
  │                                            （rnr_retry=7=∞）→ completion 不回 → 此處阻塞
  ├─ memcpy [FrameHeader(256B) || payload] → 該緩衝
  └─ post_send（SEND，不指定遠端位址；落點由 IP 端 recv WQE 決定，rdma_sender.cpp:113-122）
     失敗 → connected_=false（之後幀靜默丟棄；⚠️ 無自動重連，見 §1 已知開放項）
```

---

## 5. 主要模組職責

| 模組 | 檔案 | 職責 |
|------|------|------|
| `main` | `src/main.cpp`（568 行）| CLI 解析、設定檔路徑錨定、狀態機、frame_cb（CRC 鎖外/seq 鎖內）、11 個 8100 回呼、cam_config 讀寫、信號清理 |
| `CamManager` | `src/cam_manager.{h,cpp}`（94+335 行）| cam_map.json 載入/寫入（驗證單一標準：write 先過 load 才 rename）、`annotate`（LIST_CAMERAS 填綁定）、`open_all`（嚴格/fail-fast/冪等/primary_only_ 防 ★7）、`start_all` 逐台平行 arm、依 cam_id 路由 |
| `CamPylon` | `src/cam_pylon.{h,cpp}`（105+303 行）| pylon 開相機（auto/SN）、機器層參數顯式設定（Mono8/Auto Off/TriggerMode Off/packet size）、grab thread、FPS/drop 統計、`set_max_frames` 收滿自動停、曝光/增益 get/set、`enumerate_cameras`（唯讀列舉）、`grab_one_mean`（調參預覽）、`read_machine_params` |
| `RdmaSender` | `src/rdma_sender.{h,cpp}`（57+147 行）| RDMA 連線、收 `MrInfoEx` 握手（驗 n_slots/slot_size）、N-buffer SEND 管線、`crc_of`（靜態，鎖外算；`CFAOI_RDMA_NOCRC=1` 跳過）、背壓 `poll_one`。⚠ 非 thread-safe，呼叫端以 send_mtx 序列化 |
| `ControlServer` | `src/control_server.{h,cpp}`（97+350 行）| TCP JSON server @8100；newline-JSON；11 命令 dispatch + 參數邊界檢查；callback 注入；單 accept thread、一次服一個 client |
| `RcConn` / `MrInfoEx` | `src/rdma_common.h`（198 行）| librdmacm RC 連線樣板（make_qp/reg/connect/serve/post_send/post_recv(wr_id)/poll_one）；256B 握手結構。⚠️ 與 `ip/src/image_source/rdma_common.h` 為**同源雙副本**，wire 相關改動必須兩份同步 |
| `rdma_nslot_test` | `src/rdma_nslot_test.cpp` | 合成幀送器（免相機）；`threads>1` 完全比照 main.cpp frame_cb 結構（共用單 QP + send_mtx + CRC 鎖外），可先量 37 台序列化上限；`CFAOI_TEST_CORRUPT_EVERY=N` 注入壞 CRC |
| `image_replay_sender` | `src/image_replay_sender.cpp` | 檔案回放送器（Gap #27，dependency-free）：Python/PIL 解碼 → stdin 餵 Mono8 raw → RDMA |
| `probe_cam_nodes` / `cam_mean_gray_test` | 各自單檔 | Gap #2 Stage 0 節點探測／Stage 2+3 曝光增益單調性驗證 |

> **wire format**：`shared/FrameHeader.h`（grab 從上一層 include，與 IP 端同一份）。

---

## 6. 執行模式與命令列參數

### 6.1 cfaoi_grab（生產主程式，main.cpp:156-213）

| 旗標 | 預設 | 效果 |
|------|------|------|
| `--rdma-dest IP:PORT` | （必填）| Spark IP 端 RDMA server（如 `192.168.3.1:18515`）|
| `--cam-count N\|ALL` | 1 | 啟用台數；ALL=列舉到的全部；有 cam_map 即嚴格模式 |
| `--frames-per-panel N` | 0 | 每片每台張數（0=連續；GRAB_START params 可覆蓋）|
| `--cam-id N` | 0 | 單台模式 FrameHeader.camId（legacy）|
| `--serial STRING` | auto | pylon 序號；`auto`=第一台（單台模式）|
| `--pkt-size N` | 8192 | `GevSCPSPacketSize`（GigE jumbo）|
| `--ctrl-port N` | 8100 | 等 Control 連入的 TCP port |
| `--cam-config PATH` | exe 上一層/cam_config.json | 曝光/增益 JSON（**路徑錨定 grab/**，不隨 CWD 漂移；明確給值時維持相對 CWD 語意）|
| `--cam-map PATH` | exe 上一層/cam_map.json | MAC↔cam_id 映射（Gap #21；同上錨定）|

回傳碼：0 OK｜1 參數錯/缺 `--rdma-dest`/cam_map 格式錯（fail-fast）/ControlServer 啟動失敗。
未知參數直接 `exit 1`（main.cpp:183）。

### 6.2 rdma_nslot_test（合成幀送器，免相機）

```bash
rdma_nslot_test <server_ip> <port> <num_frames> [width] [height] [delay_ms] [threads]
# width/height 預設 256×256（小幀驗繞回）；delay_ms 模擬慢送
# threads>1 = 模擬 N 台相機共用單一 QP（比照 main.cpp send_mtx 序列化 + CRC 鎖外）
# 環境變數：CFAOI_TEST_CORRUPT_EVERY=N 每 N 幀故意送錯 CRC（驗收端丟棄路徑）
```

### 6.3 image_replay_sender（檔案回放，Gap #27）

```bash
image_replay_sender <spark_ip> <port> <width> <height>
# stdin 重複："cam_id seq\n" + width*height Mono8 raw bytes
# Python 驅動見 scripts/verify_rdma_replay.py；背壓 = RDMA credit 盡 → 停讀 stdin → pipe 滿 → 自然限速
```

### 6.4 probe_cam_nodes / cam_mean_gray_test（Gap #2 工具）

```bash
probe_cam_nodes [serial]        # Stage 0：GenICam 節點名稱/範圍/acquisition 中 access mode
cam_mean_gray_test [serial]     # Stage 2+3：曝光/增益 → mean gray 單調性（門檻 >1.4 / >1.2）
```

---

## 7. RDMA N-slot ring buffer + SEND/RECV 背壓

> Step 3 核心。**2026-07-30 資料路徑由 `RDMA_WRITE_WITH_IMM` 改為 `SEND/RECV`**（實機抓到 slot 覆寫
> 資料損毀後的根治，= STATUS ★2 專節）。不變式見 grab/CLAUDE.md 不變式 7 / ip/CLAUDE.md。

### 7.1 握手：MrInfoEx（256 bytes，rdma_common.h:52-61）

連線時 IP server **SEND** 一個 `MrInfoEx` 給 Grab：

| 欄位 | 說明 |
|------|------|
| `addr`/`rkey` | ring buffer 基底位址/金鑰——**SEND 資料路徑已不使用**，保留於 wire 供相容+診斷 |
| `len` | 整塊大小 = `n_slots × slot_size` |
| `n_slots` | slot 數量（ring 深度 = 初始 credit，預設 4；IP `--rdma-slots`）|
| `slot_size` | 每 slot 大小 = `sizeof(FrameHeader) + max_payload` |
| `crc`/`pad[228]` | 未使用／對齊至 256 bytes |

Grab `connect()` 驗證 `n_slots != 0 && slot_size >= frame_cap`（rdma_sender.cpp:28-33），否則拒絕連線；
並依 `n_slots` 配 N 個送端緩衝（一塊大 buffer 一個 MR，rdma_sender.cpp:36-38）。

### 7.2 SEND/RECV 資料路徑 + 背壓（為何不能用 WRITE_WITH_IMM）

```
Grab 端（rdma_sender.cpp:93-122）：
  buf_idx = frame_seq % n_buf_          N-buffer 管線，≤ n_buf_ 筆 in-flight
  while (posted_ >= n_buf_) poll_one()  ← 背壓阻塞點
  post_send(...)                        SEND：不指定遠端位址

IP 端（ip/src/image_source/rdma_source.cpp）：
  N 個 post_recv 各指向一個 slot（wr_id = slot 編號）
  IBV_WC_RECV → slot_id = wc.wr_id（不再 seq%n_slots 推算）；seq 從 payload 內 FrameHeader.frameSeq 取
  處理完該 slot 才重掛它的 WQE（credit 補充）

背壓鏈：
  IP 消費慢 → 不 post_recv → WQE 用完 → Grab SEND 收 RNR（rnr_retry=7=∞）
  → send completion 不回 → poll_one() 阻塞 → 自然背壓（無需額外控制通道）
```

**為何 WRITE_WITH_IMM 不行（2026-07-30 實機證據）**：舊法 Grab 自算 `write_addr = addr + (seq%n_slots)*slot_size`
直接寫；但 **RNR credit 只擋 immediate 遞送，擋不住 payload 落地**——送端一拿到前一筆 completion 就 post
下一筆 write，payload 照樣寫進 slot，且 RNR 期間持續重試、**每次重寫該 slot** ⇒ IP 正在讀的 slot 被覆寫。
實測（2 台相機+背壓）slots=2/4/16 → err=11/10/0（純由 ring 深度決定）；縮短收端讀取窗口只能降命中率
（err 10→7），消不掉。改 SEND 後**資料落點由收端 recv WQE 決定** → slot 不可能在 IP 讀取期間被覆寫
（by construction 正確）：三種深度全 err=0，ring 深度只影響吞吐不影響正確性。

配套：`seq` 必須在 `send_mtx` 鎖內指派（main.cpp:273）使 seq 順序==wire 順序；app-CRC 必須在鎖外算
（40.8MB 實測 ~16ms/幀，留鎖內會讓 37 台序列化上限 ~50 幀/s < 需求 88.8 幀/s）。

### 7.3 斷線偵測與恢復

- **IP 端**：RoCE v2 下 Grab 斷線後 `IBV_WC_WR_FLUSH_ERR` 不保證即時出現 → `rdma_source` 輪詢 CM event
  channel（`check_cm_disconnect()`）才能讓 recv_thread 退出。
- **Grab 端**：send/poll 擲例外 → `connected_=false`、印一次 stderr。
  ⚠️ **無自動重連**（grab/CLAUDE.md §5 的「重連機制」為未實作規劃）；且 `disconnect()` 在
  `connected_==false` 時早退不清 QP/MR（rdma_sender.cpp:136-137）。恢復流程 = GRAB_STOP → 重 GRAB_ARM。
  此路徑已列**審計 P0-7「上線前必修」**（rdma_sender.cpp 檔頭註記）。

**實測**：2026-06-17 WRITE 版 120 幀 CRC=OK；2026-07-30 SEND 版最終整合 4 片×3 張×2 台=24 幀
CRC 不符 0、seq 跳躍 0、背壓下 slots=2 亦 ok=20 err=0。

---

## 8. TCP 協議（Control ↔ Grab，port 8100）

格式：`{"cmd":..,"seq":..,"params":{..}}\n`，回應 `{"seq":..,"status":"OK"|"ERR",...}\n`
（newline-delimited JSON，與 IP 端 8200 同模式）。單 accept thread、**一次服一個 client（單客戶端序列處理，
已知限制：Control 佔線時第二個診斷工具只會排隊逾時）**。

**共 11 個命令**（dispatch 鏈 control_server.cpp:132-341；未知 cmd → `ERR unknown command`）：

| # | `cmd` | 輸入 params | 動作 / 回應 | dispatch 行 |
|---|-------|------------|------------|------|
| 1 | `CHECK_HEALTH` | 無 | `OK` + `data`={grabbing, armed, cams, running, frames_per_panel, grabbed, dropped, sent_frames, sent_bytes, **faulted, faulted_cams**}。計數欄皆為總和無 per-cam 明細（B9）；`faulted_cams`=B1 修法新增，每筆 {cam_id, ccd_id, err}，是唯一能從 8100 定位「哪台掉線」的欄位 | :132 |
| 2 | `LOAD_RECIPE` | `recipe`, `panel_id` | 更新 panel_id + panel_hash（atomic；不取像）；回 `OK` | :140 |
| 3 | `GRAB_ARM` | 無 | 預熱：開陣列+套曝光增益+RDMA connect（冪等）；見 §4.2 | :148 |
| 4 | `GRAB_START` | `timeout_ms?`（預設 40000，⚠️ 目前被 handler 忽略）, `frames_per_panel?`（0=連續）| 觸發：切片歸零 + start_all；未 ARM 自動先 ARM | :160 |
| 5 | `GRAB_STOP` | 無 | 停相機+斷 RDMA+清 armed/grabbing；回 `OK` | :183 |
| 6 | `SET_CAM_PARAMS` | `cam_id, exposure_us, gain_raw` | 依 cam_id 路由寫該台（★5 已修：cam_id≠0 可用）；邊界 exp∈[2,10000]µs、gain∈[256,2047]；該台未開→只存 cam_config；回 actual read-back | :188 |
| 7 | `GET_CAM_PARAMS` | `cam_id` | 該台實際曝光/增益；未開→回 cam_config 條目 | :229 |
| 8 | `LIST_CAMERAS` | 無 | 唯讀列舉（不開相機）+ cam_map annotate：`[{cam_id,ccd_id,bound,mac,model,serial,ip,online,persistent,ip_config,device_class}]`；串流中並存已實測不掉幀 | :254 |
| 9 | `GET_CAM_NODES` | `cam_id` | GigE 機器層參數（PixelFormat/Auto/Trigger/ROI/packet/SCPD）+ `cam_id` 回聲（★4 已修：錯 cam_id 回 ERR）| :265 |
| 10 | `SET_CAM_MAP` | `entries`=[{mac,cam_id,ccd_id}] 完整表 | 寫 cam_map.json（暫存→驗→rename 原子）並重載；取像中/已 ARM 拒絕；壞資料 5 種全擋原檔不動 | :284 |
| 11 | `TUNE_MEAN` | `cam_id, exposure_us, gain_raw` | 設參數+抓 1 幀回 mean_gray（調參預覽；取像中拒絕；⚠️ 測完曝光留在相機與 cam_config，需 SET_CAM_PARAMS 復原）| :306 |

> 接線狀態：模擬器→真 Control 8787→真 Grab 端到端 **L3**（CF_GRAB_START/CF_STOP 走此鏈，見 STATUS「Switch 到貨日」）。

---

## 9. FrameHeader wire format

`shared/FrameHeader.h`（256 bytes，magic `0xA01CF00D`，version 2）= cfaoi_phase1 實機驗證版，兩端共用。
**逐欄與 `Reference/cfaoi_phase1/FrameHeader.h` 完全相同**（考古抽查確認）；差異僅在 shared 版「附加」的
非-wire 相容層 `frame_panel_hash()`（FNV-1a）／`make_frame_header()`。

- Grab `send_frame` **手填**（不用 `make_frame_header`，因 frameSeq 需 u64），`crc32` 由呼叫端以
  `RdmaSender::crc_of` 在 send_mtx 之外先算（多項式 0xEDB88320；`CFAOI_RDMA_NOCRC=1` 填 0）。
- `panelId` = `frame_panel_hash(panel_id 字串)`；`bitDepth=8`、`pixelFormat=0`（Mono8）；`ptpTimestampNs=0`（無 PTP）。
- **`sliceIndex`/`totalSlice` 已填真值**（frames_per_panel>0 時：每台自己的 slice 計數 0..N-1 / N，
  main.cpp:254-275；連續模式維持 0/1 legacy 語意）。IP 端據此啟動逐 slice 路徑（2 台實機驗證）。
- `frameSeq` 全域唯一單調、**跨片不歸零**（★3）；SEND 路徑下收端從 payload 內取 seq（不受 imm 32-bit 截斷）。

詳見 docs/CLAUDE.md §5 FrameHeader 定義（不變式 2/3）。

---

## 10. 功能對照表：phase1 → grab（含尚未升級項）

> ✅=已升級到生產 / 🆕=grab 新增 / 📦=停在測試套件 / ❌=規劃但未建檔。L-level 對齊 STATUS.md。

| phase1 測試工具/能力 | phase1 位置 | 現行 grab 對應 | 狀態 | L-level |
|---|---|---|---|---|
| FrameHeader 256B wire + crc32_ieee | `FrameHeader.h` | `shared/FrameHeader.h`（+相容層；slice 真值）| ✅ | L4 |
| RcConn RDMA-CM 連線樣板 | `rdma_common.h` | `grab/src/rdma_common.h`（+MrInfoEx、post_recv wr_id；⚠️ 與 ip/ 同源雙副本）| ✅ | L4 |
| pylon 相機偵測（t30_probe）| `t30_pylon_probe.cpp` | `cam_pylon.cpp` open + `enumerate_cameras` | ✅ | L4 |
| pylon 取像 + FPS/drop（t31_grab）| `t31_pylon_grab.cpp` | `cam_pylon.cpp` grab_loop（持續 thread、收滿自動停）| ✅ | L4 |
| RDMA 發送（t40_client_pylon）| `t40_e2e_client_pylon.cpp` | `rdma_sender.cpp`（N-buffer **SEND** 管線）| ✅ | L3（SEND 版 24 幀 err=0）|
| RDMA→GPU 收圖 cudaHostAlloc（t21/t40_server）| `t21_rdma_gpu_server.cpp` | IP `rdma_source.cpp`（沿用手法）| ✅ | L4 |
| **多相機陣列 cam_manager + cam_map MAC 綁定（#21）** | t31 main() 邏輯 | `cam_manager.{h,cpp}`（嚴格模式/fail-fast/冪等）| 🆕 | **L3（2 台實機）**；6/37 台待到貨 |
| **GRAB_ARM/START 拆分（軟體觸發）** | phase1 無 | `main.cpp` do_arm + grab_start | 🆕 | L3（verify_step3_trigger 6/6，1 台+2 台）|
| **N-slot ring + MrInfoEx + SEND/RECV 背壓** | phase1 無 | `rdma_common.h` + `rdma_sender.cpp` + IP `rdma_source` | 🆕 | L3（背壓 slots=2/4/16 全 err=0）|
| **control_server（TCP JSON 8100，11 命令）** | phase1 無 | `control_server.{h,cpp}` | 🆕 | L3（真 Control+模擬器端到端）|
| **rdma_nslot_test（多執行緒模擬多相機）** | phase1 無 | `rdma_nslot_test.cpp` | 🆕 | L3 |
| **image_replay_sender（檔案回放）** | `t40_e2e_client_file.cpp` | `image_replay_sender.cpp`（stdin raw，免 OpenCV）| ✅ | L3（Gap #27）|
| **eBUS 相機（iPORT，L803K）** | `t30/t31_ebus_grab.cpp` | **無 cam_ebus.cpp** | ❌ 未建檔 | L0（eBUS SDK 未裝）|
| **MAC Persistent IP 綁定** | grab/CLAUDE.md §1 列為 `t01_pylon_mac_setup` | **無 mac_ip_binder**；需求由 cam_map.json（MAC keying）+ runbook 手動 persistent IP 流程取代 | ❌ 來源懸空* | —（取代方案 L3）|
| **frame_assembler / control_client** | grab/CLAUDE.md §6 規劃 | **無**（組幀內聯 main.cpp frame_cb；方向反轉為 control_**server**）| ❌ 不再建 | — |
| **RDMA 斷線自動重連** | grab/CLAUDE.md §5 規劃 | **無**（connected_=false 後靜默丟幀；審計 P0-7 上線前必修）| ❌ 未實作 | — |
| 機況腳本（00/10/11/20/30）| `*.sh` | 無遷移（保留為機況確認）| 📦 | 10/11 L4 |

> \* **t01 懸空引用**：`Reference/cfaoi_phase1/` 與整個 Reference 樹下 `find` 不到任何 t01/mac_setup 檔——
> 該能力從未存在於程式碼。cam_id 穩定性問題（2026-07-30 實測第二台接入後 raL8192 由 cam0 變 cam1）
> 已由 `cam_map.json` MAC keying 解決（grab/CLAUDE.md 不變式 8）。

---

## 11. 關鍵不變式

> 完整 8 條見 [grab/CLAUDE.md §8](../grab/CLAUDE.md)。重點（含程式層配套）：

1. `cam_pylon.cpp` 與（未來）`cam_ebus.cpp` **禁止互相 include**（SDK 嚴格分離）。
2. `FrameHeader.h` 必須與 IP 端同版（從 `shared/` 引用），`sizeof==256`、magic `0xA01CF00D`。
3. Grab 無 UI，所有設定從 Control 命令或 CLI 來。
4. RDMA NIC link_layer 必須是 `Ethernet`（RoCE v2）。
5. 相機 NIC MTU 9000；RDMA NIC 不需 jumbo。
6. **GB10 不可用 `nvidia_peermem`，IP 端改 `cudaHostAlloc(Portable|Mapped)`**（docs/CLAUDE.md 不變式 11）。
   ⚠️ 已知文件債：`grab/src/rdma_common.h` `reg()` 註解仍寫 peermem（phase1 殘留，GB10 不適用；
   grab 端註冊的是 host txbuf 不觸發該路徑）。
7. **資料路徑必須是 `SEND`，不可用 `RDMA_WRITE_WITH_IMM`**（grab/CLAUDE.md 不變式 7；§7.2 有完整為什麼）。
   `rdma_common.h` 的 `post_write_imm` 為棄用殘留，僅供考古。
8. **cam_id 必須來自 MAC 穩定映射（cam_map.json），不可用列舉順序**（grab/CLAUDE.md 不變式 8）：
   映射檔格式錯/重複 → 啟動即中止；無映射檔 → 舊行為 + WARN（僅限開發）。
9. **seq 單調且順序==wire 順序**：`++frame_seq` 必在 send_mtx 內（main.cpp:267-273 註解）、跨片不歸零（★3）。
10. **app-CRC 在 send_mtx 之外算**（37 台吞吐前提，main.cpp:263-266 / rdma_sender.h 註解）。
11. （IP 端配對）處理完 slot 才重掛該 slot 的 recv WQE（credit 補充順序不可換）。

---

## 12. 建置與平台支援

### 12.1 依賴與建置（`CMakeLists.txt`）

- **REQUIRED**（缺則 `FATAL_ERROR`）：pylon SDK（/opt/pylon 或 `PYLON_ROOT`）、libibverbs、librdmacm、nlohmann_json。
- pylon 偵測三法：`find_package(pylon)` → `PYLON_ROOT` 環境變數 → `pylon-config`。
- 五個目標：`cfaoi_grab`（main+cam_manager+cam_pylon+rdma_sender+control_server）、
  `rdma_nslot_test`／`image_replay_sender`（皆免 pylon）、`probe_cam_nodes`／`cam_mean_gray_test`（僅需 pylon）。

```bash
cd grab
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j$(nproc)
```

### 12.2 平台

| 角色 | 主機 | NIC | 說明 |
|------|------|-----|------|
| 截取中心 | `damac`（Ryzen 7700, x86 Linux）| ConnectX-5 MCX516A | pylon 取像 + RDMA 發送端；`enp1s0f1np1`=相機網段（192.168.5.200/24, MTU 9000）、`enp1s0f0np0`=RDMA 直連 Spark |
| 交換機 | HPE FlexFabric 5945（`CFAOI-SW1`）| 25G SFP28 埠插 1G 銅纜模組（**必下 `speed 1000`**）+ 100G 上行 | 相機接入拓樸；坑清單見 [6cam_setup_runbook](6cam_setup_runbook.md) |
| 相機 | Basler raL8192-12gm（8160×3000, 24.48MB/幀）+ raL4096-24gm（4096×3000, 12.29MB/幀，借用機）| GigE 192.168.5.x | packet_size=8192、GevSCPD=0（實測不需 inter-packet delay）|
| 鏈路 | damac↔Spark 100G DAC（192.168.3.x）| RoCE v2 | RDMA port 18515 |

頻寬帳：單台 GigE 實測上限 123.6 MB/s；37 台 = 36.6 Gbps 對 100G 上行 37% 使用率。
37 台真正風險在 damac 主機端（中斷/NIC ring/RSS），到貨時實測。

---

## 13. 驗證狀態

> L-level 依 docs/CLAUDE.md §8「完成 = 驗證過」。詳細數據見 [docs/STATUS.md](STATUS.md)「Switch 到貨日」章節。

| 項目 | 狀態 | 證據 |
|------|------|------|
| 單相機 pylon→RDMA→Spark（直連）| **L4** | 2026-06-15 Step 2：CRC 20/20 FAIL=0（[Step 2 報告](verification/verification_report_step2_20260615.md)）|
| **單相機經 5945 交換機全鏈（rdma-process 真 GPU 檢測）** | **L3** | 2026-07-30：20 幀 grabbed=sent=20 dropped=0、Spark recv ok=20 err=0 CRC 全對、3.45fps/84.5MB/s（瓶頸=相機端 1GbE，IP 餘裕極大）|
| **GRAB_ARM/START 拆分觸發鏈** | **L3** | `verify_step3_trigger.py` 6/6（1 台：冷啟 542ms/觸發 0.3ms；2 台：1257ms/0.4ms）；37 台冷啟外推 ≈23s |
| **2 台同步觸發 + 混解析度混流** | **L3** | tcpdump GVSP 首封包時間戳：skew 1.45–1.72ms 兩輪可重現（窗口餘裕 ~250×）；12.29+24.48MB 混流 CRC 全對 |
| **SEND/RECV 資料路徑 + 背壓（★2 根治）** | **L3** | 2 台+背壓 slots=2/4/16 全 ok=20 err=0；最終整合 4 片×3 張×2 台=24 幀 CRC/seq 錯誤 0、輸出夾全不同名 |
| **cam_map MAC 綁定（#21）+ SET_CAM_MAP** | **L3** | 嚴格模式載入/拒開、壞資料 5 種全擋原檔不動、2 台 annotate 正確（raL8192→CCD00、raL4096→CCD01）|
| **設定檔路徑錨定（cam_config/cam_map）** | **L3** | 2026-07-31 damac 4 測項（build/、repo 根、/tmp、明確 --cam-config）+ 殘留 WARN 點名 |
| **8100 接線端到端** | **L3** | 模擬器→真 Control 8787→真 Grab：CF_GRAB_START OK/CF_STOP OK；失敗路徑 10/10 誠實 ERR；★1–★5 修正各附數據 |
| rdma_nslot_test（N-slot + 背壓 + 多執行緒模擬）| **L3** | 120 幀 CRC=OK（WRITE 版）→ SEND 版全回歸；8 緒模擬 37 台序列化量測（CRC 鎖外 41.5 幀/s）|
| image_replay_sender（Gap #27 檔案回放）| **L3** | 舊圖→RDMA→Spark 運算鏈驗證 |
| **6 台相機陣列** | **待到貨（8/M）** | 依 [docs/6cam_setup_runbook.md](6cam_setup_runbook.md) 逐項執行 |
| 37 台全陣列 / damac 主機端調優 | **L0** | NIC ring/RSS/affinity 留到 37 台實測 |
| eBUS 路徑（cam_ebus）| **L0** | 未建檔、SDK 未裝 |
| RDMA 斷線自動重連 | **未實作** | 審計 P0-7 上線前必修（§7.3）|

---

## 14. 關鍵檔案索引

| 主題 | 檔案 |
|------|------|
| 進入點 / 狀態機 / frame_cb | [src/main.cpp](../grab/src/main.cpp) |
| 多相機陣列 + MAC 綁定 | [src/cam_manager.cpp](../grab/src/cam_manager.cpp) / [.h](../grab/src/cam_manager.h) |
| pylon 相機 | [src/cam_pylon.cpp](../grab/src/cam_pylon.cpp) / [.h](../grab/src/cam_pylon.h) |
| RDMA 發送 | [src/rdma_sender.cpp](../grab/src/rdma_sender.cpp) / [.h](../grab/src/rdma_sender.h) |
| RDMA 連線樣板 + MrInfoEx | [src/rdma_common.h](../grab/src/rdma_common.h)（⚠️ 與 ip/src/image_source/ 同源雙副本）|
| TCP 命令 server（11 命令）| [src/control_server.cpp](../grab/src/control_server.cpp) / [.h](../grab/src/control_server.h) |
| N-slot 合成測試 | [src/rdma_nslot_test.cpp](../grab/src/rdma_nslot_test.cpp) |
| 檔案回放送器 | [src/image_replay_sender.cpp](../grab/src/image_replay_sender.cpp) |
| Gap #2 工具 | [src/probe_cam_nodes.cpp](../grab/src/probe_cam_nodes.cpp) / [src/cam_mean_gray_test.cpp](../grab/src/cam_mean_gray_test.cpp) |
| 設定模板 | [cam_config.example.json](../grab/cam_config.example.json) / [cam_map.example.json](../grab/cam_map.example.json) |
| wire format | [shared/FrameHeader.h](../shared/FrameHeader.h) |
| 建置 | [CMakeLists.txt](../grab/CMakeLists.txt) |
| 不變式 | [grab/CLAUDE.md](../grab/CLAUDE.md) |
| 6 台到貨 runbook | [docs/6cam_setup_runbook.md](6cam_setup_runbook.md) |

---

*本文件 2026-08-02 由 grab/src 逐檔靜態分析整版重寫（14 檔 ~2748 行），對照 `Reference/cfaoi_phase1/` 考古
+ STATUS.md「Switch 到貨日」實機數據 + grab/CLAUDE.md 不變式。格式對齊 [ip_程式完整說明.md](ip_程式完整說明.md) /
[control_程式完整說明.md](control_程式完整說明.md)。*
