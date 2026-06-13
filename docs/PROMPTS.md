# Claude Code Prompt 集（PROMPTS.md）

> 把對應的 prompt 整段複製，貼到 VS Code 的 Claude Code 面板。
> Claude Code 已能讀取整個 workspace（含 Reference/），不需要 --add-dir。

---

## Prompt 1：IP 程式（Step 1 — offline 演算法驗證）

```
你正在開發 CF-AOI 的 IP 程式。請先閱讀 ip/CLAUDE.md 和根目錄 docs/CLAUDE.md 了解架構。

任務：以 Reference/gpu_algo/src/batch_detector.cpp 的 GPUDetectionEngine 為基礎，
建立 IP 程式。核心原則：不修改任何 CUDA kernel 邏輯（ip/src/gpu/cuda_kernels.cu
和 ip/src/ai/ai_kernels.cu 已由 bootstrap 複製好，視為唯讀）。

請建立以下檔案：

1. shared/FrameHeader.h（若不存在）
   - 256-byte packed struct，static_assert 驗證
   - 欄位見 docs/CLAUDE.md 的 FrameHeader 定義
   - magic 使用合法 hex：FRAME_MAGIC = 0xCFA0A001（不要用 0xCFAOI001，O和I非hex）
   - padding 自動算出讓 sizeof==256

2. ip/src/config/zone_config_adapter.h/.cpp
   - struct ZoneConfig（演算法參數：BTH/DTH/pitch_x/pitch_y/...）
   - ZoneConfig from_recipe_xml(...) 和 ZoneConfig from_ini(const std::string& path)
   - 讀取 ip/config/default_zone.ini 作為預設值

3. ip/src/image_source/image_source.h
   - IImageSource 介面：virtual bool next_frame(FrameHeader&, std::vector<uint8_t>&) = 0

4. ip/src/image_source/tcp_source.h/.cpp
   - 監聽 TCP，接收 Control 傳來的影像（JSON header + binary）

5. ip/src/image_source/file_source.h/.cpp
   - 讀取 tif/bmp/png，務必用 cv::IMREAD_UNCHANGED（不做任何轉換）

6. ip/src/gpu/gpu_pipeline.h/.cpp
   - 保留 GPUDetectionEngine 所有 GPU 邏輯
   - 公開：DetectionResult process_frame(const uint8_t* img, int w, int h, const ZoneConfig& cfg)
   - 移除 FileReceiver/SocketReceiver/Rivermax 依賴
   - GPU 記憶體：偵測 cudaDeviceProp.integrated 決定 zero-copy（ARM）或 cudaMalloc（x86）

7. ip/src/result_saver.h/.cpp
   - 輸出 ResultInfo.json + 缺陷 patch PNG + result overlay BMP

8. ip/src/control_server.h/.cpp
   - TCP JSON server。命令：LOAD_RECIPE, GET_STATUS, CHECK_HEALTH,
     SEND_IMAGE_STREAM_BEGIN, SEND_IMAGE_FOR_REVIEW

9. ip/src/main.cpp
   - 解析 --mode offline-tcp|offline-file，--control-port，--input
   - 根據 mode 建立對應 IImageSource，跑 GpuPipeline

10. ip/CMakeLists.txt
    - LANGUAGES CXX CUDA，CMAKE_CUDA_ARCHITECTURES 預設 native
    - find OpenCV, CUDAToolkit, fmt, nlohmann_json
    - ONNX Runtime 在 /opt/onnxruntime（可選）

完成後告訴我 build 指令。
```

---

## Prompt 2：Grab 程式（Step 2 — 相機取像 + RDMA）

```
你正在開發 CF-AOI 的 Grab 程式。請先讀 grab/CLAUDE.md。

任務：以 Reference/phase1_tests/ 的測試工具為基礎，升級為生產等級。

1. shared/FrameHeader.h 已存在，直接 include
2. grab/src/cam_pylon.cpp — 升級自 phase1_tests/src/t31_pylon_grab/，
   多相機陣列 + MAC Persistent IP + --cam-count 控制
3. grab/src/cam_ebus.cpp — 升級自 t31_ebus_grab/（禁止 include pylon 標頭）
4. grab/src/cam_manager.cpp — 統一管理 pylon/eBUS
5. grab/src/rdma_sender.cpp — 升級自 t40_e2e_client/
6. grab/src/frame_assembler.cpp — FrameHeader 組裝 + CRC32
7. grab/src/control_client.cpp — TCP client 連 Control
8. grab/src/main.cpp — 解析 --cam-count / --cam-ids / --rdma-dest
9. grab/CMakeLists.txt — 偵測 pylon/eBUS 選擇性編譯

不變式：cam_pylon.cpp 和 cam_ebus.cpp 禁止互相 include。
```

---

## Prompt 3：Control 程式（Avalonia UI）

先執行業務邏輯，再執行 UI（分兩段貼）。

### Prompt 3A — 業務邏輯
```
你正在開發 CF-AOI 的 Control 程式（Avalonia .NET 8）。請先讀 control/CLAUDE.md。

先在 control/src 建立 Avalonia 專案（若無）：
  dotnet new avalonia.app -n CfAoiControl --output .
  dotnet add package CommunityToolkit.Mvvm
  dotnet add package Avalonia.Themes.Fluent
  dotnet add package OpenCvSharp4.runtime.linux
  dotnet add package Microsoft.Extensions.Configuration.Json

然後建立業務邏輯層（參考 Reference/legacy_win/）：
1. Controllers/UpstreamServer.cs — 遷移 MainProc.cs 的 TCP server，移除 MIL/Camera
2. Controllers/IpClient.cs — TCP → IP port 8200，含 SendImageForReview
3. Controllers/ConnectionManager.cs — 連線狀態 + 自動重連
4. Models/ZoneSettingModel.cs — 54 參數，[ObservableProperty]
5. Models/RecipeModel.cs — XML 序列化（相容舊格式）
6. Models/DefectResultModel.cs — JSON 反序列化
7. Services/RecipeService.cs — EnsureRecipeExistsAsync（找不到自動生成）
8. Services/OfflineReviewService.cs — 送影像給 IP

不變式：上位機命令名稱（LoadRecipe/GrabStart/GetResult）不可改。
```

### Prompt 3B — Avalonia UI
```
繼續 Control 程式，建立 Avalonia UI（MVVM）：
1. Views/MainWindow.axaml — 左側步驟選擇器 + 連線狀態燈，右側 ContentControl
2. Views/Step1View.axaml — 影像選擇 + BTH/DTH/PitchX/PitchY 調整 + 缺陷清單 DataGrid + patch 影像
3. ViewModels/Step1ViewModel.cs — RunAnalysisCommand 呼叫 OfflineReviewService
4. Views/SystemSettingsView.axaml — IP 位址 + 測試連線 + 路徑設定
5. Program.cs — single-instance + 無 appsettings.json 時跑 FirstRunWizard

完成後告訴我 dotnet run 指令。
```
