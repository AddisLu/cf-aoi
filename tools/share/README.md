# cf-aoi 共用資料夾（Samba / Windows 網路磁碟）

讓 Windows 電腦把檔案放進這台 Linux（`user-IMB-M47`）。

| 項目 | 值 |
|---|---|
| 分享名稱 | `share` |
| Linux 路徑 | `/home/user/share` |
| 連線方式 | **用 `cfaoi` 帳號**（guest 伺服器端通，但 Windows 客戶端拒收） |
| 密碼 | 由 Addis 自行設定；忘記就 `sudo smbpasswd cfaoi` 重設 |
| 帳號說明檔 | `/home/user/cfaoi-smb-account.txt`（chmod 600） |
| 權限 | 可讀寫；檔案一律歸 `user`（guest 寫入 664、帳號寫入 644，目錄 775） |
| 刪除保護 | 刪掉的檔案進 `.recycle/`（保留目錄結構與版本） |

## 建置 / 重建

```bash
sudo bash tools/share/setup_smb_share.sh
```

可重複執行（idempotent）：會覆寫 `smb.conf` 裡 `# >>> cf-aoi share >>>` 區塊，
原始設定備份在 `/etc/samba/smb.conf.orig`。

## Windows 端怎麼連

本機有多個網段，Windows 用**和它同網段**的那個 IP：

| 介面 | IP | 用途 |
|---|---|---|
| `enp4s0` | 192.168.1.136 | 辦公/一般網路（已確認 Windows 走這條） |
| `enx462915a23ad2` | 192.168.0.144 | USB 網卡 |
| `enp3s0` | 192.168.10.21 | 控制網（cf-ctrl，目前 DOWN） |
| `tailscale0` | 100.127.60.103 | 遠端 |

檔案總管位址欄輸入（例）：

```
\\192.168.1.136\share
```

**實測結論：Windows 10/11 要用 `cfaoi` 帳號，免密碼那條走不通**（伺服器端 guest 是通的，
是 Windows 客戶端拒絕 guest 工作階段，詳見 `docs/troubleshooting/cases/20261104_smb_hosts_allow_blocks_windows.md`）。

Windows 不會主動用 guest，它會拿目前登入的本機帳號去試，失敗後直接顯示「無法存取」
而且**不跳帳密視窗**。所以要用 `net use` 強制指定帳號，見下。

### 密碼

現場要打的密碼請設成好記、好念的（純小寫+數字，避開 `O`/`0`、`l`/`1`）。
不需要知道舊密碼就能重設：

```bash
sudo smbpasswd cfaoi
```

腳本首次建帳號時會產生 `cf-xxxxxxxx` 格式的隨機密碼（已排除易混淆字元）
並寫入 `/home/user/cfaoi-smb-account.txt`；那只是個起始值，照上面重設掉即可。

在 Windows cmd 掛成 `Z:` 磁碟機（`*` 會互動問密碼，不留在命令歷史）：

```
net use * /delete
```

```
net use Z: \\192.168.1.136\share /user:cfaoi * /persistent:yes
```

第一行清掉 Windows 快取的失敗連線——不清的話它會一直重播舊結果。

### 為什麼免密碼走不通

伺服器端 guest 讀寫已實測通過（`smbclient //127.0.0.1/share -N`）。但 **Windows 10/11
預設拒絕免密碼（guest）連 SMB**，Windows 11 24H2 之後又強制 SMB 簽章而 guest 工作階段
無法簽章，所以可能被客戶端擋掉，訊息通常是：

> 您無法存取此共用資料夾，因為貴組織的安全性原則封鎖未經驗證的來賓存取

真的要免密碼，得在**每一台** Windows 用系統管理員身分的 cmd 跑：

```
reg add "HKLM\SYSTEM\CurrentControlSet\Services\LanmanWorkstation\Parameters" /v AllowInsecureGuestAuth /t REG_DWORD /d 1 /f
```

然後 `net use * /delete` 清掉舊連線再重連。每台 Windows 都要做一次，所以嫌麻煩就直接用 `cfaoi` 帳號。

## 檢查 / 排錯

```bash
systemctl status smbd --no-pager
smbclient -L localhost -N
tail -50 /var/log/samba/log.smbd
```

本機自我測試（不經 Windows，用來區分伺服器端還是客戶端的問題）：

```bash
smbclient -L 127.0.0.1 -N
```

```bash
smbclient //127.0.0.1/share -N -c "put /etc/hostname t.txt; ls; rm t.txt"
```

### 注意事項

- **不要加 `hosts allow` / `hosts deny`。** 曾經加了
  `hosts allow = ... 192.168.0.0/16` + `hosts deny = 0.0.0.0/0`，結果 Windows 整個連不進來，
  而且 Samba 的拒絕訊息只在 `log level = 2` 以上才記錄，log 看起來一片乾淨，非常難查。
  要限制範圍請改用語意明確的：

  ```
  interfaces = lo enp4s0
  bind interfaces only = yes
  ```

  這台沒有公網 IP，SMB 本來就碰不到外網。
- 最低協定 SMB2（SMB1 已關，Windows 11 本來也不支援）。
- 目前 `log level = 2` 是為了排錯開的；穩定後可以拿掉以免 log 長太快。
- 自動產生密碼時**不要用 `string.ascii_letters`**：`O`/`0`、`l`/`1`、`U`/`V` 現場打不出來。
  腳本已改成只用 `abcdefghjkmnpqrstwxyz23456789`。
- 實測 guest 寫入的檔案是 664，用 `cfaoi` 帳號寫入的是 644（`force create mode = 0664`
  對已認證的工作階段沒生效，原因未查）。因為 `force user = user`，兩者都歸 `user`、
  本機處理不受影響，所以沒再追。若之後需要群組可寫，這裡要再查。
- samba 套件會順手 enable `samba-ad-dc`（網域控制站），這裡用不到，腳本已將它 disable。
