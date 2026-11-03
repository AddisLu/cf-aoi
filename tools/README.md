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

> ⚠️ **已知限制（2026-08-02 複查 T1）**：GUI 存出的 golden 取自**縮放後**顯示圖（>2000px 自動縮，
> 線掃 8192 寬約 0.24×），XML 座標卻已還原成全解析度 → 兩者尺度不符、對位會失分。
> **正式產 golden 請用下方 CLI `--mark-rect`**（原圖裁切，尺度正確）。

**CLI fallback**（SSH server / headless，知道 Mark 座標；**亦為推薦的正式路徑**）：

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

## upstream_tester（上位機對測）

**用途**：在 Grab 桌面扮演**上位機 Controller（client）**，連 Control 的 `UpstreamServer`（8787）送 `CF_` 命令，
跟 Control 對測交握。依據 `L8A CF IP Control Handshake Spec_20240704.xlsx` 與 `20250217_CF_AOI_IP_說明.pptx`
（CF Command Flow）。只用 Python 標準函式庫，Grab 不需另裝套件；介面是本機網頁（同 cam_align）。

### 安裝桌面圖示（Grab，免 sudo）

```bash
bash tools/upstream_tester/install_desktop_icon.sh
```

雙擊「上位機對測（CF_ 協議）」→ 瀏覽器開 `http://127.0.0.1:8790/`。預設目標 `192.168.10.1:8787`（控制網 Control），畫面上可改。

### 功能

- **一鍵流程**：`CF_LOAD_RECIPE → CF_GRAB_START → CF_CHECK_ALIGN → CF_SET_ALIGN → CF_GET_RESULT`，
  `SET_ALIGN` 自動帶入 `CHECK_ALIGN` 回的 Shift；可設步驟間隔、遇錯即停。
- **單一命令**：各命令參數可填（Recipe / PanelID / Time / DetectMode…），另有 `CF_READY`、`CF_STOP`（Control 擴充）與自訂原始字串。
- **規格檢查**：每筆回應檢查欄位數（Result + Param1..9）、OK/Error、失敗時 Param9 Error Msg、
  CHECK_ALIGN Shift 位置、GET_RESULT 檔案數與 DefectCnt 數是否一致；不符標 ⚠（不擋送出）。
- **紀錄檔**：`~/cfaoi_upstream_tester/logs/YYYYMMDD.log`（含每條延遲 ms，可附給對方）。

### Headless / SSH

```bash
python3 tools/upstream_tester/upstream_tester.py --host 192.168.10.1 --flow --recipe R1 --panel P001
python3 tools/upstream_tester/upstream_tester.py --send "CF_READY"
python3 tools/upstream_tester/test_upstream_tester.py      # 工具自測（假 Control，不需網路）
```

### ⚠ 規格 20240704 與 Control 現行實作的差異（對測時會看到 ⚠）

| 項目 | 規格表 | Control `UpstreamServer.cs` |
|------|--------|-----------------------------|
| CF_CHECK_ALIGN 回應 | Param1=ShiftX、Param2=ShiftY | Param1=`Cs_AlignSet`、Param2=ShiftX、Param3=ShiftY |
| CF_SET_ALIGN 請求 | Param1=ShiftX、Param2=ShiftY | 讀 Param2/Param3（Param1 當 result） |
| 失敗 Result 字串 | `OK/Error` | `ERR` |
| CF_GRAB_START | 無參數 | 可帶 Param1=timeoutMs（空=40000） |

SET_ALIGN 在畫面上可切「規格」/「Control 現行」兩種格式送。差異要以真上位機為準，再決定改哪一邊。

---

*未來其他小工具也記在這裡。*
