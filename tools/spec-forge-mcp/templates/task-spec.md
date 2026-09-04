# TASK-<id>: <一句話目標>
domain: <cuda | cpp | csharp | python | docs>
model_hint: <nemotron | gpt-oss | qwen-coder>
max_attempts: 3

## 目標（Why，2~3 句）
<這個改動存在的目的。執行模型不知道 why 時，會在邊界情況做錯決定。>

## 改動範圍（White-list，用 codebase graph 分析後釘死）
- 修改：<檔案路徑> 的 `<函式/類別>`
- 可讀不可改：<相關標頭/介面檔>
- 禁止：不得動白名單以外的任何檔案；不得改函式簽名；不得重構無關程式碼；不得新增外部依賴

## 需求（EARS 格式，每條需求對應至少一個驗收命令）
- R1: WHEN <條件> THE <系統/模組> SHALL <可驗證的行為>
- R2: WHEN <條件> THE <系統/模組> SHALL <可驗證的行為>

## 實作提示（可選）
<已知陷阱、建議做法、相關歷史 commit、參考的既有寫法>

## 驗收命令（全過才算完成；失敗時讀輸出、修正後重試）
1. 編譯: <build 命令>
2. 正確性: <測試命令>            # 對應 R1
3. 效能: <ncu / benchmark 命令>   # 對應 R2（CUDA 任務必填）
4. 檢出率: <golden set 評估命令>  # 影像管線任務必填

## 完成定義
- 驗收命令全數通過 → commit 到 branch task/<id>，commit 訊息需引用本 spec 路徑
- 達到 max_attempts 仍失敗 → 不 commit，回報最後一次完整錯誤輸出與你的診斷
