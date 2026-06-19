# 一鍵啟動（雙擊，免命令列）

每個軟體都有對應的雙擊啟動檔，依**該軟體實際跑的機器/OS**選用。
全部只是呼叫 `scripts/run.sh <target>`（自動 build + 帶預設參數），改 code 後雙擊會自動重編。

| 軟體 | 跑在哪 | 雙擊這個 |
|------|--------|----------|
| **Control**（操作 UI） | Mac | `Control-Mac.command` |
| Control | Windows | `Control-Windows.bat` |
| Control | Linux 桌面 | `Control-Linux.sh`（檔案管理員雙擊 → 在終端機執行）|
| **IP**（offline-tcp:8200） | Linux（RTX/Spark）| `IP-Linux.sh` |
| **Grab**（需相機+RDMA） | Linux（damac）| `Grab-Linux.sh`（透傳參數，見內註）|
| **golden_maker**（對位 Mark）| Linux（需一張影像）| `golden_maker-Linux.sh` |
| 上位機 CF_ 模擬器（先開 Control）| Mac | `UpstreamSim-Mac.command` |
| 上位機模擬器 | Windows | `UpstreamSim-Windows.bat` |

## 首次使用注意
- **Mac**：第一次雙擊 `.command` 若被 Gatekeeper 擋 → 在 Finder 對該檔「右鍵 → 打開」一次即可（之後正常雙擊）。
- **Windows**：需先裝 .NET 8 SDK（Control 用）；雙擊 `.bat` 即可。
- **Linux**：檔案管理員雙擊 `.sh` 選「在終端機中執行」；或終端機 `./scripts/launchers/IP-Linux.sh`。
- **IP/Grab 機**多為 SSH 無桌面 → 直接終端機跑 `./scripts/run.sh ip` 即可（雙擊適用於有桌面的機器）。

## 真正免裝環境的單檔 App（給純操作人員）
`Control-Windows.bat` 等仍需該機裝 .NET。若要產出**免裝 .NET 的單一執行檔**（操作機什麼都不用裝），
用 `dotnet publish` 自含發佈（見 `scripts/build-control-app.sh`，如已建立）。
