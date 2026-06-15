#ifndef CFAOI_FLIGHT_RECORDER_H
#define CFAOI_FLIGHT_RECORDER_H

/**
 * ============================================================================
 * FlightRecorder — 行車紀錄（結構化診斷日誌 / 黑盒子）
 * ============================================================================
 *
 * 目的：FAB 離線環境，產線出事時自動記錄「當下完整現場」，工程師不必重現即可
 *       事後診斷。輸出人機皆可讀：
 *         <output>/_diag/<yyyyMMdd>.jsonl    每事件一行 compact JSON（RAG/jq 友善）
 *         <output>/_diag/incident_<ts>.json  完整現場 pretty-print（indent=2）
 *
 * 設計（見 docs plan「IP 行車紀錄」）：
 *   - 環形緩衝 64 張 + 只記出事：平時零磁碟 I/O，出事才 flush 最近 N 張 + 當下現場。
 *   - 跨執行緒抓現場：set_scene 把指標 release-store 進穩定 ring slot，fatal handler
 *     （atexit / set_terminate）acquire-load 跨執行緒讀 latest_（不靠脆弱的 thread_local）。
 *   - single-writer：只有主/GPU 執行緒寫 ring（set_scene/record_frame）；ring_mtx_ 守
 *     writer vs fatal-reader，fatal 端 try_lock，失敗仍 dump latest_（恆可得）。
 *   - 純觀測：不碰演算法/缺陷陣列/排序，不影響 bit-exact 決定性與 gpu_ms。
 *   - bench 不呼叫 begin_session → enabled_=false → 所有方法 no-op（gpu_ms 零擾動）。
 *
 * 本 header 不依賴 CUDA（純 CPU 型別）；GPU 資訊查詢在 .cpp 內以 cuda_runtime 完成。
 * ============================================================================
 */

#include <array>
#include <atomic>
#include <cstdint>
#include <mutex>
#include <string>
#include <vector>

namespace diag {

// 單一 zone 的參數 + 該 zone 缺陷數（出事時還原當下用哪份參數）。
struct ZoneSnap {
    int   zone_index = 0;
    float BTH = 0.f, DTH = 0.f;
    int   pitch_x = 0, pitch_y = 0;
    int   search_x = 0, search_y = 0;
    int   roi_x1 = 0, roi_y1 = 0, roi_x2 = 0, roi_y2 = 0;
    int   defects = 0;
};

// 一張影像（panel）的完整現場。
struct FrameScene {
    std::string           panel_id;
    uint64_t              frame_seq = 0;
    uint16_t              cam_id = 0;
    int                   width = 0, height = 0;
    std::vector<ZoneSnap> zones;
    double                gpu_ms = 0.0;
    int                   num_defects = 0, num_bright = 0, num_dark = 0;
    bool                  pass = true;
    bool                  complete = false;  // set_scene→false(僅參數); record_frame→true(含結果)
};

// 啟動快照（非 CUDA 欄位；GPU name/sm/memory 由 .cpp 查詢補上）。
struct SessionInfo {
    std::string mode;        // offline-file / offline-tcp / ...
    std::string ip_name;     // 缺陷檔名 IpName
    std::string ini;         // INI 路徑
    std::string recipe;      // recipe 路徑 / "(inline xml)" / ""
    std::string output_dir;  // _diag/ 落在 <output_dir>/_diag/
    bool        zero_copy = false;
    bool        ai_active = false;
};

class FlightRecorder {
public:
    static FlightRecorder& instance();

    // 啟動：建 _diag/、寫 session 一行、註冊 atexit/set_terminate。bench 不呼叫 → 全程 no-op。
    void begin_session(const SessionInfo& info);

    // process_image 入口：把「僅參數」現場寫進新 ring slot 並發布 latest_
    //（使 CUDA fatal 在 process_frame 中途、結果未生成時也抓得到當下 panel/zone 參數）。
    void set_scene(const FrameScene& scene);

    // process_image 結尾：把結果補進 set_scene 用的同一 slot（complete=true）。在 timed region 外呼叫。
    void record_frame(const FrameScene& scene);

    // 出事：flush 最近 N 張 ring + 當下現場 → incident_<ts>.json + jsonl 索引一行 + console banner。
    // 可重入（recoverable incident 如 frame_validation/bad_json 可多次）。
    void record_incident(const std::string& kind, const std::string& detail,
                         const std::string& stack = "");

    bool enabled() const { return enabled_.load(std::memory_order_acquire); }

private:
    FlightRecorder() = default;
    FlightRecorder(const FlightRecorder&) = delete;
    FlightRecorder& operator=(const FlightRecorder&) = delete;

    // fatal handler 進入點（atexit / set_terminate）——只 dump 一次。
    void on_atexit();
    void on_terminate();
    void dump_fatal_once(const std::string& kind, const std::string& detail,
                         const std::string& stack);

    static constexpr size_t kRingSize = 64;

    std::atomic<bool> enabled_{false};
    SessionInfo       session_;

    std::mutex                       ring_mtx_;       // single-writer + fatal try_lock
    std::array<FrameScene, kRingSize> ring_;
    size_t                           head_ = 0;       // 下一個要寫的 slot
    size_t                           count_ = 0;      // 已寫入筆數（<= kRingSize）
    size_t                           cur_slot_ = 0;   // set_scene 寫的 slot（record_frame 補同一格）
    std::atomic<const FrameScene*>   latest_{nullptr};

    std::atomic<bool> fatal_dumped_{false};           // 避免 atexit/terminate 重複 dump
};

}  // namespace diag

#endif  // CFAOI_FLIGHT_RECORDER_H
