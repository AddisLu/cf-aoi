# 光學調機工具（cam_align）+ iPORT CL-GigE / Basler L803k 取像環境

> 2026-09-18 起收進 cf-aoi repo（原 `~/Addis/iport`）。桌面捷徑 `cam-align.desktop`
> 指向 `tools/cam_align/cam_align.py`。測試影像、`aravis-local/` 建置產物留在舊目錄，不版控。

## 兩種相機共用同一條路徑（2026-09-18）

上段 37 隻 **Basler raL8192-12gm**（原生 GigE）與下段 18 隻 **Basler L803K**
（Camera Link，經 iPORT CL-GigE 轉乙太網、曝光/增益/行率走 CL 序列埠）共用本工具：
參數一律從相機自己的 GenICam XML 解出，不寫死機型。

raL8192 首次接入時修掉的三個坑（都只在 iPORT 上測過才沒發現）：

| 症狀 | 真因 | 修法 |
|---|---|---|
| 參數載入失敗 `Corrupt extra field` | Basler XML zip 夾帶非標準 extra field（id 0x4347 "GCV0"），Python `zipfile` 嚴格解析直接拒絕 | `genicam_client._unzip_first`：失敗時退回手動讀 local header + raw deflate |
| 取像逾時（影像其實收到了） | GVSP **標準 ID** 的 trailer 整包僅 16 bytes，舊碼 `len(pkt) < 20` 一律丟棄 → 永遠等不到結束封包。iPORT 走擴充 ID（≥28 bytes）才沒踩到 | `cam_align.parse_gvsp`：表頭長度依 ID 型式判斷 |
| 沒有增益、曝光顯示 700（無單位） | raL8192 的 XML 用**公式節點**：GainRaw 位址由 `GainSelector` 經 IntSwissKnife 算出（pAddress）、ExposureTimeAbs(µs) 由 Converter 換算。舊碼直接跳過公式節點 | `genicam_client`：GenICam 公式求值（遞迴下降 → 受限 Python 運算式）、pAddress 動態位址、Converter 雙向換算、pIsAvailable、Sign |

實機（2026-09-18 damac，4 台 raL8192 經 HPE 5945）：
`增益 256（256–2047）／曝光 70.0µs（2–10000）／黑位準 50（−2048–2047）／行數 2500（1–3573）`，
每台連續取像掉包 0，行數可即時調整。**L803K 路徑當日無硬體可驗**（iPORT 接 `enp0s31f6`，未接線）。

## 測試

```bash
python3 tools/cam_align/test_offline.py     # 34 項，不需相機
python3 tools/cam_align/cam_align.py --test # 需相機在線
```

離線測試涵蓋 zip、GVSP 封包解析、公式求值（含拒絕危險輸入）、以假 XML+假暫存器驗
動態位址與單位換算。反向對照：把 `len(pkt) < 20` 放回去 → 3 項 FAIL。

## 已知限制

- `MAX_CAMS = 6`（顏色也只有 6 組）：37+18 台的陣列需要分區/篩選機制，尚未做。
- L803K 的 Gain/曝光/行率走 CL 序列埠（`l800_serial.py`），與 GenICam 參數在 UI 上外觀相同。

---

# 原始環境筆記（iPORT CL-GigE + Basler L803k）

主機：Ubuntu 22.04.5 LTS，x86_64，kernel 6.8.0-136-generic，Secure Boot 已關閉。
相機網口：`enp0s31f6` = 192.168.5.2/24，MTU 9000。

延續 6 月的 CF-AOI 專案（`~/下載/Grab`），該專案已驗證 raL8192-12gm 走 pylon 的路徑，
本次處理 L803K 走 iPORT CL-GigE + eBUS 的路徑。

## 裝置現況

```
Model    iPORT CL-GigE-PT01-CL0IP01-128x
Firmware 1.03.03.109
MAC      00:11:1c:06:00:2b
IP       192.168.5.10 / 255.255.255.0   (持久設定，重開機保留)
```

原本裝置的 IP 組態是 DHCP+LLA（暫存器 `0x0014 = 0x06`），網段上沒有 DHCP server
所以 fallback 到 `169.254.123.147`，與主機的 192.168.5.0/24 不同段。
已用 GVCP FORCEIP 切到 192.168.5.10，並寫入持久暫存器：

| 暫存器 | 值 |
|---|---|
| `0x0014` Net IF Config | `0x05` = PersistentIP + LLA（DHCP 關閉） |
| `0x064C` Persistent IP | 192.168.5.10 |
| `0x065C` Persistent Mask | 255.255.255.0 |
| `0x066C` Persistent Gateway | 0.0.0.0 |

若要還原成出廠的 DHCP+LLA：`0x0014` 寫回 `0x06`。

## 相容性

| 項目 | iPORT CL-GigE EFG | Basler L803k | 判定 |
|---|---|---|---|
| CL 組態 | Base only | Base | OK |
| Tap 數 | 1 或 2 tap | 2 tap | OK |
| Pixel clock | 20–85 MHz | 60 MHz | OK |
| Pixel depth | 8/10/12/14/16 bit | 使用 8 bit | OK |
| 掃描模式 | line scan + area scan | line scan | OK |
| CL 接頭 | **SDR-26 (Mini CL)** | **MDR-26** | 需 MDR-26 ↔ SDR-26 線 |

規格來源：[iPORT CL-GigE datasheet](https://www.pleora.com/wp-content/uploads/2026/01/Pleora_DS_iPORT_CL-GigE_EFG.pdf)、
[Basler L800 系列手冊](https://assets-ctf.baslerweb.com/dg51pdwahxgw/7kE7EHGCzfwXsUjP5W39vn/78b2886c734963ee4fdfabe24c4c86bb/Manual_L800_all_combined.pdf)。

## 頻寬（8 bit）

L803k 全速：8160 px × 1 byte × 14.1 kHz ≈ **115 MB/s**

Jumbo frame 已用 GigE Vision test packet 實測（`gvsp_testpacket.py`）：
裝置實際送得出 **9000 bytes** 封包，效率 99.2%，1GigE 可用頻寬約 **124 MB/s**。
（`GevSCPSPacketSize` 上限 9000，寫 16384 會被拒為 `INVALID_PARAMETER`。）

→ 115 / 124 ≈ 93%，可行但沒什麼餘裕。務必把 `GevSCPSPacketSize` 設到 9000
（裝置預設只有 1476）。若 drop 率不理想就降 line rate 或縮 AOI。

參考點：6 月 raL8192-12gm 在同一張網卡實測 95 MB/s 零掉幀。

> 注意：`ping -M do` 測到的路徑上限只有 1470 bytes payload（IP MTU 1498），
> 這是 iPORT 韌體 ICMP 回應器的限制，**不是**串流上限。別被這個數字誤導。

## 工具

| 檔案 | 用途 | 需要 root |
|---|---|---|
| `gvcp_discover.py` | 列舉 GigE Vision 裝置（含跨網段） | 否 |
| `gvcp_setip.py` | FORCEIP + 持久 IP 設定 | 否 |
| `gvsp_pktsize_probe.py` | 探測 `GevSCPSPacketSize` 上限 | 否 |
| `gvsp_testpacket.py` | 用 test packet 實測 jumbo 能否送達 | 否 |
| `gvcp_probe_raw.py` | 診斷用，dump 所有收到的封包 | 否 |
| `diagnose_link.sh` | tcpdump 抓包 + 暫加 link-local 重掃 | 是 |
| `install_ebus.sh` | 安裝 eBUS SDK 與相依套件 | 是 |

以上 Python 工具都不需要 eBUS SDK。關鍵實作細節：socket 要 bind `0.0.0.0`
（裝置的 DISCOVERY_ACK 是廣播回來的），並用 `sendmsg` + `IP_PKTINFO`
指定出口網卡（否則廣播只走 default route）。

## 安裝 eBUS SDK

需自行從 Pleora Support Center 下載（**需登入帳號**）：
<https://supportcenter.pleora.com/s/topic/0TO340000004X6dGAE/ebus-sdk> → Downloads

本機需要的檔名（依 SDK 隨附的《eBUS Linux 快速上手手册》命名規則）：

```
eBUS_SDK_Ubuntu-22.04-x86_64-7.0.1-7536.deb
```

安裝後路徑為 `/opt/pleora/ebus_sdk/Ubuntu-22.04-x86_64/`，
內含 `bin/eBUSPlayer`、`lib/eBUSPython/`（Python 綁定）、`module/`（filter driver）。

5.1.x 已於 2026-03 EOL 且只支援到 Ubuntu 16.04，不要用。

> `~/下載/【友思特】eBUS SDK 7.0.1.rar` **不含 x86_64 版**，裡面只有
> Windows `.exe` 與兩個 Jetson **aarch64 ARM** `.deb`（實測 ELF 為
> `ARM aarch64`，本機是 x86_64 跑不了）。
> 那兩個 Jetson 版倒是可以用在 DGX Spark（GB10 是 aarch64）。

```bash
sudo ./install_ebus.sh ~/下載/eBUS_SDK_Ubuntu-22.04-x86_64-7.0.x.deb
source /etc/profile.d/ebus.sh
cd ~/下載/Grab && cmake -S . -B build && cmake --build build -j
./build/t30_ebus_probe
./build/t31_ebus_grab auto 500 frame_l8.raw
```

`~/下載/Grab` 的 CMakeLists.txt 已用 `PUREGEV_ROOT` 條件編譯 `t30_ebus_probe` /
`t31_ebus_grab`，SDK 裝好就會自動編出來。

## L803K Gain/曝光/行率 經 CL 序列埠直達（l800_serial.py，2026-09-01）

依 gain 隨產品/光源衰退需修正的需求，把 Basler L800 二進位序列協定
實作在 iPORT Bulk0 之上 —— L803K 的相機本體參數從此出現在調機介面，
**取像中也能即時調**（實測串流零中斷）。不再依賴 Windows CCT+。

路徑：主機 →GVCP→ iPORT Bulk0 UART(9600 8N1) →RS-644→ L803K

| 參數 | CSR | 介面呈現 |
|---|---|---|
| Gain | 0x0E00+1 (f32 dB) | 增益 Gain，-3.01~20.00 dB |
| 曝光 | 0x1500+1 (f32 µs) | 曝光 Exposure |
| 行週期 | 0x1600+1 (f32 µs) | 行率 LineRate（Hz，自動換算） |

實測（兩台皆過）：迴環自檢 OK；`.53` 讀出行週期 200µs=5000Hz —— 兩台行率
不一致的謎底；行率 5000→13699 Hz 切換與還原成功（83ms/千行取像驗證）。

踩過的坑（都已寫進程式註解）：
- GVCP WRITEMEM 長度要 4 對齊 → 序列 TX 補 0x00（L800 frame parser 忽略）
- iPORT `Bulk0SoftReset` 是**準位**不是脈衝，寫 1 要再寫 0，否則 UART 卡死
- upstream FIFO watermark 預設 256 bytes → 短回應永遠不觸發事件，設 1
- **CCP 釋放時韌體清掉 MCP（message channel）** → 每次重取控制權後要 rearm
- 行週期 Absolute Min 是動態的（受曝光牽制）→ 提高行率要先收曝光再寫週期，
  不能用當下的 Min 預先 clamp
- 事件 payload：`reserved(2) id(2) ffff(2) 0000(2) ts(8) len(4) data[len]`，
  event_id=0x9001，需回 ACK 否則裝置重送 3 次

單機測試：`python3 l800_serial.py 192.168.4.54`（含迴環自檢與寫入驗證）

## 光學調機工具 v2（cam_align.py + genicam_client.py，2026-09-01）

**任何 GigE Vision 相機**（raL8192-12gm / L803K 經 iPORT / OPT...）最多 6 台。
桌面圖示「L803K 光學調機」雙擊即開（127.0.0.1:8765 + 自動開瀏覽器）。
介面設計預覽（模擬資料，給光學團隊審）：
<https://claude.ai/code/artifact/eb795f25-8730-4a1c-a22e-edb52ef41d58>

### 通用相機層 genicam_client.py

- 用 GVCP READMEM 從相機下載它自己的 GenICam XML（支援 zip、磁碟快取
  `~/.cache/cam_align/`；READMEM 長度必須 4 對齊 —— 尾段要 pad）
- 解析 Integer/Float/Enum/Command/Boolean → IntReg/MaskedIntReg/FloatReg，
  沿 pValue 鏈追到實體位址；**不支援 Converter/SwissKnife 公式節點**，
  所以 Basler 的曝光會落在 ExposureTimeRaw（整數）而非 Abs（浮點）—— 調機夠用
- 調機面板參數（依機型自動出現）：Gain / 曝光 / 行率 / 黑位準 / 每張行數 / 測試圖樣
- 串流控制（AcquisitionMode/Start/Stop、TLParamsLocked、PixelFormat）
  也全部由 XML 解出 —— 不再寫死 iPORT 位址

### 工具本體

- 掃描（跨網卡、自動配對同網段來源 IP）、單台/全部開始停止、狀態膠囊
- SingleFrame 快照輪詢（自動退 MultiFrame+count1）；jumbo 逾時自動降 1500
- 行剖面疊圖 + 參考列 + 1:1 放大 + 快照存 `~/CamAlign_<時戳>/`
- **每台獨立顯示方向**（旋轉 0/90/180/270 + 鏡像，依 MAC 存
  `~/.config/cam_align/orient.json`）：只影響顯示；參考線與 1:1 點擊座標
  跟著轉，剖面永遠沿感測器線取樣（調直線度的基準不受顯示方向影響）
- 設定視窗：−/＋ 步進 + 套用，「同時套用到全部相機」勾選；
  **取像中也能即時改參數**（設定與 worker 共用同一 GVCP socket = 同一控制身分）
- iPORT 機型會註記：Gain/曝光在相機本體（CL 序列埠，用 eBUS Player 調）
- 停止時還原動過的所有暫存器（Mode/PixelFormat/TLParamsLocked/SCP*）
- 關閉瀏覽器分頁 → 伺服器自動停相機並結束（pagehide beacon 3 秒；
  背景分頁節流備援 90 秒無請求）；桌面圖示重開即全新啟動

驗證：`python3 cam_align.py --test` —— 兩台 iPORT 實測取像 204/78 ms/張零掉包、
XML 解析 0.4s/台、執行中 set Height=500 下一張立即 8160×500、全部還原正確。
Basler/OPT 原生相機屬同一標準路徑，8/10 到貨後跑同一測試即可。

## 故障排除記錄（2026-09-02：同事 eBUS 會談後無法取像）

症狀：Windows/Linux eBUS Player 與本工具皆取不到像。三個獨立原因疊加：

1. **GVSP 警告狀態 0x4008**：某次 eBUS 會談後裝置的 trailer（部分 payload 也是）
   帶 0x4xxx 警告類狀態碼；本工具原本非零狀態一律丟棄 → trailer 全滅 → 組不出幀。
   已修：全部接收器接受 `(st & 0xC000) == 0x4000`。
2. **持久設定被改**：救援指令 `0x1A008=1` 載出廠預設（連 CL 前端都洗掉，
   只剩 640x480 測試圖）→ 再 `0x1A010=1` 載廠商 UserSet1（L803K 雙 tap，Width=8160）。
3. **CL 線重插拔後接觸不良**：序列腳位通（相機參數可讀）但資料腳位劣化 —
   `.54` 每張隨機掉 ~25% 行（降速 8kHz 依然掉 → 非頻寬）、`.53` 只出 0xFF 亂碼。
   物理解法：斷電、CL 兩端重插鎖螺絲。

其他：首頁加 `Cache-Control: no-store`（「桌面捷徑開到舊軟體」= 瀏覽器快取舊頁）；
l800_serial 迴環首次偶發空收 → 加重試。
教訓：**同時只能有一個控制端** —— 用調機工具前先關 eBUS Player。

## 兩台經交換器同步觸發驗證通過（2026-09-01）

拓樸：2× iPORT（各接 L803K）→ 交換器 → 100G 上行 → `enp1s0f1np1`（192.168.4.2，MTU 9000）。
兩台原本各在 192.168.2.x / 192.168.1.x，已 FORCEIP + 持久化：

| IP | MAC | 韌體 |
|---|---|---|
| 192.168.4.53 | 00:11:1c:06:00:2b | 1.03.03.109（原本那台，之前的 192.168.5.10 設定已被改掉） |
| 192.168.4.54 | 00:11:1c:06:2d:d9 | 1.03.05.119 |

實測（`multi_trigger.py --iface enp1s0f1np1 --srcip 192.168.4.2 --devices 192.168.4.53 192.168.4.54 --frames 27 --height 5000`）：

```
jumbo 9000 經交換器路徑：兩台皆到達
觸發送出        25.5 us（burst 不等 ACK）
首幀抵達離散度  39.6 us = 0.5 行 @13.7kHz
張數           兩台皆 27/27，零丟包
```

**發現：兩台 free run 行速率不同** —— .54 跑 13.7 kHz（9.85s 完成），
.53 只有 5.0 kHz（27.00s 完成，恰為 1 s/張）。同步觸發沒問題，但完成時間
差 17 秒。L803K 的 free-run 行速率存在相機本身設定裡（透過 Camera Link
序列埠設定），**18 台上線前必須統一**，否則掃描節拍由最慢那台決定。

**新韌體 1.03.05.119 的 WRITEMEM bug 沒修**（寫入回 SUCCESS、實際寫 0，
與 1.03.03.109 相同）→ aravis 統一新舊相機一路正式放棄，維持 eBUS（正式）
+ 自製 GVCP 工具（診斷）。

## 多台一次性觸發（multi_trigger.py）

量測 N 台同時 Grab_Start 的實際離散度：每台用獨立接收埠，記錄第一張影像的
leader 封包抵達主機的時間，台與台的差值就是離散度。

```bash
python3 multi_trigger.py --frames 27 --height 5000            # burst（預設）
python3 multi_trigger.py --method sequential --frames 27      # 對照組
```

### 為什麼一定要用 burst（不等 ACK）

實測這台 iPORT 的 GVCP 往返：

```
WRITEREG 往返  中位數 1534 us   P99 2039 us   最大 2118 us
不等 ACK 送出  中位數  6.5 us   P99   33 us
```

GVCP 往返 1.53 ms 遠慢於 ping 的 0.47 ms（裝置命令處理本身慢）。
若每台送完等 ACK 再送下一台：

| 台數 | 序列（等 ACK） | burst（不等 ACK） |
|---|---|---|
| 3 | ~3 ms（42 行） | ~30 us（2 行） |
| 37 | ~55 ms（754 行） | ~236 us（17 行） |
| 55 | ~83 ms（1137 行） | ~355 us（26 行） |

（行數以 13.7 kHz 換算）55 台序列觸發會差到 1137 行，不可接受；
burst 只差 26 行，占 135,000 行掃描的 0.02%。

不等 ACK 的代價是沒有送達確認，因此工具改以「每台是否收到預期張數」
作為驗證，而不是靠 ACK。

### 單台實測（2026-08-05，直連）

```
觸發指令送出   14.4 us
首幀抵達延遲   1.21 ms（觸發後到第一個 leader 封包）
27 張 x 5000 行  零丟包  9.86s
```

離散度要 2 台以上才量得出來，等 8/6 三台上 HPE 5945 後再測。

## 正式流程驗證：27 張 × 5000 行（2026-08-05）

對應 Panel 進片 → Control PC → Grab_Start → free run 取 27 張後停止。
`test_multiframe.py` 實測：

```
AcquisitionMode=2 (MultiFrame)  AcquisitionFrameCount=27  Height=5000
每張 8160x5000 = 40.8 MB，27 張共 1.10 GB
收到張數  27 / 27  OK
封包遺失  0
取像耗時  9.85s        平均 111.8 MB/s (13.70 kHz)
結束後 2 秒內多收到 0 個封包 → 裝置自行停止
```

`AcquisitionFrameCount` 上限 255，27 沒問題。裝置取滿自己停，
軟體不需要送 `AcquisitionStop`。

### 踩到的坑：GVCP heartbeat 必須用同一個 socket

一開始不論設幾張，**一律停在 14 張 / 5.11 秒**（Height 2500 是 28 張、
Height 1000 是 69 張 —— 換算都是 ~70,000 行 / ~5.1 秒）。

原因不是裝置限制，是 heartbeat 發錯來源：**GVCP 以「來源 IP + 來源埠」
認定 control channel 的持有者**。原本用另一條 thread 開新 socket 定期讀
CCP，裝置不認那些封包，`GevHeartbeatTimeout`（預設 3000 ms）一到就收回
控制權、停止串流。

改成由主迴圈用持有 control channel 的同一個 `Gvcp` 實例每秒送一次即可。
`grab_gvsp.py` 有同樣的 bug，已一併修正（先前測試都不夠長所以沒踩到）。

> 這也是為什麼 eBUS 的 `t31_ebus_grab` 跑 200 張 14.61s 一直沒事 ——
> 它的 heartbeat 是對的。

### 其他模式

| Mode | 值 | 行為 |
|---|---|---|
| Continuous | 0 | 持續送，軟體自行 `AcquisitionStop` |
| MultiFrame | 2 | **本案採用**，取滿 `AcquisitionFrameCount` 自停 |
| MultiFrameRecording | 7 | 錄到板載 120 MB 記憶體，需另外 readout，實測不會直接串流 |

## eBUS SDK 7.0.1 驗證通過（2026-08-05）

安裝：`eBUS_SDK_Ubuntu-22.04-x86_64-7.0.1-7536.deb` → `/opt/pleora/ebus/Ubuntu-22.04-x86_64`
（注意是 `ebus` 不是 `ebus_sdk`；環境變數用 SDK 自產的 `bin/set_puregev_env.sh`，
7.0 的 GenICam 變數是 `V3_4` 不是 `V3_3`）

filter driver `ebUniversalProForEthernet_x86_64.ko` 對 kernel 6.8.0-136 編譯成功並載入，
`/dev/ebUniversalProForEthernet` 已建立。

`~/下載/Grab` 的測試程式編譯後實測：

```
t30_ebus_probe   [PASS] DisplayID=iPORT CL-GigE-PT01-CL0IP01-128x [192.168.5.10]
t31_ebus_grab    [PASS] throughput  FPS=13.7  頻寬=0.89 Gb/s  耗時=14.61s (200 幀)
                 [PASS] drop        漏 block=0 / 199   drop=0.0000%
```

與自製 `grab_gvsp.py` 交叉驗證一致（13.4 kHz / 108 MB/s vs 13.7 kHz / 111 MB/s，
兩者皆零丟包），影像雜訊分佈也相同。eBUS 略快，合理 —— 有 filter driver。

已知小問題：postinst 的 `eBUS Python 3.10 installation [FAIL]`，原因是系統沒裝 pip。
C++ 路徑不受影響。要用 Python 綁定的話：

```bash
sudo apt install -y python3-pip
python3 -m pip install --user $PUREGEV_ROOT/lib/eBUSPython/*.whl
```

## 取像已驗證通過（2026-08-04）

`grab_gvsp.py` 實測結果，滿幅 8160×5000 Mono8：

| 指標 | 數值 |
|---|---|
| 幀數 | 5 幀，204.0 MB |
| 封包 | 22800 個，**遺失 0** |
| 耗時 | 1.86 s |
| 資料率 | **109.7 MB/s (0.88 Gb/s)** |
| 行速率 | 13.4 kHz（L803k 上限 14.1 kHz 的 95%） |
| 封包大小 | 9000（jumbo） |

無鏡頭無光源，影像為暗場：平均 8.8、值域 3–14、暗區(<10) 81%。
灰階分佈是以 9 為中心的高斯，放大後可見每像素隨機雜訊與 line scan
逐行讀出的水平紋理 —— 感測器確實在數位化，不是全零或常數。
見 `grab_00_crop_stretched.png`（對比拉伸後的 200×100 局部放大）。

> 先前擔心的 115 MB/s 頻寬吃緊問題實測沒有發生：109.7 MB/s 零丟包，
> 而且這還是純 Python 收包。eBUS 的 filter driver 只會更好。

## 為什麼不能用 aravis 串流（韌體 bug）

**iPORT 韌體 1.03.03.109 的 `WRITEMEM_CMD` 對 GenICam 暫存器空間會回報
SUCCESS 但實際寫入 0** —— 靜默資料損毀。實測：

```
WRITEMEM Height=1234 -> SUCCESS   讀回=0
WRITEMEM Height=2000 -> SUCCESS   讀回=0
WRITEREG Height=1234 -> SUCCESS   讀回=1234    <- 正常
```

aravis 的 GenICam feature 寫入一律走 `arv_device_write_memory`（`ArvGcRegister`
的設計如此，因為 GenICam 暫存器長度不固定），所以：

- `arv-tool control Width=8160` 看似無錯誤，實際把值寫成 0
- `create_stream()` 丟 `write_memory error (invalid-parameter)`

aravis 的 `take_control` 反而正常（它用 WRITEREG 寫 CCP，實測開啟後 CCP=2）。
裝置的 GVCP Capability (0x0934) = `0xd85a0077`，沒有任何欄位可以讓 aravis
得知 WRITEMEM 壞掉，所以換新版 aravis 也不會自動解決。

**這不是裝置被鎖住。** 早先「寫回原值失敗」是因為當時用 aravis 去寫；
改用 WRITEREG 隨時都能還原，不需要重開機。

改用 `WRITEREG_CMD` 就完全正常 —— `grab_gvsp.py` 與其他 `gvcp_*.py` 都這麼做。
aravis 仍可安全用來**讀取**參數與 dump GenICam XML（READMEM 沒問題）。

> 若要用 aravis 統一新舊相機，先確認 Pleora 是否有修掉此 bug 的新韌體
> （2019-09 有過 CL-GigE 韌體更新 PCN，內容需登入才看得到）。
> 否則需自行 patch aravis，讓 4-byte 對齊的暫存器寫入改走 WRITEREG。

### 由 GenICam XML 解出的暫存器位址

| Feature | 位址 |
|---|---|
| Width | `0x12500` |
| Height | `0x12510` |
| PixelFormat | `0x12540` |
| AcquisitionStart | `0x13110` |
| AcquisitionStop | `0x13120` |

GVSP 封包用 **extended ID 格式**（20-byte 標頭：status(2) + reserved(2) +
format(1, bit7=EI) + reserved(3) + block_id(8) + packet_id(4)），
不是舊版 8-byte 標頭。

## 用 Aravis 讀參數

Aravis 是開源 GigE Vision 實作，已用 `apt-get download` + `dpkg -x` 解到
`aravis-local/`（不需 root、不動系統）。`grab_aravis.py` 會自行設好
`LD_LIBRARY_PATH` / `GI_TYPELIB_PATH`。

```bash
aravis-local/usr/bin/arv-tool-0.8                  # 列舉裝置
aravis-local/usr/bin/arv-tool-0.8 features         # 全部參數
aravis-local/usr/bin/arv-tool-0.8 genicam > x.xml  # dump XML
```

`grab_aravis.py` 保留作為對照，但因上述 WRITEMEM 問題無法串流，取像請用
`grab_gvsp.py`。

> `ClSafePowerStatus` 一直是 `Initializing`、`ClSafePowerActive=false`，
> 但取像完全正常。這是 PoCL 供電狀態機的顯示，L803K 本來就自帶 12V 電源、
> 不吃 PoCL，所以這個值不代表故障，**不要拿它當健康檢查條件**。

## 狀態

- [x] 相容性確認
- [x] 裝置探索（GVCP，不需 SDK）
- [x] IP 設定 192.168.5.10（持久，已通過重開機驗證）
- [x] Jumbo frame 9000 bytes 實測可用
- [x] Aravis 本地安裝（讀取用）
- [x] **取像驗證：滿幅 5 幀零丟包，109.7 MB/s**
- [ ] eBUS SDK 下載（卡在 Pleora 帳號登入）
- [ ] 接鏡頭與光源後的實拍驗證
