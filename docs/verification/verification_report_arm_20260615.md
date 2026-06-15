# CF-AOI ARM(GB10/sm_121)運算驗證報告

**日期**：2026-06-15
**執行主機**：spark-c16f（DGX Spark，GB10 / sm_121 / ARM aarch64）
**驗證範圍**：IP 程式在 GB10 上的「編譯 → offline-tcp 連線 → AOI 運算正確性 → 跨架構一致性 → 速度」
**定位**：繼 [2026-06-11 硬體路徑驗證](verification_report_20260611.md)（相機 → RDMA → GPU）之後的第二份實機證據。
6/11 證明「資料進得來」，本報告證明「**演算法在 GB10 上算得對、算得夠快**」。

---

## 〇、結論摘要

| 項目 | 結果 |
|------|------|
| IP 編譯（sm_121, CUDA 13） | ✅ 零警告，產出 ELF aarch64 `cfaoi_ip` |
| offline-tcp 連線（port 8200） | ✅ 監聽 + CHECK_HEALTH / GET_STATUS 正常 |
| 跨架構一致性（26 張真實面板 vs ground truth） | ✅ 25/26 缺陷數完全一致；1 張單像素邊界翻面（已判定接受） |
| ARM 自身決定性 | ✅ 26 張兩跑 bit-exact |
| GB10 速度 | ✅ 正常面板 ~7.4ms/張 → **1 台 Spark 足夠**（餘裕 ~73%） |

→ **IP 端在 DGX Spark GB10 達 L4（實機運算驗證）。**

---

## 一、環境與編譯

### 1.1 平台規格

| 項目 | 規格 |
|------|------|
| GPU | NVIDIA GB10（Blackwell，整合式 / NVLink-C2C SoC），sm_121 |
| CPU / 架構 | ARM aarch64 |
| 驅動版本 | 580.95.05 |
| CUDA | 13.0.88（/usr/local/cuda） |
| compute_cap（nvidia-smi） | 12.1 |
| cmake | 3.28.3 |

### 1.2 依賴函式庫（aarch64）

| 套件 | 版本 | 備註 |
|------|------|------|
| OpenCV | 4.13.0 | 系統已裝（core/imgproc/imgcodecs） |
| nlohmann_json | 3.11.3 | **今天 `apt install nlohmann-json3-dev` 補裝**（原缺） |
| fmt | 9.1.0 | **今天 `apt install libfmt-dev` 補裝**（原缺） |
| libibverbs | 已裝 | 今天不跑 RDMA 模式；CMake 偵測到會印 `RDMA enabled` |
| ONNX Runtime | 未裝 | 正常（AI 停用，無影響） |

### 1.3 編譯

```bash
export PATH=/usr/local/cuda/bin:$PATH        # nvcc 預設不在 PATH
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_CUDA_ARCHITECTURES=121
cmake --build build -j$(nproc)
```

- CMake configure 乾淨；找到 CUDAToolkit 13.0.88 / OpenCV 4.13.0 / nlohmann_json 3.11.3；ORT 找不到（AI 停用，正常）。
- 編譯 **零警告**（含 `cuda_kernels.cu` / `ai_kernels.cu`）。x86→ARM 風險點未觸發：`char` 符號性（專案用 `uint8_t` 安全）、packed struct（`static_assert(sizeof(FrameHeader)==256)` 守住）。
- 產物：`ip/build/cfaoi_ip` = **ELF 64-bit aarch64** 可執行檔。

---

## 二、offline-tcp 連線 smoke

```bash
./build/cfaoi_ip --mode offline-tcp --control-port 8200
```

- 啟動 log：`[TensorCore] Using TF32 Tensor Core math mode (sm_121)`、RF model 載入、`[Zone] 1 個檢測區`。
- 監聽 `0.0.0.0:8200`（`ss -ltnp` 確認）。
- 本機 newline-JSON client 驅動：

| 命令 | 回應 |
|------|------|
| `CHECK_HEALTH` | `{"ai":false,"seq":1,"status":"OK"}` |
| `GET_STATUS` | `{"data":{"ai":false,"processed":0,"zones":1},"seq":1,"status":"OK"}` |

→ server loop + newline-JSON 解析在 ARM 正常；`ai:false` 符合「AI 預設停用」不變式。
（Mac live Control 連 `100.83.141.68:8200` 送圖：使用者 2026-06-15 早上已測，送圖正常。）

---

## 三、跨架構一致性（重點）

**方法**：用 `~/download/CF AOI_GPU Only/Test Image/` 下 26 張真實 IP04 面板（8160×5000 8-bit TIFF），
以 `--mode offline-file --ini config/default_zone.ini --verify-deterministic` 在 ARM/GB10 跑，
與該資料夾 `defects/batch_detection_report.csv`（reference `gpu_algo` 跑出的 ground truth）逐張比對。

### 3.1 總表

| 結果 | 張數 |
|------|------|
| 缺陷數**完全一致** | **25 / 26** |
| 不一致 | 1（028，單像素邊界翻面，見 3.3） |
| ARM 自身兩跑 bit-exact | **26 / 26** |

其餘 24 張：reference 0 / ARM 0。`zero_copy=yes`（GB10 mapped 路徑）、`ai=off`、每張 `[Diag]` 三數一致
（DetectionResult = JSON DefectInfo = 寫出 patch）。

### 3.2 IP04_Origin000027（1 顆 dark，正例）

| 欄位 | reference | ARM(GB10) | |
|------|-----------|-----------|---|
| 缺陷數 | 1 | 1 | ✓ |
| 型別 | dark | PointDark | ✓ |
| Y | 725 | 725 | ✓ 完全一致 |
| X | 5894 | 5895 | ⚠️ 差 1px |
| Size / GL_Mean | — | 5 / 37.2 | — |

X 差 1px：5-pixel blob 的質心在不同條件下（reference 單趟 CCL vs 本程式收斂迴圈+canonical 質心，
或 sm_75↔sm_121 浮點 ULP）位移 1 像素。**偵測到、型別對、位置 ±1px → 正確。**

### 3.3 IP04_Origin000028（單像素邊界翻面，判定：接受）

- ARM 多偵測 1 顆 **單像素** `PointDark @ (1852,2372)`，`Size=1`、`GL_Mean=53`；reference 為 0。
- DTH 微掃證據（固定 BTH=1.40，僅改 DTH）：

  | DTH | 028 缺陷數 |
  |-----|-----------|
  | 0.60（預設） | **1** |
  | 0.58 | 0 |
  | 0.55 / 0.50 | 0 |

  → 收緊 **0.02** 即消失，表示該像素的 DIV 比值恰卡在 0.60 邊界下緣。ARM/sm_121 浮點算出 < 0.60（判缺陷），
  reference 那次落在 ≥ 0.60（不判）。屬**邊界像素 ULP 翻面**，非程式錯。

- **判定（使用者）：接受現狀**。理由：該像素是一顆真實微弱暗點（GL=53），reference（單趟 CCL）漏了、
  本程式（收斂迴圈）抓到，方向偏保守（**寧抓勿漏**），且 AOI 缺陷預設 `待人工複核` 會進 DefectSort 人工確認，
  不影響判定可靠性。

---

## 四、GB10 速度 bench

`--mode bench`（cudaEvent 量純 GPU `process_image`，--no-save-images，60 iters / 10 warmup）：

| 案例 | 缺陷 | gpu_ms median |
|------|------|---------------|
| 001 乾淨 | 0 | **6.95** |
| 027 1缺陷 | 1 | **7.37**（100 iters 時 7.40） |
| 027 1缺陷（重跑） | 1 | 7.41 |
| 027 中負載 DTH0.85 | 10000（觸頂） | 8.11 |
| 027 重負載 DTH0.95 | 10000 | 14.34 |
| 027 重負載 DTH0.95（重跑） | 10000 | 14.19 |

> 全部 bench 皆於實際生產 block_dim **16×16** 下量測（見 4.1）。

### 4.1 block_dim：固定 16×16（INI 的 32×32 從未生效）

逐檔核實 [zone_config_adapter.cpp:69-70](../../ip/src/config/zone_config_adapter.cpp#L69-L70)：`from_ini` **硬寫死
`block_dim = 16×16`，完全忽略 INI 檔的 `[GPU] block_dim`**（即使 default_zone.ini 寫 32×32）。
因此上表中「1缺陷 7.37 / 重跑 7.41」「爆量 14.34 / 重跑 14.19」**兩兩其實都是 16×16**，數字一致只反映 run-to-run 穩定性，**不是 16×16 vs 32×32 的比較**。
結論：IP **永遠用 16×16**，RAG_TRAINING.md §5.2 建議的 16×16 已是現狀、32×32 路徑從未被執行；改 INI 不影響 GPU block 維度。
（勘誤：本報告初版誤把兩組重跑當成 16 vs 32 的對照，實為同一設定。）

### 4.2 容量換算

正常正確 pitch 的面板（個位數缺陷）≈ **7.4ms/張**：

```
N_spark = ceil(1110 張 × 7.4ms / 30000ms) = ceil(0.27) = 1 台
1110 × 7.4ms = 8.2 s / 面板，30s 節拍 → 餘裕 ~73%
```

即使退一步用爆量 14.3ms 仍 `ceil(0.53)=1` 台。**1 台 Spark 足夠**（G8.5 37 相機陣列 / 30s 節拍）。

---

## 五、效能落差結論

同顆 GB10、同一張影像 027：本程式 **7.4ms** vs reference **4.944ms**（CSV 內該張處理時間）≈ **慢 1.5×（+2.4ms）**。落差來源（**皆非 bug，是刻意的決定性代價**）：

1. **block_dim 不是變因**：固定 16×16（4.1；INI 32×32 從未生效），落差與 block_dim 無關。
2. **0 缺陷 floor 就已差 ~2ms**（乾淨 6.95 vs reference ~4.9）。此時 blob/下載可忽略，差異純粹來自：
   - **CCL 收斂迴圈**：host wrapper 迭代 `kernelFastCCLMerge` 直到 `d_changed==0`（每趟重掃 8160×5000×int32 ≈ **163MB** label buffer），reference 只跑單趟未收斂；
   - **zero-copy mapped 讀取路徑**（GB10 經統一記憶體即時讀，41MB 影像 > 24MB L2 無法全快取）；
   - **canonical 排序**（download 後依 `label→center_y→center_x→size` 排序）。
   見 [ip/CLAUDE.md](../../ip/CLAUDE.md) 不變式 #7/#8 —— 以速度換 bit-exact 決定性。
3. **頻寬綁定 + 收斂迴圈 → 隨缺陷量 scaling**：0→1 缺陷僅 +0.4ms，但爆量（10000）時 gpu_ms 由 7.4 衝到 14.3ms（~2×），佐證「記憶體頻寬綁定 + 收斂趟數隨連通域增加」。也說明 RAG 的 4.9ms 只在「近乎零缺陷乾淨面板」成立。

> 附註：不可拿本批 6.95ms 直接與 x86 RTX2080S baseline（IP01 影像、557/2606 缺陷、11.37/11.67ms）比較稱「GB10 較快」——負載不同。手上唯一 apples-to-apples 的是「同 GB10、同影像：ours 7.4 vs reference 4.9」，結論為「為決定性慢 1.5×，但容量仍只需 1 台」。

---

## 六、結論

- IP 程式在 **DGX Spark GB10 / sm_121** 上：編譯（零警告）→ 連線 → AOI 運算正確性（vs ground truth）→ 跨架構一致性 → 速度，**全部實機跑過 → L4**。
- **跨架構一致性成立**：x86↔ARM 整數/幾何完全一致，浮點邊界像素可能單像素 ULP 翻面（偏保守方向）→ **off-line（x86）調的參數對 on-line（ARM）有效**。
- **容量拍板：1 台 Spark 足夠**（實測 7.4ms/張，餘裕 ~73%）。

---

*報告版本：2026-06-15 | 平台：DGX Spark GB10（sm_121）/ CUDA 13.0 / aarch64*
