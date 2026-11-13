# 下 Grab 複製清單（來源：上 Grab `user-IMB-M47`，2026-11-13）

> 對照快照：[machine_state_upper_grab_20261113.txt](verification/machine_state_upper_grab_20261113.txt)
> （用 `tools/grab_setup/collect_machine_state.sh` 產生，下 Grab 裝完可再跑一份對照）
> 相關：[grab_machine_migration.md](grab_machine_migration.md)（damac → 上 Grab 的移機 runbook，§5 有 22.04 版 eBUS 裝在 24.04 的處理）、
> `tools/grab_setup/bootstrap.sh`（一鍵安裝）、`tools/grab_setup/verify_grab_host.sh`（驗收）

## 0. 先講結論

**不需要手動一項一項裝。** repo 裡的 `tools/grab_setup/bootstrap.sh` 是冪等的一鍵安裝，會處理 apt 相依、pylon、eBUS SDK、網路（nmcli）、sysctl、群組、桌面捷徑、建置 grab 並跑離線測試。

你要準備的只有三件事：**安裝檔**、**不版控的機器本地檔**、**改掉認網卡的 MAC 和 IP**。

---

## 1. 安裝檔（必須手動複製，約 390 MB）

| 檔案 | 位置 | 大小 |
|---|---|---|
| `pylon_26.05.0-deb0_amd64.deb` | `~/下載/pylon/` | Basler 相機 |
| `codemeter_8.20.6558.501_amd64.deb` | `~/下載/pylon/` | 授權（eBUS 也要） |
| `eBUS_SDK_Ubuntu-22.04-x86_64-7.0.1-7536.deb` | `~/下載/` | Pleora L803K |

```bash
scp -r 上Grab:'~/下載/pylon' ~/下載/pylon
scp 上Grab:'~/下載/eBUS_SDK_Ubuntu-22.04-x86_64-7.0.1-7536.deb' ~/下載/
```

選配（非 CF-AOI 必需）：`google-chrome-stable_current_amd64.deb`、`claude-desktop_amd64.deb`，同在 `~/下載/`。

> ⚠️ eBUS SDK 是 **22.04 版**裝在 24.04 上，runbook §5 有記錄相容性處理，照著走。

---

## 2. bootstrap 會自動裝的（不用你管）

apt：`build-essential cmake nlohmann-json3-dev rdma-core libibverbs librdmacm ibverbs-utils infiniband-diags perftest pkg-config gdb git curl ethtool tcpdump openssh-server python3-pip python3-netifaces python3-pil`

網路（nmcli 持久化）：相機 `192.168.5.200/24` + `169.254.0.200/16` + `192.168.4.2/24`、RDMA `192.168.3.2/24`、控制網 `192.168.10.21/24`、**MTU 9000**

sysctl（`/etc/sysctl.d/90-cfaoi.conf`）：`rmem_max=33554432`、`rp_filter=0`、`ip_forward=1`

---

## 3. ⚠️ bootstrap 裡一定要改的三個值

`tools/grab_setup/bootstrap.sh` 最上面的 CONFIG 區塊是**寫死上 Grab 的值**：

```bash
RDMA_MAC='98:03:9B:06:CC:18'    # ← 下 Grab 的 ConnectX-5 是另一張卡，MAC 不同
CAM_MAC='98:03:9B:06:CC:19'     # ← 同上
CTRL_ADDRS='192.168.10.21/24'   # ← 下 Grab 規劃是 .22
```

另外要一起調整的（多數 [docs/CLAUDE.md](CLAUDE.md) §2 已定案）：

| 項目 | 上 Grab | 下 Grab | 依據 |
|---|---|---|---|
| 相機 | 37× Basler raL8192-12gm（CCD01–CCD37）| **18× L803K 經 Pleora iPORT，CCD38–CCD55** | CLAUDE.md §2 |
| 相機網段 | `192.168.5.nn`（存在相機內）| **iPORT persistent IP `192.168.4.nn`**（尾碼=編號，用 `grab/build/iport_provision set <SN> CCDnn` 寫入）| 同上 |
| 啟動參數 | pylon 後端 | **`GRAB_EXTRA=--camera ebus`** | 同上 |
| 對應 Spark | spark-02（`192.168.3.1`）| **spark-03** | 同上 |
| 控制網 | `192.168.10.21` | **`192.168.10.22`**（接同一台控制網交換器，spark-03 是 `.12`）| 同上 |
| **RDMA 網段** | `192.168.3.2/24` ↔ spark-02 `.1` | **⚠️ 未定案**（CLAUDE.md 標「下排 RDMA（待定）」）| — |

> **只剩 RDMA 網段要你決定。** 兩組 Grab↔Spark 是各自獨立的直連線（不經交換機），
> 技術上可以都用 `192.168.3.x`；但上 Grab 現在已經有經 spark-02 轉送到 `192.168.177.0/24` 的路由
> （2026-11-13 加），日後若兩組要互通就會撞，建議下排改用另一個網段（例如 `192.168.6.x`）。

`CAM_COUNT` 填**實際接上的台數**，不要用 ALL（某台沒起來時 ALL 照樣 OK 只跑 N-1 台，數字則當場報錯）。

---

## 4. 不版控、必須手動帶的機器本地檔

| 檔案 | 說明 |
|---|---|
| `~/Addis/cf-aoi/grab/cam_config.json` | 曝光/增益（目前 6 台，`exposure_us: 70.0`、`gain_raw: 256`）。下排是 L803K，多半要重調 |
| `/etc/default/cfaoi-grab` | `CAM_COUNT`、`RDMA_DEST=192.168.3.1:18515` ← **下 Grab 要改成 spark-03** |
| `/etc/default/cfaoi-agent` | `ROLE=grab`、`PORT=8300`、`ALLOW` 網段清單 |
| `/etc/default/cfaoi-cleanup` | 保留天數、水位保護（可直接照抄） |
| `~/.ssh/` 金鑰與 `config` | GitHub（clone 用）＋ Spark。**金鑰請你自己複製，我不碰** |
| `~/.config/cfaoi/remote_hosts.conf` | 遠端桌面位址表（今天新建） |

`/etc/default/cfaoi-*` 這三個檔由 `scripts/deploy/install_linux_services.sh grab` 產生，產生後再照上表改值即可。

---

## 5. bootstrap **沒有**涵蓋、9/21 之後才加上去的

比對 9/21 快照後新增的套件：`rustdesk`、`samba`、`smbclient`、`chrony`、`nodejs`

| 項目 | 怎麼來的 | 下 Grab 要不要 |
|---|---|---|
| **RustDesk 1.5.0** | `scripts/deploy/setup_rustdesk.sh` | 要（遠端桌面） |
| **samba / smbclient** | 手動裝（與 Windows Control 共享） | 看是否需要 |
| **chrony** | 時間同步 | 建議要 |
| **nodejs** | Claude Code CLI | 選配 |
| **桌面捷徑 4 個** | `cam-align`、`cfaoi-grab`、`cfaoi-upstream-tester`、兩個遠端桌面 | 見下 |
| **`~/.local/bin/`** | `cfaoi-remote`、`cfaoi-upstream-tester` | 要 |
| **systemd 服務 3 個** | `install_linux_services.sh grab` 產生 | 要 |

`cfaoi-grab.desktop` 和 `cam-align.desktop` 由 `install_desktop_launchers.sh grab` 產生；
`cfaoi-remote-spark-0X` 和 `cfaoi-upstream-tester` 是手動放的，要連同 `~/.local/bin/` 那兩支一起複製。

---

## 6. ✅ 複製前要先 push（2026-11-13 已處理）

下 Grab 是用 `git clone` 取得程式碼，**上 Grab 本機沒 push 的東西它一律拿不到**。

2026-11-13 清掉的那一批（原本 `origin/main` 落後 5 筆，其中 11/4 那三筆讓
`tools/upstream_tester/` **整個目錄**在遠端不存在）：

```
785e6f1  docs: 下 Grab 架設清單 + 上 Grab 現況快照
11e10f5  tools/share: Samba 共用資料夾一鍵建置
efe3ea5  tools/upstream_tester: Server 模式自動判斷 XML 框格式
8dce1af  tools/upstream_tester: 修 accept 執行緒被殺掉
4a65b15  tools/upstream_tester: 上位機對測工具完整版 + 一鍵安裝
1f7faa5  tools/upstream_tester: Grab 桌面上位機對測工具
```

> 之後每次要從上 Grab 複製到別台前，先 `git status` + `git log origin/main..HEAD` 確認兩者都是空的。
> （`docs/CLAUDE.md` §10：在 remote 改了 code 就立刻 commit+push，否則視為拋棄。）

---

## 7. IP 位址總表（對應 KVM 接線架構圖的預留欄位）

> 圖：`LCDG85_CF_AOI_KVM_editable.pptx`「LCD G8.5 CF AOI | KVM 接線架構」。
> ✅ = 實機確認或已定案；⚠️ = 待定。

| 架構圖位置 | 位址 | 狀態 |
|---|---|---|
| **Control**（Windows，= 上位機同機）| `192.168.10.1/24`＋持久路由 `route -p add 192.168.3.0 mask 255.255.255.0 192.168.10.21`（不設預設閘道）| ✅ |
| **Grab(上)** 控制網 `enp3s0` | `192.168.10.21/24` | ✅ |
| **Grab(上)** → IP(上)，100G `enp1s0f0np0` | `192.168.3.2/24`，MTU 9000 | ✅ |
| **Grab(上)** → Switch(上)，100G `enp1s0f1np1` | `192.168.5.200/24` + `169.254.0.200/16` + `192.168.4.2/24`，MTU 9000 | ✅ |
| **Switch(上)** | HPE 5945，Grab 接 `HGE1/0/25`；48 個 25G 埠已全部預設 1G；console `/dev/ttyUSB0` 9600 8N1 | ✅ |
| **IP(上)** = spark-02（`spark-3961`）port0 | `192.168.3.1/24`＋回程路由 `192.168.10.0/24 via 192.168.3.2` | ✅ |
| **IP(上)** ↔ **IP(下)** port1 | `192.168.177.12` / `192.168.178.12`，MTU 9000 | ✅ |
| **Camera 01–37**（Basler raL8192）| **CCD01–CCD37** → `192.168.5.nn`（nn = 編號，存在相機 flash）| ✅ |
| **Grab(下)** 控制網 | `192.168.10.22/24` | ✅ |
| **Grab(下)** → IP(下)，100G | **⚠️ 待定** — 建議不要也用 `192.168.3.x`（見 §3） | ⚠️ |
| **Grab(下)** → Switch(下)，100G | iPORT 網段，本機位址比照上排用 `.2`（例：`192.168.4.2/24`）| ✅ |
| **IP(下)** = spark-03（`spark-010f`）控制網 `enP7s7` | `192.168.10.12/24`，不設閘道 | ✅ |
| **IP(下)** port0 | **⚠️ 待定**（同 Grab(下) 的 RDMA 網段，對端）| ⚠️ |
| **IP(下)** ↔ **IP(上)** port1 | `192.168.177.13` / `192.168.178.13`，MTU 9000 | ✅ |
| **Camera 01–18**（L803K 經 iPORT）| **CCD38–CCD55** → `192.168.4.38`–`192.168.4.55` | ✅ 2026-11-13 定案 |

**服務埠**：上位機→Control `8787`；Control→Grab `8100`、→IP `8200`；Grab→IP RDMA `18515`；節點代理 `8300`。

> ⚠️ **架構圖的下側相機標示要改**：圖上寫 `Camera 01–18`，實際編號是 **CCD38–CCD55**（接在上排 37 顆之後連號）。
> 這不只是標示問題——iPORT 的 persistent IP 是 `192.168.4.nn`、**nn = 編號**，照圖上的 01–18 會寫成
> `192.168.4.1–18`，和定案的 `.38–.55` 差一整段，而且 `iport_provision set <SN> CCDnn` 寫進硬體後不好改。
> 程式端已經是這個規則：`provision.default_ip('iport', 38) == '192.168.4.38'`（`tools/cam_align/test_offline.py`）。

> ⚠️ **佈建 CCD53 / CCD54 時注意**：現在台架上那兩顆測試用 iPORT 位址是 `192.168.4.53` / `.54`
> （`tools/cam_align/l800_serial.py`、`bisect_serial.py` 的範例位址）。正式佈建到這兩號時，
> 要先把舊的那顆重新佈建或斷電，否則會短暫撞 IP。`provision.check_conflicts()` 會擋下撞名/撞號。

---

## 8. 建議順序

1. 上 Grab：commit + push（第 6 節）
2. 下 Grab：複製安裝檔（第 1 節）＋ SSH key
3. `git clone git@github.com:AddisLu/cf-aoi.git ~/Addis/cf-aoi`
4. 改 `tools/grab_setup/bootstrap.sh` 的 CONFIG（第 3 節）
5. `./bootstrap.sh --dry-run` 看它要做什麼 → 確認無誤再 `./bootstrap.sh`
6. `scripts/deploy/install_linux_services.sh grab` → 改 `/etc/default/cfaoi-*`
7. `scripts/deploy/install_desktop_launchers.sh grab` → 再補手動的捷徑與 `~/.local/bin/`
8. `setup_rustdesk.sh` → 裝 RustDesk
9. 帶 `cam_config.json`
10. **驗收**：`tools/grab_setup/verify_grab_host.sh`（上 Grab 當時是 10 通過 / 8 失敗，失敗 8 項全是缺網卡的檢查）
11. 再跑一次 `collect_machine_state.sh`，跟上 Grab 的快照 diff 對照
