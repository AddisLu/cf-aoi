# CF-AOI tools/

小工具目錄。各工具不依賴 ip/grab/control 的內部模組，獨立編譯。

---

## golden_maker

**用途**：製作對位 Golden Pattern（搜尋 Mark 截圖）並輸出 RecipeInfo.xml `<M_AlignRoi>` 片段。

**對應流程**：Step 1 調 Recipe 前，用一張代表幀截出 Mark 區域 → 嵌入 RecipeInfo.xml → Control 端 LOAD_RECIPE 時以 base64 傳給 IP。

### 編譯

```bash
cd tools/golden_maker
mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
make -j4
```

依賴：OpenCV（只需 core/highgui/imgcodecs/imgproc）。

### 用法

**GUI 模式**（有桌面）：

```bash
./golden_maker --image /path/to/frame.png [--output mark.png] [--search-margin 3]
```

- 滑鼠拖拽框選 Mark 區域
- `S` → 存 8-bit 灰階 PNG + 把 AlignRoi XML 印到 stdout
- `R` → 重設框選
- `Q` / ESC → 離開

**CLI fallback**（SSH server / headless，知道 Mark 座標）：

```bash
./golden_maker --image frame.png --mark-rect x,y,w,h [--output mark.png] [--search-margin 3]
# 例：
./golden_maker --image frame.png --mark-rect 1170,770,60,60
```

### stdout 輸出（貼入 RecipeInfo.xml `<M_AlignRoi>`）

```xml
<!-- 貼入 RecipeInfo.xml <M_AlignRoi> -->
<PatternPath>mark.png</PatternPath>
<ReferX>1200</ReferX>
<ReferY>800</ReferY>
<SearchWidth>180</SearchWidth>
<SearchHeight>180</SearchHeight>
```

`SearchWidth/Height` = Mark 尺寸 × `--search-margin`（預設 3），給影像移動足夠搜尋空間。

### 參數說明

| 參數 | 說明 | 預設 |
|------|------|------|
| `--image <path>` | 來源影像（任何 OpenCV 支援格式） | 必填 |
| `--output <path>` | 輸出 Golden PNG | `mark.png` |
| `--mark-rect x,y,w,h` | CLI fallback：直接指定 Mark 矩形（整數像素） | — |
| `--search-margin N` | SearchWidth/Height = Mark 尺寸 × N | `3` |

---

*未來其他小工具也記在這裡。*
