# CF-AOI 完整建置指南（SETUP.md）

> 🟢 **2026-06-17 現況**：三程式已建置（IP L4 / Grab 單相機 L4 / Control L1–L3）。本指南保留為**新機器 onboarding 參考**
> （env 安裝 / 目錄結構 / build 指令）。原「用 Claude Code 生成程式」的 `PROMPTS.md` 已移除（程式已存在）——
> 各程式現狀改見 `ip/CLAUDE.md`、`grab/CLAUDE.md`、`control/CLAUDE.md` 與 `docs/*_程式完整說明.md`。
> Reference 子目錄正名：`Demo`（原 gpu_algo）/ `PrjCfAoi`（原 legacy_win）/ `cfaoi_phase1`（原 phase1_tests）。
>
> 圖例：`[手動]` 你要做的事 ｜ `[自動]` 執行指令 ｜ `[Claude]` 用 Claude Code ｜ `[驗證]` 確認結果

---

## 最終目錄結構（重要：先看懂這張圖）

```
~/cf-aoi/                              ← 專案根目錄
│
├── cf-aoi.code-workspace             ← VS Code 用這個開啟
├── docs/                             ← 所有說明文件
│   ├── CLAUDE.md                     ← 全域 context（給 Claude Code）
│   ├── STATUS.md                     ← 完成度盤點（L0–L4）+ 權威 Gap 表（#1–#28）
│   ├── SETUP.md                      ← 本文件
│   └── *_程式完整說明.md             ← ip / control / grab / tools 各一份
│
├── ip/                               ← IP 程式（RTX2080/Spark）
│   ├── CLAUDE.md
│   ├── CMakeLists.txt                ← Claude Code 生成
│   ├── config/
│   │   └── default_zone.ini          ← bootstrap 自動複製自 config.ini
│   ├── models/                       ← AI 模型（自動複製）
│   └── src/
│       ├── gpu/cuda_kernels.cu       ← bootstrap 自動複製（不可修改）
│       ├── ai/ai_kernels.cu          ← bootstrap 自動複製（不可修改）
│       └── ...                       ← Claude Code 生成其餘
│
├── grab/                             ← Grab 程式（Step 2 才用）
│   └── CLAUDE.md
│
├── control/                          ← Control 程式（Avalonia）
│   ├── CLAUDE.md
│   └── src/                          ← Claude Code 生成
│
├── shared/                           ← 跨程序共用 header
│   └── FrameHeader.h                 ← Claude Code 生成或從 phase1 複製
│
├── scripts/                          ← 工具腳本
│   ├── bootstrap.sh                  ← 一鍵初始化
│   ├── control_test.py               ← Step 1 測試
│   └── estimate_pitch.py             ← Pitch 估算
│
├── test_images/                      ← 你的 MIL 測試影像放這裡
│
└── Reference/                        ← ★ 唯讀，舊版程式放這裡 ★
    ├── Demo/                     ← 全 GPU 演算法
    │   ├── src/cuda_kernels_fast.cu  ← 必須有這個檔
    │   ├── src/batch_detector.cpp
    │   ├── src/tensor_core_classifier.cu
    │   ├── include/
    │   └── config.ini
    ├── PrjCfAoi/                   ← 舊版 Windows（PrjCfAoi.sln 等）
    └── cfaoi_phase1/                 ← Phase-1 測試套件（Step 2 才需要）
```

**關鍵點**：
- `docs/`、`ip/`、`grab/`、`control/`、`shared/`、`Reference/` 是**平行的 6 個資料夾**
- workspace 把這 6 個資料夾分別當作根目錄顯示，**不會互相巢狀重複**
- 你的舊程式只放在 `Reference/`，新程式碼分別在 `ip/`、`grab/`、`control/`

---

## 你的舊程式要放哪裡（最重要）

| 你的舊資料夾 | 放到這裡 | 必須有的關鍵檔案 |
|------------|---------|---------------|
| 全 GPU 演算法（Demo/）| `~/cf-aoi/Reference/Demo/` | `src/cuda_kernels_fast.cu` |
| 舊版 Windows（PrjCfAoi）| `~/cf-aoi/Reference/PrjCfAoi/` | `PrjCfAoi.sln` |
| Phase-1 測試（Step 2 才需要）| `~/cf-aoi/Reference/cfaoi_phase1/` | `shared/FrameHeader.h` |
| MIL 測試影像 | `~/cf-aoi/test_images/` | `*.tif` |

---

## Phase 0：放置檔案（一次性）

### 0.1 在 Linux PC 建立根目錄

`[自動]` SSH 到 Linux PC（或 VS Code terminal）：
```bash
mkdir -p ~/cf-aoi/{docs,scripts,Reference/Demo,Reference/PrjCfAoi,Reference/cfaoi_phase1,test_images}
```

### 0.2 從 Mac 傳本 repo 的檔案

`[手動]` 把下載的檔案 SCP 到 Linux（在 Mac Terminal，假設檔案在 ~/Downloads）：
```bash
cd ~/Downloads

# 文件
scp cf-aoi.code-workspace addis@<LINUX_IP>:~/cf-aoi/
scp CLAUDE_master.md      addis@<LINUX_IP>:~/cf-aoi/docs/CLAUDE.md
scp SETUP.md              addis@<LINUX_IP>:~/cf-aoi/docs/

# 腳本
scp bootstrap.sh          addis@<LINUX_IP>:~/cf-aoi/scripts/
scp control_test.py       addis@<LINUX_IP>:~/cf-aoi/scripts/
scp estimate_pitch.py     addis@<LINUX_IP>:~/cf-aoi/scripts/

# 各程式 CLAUDE.md（先建好目錄再傳）
ssh addis@<LINUX_IP> "mkdir -p ~/cf-aoi/{ip,grab,control}"
scp CLAUDE_ip.md          addis@<LINUX_IP>:~/cf-aoi/ip/CLAUDE.md
scp CLAUDE_grab.md        addis@<LINUX_IP>:~/cf-aoi/grab/CLAUDE.md
scp CLAUDE_control.md     addis@<LINUX_IP>:~/cf-aoi/control/CLAUDE.md
```

### 0.3 傳你的舊程式

`[手動]` 在 Mac Terminal（路徑換成你的實際路徑）：
```bash
# GPU 演算法（注意結尾的 / 讓內容直接進 Demo，不會多一層）
scp -r /Users/yourulyu/Documents/Demo/*  addis@<LINUX_IP>:~/cf-aoi/Reference/Demo/

# 舊版 Windows 程式
scp -r /你的路徑/PrjCfAoi/*  addis@<LINUX_IP>:~/cf-aoi/Reference/PrjCfAoi/

# MIL 測試影像
scp /你的路徑/*.tif  addis@<LINUX_IP>:~/cf-aoi/test_images/
```

> 💡 也可以用 VS Code：左側 Reference/Demo 右鍵 → Upload，直接拖檔案。

### 0.4 執行 bootstrap（自動建結構 + 複製 kernel）

`[自動]` Linux PC 上：
```bash
cd ~/cf-aoi
bash scripts/bootstrap.sh
```

`[驗證]`：
```bash
ls ip/src/gpu/cuda_kernels.cu       # 應存在
ls ip/config/default_zone.ini        # 應存在
```

---

## Phase 1：Step 1 — 演算法離線驗證

### 1.1 環境套件（若尚未安裝）

`[自動]`：
```bash
sudo apt install -y libopencv-dev nlohmann-json3-dev libfmt-dev
```

### 1.2 安裝 Claude Code（若尚未安裝）

`[自動]`：
```bash
curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
sudo apt install -y nodejs
mkdir -p ~/.npm-global
npm config set prefix '~/.npm-global'
echo 'export PATH=~/.npm-global/bin:$PATH' >> ~/.bashrc
source ~/.bashrc
npm install -g @anthropic-ai/claude-code
claude --version
```

或用 VS Code 的 Claude Code 擴充套件（更方便，免裝 CLI）。

### 1.3 用 Claude Code 生成 IP 程式

`[Claude]` IP 程式**已建置**（見 `ip/CLAUDE.md` + `docs/ip_程式完整說明.md`）。若需在新機器從頭重建，參考該兩份文件的遷移對照表（`Reference/Demo/` → `ip/src/`）。

### 1.4 Build IP

`[自動]`：
```bash
cd ~/cf-aoi/ip
cmake -S . -B build -DCMAKE_CUDA_ARCHITECTURES=75 -DCMAKE_BUILD_TYPE=Release
cmake --build build -j$(nproc)
```

`[驗證]`：
```bash
./build/cfaoi_ip --help        # 顯示用法
```

### 1.5 估算 PitchX/PitchY

`[手動]`（重要，影響檢測正確性）：
```bash
pip3 install Pillow numpy
python3 ~/cf-aoi/scripts/estimate_pitch.py ~/cf-aoi/test_images/你的影像.tif
# 記下輸出的 PitchX / PitchY
```

### 1.6 端到端測試

`[自動]` Terminal 1 啟動 IP：
```bash
cd ~/cf-aoi/ip
./build/cfaoi_ip --mode offline-tcp --control-port 8200
```

`[自動]` Terminal 2 送影像（先用 --help 確認 control_test.py 參數）：
```bash
python3 ~/cf-aoi/scripts/control_test.py --help
python3 ~/cf-aoi/scripts/control_test.py \
    --ip 127.0.0.1 --image ~/cf-aoi/test_images/你的影像.tif
```

`[驗證]`：應顯示缺陷清單 + 處理時間（RTX 2080 約 5-6ms）。

---

## Phase 2：Step 2-3（Grab + RDMA，之後）

### 2.1 放 Phase-1 測試套件
```bash
scp -r /你的路徑/cfaoi_phase1/*  addis@<LINUX_IP>:~/cf-aoi/Reference/cfaoi_phase1/
```

### 2.2 安裝 pylon / eBUS SDK
`[手動]` 從 Basler / Pleora 官網下載安裝。

### 2.3 用 Claude Code 生成 Grab
`[Claude]` Grab 程式**已建置**（單相機 pylon 路徑 L4）。見 `grab/CLAUDE.md` + `docs/grab_程式完整說明.md`。

---

## Phase 3：Control（Avalonia UI）

### 3.1 安裝 Avalonia
`[自動]`：
```bash
sudo apt install -y dotnet-sdk-8.0 libx11-dev libxcursor-dev libxi-dev \
    libxrandr-dev libfontconfig1 libgl1-mesa-dev
dotnet new install Avalonia.Templates
```

### 3.2 用 Claude Code 生成 Control
`[Claude]` Control 程式**已建置**（Avalonia .NET 8）。見 `control/CLAUDE.md` + `docs/control_程式完整說明.md`。

---

## 常用指令

### 一鍵啟動（推薦；自動 build + 帶預設參數）

```bash
# 通用：scripts/run.sh <control|ip|grab|golden|sim|selftest>
./scripts/run.sh ip            # IP 機(Linux)：build + offline-tcp:8200
./scripts/run.sh control       # Mac/Linux：跑 Control（自動監聽上位機 8787）
./scripts/run.sh sim           # 上位機 CF_ 模擬器（需先開 Control）
./scripts/run.sh selftest upstream   # 自測：upstream|store|topology|singleccd|camera

# 免命令列：雙擊 scripts/launchers/ 內的檔（見該夾 README）
#   Control-Mac.command / Control-Windows.bat / IP-Linux.sh / UpstreamSim-Mac.command …
# 免裝 .NET 單檔 App（給純操作人員）：
./scripts/build-control-app.sh        # → control/publish/<rid>/CfAoiControl(.exe) 雙擊即可
```

### 手動指令（等價）

```bash
# 啟動 IP（Step 1）
cd ~/cf-aoi/ip && ./build/cfaoi_ip --mode offline-tcp --control-port 8200
# 重建 IP
cd ~/cf-aoi/ip && cmake --build build -j$(nproc)
# Control
cd ~/cf-aoi/control/src && dotnet run
# 上位機端到端（L3）：先開 Control，再
python3 ~/cf-aoi/scripts/upstream_simulator.py --host 127.0.0.1 --port 8787
```
