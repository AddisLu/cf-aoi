# CF-AOI 完整建置指南（SETUP.md）

> 圖例：`[手動]` 你要做的事 ｜ `[自動]` 執行指令 ｜ `[Claude]` 用 Claude Code ｜ `[驗證]` 確認結果

---

## 最終目錄結構（重要：先看懂這張圖）

```
~/cf-aoi/                              ← 專案根目錄
│
├── cf-aoi.code-workspace             ← VS Code 用這個開啟
├── docs/                             ← 所有說明文件
│   ├── CLAUDE.md                     ← 全域 context（給 Claude Code）
│   ├── SETUP.md                      ← 本文件
│   └── PROMPTS.md                    ← Claude Code 用的 prompt 集
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
    ├── gpu_algo/                     ← 全 GPU 演算法
    │   ├── src/cuda_kernels_fast.cu  ← 必須有這個檔
    │   ├── src/batch_detector.cpp
    │   ├── src/tensor_core_classifier.cu
    │   ├── include/
    │   └── config.ini
    ├── legacy_win/                   ← 舊版 Windows（PrjCfAoi.sln 等）
    └── phase1_tests/                 ← Phase-1 測試套件（Step 2 才需要）
```

**關鍵點**：
- `docs/`、`ip/`、`grab/`、`control/`、`shared/`、`Reference/` 是**平行的 6 個資料夾**
- workspace 把這 6 個資料夾分別當作根目錄顯示，**不會互相巢狀重複**
- 你的舊程式只放在 `Reference/`，新程式碼分別在 `ip/`、`grab/`、`control/`

---

## 你的舊程式要放哪裡（最重要）

| 你的舊資料夾 | 放到這裡 | 必須有的關鍵檔案 |
|------------|---------|---------------|
| 全 GPU 演算法（Demo/）| `~/cf-aoi/Reference/gpu_algo/` | `src/cuda_kernels_fast.cu` |
| 舊版 Windows（PrjCfAoi）| `~/cf-aoi/Reference/legacy_win/` | `PrjCfAoi.sln` |
| Phase-1 測試（Step 2 才需要）| `~/cf-aoi/Reference/phase1_tests/` | `shared/FrameHeader.h` |
| MIL 測試影像 | `~/cf-aoi/test_images/` | `*.tif` |

---

## Phase 0：放置檔案（一次性）

### 0.1 在 Linux PC 建立根目錄

`[自動]` SSH 到 Linux PC（或 VS Code terminal）：
```bash
mkdir -p ~/cf-aoi/{docs,scripts,Reference/gpu_algo,Reference/legacy_win,Reference/phase1_tests,test_images}
```

### 0.2 從 Mac 傳本 repo 的檔案

`[手動]` 把下載的檔案 SCP 到 Linux（在 Mac Terminal，假設檔案在 ~/Downloads）：
```bash
cd ~/Downloads

# 文件
scp cf-aoi.code-workspace addis@<LINUX_IP>:~/cf-aoi/
scp CLAUDE_master.md      addis@<LINUX_IP>:~/cf-aoi/docs/CLAUDE.md
scp SETUP.md              addis@<LINUX_IP>:~/cf-aoi/docs/
scp PROMPTS.md            addis@<LINUX_IP>:~/cf-aoi/docs/

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
# GPU 演算法（注意結尾的 / 讓內容直接進 gpu_algo，不會多一層）
scp -r /Users/yourulyu/Documents/Demo/*  addis@<LINUX_IP>:~/cf-aoi/Reference/gpu_algo/

# 舊版 Windows 程式
scp -r /你的路徑/PrjCfAoi/*  addis@<LINUX_IP>:~/cf-aoi/Reference/legacy_win/

# MIL 測試影像
scp /你的路徑/*.tif  addis@<LINUX_IP>:~/cf-aoi/test_images/
```

> 💡 也可以用 VS Code：左側 Reference/gpu_algo 右鍵 → Upload，直接拖檔案。

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

`[Claude]` 開啟 VS Code 的 Claude Code 面板，貼上 `docs/PROMPTS.md` 的 **「Prompt 1: IP 程式」**。

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
scp -r /你的路徑/phase1_tests/*  addis@<LINUX_IP>:~/cf-aoi/Reference/phase1_tests/
```

### 2.2 安裝 pylon / eBUS SDK
`[手動]` 從 Basler / Pleora 官網下載安裝。

### 2.3 用 Claude Code 生成 Grab
`[Claude]` 貼上 PROMPTS.md 的「Prompt 2: Grab 程式」。

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
`[Claude]` 貼上 PROMPTS.md 的「Prompt 3: Control 程式」。

---

## 常用指令

```bash
# 啟動 IP（Step 1）
cd ~/cf-aoi/ip && ./build/cfaoi_ip --mode offline-tcp --control-port 8200

# 測試
python3 ~/cf-aoi/scripts/control_test.py --ip 127.0.0.1 --image test_images/x.tif

# 重建 IP
cd ~/cf-aoi/ip && cmake --build build -j$(nproc)

# Control
cd ~/cf-aoi/control/src && dotnet run
```
