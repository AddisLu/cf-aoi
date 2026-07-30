#pragma once
// CamManager — 多相機陣列管理（STATUS #94；docs plan「37 CCD 觸發設計」）
//
// 軟體觸發架構：GRAB_START = 觸發。逐台平行 arm（StartGrabbing），啟動順序/skew 不影響
// 座標精度——同舊系統「逐台 arm + 共用觸發」原則，新架構的啟動時間差由 IP 端玻璃前緣
// 對位（edge_check + per-CCD 對位）吸收，前提是前緣落在第一張（5000 條 = 40mm 窗口）內。
// 每台收滿 frames_per_panel 張自動停（CamPylon::set_max_frames）。
//
// cam_id 來源（Gap #21，2026-07-30 落地）：
//   有 cam_map.json → **MAC 穩定映射**（權威）。列舉到但未列於映射的相機一律拒開，
//     不默默佔用槽位（docs/CLAUDE.md 約束②：宣告狀態與偵測狀態不可假 merge）。
//   無 cam_map.json → 退回列舉順序暫派 0..N-1 並印 WARN（舊行為；重插拔後 cam_id 會變）。

#include "cam_pylon.h"

#include <map>
#include <memory>
#include <string>
#include <vector>

class CamManager {
public:
    struct Entry {
        std::unique_ptr<CamPylon> cam;
        uint16_t    cam_id = 0;
        std::string serial;   // 開機時鎖定的序號
        std::string mac;      // 列舉快照的 MAC（cam_map.json 的比對鍵）
        std::string ccd_id;   // 顯示標籤（例 CCD00）；未綁定或無映射時為空
    };

    // cam_map.json 的一筆綁定
    struct MacBinding {
        uint16_t    cam_id = 0;
        std::string ccd_id;
    };

    ~CamManager() { stop_all(); }

    // ---- Gap #21：MAC ↔ cam_id 穩定映射 -------------------------------------
    // 載入映射檔。檔案不存在 → 回 true 且 has_map()==false（退回舊行為，warn 帶提示）。
    // 檔案存在但格式錯/cam_id 重複/MAC 重複 → 回 false（err 說明；**不可**默默當成沒映射，
    // 否則等於在生產機上悄悄退回不穩定的列舉順序）。
    bool load_map(const std::string& path, std::string& err, std::string& warn);
    bool has_map() const { return !mac_map_.empty(); }
    size_t map_size() const { return mac_map_.size(); }

    // 寫入映射檔並立即重載（SET_CAM_MAP）。entries 需為 {mac, cam_id, ccd_id} 的 JSON 陣列字串。
    // 驗證規則**與 load_map 完全共用**（先寫暫存檔→試 load→過了才 rename），
    // 避免出現「寫得進去但下次開機載不起來」的兩套標準。
    // ⚠️ 呼叫端必須確保**非取像中**：改映射 = 改相機身分，取像途中換等於資料對錯台。
    bool write_map(const std::string& path, const std::string& entries_json,
                   std::string& err);

    // 依映射把 cam_id/ccd_id/bound 填進列舉結果（LIST_CAMERAS 用；不開相機）。
    // 無映射時：cam_id 維持列舉 index、bound=false（誠實表示「未綁定」）。
    void annotate(std::vector<CamInfo>& infos) const;

    // MAC 正規化：去除 ':' '-' '.' 空白後轉大寫（"00:30:53:..." 與 "003053..." 視為同一個）
    static std::string normalize_mac(const std::string& s);

    // 開 want 台（want<=0 = ALL 列舉到的）。任一台開失敗 → 全關、回 false（fail-fast 不半開）。
    // 有映射（has_map()）：列舉 → 依 MAC 查 cam_id → **依 cam_id 由小到大**取前 want 台。
    //   未列於映射的相機 → 直接報錯（不默默佔槽）。cli_serial 若非 "auto" 仍可指定單台。
    // 無映射：want==1 沿用舊單台語意（不列舉，直接依 cli_serial 開）；
    //   want>1 列舉後依序取前 want 台並印 WARN（cam_id 不穩定）。
    bool open_all(int want, const std::string& cli_serial, int64_t pkt_size, std::string& err);

    // 平行啟動全部（每台自帶 grab thread）；max_frames_per_cam=0 → 連續（legacy）。
    // cb 會被 N 個相機 thread 併發呼叫 → 呼叫端負責 thread-safe（RDMA 單 QP 需序列化）。
    void start_all(uint64_t max_frames_per_cam, FrameCb cb);
    void stop_all();   // 停 thread + 關相機 + 清列表

    size_t    size()  const { return cams_.size(); }
    bool      empty() const { return cams_.empty(); }
    CamPylon* get(int cam_id);
    CamPylon* primary() { return cams_.empty() ? nullptr : cams_.front().cam.get(); }

    // idle 調參路徑（TUNE_MEAN / GET_CAM_NODES）：尚無相機時開單台（舊語意），有則回第一台。
    CamPylon* get_or_open_primary(const std::string& cli_serial, int64_t pkt_size);

    int64_t  max_payload() const;      // 所有台最大 PayloadSize（RDMA frame_cap 用）
    uint64_t total_grabbed() const;
    uint64_t total_dropped() const;
    size_t   running_count() const;    // 仍在取像的台數（收滿自動停後遞減）

    std::vector<Entry>& entries() { return cams_; }

private:
    std::vector<Entry> cams_;
    std::map<std::string, MacBinding> mac_map_;   // key = normalize_mac(mac)
    // true = 目前 cams_ 是 idle 調參路徑（get_or_open_primary）開的單台，非完整陣列。
    // open_all 看到此旗標一律重開，避免把單台當成整個陣列（靜默少台）。
    bool primary_only_ = false;
};
