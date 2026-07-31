# 部署（線上人員操作便利化）

三套軟體的「免終端機」啟動方式。目標：操作員只碰 **Mac 桌面的 CF-AOI Control**；
damac/Spark 的 Grab/IP 做成 systemd 服務，**開機自動起、崩潰自動重啟**，平時無人碰。

## Mac — CF-AOI Control.app（操作員主介面）

```bash
bash scripts/deploy/make_mac_app.sh          # 產出 ~/Desktop/CF-AOI Control.app
```

- 雙擊即開，self-contained（不需安裝 .NET）。可拖進 Dock。
- 連線設定在 app 內：`CF-AOI Control.app/Contents/MacOS/appsettings.json`
  （改 IP/Grab 位址、`Grab.FramesPerPanel` 後重開 app 即生效）。
- 程式碼更新後重跑 `make_mac_app.sh` 重新打包即可（會覆蓋舊 app）。

## Spark — IP 服務

```bash
cd ~/Addis/cf-aoi && bash scripts/deploy/install_linux_services.sh ip
```

- 預設啟用 `cfaoi-ip-offline`（offline-tcp，Step 1 調參，開機自啟）。
- 進 Step 4/5 生產時切換（兩者互斥，systemd `Conflicts=` 自動擋）：
  ```bash
  sudo systemctl disable --now cfaoi-ip-offline
  sudo systemctl enable  --now cfaoi-ip-production   # rdma-process；Grab 斷線退出後自動重生
  ```
- 輸出：`~/cfaoi_output`；log：`journalctl -u cfaoi-ip-offline -f`。

## damac — Grab 服務

```bash
cd ~/Addis/cf-aoi && bash scripts/deploy/install_linux_services.sh grab   # sudo 需輸入密碼
```

- `cfaoi-grab` 開機自啟；cam_config/cam_map 路徑已錨定 `grab/`（不受服務 CWD 影響）。

## 注意

- **rdma-process 服務 `Restart=always` 是刻意的**：Grab 斷線（每片結束/GRAB_STOP）IP 依設計退出，
  systemd 自動重生 = 下一片 session 自動就緒。
- Grab/IP 版本更新流程：`git sync` → 重編 → `sudo systemctl restart <服務>`。
- 疑似 damac 網路上行接在 5945 上（2026-07-31 關交換機後 Tailscale 失聯）——若屬實，
  damac 的服務化要等交換機供電才可遠端管理；建議管理網改走獨立路徑。
