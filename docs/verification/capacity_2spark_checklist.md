# 雙 Spark 產能與 mode2 — 上線前驗證清單

> 目的：把 2026-07-16 產能討論定案的待辦落成可逐項勾選的驗收單，避免遺忘。
> 背景決策：**已購 2 台 DGX Spark**，要處理 **上 37＋下 18＝55 CCD**（30s 節拍）。
> 生產基準 = **DIV mode0**（2026-07-12 Addis 決策，`7.4ms/張` GB10 L4 實測）；**mode2（DIV-voting 融合）為候選升級**——
> 設計上準確度較高（暗區棄權＋16 路投票＋多尺度，對症修 mode0 的邊緣 FP 與大顆低對比漏檢），
> 但**效能未 bench（審計 P0-2 殘餘）、準確度無 A/B（審計 B2）、缺 DTH 域值防呆（審計 P0-5）**。
>
> 誠實分級標注：7.4ms/張＝**L4 實測**（cudaEvent 含 H2D）；分段 0.9(H2D)/3.4(kernel)/1.6(CCL)/0.9(Blob)/0.6(排序) ms＝教材拆解；
> **mode2 倍率 r＝未測**（教材動畫 +2.3ms 多尺度為示意值）。本清單所有台數結論在 V1 完成前都是投影。
>
> 相關：審計 [architecture_audit_20260712.md](../architecture_audit_20260712.md)（P0-2 / P0-5 / A2 / B2 / B6）、
> [verification_report_arm_20260615.md](verification_report_arm_20260615.md)（7.4ms 出處）。
>
> 圖例：☐ 待驗 · ✅ 通過 · ❌ 有問題（記下現象）

---

## V1. mode2/mode1 效能 bench（GB10）——收口審計 P0-2/A2，決定 2 台分配法

在 Spark 上執行（三個 mode 都測，A2 要求；每輪順帶 `--verify-deterministic` 確認不破 bit-exact）：

```bash
cd ~/Addis/cf-aoi/ip
# mode2 + 多尺度 2×
./build/cfaoi_ip --mode offline-file --input ../test_images/IP01_Origin000002.tif \
    --output /tmp/m2bench --algo-mode 2 --multiscale 1 --no-save-images
# mode2 + 多尺度 2×+4×
./build/cfaoi_ip --mode offline-file --input ../test_images/IP01_Origin000002.tif \
    --output /tmp/m2bench --algo-mode 2 --multiscale 2 --no-save-images
# mode1 SUB（對照組）
./build/cfaoi_ip --mode offline-file --input ../test_images/IP01_Origin000002.tif \
    --output /tmp/m2bench --algo-mode 1 --no-save-images
# 讀 log 的 gpu_ms（cudaEvent 含 H2D，與 7.4ms 同一把尺）
```

| # | 項目 | 判準 | 結果 |
|---|------|------|------|
| V1-1 | mode2＋multiscale 1 的 gpu_ms | 記下 r = gpu_ms / 7.4 | ☐ r = ___ |
| V1-2 | mode2＋multiscale 2 的 gpu_ms | 同上（4× 邊際成本應小） | ☐ r = ___ |
| V1-3 | mode1 SUB 的 gpu_ms | 對照組記錄 | ☐ |
| V1-4 | `--verify-deterministic` 三 mode 皆過 | 同圖兩跑 bit-exact | ☐ |
| V1-5 | 依 r 查下方門檻表，圈定分配法 | 37/18 或 28/27 | ☐ 決定：___ |
| V1-6 | 回填：審計 P0-2 殘餘劃掉；STATUS/CLAUDE.md 容量敘述加註「mode2 = X ms 實測」 | docs 同步 | ☐ |

**門檻表（55 CCD、30s 節拍、2 台）**：

| mode2 倍率 r | A 台（上 37）負載 | 結論 |
|---|---|---|
| ≤ 2.7 | ≤ 74% | **37/18 自然分**（餘裕 ≥26%）|
| 2.7 – 3.65 | 74–100% | **改均衡分 28/27**（硬上限拉到 4.8，餘裕 30% 線在 3.4）|
| 3.65 – 4.8 | 破表 | 必須均衡分 |
| > 4.8 | — | 2 台不夠，升級討論（實務上到不了）|

> mode0 參考：A 台 8.2s（27%）／B 台 4.0s（13%）；單台可扛 55 CCD（12.2s＝41%）→ 若最終選 mode0，第二台可規劃**熱備／A-B 驗證機**，順帶解掉單點失效。

## V2. mode0 vs mode2 準確度 A/B——把「設計上較準」升級成實測結論（順帶收口審計 B2）

| # | 項目 | 判準 | 結果 |
|---|------|------|------|
| V2-1 | 26 張 GB10 驗證集＋實掃面板，同配方 `--algo-mode 0` 與 `2` 各跑一輪 | 兩組結果落地 | ☐ |
| V2-2 | DefectSort 人工標 TrueDefect/Particle 當 ground truth | 每張標完 | ☐ |
| V2-3 | 統計：檢出率（漏檢）＋誤報率 兩組對比 | mode2 邊緣 FP 應降、大顆低對比檢出應升；若否，記錄反例 | ☐ |
| V2-4 | 凍結 mode2 golden 回歸基準庫（B2：非 bit-exact-vs-legacy，誠實標注）＋掛 RTX2080 nightly | baseline 落地 | ☐ |

## V3. mode2 DTH 域值防呆（審計 P0-5）——V1/V2 的前置修碼

| # | 項目 | 判準 | 結果 |
|---|------|------|------|
| V3-1 | `zone_config_adapter.cpp` is_fused 分支補域值防呆（比照 mode0 的 DTH<0 拒載） | 修碼＋rules_verify | ☐ |
| V3-2 | 驗證：SUB 灰階閾值（如 +17/−16）填入 mode2 配方 → **拒載或 WARN** | 不得靜默 0 缺陷 PASS | ☐ |

## V4. 雙 Spark 接入（2 台上線的真正 blocker）

> Control 目前只支援單一 `ActiveIpNode`；gap #1/#5 的「IP 多 CCD：單進程多工 vs N 進程」尚待決（審計 B6 骨架）。
> **因 2 台採購，本項由 batch-later 升為上線前必做。**

| # | 項目 | 判準 | 結果 |
|---|------|------|------|
| V4-1 | IP 多 CCD 多工方案定案（單進程多工 vs N 進程） | 設計文件/決策記錄 | ☐ |
| V4-2 | Control 多運算單元：兩台 IP 同時連線＋雙節點健康燈 | 不假綠、斷一台看得出來 | ☐ |
| V4-3 | 配方分發到兩台（LOAD_RECIPE ×2，network-clean 不變） | 兩台各載對自己的 per-CCD 分區 | ☐ |
| V4-4 | `CF_GET_RESULT` 聚合兩台結果回上位機 | 路徑+缺陷數合併正確；單台失聯回 ERR（誠實失敗，不假 OK——連動審計 P0-3） | ☐ |

## V5. 拓樸與頻寬

| # | 項目 | 判準 | 結果 |
|---|------|------|------|
| V5-1 | `array_topology.json` 宣告 55 槽 × 2 運算單元（37/18 或 28/27，依 V1-5） | 宣告≠live 綁定（約束②），未綁不標線上 | ☐ |
| V5-2 | 頻寬確認：單台最重 37×0.76≈28Gbps（100G 鏈路 28%）；switch 佈線讓兩台都收得到對應 Grab 流量 | 均衡分法需跨上下陣列路由 | ☐ |

---

## 附錄 A：容量門檻速查表（55 CCD、30s 節拍）

| 每張耗時 | 倍率 vs 7.4ms | A 台上 37（1110 張） | B 台下 18（540 張） | 判定 |
|---|---|---|---|---|
| 7.4ms（mode0 實測）| 1.0× | 8.2s＝27% | 4.0s＝13% | ✓✓ 單台亦可扛 55（41%）|
| 9.7ms | 1.3× | 10.8s＝36% | 5.2s＝17% | ✓ 輕鬆 |
| 14.8ms | 2.0× | 16.4s＝55% | 8.0s＝27% | ✓ |
| 20ms | 2.7× | 22.2s＝74% | 10.8s＝36% | 37/18 分法的舒適線 |
| 27ms | 3.65× | 30s＝100% | — | 37/18 破表 → 均衡 28/27（各 ~840/810 張）|
| 35.7ms | 4.8× | （均衡）30s＝100% | 同左 | 2 台硬上限 |

## 附錄 B：G7 新機台估算摘要（2026-07-16 討論，全部為投影）

6 隻 16K（opt-cl1-m16-xg3-02）LineScan、2µm、G7（以 1870×2200mm 計）、TT 168s、Stage Y 來回掃描＋CCD X-shift：

- 資料量 ≈ **1.03 Tpx/板**（含 5% 重疊 ~1.08T）；10 趟（每趟 6×16384px＝196.6mm 帶寬）；取像流 ~6.1GB/s（每台 Spark 2 CCD 僅 ~18Gbps）
- **DIV mode0**（5.54 Gpx/s/台）：1 台 195s ✗；**2 台 97s（餘裕 42%）可行；3 台 65s（餘裕 61%）建議**，6 CCD 對稱各 2
- **mode2**（未 bench，抓 2×）：**4–5 台**；V1 的 r 出來後按比例重算
- SUB 全 panel（0.71 Gpx/s，24 CCD 實測 40.3s 換算）：~10 台起跳，不可行——維持 DIV 路線
- 16K 寬幀（16384×5000）的 14.8ms/幀為線性外推；上線前在 GB10 用 16K 影像跑一次 offline-file bench 收口
