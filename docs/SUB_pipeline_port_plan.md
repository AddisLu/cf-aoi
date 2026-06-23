# Legacy SUB 偵測管線移植計畫（T550QVN10 真實 recipe）

> 2026-06-23 起。決策：真實線上 recipe 全 24 顆是 **SUB**（非 DIV）+ 全 panel 拼接座標 + 含前處理，與現行 DIV-only/逐 slice/無前處理 IP 三層不相容（見 [[real-recipes-are-sub-not-div]] / `rdma_replay_驗證報告_20260623.html`）。Addis 拍板：**移植 legacy SUB 偵測管線到 IP**。本文件記錄從 `Reference/PrjCfAoi/CudaCore` 逐行考證出的**精確規格**與**分階段實作/驗證計畫**。

## ⚠️ 重要更正：真實演算法比第一版調查所述複雜得多

第一版相容性 workflow 把 8-Way-Star SUB 簡化成「center − mean8(8鄰) 單次比較」。**逐行讀 legacy CUDA 後證實這是錯的**。真實演算法是**逐路差分 + 投票**，且帶 3×3 SAD 局部搜尋與 9 種邊界 zone-type。現行 IP 的 DIV kernel（單次 `center*8 vs Σ鄰` 比例式）**不是 legacy 的簡化版，是另一套架構**。

## 精確 legacy 規格（出處逐一標註）

### 1. 8-Way-Star SUB kernel — `Algo_8WAY_STAR_SUB_8bits`（CUDA_Kernel.cu:1044-1564）
每像素：
- 載入中心的 3×3 鄰域 9 px 進 shared（`RescentData`，CUDA_Kernel.cu:1077-1085）。
- 對 `cnt_Pitch = 0 .. PitchTimes-1`（本 recipe PitchTimes=2）：
  - `PitchX = recipe.PitchX × (cnt_Pitch+1)`、`PitchY = recipe.PitchY × (cnt_Pitch+1)`（:1121-1122）。
  - 對 8 個方向（上/右上/右/右下/下/左下/左/左上，:1124-1162）：
    - `index_Cmp = GetCompareIndex(...)` ← 3×3 SAD 局部最佳匹配（見 §2）。
    - `CompareResult[cnt_Pitch*8 + way] = (int)center − (int)src[index_Cmp]`（逐路灰階差，:1127）。
  - ⇒ 共 `PitchTimes × 8 = 16` 個 per-way 差值。
- **投票判定**（CUDA_Kernel.cu:1521-1562）：
  ```
  CountBP=0; CountDP=0; MinBPDiff=255; MinDPDiff=255;
  for k in 0..PitchTimes*8-1:
      if CompareResult[k] >= ThB: CountBP++; MinBPDiff=min(MinBPDiff, abs(CompareResult[k]))
      if CompareResult[k] <= ThD: CountDP++; MinDPDiff=min(MinDPDiff, abs(CompareResult[k]))
  if   CountBP >= ChooseAmount: pixel=BrightDefect (cmp=MinBPDiff)
  elif CountDP >= ChooseAmount: pixel=DarkDefect   (cmp=MinDPDiff)
  else:                         pixel=Background
  ```
  - `ThB=BrightThreshold`(如+17)、`ThD=DarkThreshold`(如-16) = **灰階差**（非比例；CoreProcessor.cpp ThB=(float)BrightThreshold 直接賦值）。
  - `ChooseAmount`（本 recipe=13）：16 路中至少 13 路超標才算缺陷 → robust 投票。
- **9 種 PixelZoneType**（Type 1=內部全 8-way；Type 2-9=邊/角，用單邊/twice 替代越界方向；Type 0=OutOfZone；<0=ErrZone）。逐 type 各自一段 gather（:1114-1519）。

### 2. 局部搜尋 — `GetCompareIndex`（CUDA_KernelFunction.cu:204-242）
對目標鄰點 (ComparedX,ComparedY)，在 `[-SearchX..SearchX]×[-SearchY..SearchY]`（本 recipe 1×1 → 3×3=9 位置）逐位置算「候選 3×3 鄰域 vs 中心 3×3 鄰域」的 9-px SAD，取 SAD 最小者的索引回傳。= 容忍面板 skew 的逐路 3×3 配準。
- 成本：每 way 9 位置 × 9px SAD = 81 reads；×16 way ≈ 1300 reads/輸出像素（SearchX/Y=1）。重，但 legacy 即如此。

### 3. CUDAZone 欄位（CUDA_Func.h:102-122）
`Algorithm, ThB(float), ThD(float), PitchX, PitchY, PitchTimes, ChooseAmount, SearchX, SearchY`。
→ 現行 `ZoneConfig`（zone_config_adapter.h）只有 `BTH/DTH/pitch_x/pitch_y/search_range_x/y/fast_search_range`，**缺 pitch_times、choose_amount、algo_mode**，且 BTH/DTH 註記為 DIV 比例域。

### 4. 前處理（CUDA_Kernel.cu）
- **Ip_Remap** = MIL `MimRemap(M_FIT_SRC_DATA)`：依子影像 src min/max 線性拉伸到全動態範圍（對比正規化），偵測前對每個 DetectRoi 子圖就地套用（legacy CamProc.cs:591-598）。
- **高斯平滑** = `Algo_ImageSmooth3x3_8bits`(CUDA_Kernel.cu:2530) / `5x5`(:2572)。本 recipe `SmoothTimes=0`(5x5 不跑)、`SmoothTimes2=1`(3x3 跑 1 次)，kernel 係數 Gau3x3_8（除數 8）。

### 5. Blob 過濾（RecipeSetting + DetectRoi）
`BlobMinSize=3 / BlobMaxSize=100000 / Blob*MergeDistance=26`：連通元件 → 面積過濾 + 距離合併。現行 IP 完全沒有（zone_config_adapter.cpp:162 明示忽略 Blob*）。

### 6. 座標系 / slice（CamProc.cs:1016-1531）
legacy 把每 CCD N 張 slice 在 Y 拼成大 buffer（高=ImageSizeY×FrameCnt≈155000），用全域 Y ROI 偵測；每 frame 算 band [cint_frame×ImageSizeY, +ImageSizeY) 與 ROI 取交集，缺陷 GlobalPosY=frameStartY+local。現行 IP 逐 frame、無此映射（main.cpp:166-173 直接 clamp）。

## 進度（2026-06-23）

| 步驟 | 狀態 | 結果 |
|------|------|------|
| **A. SUB 投票 kernel + 守門** | ✅ commit `6ec2ff8` | 8-Way-Star SUB(16路逐路差+3×3 SAD+ChooseAmount投票)；守門改讀 M_AlgorithmWayCompare。IP04 img027→1缺陷✓、clean→0、bit-exact。 |
| **C/D. Ip_Remap + 高斯平滑** | ✅ commit `21ea99c` | 平滑降噪(029 FP 482→285)；remap 對比拉伸(直方圖 min/max)。remap 會放大噪點 FP → 需 Step E Blob 控制。 |
| **B. --stitch 全panel拼接** | ✅ commit `21ea99c` | 目錄 slice 在 Y vconcat 成整片 panel 再偵測。025-029 拼接:027 於 panel 座標正確檢出。 |
| **★ ROI 真因** | ✅ 已解 | **028 一直漏抓 = 我手寫測試 recipe 誤用 IP01 的 ROI X[4013,8160]；IP04 真實 ROI 是 X[0,8160] 全寬。** 028 缺陷在 X=1853(左半,Size=1 PointDark)→被排除。改全寬後 027→1✓、028→1✓ = **Addis 驗收達標**。**教訓:用各 CCD 自己的 RecipeInfo.xml,勿手寫假設值。** |
| **E. Blob 過濾** | ⏳ 待做 | BlobMinSize=3/MergeDistance=26 壓 029 類 FP。**⚠️ 開放問題:028 真缺陷 Size=1,若 BlobMinSize=3 直接過濾會把它濾掉 → 需 legacy golden 確認 legacy 的 Size 度量(是否含平滑擴散/bounding-box)才不會誤濾。** |
| **F. 真 recipe 全鏈重驗** | ⏳ 待做 | 用 IP04 真 RecipeInfo.xml(全panel ROI Y→146k)+ 全 31 slice 拼接;需把 31 slice 弄到 Spark(damac→spark 直送 SSH 信任被自動拒;Mac 中轉慢;或建 RDMA-stitch)。 |

## 分階段實作 + 驗證（每步 commit+push+L3）

| 步驟 | 內容 | 驗證 |
|------|------|------|
| **A. SUB 投票 kernel + ZoneConfig + 守門** | 新增 `kernelSub8WayStarVoting`（不改 DIV kernel；invariant 1 守住）：內部像素 16-way 逐路差 + 3×3 SAD 搜尋 + ChooseAmount 投票；邊界沿用 margin skip（本 recipe BypassEdge=50 ≥ pitch reach 52，邊界差異落在 bypass 內）。ZoneConfig 加 `algo_mode/pitch_times/choose_amount`；守門改讀 `M_AlgorithmWayCompare`（含 `_Sub`→SUB，不再被 stale `<AlgorithmCompare>DIV>` 騙過）。SUB 時 ThB/ThD 當灰階差。 | 合成圖（已知亮/暗點，手算 ≥ChooseAmount→偵到；均勻區→0）；單張真實 slice 缺陷數合理（非 0、非爆 10000）；bit-exact 重跑。 |
| **B. slice→Y-offset ROI 映射** | sender 設 `FrameHeader.sliceIndex`=影像序；IP 依 `frameStartY=sliceIndex×height` 對每 zone 做 band 交集→落本幀局部座標，缺陷 GlobalPosY=frameStartY+local（對齊 CamProc.cs:1458-1531）。 | 拼接座標 recipe 餵逐 slice，ROI 落在正確 band；跨 slice 缺陷全域座標正確。 |
| **C. Ip_Remap 前處理** | 子圖 min/max 線性拉伸（MimRemap M_FIT_SRC_DATA 等價）。 | remap on/off 對同圖缺陷數量級差異；與 legacy 公式核對。 |
| **D. 高斯平滑** | SmoothTimes2×3x3（Gau3x3_8 係數，除 8），偵測前。 | smooth on/off 缺陷數差異。 |
| **E. Blob 過濾** | 連通元件 + BlobMinSize 面積過濾 + MergeDistance 合併。 | 已知 blob 圖核對過濾/合併數。 |
| **F. 全 24 CCD 重驗** | 真實 recipe 全鏈 RDMA→Spark；缺陷數合理 + 對位（IP01）。 | **理想：legacy golden ResultInfo.xml 逐缺陷比對**（需舊機台跑同圖）。無 golden 則合成+sanity。 |

## 驗證的硬限制（誠實記帳）
- **真正 bit-exact 對 legacy 需 legacy golden 輸出**（舊機台 Windows 對同批 TIF 跑出的 ResultInfo.xml）。`RecipeSetting.OfflineLoadImageFolder=D:\Cf_Aoi\Image\20260429\00201B0A_T550QVN10_TGT_G_Src` 顯示舊機台有來源圖 → 若能提供 golden，可做逐缺陷 GC_X/GC_Y/Size/Type 比對（最高等級驗證）。
- 無 golden 時：每步對「ported 公式」在合成圖上手算驗證（可證）+ 真實圖 sanity（缺陷數合理、視覺合理），標 L2/L3-partial，不宣稱 bit-exact-vs-legacy。
- invariant 1：只**新增** kernel，不改現有 DIV/AI kernel。invariant 10：同圖重跑 bit-exact（投票 kernel 為整數確定性，OK）。
