# CCD 身分解析測試（DeviceUserID 優先）

驗證 `CamManager::resolve()` 的規則：相機 DeviceUserID `CCDnn` > `cam_map.json` MAC 綁定 > 未綁定；
嚴格模式下未綁定、UserID 格式錯、cam_id 重複一律拒開。純邏輯，不需相機。

借用 `b1_fault_containment/pylon_stub` 讓 `cam_manager.cpp`/`cam_pylon.cpp` 能連結（不接進 CMake，理由同 B1）：

```bash
g++ -std=c++17 -Wall -Wextra -pthread \
    -Igrab/test/b1_fault_containment/pylon_stub -Igrab/src \
    grab/test/ccd_identity/ccd_identity_test.cpp grab/src/cam_manager.cpp grab/src/cam_pylon.cpp \
    -o /tmp/ccd_identity_test && /tmp/ccd_identity_test
```

預期 `全數通過`、exit 0（2026-09-17 damac gcc，31 項）。
**不涵蓋**：真 pylon 讀 `GetUserDefinedName`——已於 damac 以 `LIST_CAMERAS` 實機確認 4 台 `bind_source=user_id`。
