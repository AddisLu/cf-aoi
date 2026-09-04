# Constitution — C#（不隨任務變的 house rules）

## 註解與語言
- 所有新增/修改的註解一律使用繁體中文。

## 風格與慣例
- 跟隨檔案既有風格與命名（PascalCase 公開成員、_camelCase 欄位等以現有碼為準）。
- `IDisposable` 資源一律 `using`；事件註冊必須有對應反註冊。
- UI 執行緒與工作執行緒的切換遵循該模組既有模式（Invoke/Dispatcher），不得自創。

## Interop（C# ↔ C++）
- P/Invoke 簽名、struct marshaling 不得改動，除非 spec 明示且兩側同步修改列入範圍。
- 跨邊界的緩衝區生命週期：由既有擁有權慣例決定，不得改變擁有方。

## 禁止事項
- 不得改動公開 API 簽名，除非 spec 明示。
- 不得新增 NuGet 依賴。
- 不得順手重構 spec 範圍外的程式碼。
