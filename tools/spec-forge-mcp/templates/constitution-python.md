# Constitution — Python（不隨任務變的 house rules）

## 註解與語言
- 所有新增/修改的註解與 docstring 一律使用繁體中文。

## 風格與慣例
- 跟隨檔案既有風格；新函式必須有 type hints。
- 路徑操作用 `pathlib`；子行程用 `subprocess.run(..., check=True)`。
- 腳本必須可重複執行（idempotent）：重跑不得產生重複資料或錯誤。

## 測試
- 修改既有函式時，若該函式已有測試，必須維持通過；spec 要求時補測試。

## 禁止事項
- 不得新增 pip 依賴，除非 spec 明示。
- 不得改動既有 CLI 參數介面。
- 不得順手重構 spec 範圍外的程式碼。
