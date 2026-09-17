# 相機架設 / 換相機 Runbook（8/M 平台）

> 依據：2026-07-30/31 HPE 5945 + 2 相機實機驗證的全部經驗（STATUS.md「Switch 到貨日」章節）。
> 到貨當天照本檢查單逐項執行；每個坑都是實際踩過的，**不要跳步**。
> 環境：HPE FlexFabric 5945（`CFAOI-SW1`）｜damac（截取中心）｜spark-c16f（IP）。
>
> ⚠️ **2026-08-02 全面複查後追加的到貨日注意事項**（未修的已知缺陷，會直接影響下列步驟；
> 完整清單見 [code_review_20260802.md](code_review_20260802.md)）：
> 1. **起 grab 一律用 `--cam-count 6`，不要用 `ALL`**（B4）：`ALL` 沒有「應到幾台」概念，
>    某台沒上電/link 沒起時 **ARM 照樣回 OK 只跑 5 台**；`--cam-count 6` 走 fail-fast 會當場報錯。
> 2. **`GET_CAM_NODES` 目前只會回 cam0 的參數**（X1，Control 端未送 cam_id）：§5 逐台健檢時
>    **改用 8100 直下並自帶 `cam_id`** 核對，不要只看 Control UI 顯示（會靜默看到同一台）。
> 3. **idle 狀態（未 ARM）下的 `GET_CAM_NODES`/`TUNE_MEAN` 會開「列舉第一台」而非指定台**（B6）；
>    `TUNE_MEAN` 還會把量到的曝光**寫進指定 cam_id 的設定槽** → §5 健檢請**先 `GRAB_ARM` 再逐台查**。
> 4. **單台隔離排障時 `--cam-count 1` 會讓 `--cam-id` 蓋掉 MAC 綁定**（B7）：該台會以 `camId=0` 送圖
>    並套用 CCD00 的曝光 → 單台測試結果要自行換算，勿直接當該槽位的結論。
> 5. ~~一台相機拔線/斷電會讓整個 cfaoi_grab 行程死亡~~（B1 **已修，L2 自動化測試過，待實機補驗**）：現在只有**該台**
>    停下，其餘相機續跑，行程存活。判讀改成——`running` 少一台**不代表它收滿了**，要看 8100
>    `CHECK_HEALTH` 回應裡的 **`faulted` / `faulted_cams`**（含 cam_id + pylon 錯誤訊息）才知道是斷線。
>    ⚠️ **Control UI 尚未顯示這個欄位**，現場請直接對 8100 查或看 Grab stdout 的
>    「⚠️ camN 取像中止」。掉線的台**不會自動重連**，排除線路後要 `GRAB_STOP` → `GRAB_ARM`。
> 6. **RDMA 送失敗後會靜默丟幀且 `dropped` 仍為 0**（B2）：對帳只信 **Spark 端 `recv ok/err`**，
>    不要只看 Grab 的 `sent_frames`。

---

## 0. 到貨前可先做（不需相機）

- [ ] 決定 6 台相機的**槽位對應**：`ccd_id`（CCD00–CCD05）↔ 交換機埠位 ↔ 預計 IP（建議 192.168.5.1–.6）。
- [ ] 決定交換機埠位（建議 `WGE1/0/33-38`：33/35 已驗通；**注意 port-group**，見下）。
- [ ] 準備 `grab/cam_map.json` 草稿（MAC 到貨才知道，先留槽位）；格式見 `grab/cam_map.example.json`。
- [ ] 確認 damac `grab/cam_config.json`、`grab/cam_map.json` 為唯一副本
      （2026-07-31 起路徑錨定 `grab/`，啟動 log 會印絕對路徑；CWD 殘留副本會被 WARN 點名——看到就刪）。

## 1. 交換機

**2026-09-17 起 5945 全部 48 個 25G 埠（WGE1/0/1–24、33–56）已預設 `speed 1000` + `stp edged-port` 並 `save force`**
（撐到 SN2201 到貨）→ 相機插任一 WGE 埠即 UP 1G，**不必再逐埠設定**。
驗證：`display interface brief | include WGE` 該埠應 UP 1G。
（25G 埠要改回高速時，須對該 port-group 重下 `speed`。console = damac `/dev/ttyUSB0`，9600 8N1。）

已踩過的坑（仍適用於 SN2201 以外的新交換機）：
1. **25G SFP28 埠插 1G 銅纜模組，auto-neg 永不 link up，必須 `speed 1000`**。只插模組不下 speed：
   `display transceiver` 看得到模組但 link DOWN、`Input 0 packets`——像壞線，其實是 auto-neg。
2. **`speed 1000` 有 port-group 連動**（1–4、5–8…33–36、37–40…四埠一組）：對組內任一埠下即全組生效，
   且會跳 `[Y/N]` 確認——**腳本化時要處理提示**，否則後續指令被當答案吃掉。
   2026-09-17 實例：37–40、41–44 組沒回到 Y → 顯示仍 `auto`，4 台相機全失聯。
3. OEM `SFP-GE-T` 模組 EEPROM 報成 `1000_BASE_SX_SFP`（實為 RJ45），下 `speed 1000` 後照常 UP，無妨。
4. jumbo 原廠已 `Maximum frame length: 9416`，**不需再設**。

## 2. 相機身分與網路（每台；換相機也照這節）

**規則：CCD 身分存在相機本身。** `DeviceUserID = CCDnn` → cam_id = nn；persistent IP = `192.168.5.(nn+1)/24`。
pylon Viewer 清單直接顯示 CCD 名稱；grab 依此綁定，不必改任何檔案。

- [ ] damac `enp1s0f1np1` 需有 `192.168.5.200/24`（相機網段）+ `169.254.0.200/16`（出廠 AutoIP 新相機直接可見），
      皆 nmcli 持久化。
- [ ] 設定（二擇一）：
  - **pylon Viewer**：開相機 → Device Control → **Device User ID** = `CCDnn`；
    IP 用 **pylon IP Configurator** 設 Static IP `192.168.5.(nn+1)` / `255.255.255.0`。
  - **CLI**：`grab/build/cam_provision list` 找序號 →
    `grab/build/cam_provision set <serial> CCDnn`（一次寫 UserID + persistent IP 並 ForceIp 立即生效；
    名稱/IP 撞到別台會拒絕）。
- [ ] `ping 192.168.5.(nn+1)` 通、`cam_provision list` 看到 `USER_ID=CCDnn` 再做下一台。
- [ ] **設定時相機不可被開著**：cfaoi_grab 先 `GRAB_STOP`、pylon Viewer 關掉該台。
- [ ] **換下來的舊相機若要接回（備品），先把它的 UserID 改掉**——兩台同名 grab 會拒開（fail-fast）。
- [ ] 新相機出廠 IP 可能在**任意網段**（借用機實測在 192.168.30.50）。跨網段時 pylon Viewer 看不到，
      但 **pylon IP Configurator 看得到**（廣播探索）；也可用 GVCP 原始廣播（`DISCOVERY_CMD` flag=0x11，
      **須 `SO_BINDTODEVICE` 綁 `enp1s0f1np1` 且 socket 綁 0.0.0.0**——相機回的是 255.255.255.255 廣播，
      綁特定 IP 的 socket 收不到）。
- [ ] `packet_size=8192`、`GevSCPD=0`（**不要開** inter-packet delay：每台獨立 1G access port +
      100G 上行，實測 wire rate 跑滿、p99 抖動近零、37 台帳面僅 100G 的 37%）。

2026-09-17 實機配置：

| 交換機埠 | SN | MAC | CCD | IP |
|---|---|---|---|---|
| WGE1/0/37 | 25564093 | 00:30:53:54:E6:BD | CCD00 | 192.168.5.1 |
| WGE1/0/39 | 25563179 | 00:30:53:54:E3:2B | CCD01 | 192.168.5.2 |
| WGE1/0/41 | 25563177 | 00:30:53:54:E3:29 | CCD02 | 192.168.5.3 |
| WGE1/0/43 | 25563161 | 00:30:53:54:E3:19 | CCD03 | 192.168.5.4 |

## 3. damac 端

- [ ] `ip -br addr show enp1s0f1np1` 有 192.168.5.200/24、169.254.0.200/16，MTU 9000。
- [ ] `ping 192.168.5.x` 每台 0% loss 才繼續。
- [ ] **殺行程用 `pkill -x cfaoi_grab`**——`pkill -f` 會殺掉自己的 ssh（remote command line 也含該字串）。

## 4. 綁定確認（嚴格模式）

1. 起 grab：`grab/build/cfaoi_grab --rdma-dest 192.168.3.1:18515 --cam-count N`
   （★ **用實際台數不用 ALL**，見標頭注意事項 1）。
2. `LIST_CAMERAS`：每台 `bound=true`、`bind_source="user_id"`、`ccd_id` 正確。
3. 身分優先序：**DeviceUserID `CCDnn` > `grab/cam_map.json` 的 MAC 綁定（過渡/備援）> 未綁定**。
   - 任一台有 UserID 或存在 cam_map → 嚴格模式：**沒有身分、UserID 打錯（非 `CCDnn`）、cam_id 重複 → ARM 拒開**並點名序號。
   - UserID 與 cam_map 衝突 → 以 UserID 為準並印 WARN（請更新/刪除 cam_map 該筆）。
   - `SET_CAM_MAP`（Control 拓樸頁綁定鈕）仍可用，寫入的是備援 MAC 映射。
4. ⚠️ **cam_id 決定 `cam_config.json` 曝光/增益歸屬、`FrameHeader.camId`、IP 輸出夾 `CCD{n}`**。
   列舉順序會隨接入台數改變——一切以相機身分為準。

## 5. 逐台健檢（idle，不需 RDMA）

- [ ] **先 `GRAB_ARM` 再逐台查**（★ 重要，B6）：idle 未 ARM 時 `GET_CAM_NODES`/`TUNE_MEAN` 會落到
      「列舉第一台」而非指定台（回應的 cam_id 回聲仍是你問的那台 = 靜默錯台）。
- [ ] `GET_CAM_NODES {"cam_id":N}` 每台：解析度/PixelFormat=Mono8/TriggerMode=Off/packet_size=8192
      （★4 已修 Grab 端：回應帶 cam_id 回聲、錯 cam_id 回 ERR；
      ⚠️ 但 **Control UI 的「讀取機器層參數」鈕不送 cam_id、永遠顯示 cam0**（X1 未修）→ 逐台核對請走 8100 直下）。
- [ ] `SET_CAM_PARAMS`/`TUNE_MEAN` 每台可調（★5 已修：cam_id≠0 不再被擋）。
- [ ] **`TUNE_MEAN` 測完曝光會留在相機上**——調參後務必 `SET_CAM_PARAMS` 復原
      （踩過：留在 2000µs → 6s/幀 0.11fps，誤判成傳輸問題）。
- [ ] 暗場 `mean_gray≈2.5` 是 noise floor 正常值，不是故障；要看灰階變化必須打光。

## 6. 全鏈驗證（需 Spark）

順序照抄（每步過了才下一步）：

| # | 步驟 | 指令 / 期望 |
|---|---|---|
| 1 | Spark 起收端 | `ip/build/cfaoi_ip --mode rdma-process --recipe <recipe> --output <dir>`；**8200 起動即通**（2026-07-31 起不用等 Grab 連上），Control 心跳應綠 |
| 2 | 觸發鏈 | `scripts/verify_step3_trigger.py <damac> 8100 5 6` → **6/6 PASS**（expect_cams=6） |
| 3 | ARM 冷啟時間 | 外推 ≈ **3.8s**（實測 1 台 542ms／2 台 1257ms，線性；37 台 ≈23s）。確認產線時序 ARM 遠早於觸發；冪等重呼應 ms 級 |
| 4 | 全鏈對帳 | 每台 5 幀：`grabbed=30 sent=30 dropped=0`；Spark `recv ok=30 err=0` CRC 全對；輸出夾 CCD00–CCD05 各 5。⚠️ **以 Spark 端 recv 為準**——RDMA 送失敗時 Grab 端 `dropped` 仍是 0（B2）|
| 5 | 連續兩片 | 第二片不重 ARM、觸發 <1ms、seq 不歸零（★3 已修）、輸出夾不覆蓋 |
| 6 | 串流中控制 | 串流中 Control 心跳/GET_STATUS 應全 OK（2026-07-31 實測 2 台 381 次 0 失敗）；`LIST_CAMERAS` 並存不掉幀（已驗） |

## 7. 已知限制（先知道，別當故障）

- **光源強度差產線行速 25–30 倍**：70µs/行時 gain 拉滿 mean_gray 僅 5.81。6 台調參可以做，
  但 Step 4/5 生產速度取像**必須先解光源**（加強亮度或大光圈鏡頭）。
- **free-run 無 encoder**：無相對運動時線掃 Y 軸是「時間」不是「位置」，拍不出真實 2D 面板。
  真面板缺陷正確性驗證需接 encoder 行觸發（`TriggerSelector=LineStart` + ShaftEncoderModuleOut）或讓工件移動。
- **IP/Grab 8100/8200 皆單客戶端序列處理**：Control 佔線時第二個診斷工具只會排隊逾時。
  診斷腳本要等 Control 斷開，或錯開使用。
- **6 台頻寬**：6 × 123.6 MB/s ≈ 5.9 Gbps 進 damac —— 遠低於 37 台的 36.6 Gbps，
  NIC ring/RSS/中斷調優可留到 37 台再做。

## 8. 收工檢查

- [ ] `save force`（交換機）
- [ ] `grab/cam_map.json`、`grab/cam_config.json` 內容確認 + 曝光復原
- [ ] STATUS.md 入帳（分級照 §8：貼數據才算驗證）
