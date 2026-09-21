#pragma once
// CamManager — 多相機陣列管理（STATUS #94；docs plan「37 CCD 觸發設計」）
//
// 軟體觸發架構：GRAB_START = 觸發。逐台平行 arm（StartGrabbing），啟動順序/skew 不影響
// 座標精度——同舊系統「逐台 arm + 共用觸發」原則，新架構的啟動時間差由 IP 端玻璃前緣
// 對位（edge_check + per-CCD 對位）吸收，前提是前緣落在第一張（5000 條 = 40mm 窗口）內。
// 每台收滿 frames_per_panel 張自動停（CamPylon::set_max_frames）。
//
// cam_id 來源（2026-09-17 起；Gap #21 的延伸）：
//   ① 相機 DeviceUserID = "CCDnn" → cam_id = nn（**權威**；存在相機 flash，pylon Viewer 直接顯示，
//      換相機只要在 pylon Viewer 設名稱，不必改檔）。
//   ② 否則查 cam_map.json 的 MAC 綁定（過渡/備援）。① ② 衝突時以 ① 為準並印 WARN。
//   任一台有 ① 或存在 cam_map → **嚴格模式**：未綁定、cam_id 重複 → 拒開，
//     不默默佔用槽位（docs/CLAUDE.md 約束②：宣告狀態與偵測狀態不可假 merge）。
//   兩者皆無 → 退回列舉順序暫派 0..N-1 並印 WARN（舊行為；重插拔後 cam_id 會變）。

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
        std::string bind_source;  // "user_id" / "mac" / ""（列舉順序暫派）
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

    // 依身分規則（見檔頭 ①②）把 cam_id/ccd_id/bound/bind_source 填進列舉結果。不開相機。
    // 未綁定：cam_id 維持列舉 index、bound=false（誠實表示「未綁定」）。
    // 回 false = 嚴格模式下不可開陣列（有未綁定相機、或 cam_id 重複），err 說明全部問題。
    // 非嚴格模式（無 UserID 也無映射）一律回 true。
    bool resolve(std::vector<CamInfo>& infos, std::string& err) const;

    // LIST_CAMERAS 用：同 resolve，但只填欄位、不回報錯誤（顯示用途，問題由 ARM 擋）。
    void annotate(std::vector<CamInfo>& infos) const;

    // DeviceUserID 是否為合法 CCD 名稱 "CCDnn"（兩位數，00–99）；是則回 true 並填 cam_id。
    static bool parse_ccd_name(const std::string& s, uint16_t& cam_id);

    // MAC 正規化：去除 ':' '-' '.' 空白後轉大寫（"00:30:53:..." 與 "003053..." 視為同一個）
    static std::string normalize_mac(const std::string& s);

    // 開 want 台（want<=0 = ALL 列舉到的）。任一台開失敗 → 全關、回 false（fail-fast 不半開）。
    // 嚴格模式（見檔頭）：列舉 → resolve 取 cam_id → **依 cam_id 由小到大**取前 want 台。
    //   resolve 不過（未綁定/重複）→ 直接報錯（不默默佔槽）。cli_serial 若非 "auto" 仍可指定單台。
    // 非嚴格：want==1 沿用舊單台語意（依 cli_serial 開）；
    //   want>1 依列舉順序取前 want 台並印 WARN（cam_id 不穩定）。
    bool open_all(int want, const std::string& cli_serial, int64_t pkt_size, std::string& err);

    // 平行啟動全部（每台自帶 grab thread）；max_frames_per_cam=0 → 連續（legacy）。
    // cb 會被 N 個相機 thread 併發呼叫 → 呼叫端負責 thread-safe（RDMA 單 QP 需序列化）。
    void start_all(uint64_t max_frames_per_cam, FrameCb cb);

    // 之後每次開相機（open_all / get_or_open_primary）都套用的 ROI。0 = 不動相機現值。
    void set_roi(Roi roi) { roi_ = roi; }
    // 行速率（Hz）：>0 設為該值；0 不動相機現值；<0 設為節點上限（不設限）。見 CamPylon::open。
    void set_line_rate(double hz) { line_rate_hz_ = hz; }
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

    // ---- B1：故障台彙總（拔線/斷電導致該台 grab thread 中止）----
    // ⚠️ running_count() 遞減有兩種原因：收滿 N 張正常停 vs 故障中止。**兩者外觀相同**，
    //    上位判斷（「全部收完了嗎」）必須先看 faulted_count()==0，否則會把斷線當成正常收完。
    struct Fault {
        uint16_t    cam_id = 0;
        std::string ccd_id;    // 顯示標籤（無映射時為空）
        std::string message;   // 相機層例外訊息
    };
    size_t             faulted_count() const;
    std::vector<Fault> faults() const;

    std::vector<Entry>& entries() { return cams_; }

private:
    std::vector<Entry> cams_;
    std::map<std::string, MacBinding> mac_map_;   // key = normalize_mac(mac)
    // true = 目前 cams_ 是 idle 調參路徑（get_or_open_primary）開的單台，非完整陣列。
    // open_all 看到此旗標一律重開，避免把單台當成整個陣列（靜默少台）。
    bool primary_only_ = false;
    Roi  roi_;
    double line_rate_hz_ = -1;   // 預設不設限（與出廠相機一致）
};
