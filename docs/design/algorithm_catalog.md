# 演算法方案目錄（2026-10-06）

> Addis：fab **不能連外網** → 所有演算法、模型、相依套件都要在機台本地；演算法要 robust，且**每一段都有多個方案讓軟體選**。
> 選法：`tools/autotune/bench_methods.py` 在乾淨影像上用**平台法**把每個組合校準到「雜訊剛好壓住」，
> 再在**人工植入缺陷**（大小 1–9 px × 對比 15–55% × 暗/亮）上比檢出率、誤判、GPU 時間 → 同樣不誤判下抓最多的勝出。
> 不需要真值、不需要外網；換產品/換光源後重跑即可。

狀態：**IP** = IP 已有（可由配方/ini 選）；**原型** = Python 已做、bench 可評，有效再搬進 IP；**規劃** = 尚未做。

## 1. 前處理

| 方案 | 做什麼 | 狀態 | 適用 |
|---|---|---|---|
| 無 | — | IP | DIV（比值本身抵消照度）|
| **FFC 逐欄平場** | 欄平均 ÷ 同相位鄰欄（±k·pitch）中位數 = 感測器逐欄增益（PRNU）；pitch 寬移動平均 = 暗角/光源。**由調參收集的影像算，不必另拍白板** | 原型 | SUB（灰階差受暗角影響大：T550 CCD 中央/邊緣亮度 2.5×）、PRNU 大的相機 |
| **逐列正規化** | 每張影像每列平均 ÷ 同相位鄰列中位數 → 抵消線掃光源閃爍 / 編碼器抖動造成的整列亮暗 | 原型 | 光源閃爍、整列假缺陷 |
| LSC 鏡頭暗角 | 多項式徑向增益（k1/k2/k3）| IP（`LscEnable`）| 無 FFC 時的近似 |
| Remap 對比拉伸 | 子圖 min/max 線性拉滿 | IP（`Ip_Remap`，SUB 用）| legacy SUB |
| 高斯平滑 3×3 | 降雜訊 | IP（`SmoothTimes2`）| 雜訊大、缺陷 ≥ 2 px |
| 高斯平滑 5×5 | 同上，更強 | 規劃（欄位已有，kernel 待補）| |
| 中值 3×3 | 去椒鹽雜訊、保邊 | 原型 | 單點雜訊多 |
| 補邊（edge_fill）| zone 碰影像邊的邊補「往內平移 ≈3 pitch」的 pattern → 消 kernel 死區 | IP（2026-10-05，預設關）| slice 接縫、CCD 左右 |

## 2. 影像處理（偵測）

| 方案 | 做什麼 | 狀態 | 特性 |
|---|---|---|---|
| **DIV**（mode 0）| center ÷ 8 方向 ±pitch 鄰居平均，比值門檻 | IP（L3）| 快（≈5 ms/張）、照度不變；T550 已校準 0 誤判 |
| **SUB 投票**（mode 1）| 逐路灰階差，PitchTime×8 路中 ≥ ChooseAmount 路超標才算；3×3 SAD 局部搜尋 | IP（L3，= T550 生產配方）| 抗單一鄰居異常（ghost）；受暗角影響 → 配 FFC |
| **DIV 投票**（mode 2）| 比值域逐路投票 + 暗區棄權 | IP | DIV 的抗 ghost 版 |
| 多尺度 2×/4× | 縮小後再偵測、OR 回原圖 | IP（mode 2）| 大顆缺陷（> pitch）全解析度會漏 |
| local search | 鄰居位置 ±1–2 px 找最像的 | IP（`SearchX/Y`）| pitch 非整數（18.41）|
| 對位（golden mark）| 每台 CCD 對位 Mark 吸收起始點差 | IP（`M_AlignRoi`）| 多 CCD 拼接座標 |
| 週期中位數模板 | 每點 = ±1..±2 pitch 共 24 個同相位點的中位數，比對殘差 | 規劃 | 最 robust（中位數抗鄰居缺陷），運算量大 |
| 低頻 mura | 以 pitch 盒濾波消掉 pattern → 與大尺度背景比（DoG）| 規劃 | 點偵測抓不到的大面積淡不均 |
| 線缺陷 | 殘差逐列/逐欄投影 | 規劃 | 整條線（刮傷、短路線）|

## 3. 後處理

| 方案 | 做什麼 | 狀態 |
|---|---|---|
| Blob 大小 / 合併 | `BlobMinSize/MaxSize/AllMergeDistance` | IP |
| 邊界略過 | `bypass_edge` | IP（#32）|
| Rule 改判 | 依特徵改判類別 | IP（#16）|
| 爆量保護 | MaxDefectCountPass、flood skip | IP |
| **AI 分類** | Tensor Core 分類器（模型在本機 `models/`）| IP（**停用**：訓練資料不足；缺陷標「待人工複核」）|
| AI 檢 IOI | dummy 帶、晶片邊、前後緣外圍裁圖給 AI | IOI 裁圖 = IP；AI 模型 = 規劃 |
| 固定位置重複 | 同一台 CCD 同一 x（或同 x,y）跨張/跨片重複 → 判鏡頭/感測器髒污，不算面板缺陷 | 規劃 |
| 補邊帶 size 規則 | 補邊那一圈 size ≥ 2 才報（T550：邊帶假點都是 1 px）| 規劃（待 24 CCD 驗）|
| 人工確認回饋 | DefectSort 真缺陷/誤判 → 每台誤判統計 → 建議放寬 | 設計稿 ③ |

## 4. 離線（fab 無外網）注意

- Python 端（numpy、opencv）與 IP 執行檔都包進**離線更新包**；不在現場 `pip install`。
- 本地模型（Loop 的 DeepSeek-V4-Flash、視覺模型 Qwen3.6）權重**進 fab 前下載完**放 Spark 本機 HF cache；
  vLLM 設 `HF_HUB_OFFLINE=1`，避免啟動時嘗試連線卡住（本次實測：權重缺檔時 vLLM 會卡在載入不報錯）。
- AI 分類模型的訓練在 Spark 本機（GB10）進行；訓練資料 = DefectSort 人工標註 + bench 植入缺陷。
