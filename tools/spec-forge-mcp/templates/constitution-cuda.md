# Constitution — CUDA（不隨任務變的 house rules）

## 註解與語言
- 所有新增/修改的註解一律使用繁體中文。

## Kernel 撰寫
- Launch 參數不得寫死：block size 用既有的常數/工具函式推導，grid size 以 `(N + block - 1) / block` 計算。
- 一律檢查邊界：任何以 thread index 存取記憶體前必須有 `if (idx < n)` 類守門。
- 新 kernel 必須支援非 32 對齊的輸入尺寸。
- 禁止在 kernel 內使用 `printf` 留在正式碼（除錯後移除）。

## 記憶體與 Stream
- 遵循專案既有的 stream 管理方式，不得自行 `cudaStreamCreate` 除非 spec 明示。
- 所有 `cudaMalloc`/`cudaFree` 走專案的記憶體池/RAII 包裝，不得裸呼叫。
- 每個 CUDA API 呼叫必須經過專案的錯誤檢查巨集（如 `CUDA_CHECK`）。

## 效能
- 不得為了效能犧牲正確性；效能改動必須通過 spec 中的 ncu 相對比較驗收。
- 注意目標架構差異：開發驗證機（Blackwell/GB10）與產線（Turing）行為不同，不得使用僅特定架構可用的 intrinsic，除非 spec 明示。

## 禁止事項
- 不得改動 kernel 的公開函式簽名。
- 不得順手重構 spec 範圍外的程式碼。
