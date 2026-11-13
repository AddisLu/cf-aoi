# Windows 連不到共用資料夾 \\192.168.1.136\share → smb.conf 的 hosts allow/deny 把全部連線擋掉

- 日期：2026-11-04　來源：實驗室（新建 Samba 共用資料夾給 Windows 放檔案）　機台：user-IMB-M47 + Windows `desktop-q1flbom`（192.168.1.135）
- 分類：host / 網路分享　嚴重度：提示（新功能建置期，未影響產線）
- 關鍵字：samba、smb、hosts allow、hosts deny、CIDR、log level、NT_STATUS、guest、AllowInsecureGuestAuth、445

## 線上人員看到的（原話）
> 目前還是連不到喔

> Windows無法存取\\192.168.1.136\share

## 證據

關鍵線索在 `/var/log/samba/` 的檔案 mtime，不是 log 內容：

```
drwx------ ... 18:04:23 cores
-rw-r--r-- ... 18:04:36 log.192.168.1.135      ← 0 bytes
-rw-r--r-- ... 18:04:36 log.desktop-q1flbom    ← 0 bytes
-rw-r--r-- ... 18:06:26 log.nmbd
-rw-r--r-- ... 18:09:13 log.smbd               ← 內容只有 DBus warning
```

時間軸：

| 時間 | 事件 | 結果 |
|---|---|---|
| 18:04:36 | samba 剛裝好（**預設設定**），Windows 連進來 | 連線成立，留下 `log.<ip>` 與 `log.<netbios名>`；但當時還沒定義 `[share]`，所以使用者看到失敗 |
| 18:06:26 | 寫入 `[share]` 並**加上 `hosts allow` / `hosts deny`** 後重啟 | — |
| 18:06～18:12 | 使用者多次重試 | **log 檔 mtime 完全沒更新、`ss` 看不到任何 445 連線** → 封包沒進到 smbd |

當時加的設定：

```
hosts allow = 127.0.0.1 192.168.0.0/16 172.16.0.0/12 10.0.0.0/8 169.254.0.0/16 100.64.0.0/10
hosts deny = 0.0.0.0/0
```

移除這兩行並重啟後，本機實測立刻全部通過：

```
smbclient -L 127.0.0.1 -N
        share           Disk      cf-aoi shared folder

smbclient //127.0.0.1/share -N -c "put /tmp/smbt.txt guest_test.txt; ls"
putting file /tmp/smbt.txt as \guest_test.txt (10.7 kb/s)
  guest_test.txt                      A       11  Wed Nov  4 18:15:10 2026
```

## 根因

`hosts allow` / `hosts deny` 的組合把連線擋在 SMB 交握之前。
**最大的陷阱是它不留痕跡**：Samba 的拒絕訊息 `Denied connection from ...` 記在
`log level = 2`，而預設是 `0`，所以 log 看起來完全乾淨。

這直接導致誤判：看到「log 沒有拒絕紀錄」就推論「沒被擋，所以是 Windows 端擋 guest」，
把方向帶到 Windows 登錄檔去了。**log 乾淨 ≠ 沒被擋，要先確認該事件的 log level。**

## 正確處理

1. 從 smb.conf 移除 `hosts allow` / `hosts deny`。這台沒有任何公網 IP，SMB 碰不到外網，
   為了一點硬化風險把服務弄掛不值得。
2. 要限制範圍改用語意明確的介面綁定：

   ```
   interfaces = lo enp4s0
   bind interfaces only = yes
   ```

3. 排錯期把 `log level = 2` 開起來，否則這類拒絕查不到。

## 教訓

- **先用本機 `smbclient` 自我測試**，區分伺服器端 vs 客戶端，再去動 Windows。
  少了這一步，前面整段都在猜。本機測試要 `smbclient` 套件（`samba` 不含它）。
- **log 檔的 mtime 比 log 內容更早告訴你「封包有沒有進來」**。
- Samba 的 ACL 設定 `hosts allow`/`hosts deny` 寧可不用；想限制就綁介面。
- `set -e` + `set -o pipefail` 的腳本裡不要寫 `tr -dc ... </dev/urandom | head -c N`：
  `head` 取滿就關管線，`tr` 吃 SIGPIPE 回傳非零，整支腳本會**安靜地**中止。
  改用 `python3 -c "import secrets..."`。
- `awk '/密碼/'` 會誤中含「免密**碼**」的說明文字行，把兩行黏成多行密碼
  → `NT_STATUS_LOGON_FAILURE`。取值要用 `sed -n 's/^ *密碼 *: *//p'` 這種錨定寫法。

## 續篇：移除 ACL 後 Windows 仍然「無法存取」→ 客戶端拒絕 guest 工作階段

`log level = 2` 打開後，`log.192.168.1.135` 給出 Windows 實際送了什麼：

```
Auth: [SMB2,(null)] user [DESKTOP-Q1FLBOM]\[User] with [NTLMv2]
      status [NT_STATUS_NO_SUCH_USER] workstation [DESKTOP-Q1FLBOM]
      remote host [ipv4:192.168.1.135:62853] local host [ipv4:192.168.1.136:445]
```

Windows **不會主動用 guest**，它先拿目前登入的本機帳號（這台叫 `User`）去試 NTLMv2。

`NT_STATUS_NO_SUCH_USER` 看起來像兇手，但**它不是**。從本機模擬同一個情境：

```
smbclient //192.168.1.136/share -U "User%whatever" -c "ls"   → 成功列出目錄
smbclient //192.168.1.136/share -U "User%whatever" -W DESKTOP-Q1FLBOM -c "ls" → 成功
smbclient //192.168.1.136/share -N -c "ls"                   → 成功
```

`map to guest = Bad User` 正常運作：帳號不存在就映射成 guest，伺服器照樣發給工作階段。
那行 `NO_SUCH_USER` 只是認證模組第一階段的紀錄，不等於連線失敗。

**所以根因在客戶端**：伺服器發回的是 guest 工作階段，而 Windows 10/11 的
`LanmanWorkstation` 預設拒絕未認證的 guest 連線（Windows 11 24H2 之後又強制 SMB 簽章，
guest 工作階段無法簽章），於是顯示一個沒有細節的「無法存取」。

### 解法

強制帶帳號連線，不要讓 Windows 自己決定身分：

```
net use * /delete
net use Z: \\192.168.1.136\share /user:cfaoi * /persistent:yes
```

真要免密碼，得在每一台 Windows 開 `AllowInsecureGuestAuth`（見
`tools/share/README.md`）。伺服器端不必再動。

### 教訓（續）

- **auth log 裡的 `NT_STATUS_*` 不一定是失敗原因。** `map to guest` 會在那之後接手。
  要判斷伺服器到底給不給連，用 `smbclient` 把客戶端的情境原樣重現一次最快。
- Windows 送的是**目前登入的本機帳號名**，不是 guest。所以「分享設成 guest ok」
  不等於「Windows 連得進來」，這兩件事中間還隔著客戶端政策。

## 相關

- 建置腳本與使用說明：`tools/share/setup_smb_share.sh`、`tools/share/README.md`
- 待確認：Windows 10/11 預設拒絕 guest（24H2 之後又強制 SMB 簽章），
  伺服器端 guest 已通，但客戶端是否放行尚未驗證；備援帳號 `cfaoi` 已備好。
