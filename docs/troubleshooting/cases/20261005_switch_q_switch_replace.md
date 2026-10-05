# 知識題：廠商換來新的 HPE 5945，怎麼設定讓相機正常

- 日期：2026-10-05　來源：機況助手考題（不需注入）　分類：switch
- 正確答案要點：見 docs/troubleshooting_線上異常處理.md 對應段落

## 機況助手表現（0–2 分）
- 只給問題（A，舊知識庫）：**2** — speed 1000 + port-group [Y/N] + stp edged-port + jumbo，引用 runbook（混入舊主機名 damac）
- 問題 + 健檢報告（B）：**—** — 無健檢報告（知識題）

<details><summary>助手 A 回答原文</summary>

根據參考資料，這是一台 HPE FlexFabric 5945（CFAOI-SW1），以下是完整的設定步驟。

## 結論

交換機是 **CFAOI-SW1（HPE FlexFabric 5945）**，接回後要設 **埠 speed**、**stp**、**damac 網卡 IP/MTU**，才能讓相機經 100G 上行到 damac。

---

## 必要設定步驟（可照順序做）

### 1️⃣ 交換機本體設定（對 console 下指令，console 在 damac `/dev/ttyUSB0`，9600 8N1）

每插一台相機，該埠就需要設定。**2026-09-17 起全部 48 個 25G 埠已預設好**，但如果新機是出廠值，要手動做：

**每個相機插的 WGE 埠**（建議插 WGE1/0/33–38）：
```
interface WGE1/0/33
 speed 1000
 stp edged-port
 quit
 save force
```

> ⚠️ **25G SFP28 埠插 1G 銅纜模組，auto-neg 永不 link，一定手動下 `speed 1000`**，否則 link DOWN。 [4]
>
> ⚠️ **`speed 1000` 有 port-group 連動**（四埠一組：1–4、5–8…33–36、37–40…），下指令會問 `[Y/N]`，要回 **Y** 否則該組沒生效。 [4]

**100G 上行埠**（到 damac ConnectX-5 f1）：
```
interface HGE1/0/25
 quit
```
（jumbo 原廠已有 9416，不必再設）[4]

**驗證**：`display interface brief | include WGE` 看該埠應顯示 **UP 1000Mbps/F**。

---

### 2️⃣ damac 網路設定（需 root 權限）

damac 的 `enp1s0f1np1`（ConnectX-5 f1，MAC 尾 …:CC:19）要設相機同網段 IP：
```
nmcli conn mod enp1s0f1np1 ipv4.addresses 192.168.5.200/24
nmcli conn mod enp1s0f1np1 ipv4.method manual
nmcli conn mod enp1s0f1np1 802-3-ethernet.mtu 9000
nmcli conn up enp1s0f1np1
```

`enp1s0f0np0`（直連 Spark 的 RDMA 路徑，MAC 尾 …:CC:18）MTU 也拉到 9000：
```
nmcli conn mod enp1s0f0np0 802-3-ethernet.mtu 9000
nmcli conn up enp1s0f0np0
```

> ⚠️ **兩條線不要接反**：f0（…:CC:18）→ Spark DAC 直連、f1（…:CC:19）→ 5945 `HGE1/0/25`。接反相機與 RDMA 會對調 [6]。

---

### 3️⃣ 確認相機可達

```
ping 192.168.5.1
```
應 0% loss（CCD01 的 IP = 192.168.5.1）。

```
LIST_CAMERAS
```
應看到該台相機 `online=true`。

---

## 怎麼確認修好

1. 交換機 `display interface brief | include WGE` — 相機埠 **UP 1000M**，HGE1/0/25 **UP 100G**
2. damac `ping 192.168.5.1` — 0% loss，rtt < 1ms
3. `LIST_CAMERAS` — 該台相機 `online=true`
4. 起 grab 時要用 `--cam-count 6`，不要用 `ALL`（B4 已知缺陷）[2]

</details>

<details><summary>助手 B 回答原文</summary>

（無）

</details>
