# tools 小工具完整說明

> 版本：2026-06-17 整理（逐檔靜態分析 tools/ + 對齊 Gap #1 對位 pipeline）
> 目錄：`tools/`（各工具獨立編譯，不依賴 ip/grab/control 內部模組）
> 現有工具：`golden_maker`（對位 Golden Pattern 製作）

---

## 目錄

1. [概述](#1-概述)
2. [golden_maker](#2-golden_maker)
3. [在 Gap #1 對位 pipeline 中的位置](#3-在-gap-1-對位-pipeline-中的位置)
4. [關鍵檔案索引](#4-關鍵檔案索引)

---

## 1. 概述

`tools/` 放**獨立小工具**：不連結 ip/grab/control 的內部模組、各自一份 `CMakeLists.txt`、可單獨編譯部署。
目前只有 `golden_maker`（對位用），未來其他工具也記於此。

| 工具 | 用途 | 依賴 | L-level |
|------|------|------|---------|
| `golden_maker` | 框選 Mark → 輸出 Golden PNG + RecipeInfo.xml `<M_AlignRoi>` 片段 | OpenCV（core/highgui/imgcodecs/imgproc）| L2（CLI 路徑隨 Gap #1 對位 L3 間接驗過；GUI 互動未自動測）|

---

## 2. golden_maker

### 2.1 用途

製作**對位 Golden Pattern**（搜尋 Mark 截圖）並輸出 RecipeInfo.xml 的 `<M_AlignRoi>` 片段。

**對應流程**：Step 1 調 Recipe 前，用一張代表幀截出 Mark 區域 → 嵌入 RecipeInfo.xml → Control 端 LOAD_RECIPE 時以 base64 把 Golden PNG 傳給 IP（network-clean，不變式 8）→ IP `align_engine` 用它做對位（CHECK_ALIGN）。

### 2.2 兩種模式（golden_maker.cpp:104-207）

| 模式 | 觸發 | 適用 |
|------|------|------|
| **GUI** | 不帶 `--mark-rect` | 有桌面：滑鼠拖拽框選 |
| **CLI fallback** | 帶 `--mark-rect x,y,w,h` | SSH server / headless：已知 Mark 座標 |

**GUI 操作**：拖拽框選 → `S` 存 8-bit 灰階 PNG + 印 AlignRoi XML 到 stdout；`R` 重設；`Q`/ESC 離開。
（GUI 大圖自動縮放顯示，上限 2000px；存檔時用 `inv_scale` 還原回原始座標，golden_maker.cpp:160-199。）

### 2.3 命令列參數

| 參數 | 說明 | 預設 |
|------|------|------|
| `--image <path>` | 來源影像（任何 OpenCV 格式，以 `IMREAD_GRAYSCALE` 讀）| 必填 |
| `--output <path>` | 輸出 Golden PNG | `mark.png` |
| `--mark-rect x,y,w,h` | CLI fallback：直接指定 Mark 矩形（整數像素）| —（給了就走 CLI）|
| `--search-margin N` | `SearchWidth/Height = Mark 尺寸 × N` | `3` |

### 2.4 stdout 輸出（print_align_roi_xml，golden_maker.cpp:86-101）

```xml
<!-- 貼入 RecipeInfo.xml <M_AlignRoi> -->
<PatternPath>mark.png</PatternPath>
<ReferX>1200</ReferX>          <!-- = orig_x + orig_w/2（Mark 中心）-->
<ReferY>800</ReferY>           <!-- = orig_y + orig_h/2 -->
<SearchWidth>180</SearchWidth>  <!-- = Mark 寬 × search-margin -->
<SearchHeight>180</SearchHeight><!-- = Mark 高 × search-margin -->
```

- `ReferX/Y` = **Mark 中心**（非左上角）。
- `SearchWidth/Height` = Mark 尺寸 × `--search-margin`（預設 3），給影像移動足夠搜尋空間。
- 框選/矩形 < 4×4 px 會拒絕（太小）。

### 2.5 編譯

```bash
cd tools/golden_maker
mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
make -j4
```

依賴：OpenCV（core/highgui/imgcodecs/imgproc）。

### 2.6 用法範例

```bash
# GUI（有桌面）
./golden_maker --image /path/to/frame.png --output mark.png --search-margin 3

# CLI（SSH / headless，已知 Mark 在 (1170,770) 大小 60×60）
./golden_maker --image frame.png --mark-rect 1170,770,60,60
```

---

## 3. 在 Gap #1 對位 pipeline 中的位置

golden_maker 是 **Gap #1 對位 pipeline 的離線前置工具**（2026-06-17 對位 pipeline L3）：

```
[離線，調 Recipe 前]
  golden_maker --image 代表幀 --mark-rect ...
    → mark.png（Golden Pattern）+ <M_AlignRoi> XML 片段
    → 人工貼入 RecipeInfo.xml 的 <M_AlignRoi>

[線上，每片一次]
  Control LOAD_RECIPE：讀 PatternPath → base64 嵌入 golden_png_base64 → 送 IP
  IP align_engine：記憶體 decode Golden（不寫磁碟）
  CF_GRAB_START → CHECK_ALIGN（收 500×500 搜尋 ROI）
    → run_align()：13 角 × TM_CCOEFF_NORMED + 拋物線 sub-pixel → ShiftX/Y
    → SET_ALIGN：aligned_* = roi_* + round(shift_*) 套回所有 zones
```

對位演算法本體在 IP（`ip/src/align_engine.{h,cpp}`），golden_maker 只負責**產生 Golden + AlignRoi 參數**。
驗證鏈：`ip/src/align_verify.cpp`（Stage 1 14/14 PASS）+ `scripts/verify_alignment.py`（Stage 2 8/8 PASS）。詳見 ip/CLAUDE.md 不變式 24 + STATUS.md「對位 pipeline（Gap #1）」列。

> 考古對應：取代 legacy `PrjCfAoi` 的 MIL `MpatFind` Pattern Match（`CamProc.cs:307-485`）。
> legacy 用 MIL Mark 圖；新架構改 OpenCV TM_CCOEFF_NORMED + golden_maker 產生 Golden（演算法重寫，非移植）。

---

## 4. 關鍵檔案索引

| 主題 | 檔案 |
|------|------|
| golden_maker 原始碼 | [tools/golden_maker/golden_maker.cpp](../tools/golden_maker/golden_maker.cpp) |
| golden_maker 建置 | [tools/golden_maker/CMakeLists.txt](../tools/golden_maker/CMakeLists.txt) |
| 簡要 README | [tools/README.md](../tools/README.md) |
| IP 端對位引擎 | [ip/src/align_engine.cpp](../ip/src/align_engine.cpp) |
| 對位驗證 | [ip/src/align_verify.cpp](../ip/src/align_verify.cpp) / [scripts/verify_alignment.py](../scripts/verify_alignment.py) |
| 對位不變式 | [ip/CLAUDE.md](../ip/CLAUDE.md)（不變式 24）|

---

*本文件由 tools/ 逐檔靜態分析整理。`tools/README.md` 為簡要操作卡，本文件為完整說明（含 Gap #1 對位 pipeline 脈絡 + 考古對應）。未來新增工具同步補於此。*
