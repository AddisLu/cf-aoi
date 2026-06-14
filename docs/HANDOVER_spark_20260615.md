# Spark 交接文件 — 2026-06-15（給明天在 DGX Spark 上的 Claude Code）

> 你（明天的 Claude Code）在 **DGX Spark（ARM, GB10）** 上，沒有昨天 x86 對話記憶，只能讀 repo。
> **先讀**：`docs/STATUS.md`（完成度盤點）→ `docs/CLAUDE.md`（全域）→ `ip/CLAUDE.md` + `grab/CLAUDE.md`（不變式）→ 本檔。
> 核心任務：**在 Spark 編 IP → 量 GB10 速度 → 驗 x86↔ARM 結果一致** → 之後才做 RDMA。
> 昨晚（x86）已備好工具與基準，commit 已 push；`git pull` 後全到位。

---

## 1. 明天目標（按序做，每步過了再下一步）

### 1a. 在 Spark 編譯 IP（sm_121, CUDA 13）
```bash
cd ~/cf-aoi/ip      # repo 路徑見 §5
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_CUDA_ARCHITECTURES=121
cmake --build build -j$(nproc)
```
**必要依賴**（CMakeLists REQUIRED）：CUDAToolkit、OpenCV(core/imgproc/imgcodecs)、nlohmann_json、fmt、Threads。
**可選（找不到自動略過，今天用不到）**：ONNX Runtime（ORT，AI 用但 AI 停用）、libibverbs（RDMA 模式用，bench/offline 不需要）。
**可能卡的點（ARM）**：
- OpenCV ARM：`sudo apt install libopencv-dev`（Ubuntu aarch64 有）；找不到就裝或自編。
- nlohmann_json / fmt：`sudo apt install nlohmann-json3-dev libfmt-dev`。
- CUDA 13 在 `/usr/local/cuda`；確認 `nvcc --version` = 13，`CMAKE_CUDA_ARCHITECTURES=121`（GB10 Blackwell）。
- ORT 找不到 → 正常（AI 停用，無影響）；ibverbs 找不到 → 正常（RDMA 模式今天不編/不跑）。
- **char 符號性**：ARM `char` 預設 **unsigned**（x86 signed）。本專案 CRC/hash 用 `uint8_t`/`unsigned char`（安全）；若編到任何用裸 `char` 做運算的地方要留意（目前無已知）。

### 1b. 量 GB10 速度（--mode bench）→ 算幾台 Spark
```bash
# 同一張影像、同 ini（與 x86 基準同條件才可比 speedup）
./build/cfaoi_ip --mode bench --input ~/cf-aoi/test_images/IP01_Origin000002.tif \
                 --ini config/default_zone.ini --bench-iters 100 --bench-warmup 10
# 另量上下界：--bth 2.0 --dth 0.3（乾淨557）；不要量觸頂(見 §2 cap 非決定)
```
- 看輸出 `[Bench] gpu_ms(cudaEvent) median` 與最後一行 `容量換算 → N_spark`。
- **x86 RTX2080S 基準（昨天實測，同影像/同 ini，AI off）**：

  | 負載 | 缺陷 | gpu_ms median | 1110 張 | N_spark |
  |---|---|---|---|---|
  | 乾淨(BTH2.0/DTH0.3) | 557 | **11.37** | 12.6s | 1 |
  | realistic(預設) | 2606 | **11.67** | 13.0s | 1 |
  | 觸頂(pitch30) | 10000 | 13.27 | 14.7s | 1 |
- **期望**：GB10 zero-copy 真共享統一記憶體（免 H2D upload），RAG 文件稱 ~4.9ms → 1110×4.9 = **5.4s / 面板，30s 餘裕 ~82%**。**但這是期望值**：GB10 記憶體是 LPDDR5X(~273GB/s) < RTX2080S GDDR6(496GB/s)，此 kernel 偏頻寬綁定，**GB10 不保證更快 → 以實測 gpu_ms 為準**。
- **拍板規則**：`N_spark = ceil(1110 × gpu_ms / 30000)`。只要 gpu_ms ≤ 27ms 就 **1 台**（即使比 x86 慢 2 倍仍 1 台）。

### 1c. 跨架構一致性（重點：證明 off-line x86 調的參數 = on-line ARM 結果）
```bash
# (1) 先確認 ARM 自身決定性（期望 2606 + bit-exact）
./build/cfaoi_ip --mode offline-file --input ~/cf-aoi/test_images/IP01_Origin000002.tif \
   --ini config/default_zone.ini --output /tmp/arm_real --no-save-images --verify-deterministic
# 取 ARM 的 ResultInfo.json
ARM=$(ls /tmp/arm_real/*/*/*_ResultInfo.json | head -1)
# (2) 與 x86 基準比對（基準在 repo，已 pull 到）
python3 ~/cf-aoi/scripts/compare_results.py \
   ~/cf-aoi/docs/verification/baseline_x86/realistic_x86.json  "$ARM"
# 同樣比 clean：--bth 2.0 --dth 0.3 跑 ARM → 比 clean_x86.json
```
**判讀**：
- 理想：`[Tier-0] 0 項不一致` + `GL_Mean max|Δ| ≈ 0`（< 0.5）→ **一致性成立，off-line 調參對 on-line 有效**。
- 若 (1) ARM 自身就非 bit-exact → 先查 ARM zero-copy/CCL 收斂迴圈（不是跨架問題）。
- 若 (1) 過、(2) Tier-0 差幾筆 → 邊界像素 FP 翻面（sm_75↔sm_121 ULP）；看 `max|Δ|` 與孤立缺陷座標，量化後決定容差。**務必把 compare 輸出貼回給使用者判定**。

### 1d.（之後，1c 過再做）ring buffer / RDMA 直連
- 先「整張收完再算」（對齊 offline）；分塊串流列後續。
- IP 加 `RdmaRingImageSource : IImageSource`（N=4 pinned-mapped slot + `cudaHostAlloc(Portable|Mapped)` + `ibv_reg_mr`），骨架取 `Reference/phase1_tests/t40_e2e_server.cpp` + `rdma_common.h::RcConn`。詳規劃見昨日對話結論（STATUS / image_source.h 註解已預留 RdmaImageSource）。

---

## 2. 關鍵決策狀態（明天必須遵守）

- **FrameHeader 採 phase1 版 `0xA01CF00D`** —— `shared/FrameHeader.h` **昨晚已對齊**（frameSeq u64 / panelId / sliceIndex / machineCoordX,Y）。舊 `0xCFA0A001` 版**作廢**（從未經 RDMA）。phase1 版即 t21/t40 實機驗證過的線格式。**勿改回 0xCFA0A001**。phase1 無 system_id/flags，日後 online 需要時用 `reserved[]` 擴充（勿動既有欄位順序）。
- **GB10 不可用 `nvidia_peermem`**（GB10 NVLink-C2C SoC，GPU Bus ID 非標準 PCIe → modprobe 回 EINVAL）。RDMA 收圖用 **`cudaHostAlloc(cudaHostAllocPortable|cudaHostAllocMapped)`** 配 pinned host memory + `cudaHostGetDevicePointer`，NVLink-C2C ~900GB/s。見 `grab/CLAUDE.md` 不變式 6 / `docs/CLAUDE.md` 不變式 11 / `Reference/phase1_tests/t40_e2e_server.cpp`（已採此法）。⚠️ `RcConn::reg()` 註解「失敗多半是 peermem 未載入」在 GB10 **不適用**，移植時改掉。
- **repo `ip/`（XML 配方 / JSON / `--mode`）是主力**：x86(off-line) 與 ARM(on-line) **兩邊都編、都跑這個同一份**。`Reference/gpu_algo/`（含 `RAG_TRAINING.md`）是**原版演算法參考真相，不是要跑的程式**。
- **AI 停用**（TrueDefect 樣本不足）：模型仍載入但 `set_ai_active(false)`，缺陷標「待人工複核」。`--use-ai` 才開。bench/一致性都在 AI off 下做。
- **DIV-only**：配方只收 `AlgorithmCompare="DIV"`，SUB 拒絕。
- **1 台 Spark 夠（預判）**：期望 4.9ms×1110=5.4s（餘裕82%）；即使持平 x86 11.67ms 也只 13s（1 台）。**待 1b bench 實測 gpu_ms 確認**。

---

## 3. 昨晚已備好的工具與基準（pull 後都在）

- **`scripts/compare_results.py`** —— 跨架比對。Tier-0 整數/幾何完全一致、Tier-1 GL_Mean 容差 0.5 且**永遠報實測 max|Δ|**、忽略計時、count 不同列孤立缺陷、exit 0/1。
  昨晚 x86 自對自 sanity：realistic 2606 vs 同配置第二次 → **✅ PASS, max|Δ|=0.000000**；clean vs realistic → 正確 FAIL。
- **x86 基準 JSON**（`docs/verification/baseline_x86/`）：
  - `clean_x86.json`（557，`--bth 2.0 --dth 0.3`，各自 `--verify-deterministic` bit-exact）
  - `realistic_x86.json`（2606，預設 ini，bit-exact）
  - ⚠️ **無 cap(10000) 基準**：MAX_DEFECTS=10000 觸頂時，blob `atomicAdd`-append 截斷是 race-dependent → 同機兩跑「哪 10000 個」不同（非 bit-exact）。**一致性/決定性只用未觸頂負載**（clean/realistic）；觸頂僅供 perf。正常面板不該接近 10000（接近＝Pitch 設錯爆量）。
- **`--mode bench`**（已在 IP）：x86 基準 gpu_ms median = **11.67ms（realistic 2606）** / 11.37（clean）/ 13.27（cap）。
- **Phase-1 驗證報告**：`docs/verification/verification_report_20260611.md`（相機/RDMA/GPU 全 PASS、1.44μs、CRC 一致）。

---

## 4. x86 → ARM 移植檢查清單

| 項目 | 風險 | 結論 / 處置 |
|---|---|---|
| 依賴函式庫 ARM 版 | OpenCV/json/fmt 需 aarch64 | apt 裝；ORT/ibverbs 可選、今天不需 |
| CUDA arch | 需 sm_121 | `-DCMAKE_CUDA_ARCHITECTURES=121`、CUDA 13 |
| packed struct 對齊 | FrameHeader `#pragma pack(1)` | 兩端同 pragma，ARM/x86 一致；`static_assert(sizeof==256)` 守住 |
| `char` 符號性 | ARM char unsigned / x86 signed | 專案 CRC/hash 用 `uint8_t`/`unsigned char`（安全）；裸 `char` 運算要留意（目前無） |
| endianness | — | 兩邊都 little-endian（ARM LE），**安全** |
| 浮點一致性 | sm_75 vs sm_121 FMA/rounding | **這是 1c 要驗的重點**：閾值邊界像素可能翻面 → 用 compare_results.py 量 max|Δ| 與 Tier-0 |
| zero-copy 路徑 | GB10 mapped vs x86 cudaMalloc+H2D | 結果應同（只差存取路徑）；先跑 `--verify-deterministic` 確認 ARM 自身決定性 |

---

## 5. 環境資訊

- **DGX Spark**：hostname `spark-c16f`，GB10 / sm_121 / CUDA 13 / 驅動 580.95.05，Tailscale `100.83.141.68`，RDMA NIC ConnectX-7 內建，RDMA IP `192.168.3.1`。
- **截取中心 PC**：hostname `damac`，AMD Ryzen 7700 + ConnectX-5 MCX516A，RDMA IP `192.168.3.2`，相機網口 `192.168.5.2`。
- **相機**：Basler raL8192-12gm，SN 25445953，GigE `192.168.5.1`，pylon 26.05。
- **鏈路**：相機 1GbE→damac；damac↔Spark 100G DAC（`192.168.3.x`）。明天為 **1 台相機直連、無 SN2201 Switch**。
- **git repo**：`https://github.com/AddisLu/cf-aoi.git`（remote `origin`）。`git pull` 後讀 `docs/STATUS.md` + `docs/CLAUDE.md` + 本檔。
- **開發機（x86 基準產生處）**：Linux + RTX 2080 Super（sm_75, CUDA 12.x）。

---

## 6. 一句話總結給明天的你
編 IP（sm_121）→ `--mode bench` 得 gpu_ms 套 `ceil(1110×gpu_ms/30000)` 拍板台數（預期 1 台）→ `--verify-deterministic` 確認 ARM 自身 bit-exact → `compare_results.py` 比對 `docs/verification/baseline_x86/realistic_x86.json`，看 **Tier-0 是否 0 項、GL_Mean max|Δ| 多少**，把結果貼回使用者判定一致性。FrameHeader/GB10/AI/DIV 決策照 §2，勿改回舊值。
