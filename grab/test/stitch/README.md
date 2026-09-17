# 拼接測試（GigE 單幀行數上限 → grab 端拼接）

raL8192 單幀最多 3573 行（寬 8192），`CamPylon` 依 `--height` 把 k 張相機幀拼成一張送出。
本測試用 b1 的 pylon stub 以腳本逐張交幀，驗：拼接張數推導、拼接順序、
**拼到一半掉幀（BlockID 缺口 / skipped / GrabFailed）→ 半張作廢重對齊**、
frames_per_panel 以送出幀計、幀大小不符 = 故障、不拼接時行為不變。不需相機。

```bash
g++ -std=c++17 -Wall -Wextra -pthread \
    -Igrab/test/b1_fault_containment/pylon_stub -Igrab/src \
    grab/test/stitch/stitch_test.cpp grab/src/cam_pylon.cpp \
    -o /tmp/stitch_test && /tmp/stitch_test
```

預期 `全數通過`、exit 0（2026-09-17 damac gcc，22 項）。
反向對照：把 `grab_loop_body` 的「`lost > 0 && parts > 0` → 作廢」拿掉，3 項 FAIL（確認測試抓得到）。
**不涵蓋**：真相機 BlockID 行為——實機已驗 SN25564093 連續 3 張 8192×5000 dropped=0。
