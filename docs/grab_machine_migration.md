# 截取中心主機移機 Runbook（damac → 新機，Ubuntu 24.04）

> 對象：把 grab（相機擷取 + RDMA 發送）從測試機 **damac** 搬到正式機。
> 原則：**damac 不動**——新機沒驗完之前保留舊機，隨時可切回去。
> 依據：damac 現況快照 [machine_state_damac_20260918.txt](verification/machine_state_damac_20260918.txt)
> （用 `tools/grab_setup/collect_machine_state.sh` 產生，新機裝完可再跑一份對照）。

## 0. damac 現況（要複製什麼過去）

| 類別 | 內容 | 怎麼帶 |
|---|---|---|
| OS | Ubuntu 22.04.5（新機為 **24.04**，差異見 §5） | — |
| 硬體 | i5-6400 / 7.6GB / **ConnectX-5 MCX516A-CCAT**（f0=RDMA 直連 Spark、f1=相機經 5945） | **拆卡搬到新機**（MAC 不變 → 交換機與 Spark 端不用改） |
| 相機軟體 | pylon 26.05 + codemeter | `~/下載/pylon/*.deb` 複製過去 |
| L803K 路徑 | Pleora eBUS SDK 7.0.1 + CodeMeter（軟體授權，無 USB dongle） | `~/下載/eBUS_SDK_Ubuntu-22.04-*.deb`（⚠️ 見 §5） |
| 建置相依 | build-essential / cmake / nlohmann-json3-dev / rdma-core / libibverbs-dev / librdmacm-dev | bootstrap 自動裝 |
| 網路 | 相機 `192.168.5.200/24` + `169.254.0.200/16`、RDMA `192.168.3.2/24`、**MTU 9000**、nmcli 持久化 | bootstrap 自動設 |
| 系統參數 | `net.core.rmem_max=33554432`、`rp_filter=0` | bootstrap 自動設 |
| 程式碼 | `git@github.com:AddisLu/cf-aoi.git` | `git clone`（SSH key 要帶） |
| 機器本地檔 | `grab/cam_config.json`（曝光/增益，**不版控**） | 手動複製；不帶則用預設值重調 |
| 相機身分 | **存在相機本身**（DeviceUserID + persistent IP）→ **不必搬** | 換主機不影響 |
| 其他 | SSH key（GitHub / Spark）、tailscale、Claude Code CLI | 手動 |

> `grab/cam_map.json` 已停用（身分改存相機內，見 [STATUS](STATUS.md)「4 相機到貨日」節），不必帶。

## 1. 硬體

1. **關機、拆 ConnectX-5**（damac PCIe）→ 裝到新機 x8 以上插槽（100G 卡吃頻寬，勿插 x4）。
2. 兩條線照舊接：**f0（MAC …:CC:18）→ Spark DAC 直連**、**f1（MAC …:CC:19）→ 5945 `HGE1/0/25`**。
   接反不會壞，但相機與 RDMA 會對調 → bootstrap 以 MAC 認卡，接反時會設到錯的網段，
   實機驗收（§4）的 ping 會抓到。
3. 交換機 console 線（USB 轉序列）也移過去；新機要在 `dialout` 群組（bootstrap 會加）。
4. 開機後確認：`lspci | grep -i mellanox` 看得到兩個 function。

## 2. 安裝

```bash
# 1) 把安裝檔從 damac 複製過來（在新機執行）
scp -r damac:'~/下載/pylon' ~/下載/pylon
scp damac:'~/下載/eBUS_SDK_Ubuntu-22.04-x86_64-7.0.1-7536.deb' ~/下載/

# 2) 取得程式碼（SSH key 先放好，或先用 https clone）
git clone git@github.com:AddisLu/cf-aoi.git ~/Addis/cf-aoi

# 3) 一鍵安裝（冪等，可重跑；先 --dry-run 看它要做什麼）
~/Addis/cf-aoi/tools/grab_setup/bootstrap.sh --dry-run
~/Addis/cf-aoi/tools/grab_setup/bootstrap.sh
```

bootstrap 做的事（每步都會印）：apt 相依 → pylon（+codemeter）→ eBUS SDK → 網路（nmcli，
**以 MAC 認卡**，介面名叫什麼都不影響）→ sysctl → dialout 群組 + 桌面捷徑 → 建置 grab + 跑離線測試。

裝完把機器本地檔帶過來：

```bash
scp damac:'~/Addis/cf-aoi/grab/cam_config.json' ~/Addis/cf-aoi/grab/
```

## 3. 相機與交換機（多半不用動）

- **相機身分/IP 存在相機自己身上**（DeviceUserID `CCDnn` + persistent IP 192.168.5.x）→ 換主機不受影響。
- **交換機不用改**：全部 48 個 25G 埠已預設 `speed 1000` + `stp edged-port` 並 `save force`。
  ConnectX-5 拆過去 MAC 不變，`HGE1/0/25` 的 MAC table 會自己重學。
- **Spark 端不用改**：對端仍是 `192.168.3.2`（本機）↔ `192.168.3.1`（Spark）。

## 4. 驗收（沒跑過這關不算搬完）

```bash
~/Addis/cf-aoi/tools/grab_setup/verify_grab_host.sh          # 只讀
~/Addis/cf-aoi/tools/grab_setup/verify_grab_host.sh --arm    # 需 Spark 已起 rdma-process
```

檢查項與判讀：

| 項目 | 期望 | 失敗時看哪裡 |
|---|---|---|
| 相機/RDMA 網段、MTU 9000 | 兩張卡都在、100000Mb/s | 線接反或卡沒認到 → `lspci`、`nmcli con show` |
| `169.254.0.200/16` | 有 | 少了這條，出廠 AutoIP 的新相機在 pylon 裡看不到 |
| `rmem_max` / `rp_filter` | 33554432 / 0 | `/etc/sysctl.d/90-cfaoi.conf` |
| pylon 列舉相機 | 台數正確、每台有 `USER_ID` | 0 台 → 先查交換機埠 `speed 1000`、相機供電（[6cam runbook](6cam_setup_runbook.md) §1） |
| `ping 192.168.5.x` | 每台 0% loss | 同上 |
| RDMA port | ACTIVE + Ethernet + ping Spark 通 | 線、Spark 端狀態 |
| 三組 grab 離線測試 + 調機工具 34 項 | 全過 | 各測試的 README |
| `--arm` | `{"status":"OK"}` + 每台 `8192x5000 PayloadSize=40960000` | Spark 未起 rdma-process 會停在 `RDMA connect failed` |

damac 上的參考結果（2026-09-18）：**23 項全過**。新機應該一樣。

## 5. Ubuntu 22.04 → 24.04 的已知差異（先知道，別當故障）

| 項目 | damac(22.04) | 新機(24.04) | 影響 |
|---|---|---|---|
| **eBUS SDK** | 7.0.1（22.04 版 deb） | **官方無 24.04 版 deb** | L803K 路徑可能裝不起來 → 需向 Pleora/友思特索取 24.04 版。**不影響 raL8192 與調機工具**（調機工具自製 GVCP/GVSP，不依賴 eBUS） |
| gcc | 11 | 13 | grab 以 C++17 寫、無 GNU 擴充，預期可編；bootstrap 會實際編一次 |
| nlohmann-json | 3.10.5 | 3.11.x | API 相容 |
| rdma-core | 39 | 50 | RoCE v2 行為不變；`ibv_devinfo` 欄位略有增減 |
| pylon 26.05 | 支援 | 支援 | deb 直接裝 |
| Python | 3.10 / Pillow 9 | 3.12 / Pillow 10 | 調機工具只用標準庫 + Pillow，**離線測試 34 項是把關點** |

## 6. 切換與回退

**切換**：新機驗收全過 → 停 damac 上的 grab（`pkill -x cfaoi_grab`）→ 新機起 grab：

```bash
cd ~/Addis/cf-aoi && stdbuf -oL -eL grab/build/cfaoi_grab \
  --rdma-dest 192.168.3.1:18515 --cam-count 4 >> ~/cfaoi_grab_$(date +%Y%m%d).log 2>&1 &
```

> `stdbuf -oL` 不要省：stdout 導到檔案時預設整批緩衝，開相機那幾行會延遲好幾分鐘才出現。
> Control 端連的是 **8100**，主機換了要改 Control 的 Grab 位址設定。

**回退**：把 ConnectX-5 插回 damac、兩條線接回去即可（damac 的 nmcli 設定仍在）。
相機端完全不用動——身分與 IP 都存在相機裡。

## 7. 移機後才做得了的事

- **舊機 damac**：轉為測試/備援機。若要同時開著，**注意兩台不可同時對同一台相機取像**
  （GigE 一次只允許一個控制端；會得到 `controlled by another application`）。
- 重新確認 [STATUS](STATUS.md) 未完成項：RDMA 全鏈（Spark 切 `rdma-process`）、L803K/iPORT、
  persistent IP 斷電複驗、拼接處畫面連續性。
