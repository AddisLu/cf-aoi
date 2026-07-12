// ═══ 📖 手冊對照（docs/html/cf-aoi-training.html，開啟後 ⌘K 搜章節）═══
// [手冊 ch6] 行車記錄器全章：jsonl 五行型/十種 incident/環形緩衝動畫（蒸發/落地/節流）
// [手冊 p2] 逐 kind 破案卡 / Log 分析器（頂部導覽 🩺）會解析本檔輸出
// [手冊 p3] verify_flight_v2.py（11 項）+ verify_flight_src.py（9 項）回歸
// ═══════════════════════════════════════════════════════════════
/**
 * FlightRecorder 實作 — 見 flight_recorder.h 的設計說明。
 */

#include "diag/flight_recorder.h"

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <exception>
#include <filesystem>
#include <algorithm>
#include <fstream>
#include <iostream>
#include <mutex>
#include <thread>

#include <cuda_runtime.h>

#include <nlohmann/json.hpp>

namespace fs = std::filesystem;
using json = nlohmann::json;

namespace diag {

namespace {

// 本地時間字串。kind=0 → ISO8601(含毫秒) 給 JSON；kind=1 → yyyyMMdd 給日檔名；
// kind=2 → yyyyMMdd_HHMMSS_mmm 給 incident 檔名。
std::string time_str(int kind) {
    using namespace std::chrono;
    auto now = system_clock::now();
    std::time_t t = system_clock::to_time_t(now);
    long ms = (long)(duration_cast<milliseconds>(now.time_since_epoch()).count() % 1000);
    std::tm tm{};
    localtime_r(&t, &tm);
    char buf[64];
    if (kind == 1) {
        std::strftime(buf, sizeof(buf), "%Y%m%d", &tm);
        return buf;
    }
    if (kind == 2) {
        std::strftime(buf, sizeof(buf), "%Y%m%d_%H%M%S", &tm);
        char out[80];
        std::snprintf(out, sizeof(out), "%s_%03ld", buf, ms);
        return out;
    }
    std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%S", &tm);
    char out[80];
    std::snprintf(out, sizeof(out), "%s.%03ld", buf, ms);
    return out;
}

// 把 __FILE__（可能為絕對/建置路徑）正規化成 repo 相對位置（如 "ip/src/x.cpp:503"），
// 讓 incident 的 "src" 與機器無關；檢視器再依本機 repo 根目錄組 vscode://file 連結。
// 依序嘗試錨點 "cf-aoi/" → "ip/src/"，皆無則退回 basename。輸入含結尾 ":行號"，原樣保留。
std::string repo_relative(const std::string& p) {
    if (p.empty()) return p;
    auto pos = p.rfind("cf-aoi/");
    if (pos != std::string::npos) return p.substr(pos + 7);
    pos = p.find("ip/src/");
    if (pos != std::string::npos) return p.substr(pos);
    pos = p.find_last_of('/');
    return (pos == std::string::npos) ? p : p.substr(pos + 1);
}

// 查詢 GPU 靜態 + 即時記憶體狀態（出事時尤其想知道是否 OOM）。失敗的欄位留預設。
json gpu_info_json() {
    json g;
    int dev = 0;
    if (cudaGetDevice(&dev) == cudaSuccess) {
        cudaDeviceProp prop{};
        if (cudaGetDeviceProperties(&prop, dev) == cudaSuccess) {
            g["name"] = prop.name;
            g["sm"] = prop.major * 10 + prop.minor;
            g["integrated"] = (bool)prop.integrated;
        }
    }
    size_t free_b = 0, total_b = 0;
    if (cudaMemGetInfo(&free_b, &total_b) == cudaSuccess) {
        g["free_mb"] = (uint64_t)(free_b / (1024 * 1024));
        g["total_mb"] = (uint64_t)(total_b / (1024 * 1024));
    }
    return g;
}

// 完整現場（含各 zone 參數）。
json scene_full_json(const FrameScene& s) {
    json j;
    j["panel_id"] = s.panel_id;
    j["frame_seq"] = s.frame_seq;
    j["cam_id"] = s.cam_id;
    j["width"] = s.width;
    j["height"] = s.height;
    j["complete"] = s.complete;
    json zs = json::array();
    for (const auto& z : s.zones) {
        zs.push_back({{"zone_index", z.zone_index},
                      {"algo_mode", z.algo_mode},
                      {"BTH", z.BTH}, {"DTH", z.DTH},
                      {"pitch_x", z.pitch_x}, {"pitch_y", z.pitch_y},
                      {"search_x", z.search_x}, {"search_y", z.search_y},
                      {"roi", {z.roi_x1, z.roi_y1, z.roi_x2, z.roi_y2}},
                      {"pitch_times", z.pitch_times}, {"choose_amount", z.choose_amount},
                      {"mean_low", z.mean_low_threshold},
                      {"multiscale", z.enable_multiscale},
                      {"blob", {z.blob_min, z.blob_max, z.blob_merge}},
                      {"smooth3x3", z.smooth_times2},
                      {"remap", z.preproc_remap}, {"lsc", z.lsc},
                      {"defects", z.defects}});
    }
    j["zones"] = std::move(zs);
    j["gpu_ms"] = s.gpu_ms;
    j["num_defects"] = s.num_defects;
    j["num_bright"] = s.num_bright;
    j["num_dark"] = s.num_dark;
    j["pass"] = s.pass;
    if (s.queue_depth >= 0) j["queue_depth"] = s.queue_depth;
    return j;
}

// 精簡現場（recent_frames 用）。
json scene_compact_json(const FrameScene& s) {
    return json{{"panel_id", s.panel_id}, {"frame_seq", s.frame_seq},
                {"num_defects", s.num_defects}, {"gpu_ms", s.gpu_ms},
                {"pass", s.pass}, {"complete", s.complete}};
}

}  // namespace

FlightRecorder& FlightRecorder::instance() {
    static FlightRecorder rec;
    return rec;
}

void FlightRecorder::begin_session(const SessionInfo& info) {
    session_ = info;

    // 建 _diag/ 並寫 session 一行。
    try {
        fs::path diag_dir = fs::path(session_.output_dir) / "_diag";
        fs::create_directories(diag_dir);
        json s;
        s["type"] = "session";
        s["ts"] = time_str(0);
        s["mode"] = session_.mode;
        s["ip_name"] = session_.ip_name;
        s["ini"] = session_.ini;
        s["recipe"] = session_.recipe.empty() ? "(none)" : session_.recipe;
        s["zero_copy"] = session_.zero_copy;
        s["ai_active"] = session_.ai_active;
        s["gpu"] = gpu_info_json();
        std::ofstream f(diag_dir / (time_str(1) + ".jsonl"), std::ios::app);
        if (f) f << s.dump() << "\n";
    } catch (const std::exception& e) {
        std::cerr << "[FlightRecorder] begin_session 寫檔失敗: " << e.what() << "\n";
    }

    enabled_.store(true, std::memory_order_release);

    // 註冊 fatal handler（只註冊一次）。atexit 抓 CUDA_CHECK 的 exit()；
    // set_terminate 抓未捕捉例外。兩者皆讀 latest_（跨執行緒）。
    static std::once_flag once;
    std::call_once(once, [] {
        std::atexit([] { FlightRecorder::instance().on_atexit(); });
        std::set_terminate([] { FlightRecorder::instance().on_terminate(); });
    });
}

void FlightRecorder::set_scene(const FrameScene& scene) {
    if (!enabled()) return;
    std::lock_guard<std::mutex> lk(ring_mtx_);
    cur_slot_ = head_;
    ring_[cur_slot_] = scene;
    ring_[cur_slot_].complete = false;
    head_ = (head_ + 1) % kRingSize;
    if (count_ < kRingSize) ++count_;
    latest_.store(&ring_[cur_slot_], std::memory_order_release);
}

void FlightRecorder::record_frame(const FrameScene& scene) {
    if (!enabled()) return;
    std::lock_guard<std::mutex> lk(ring_mtx_);
    // 補進 set_scene 用的同一格（若沒先 set_scene，退而求其次寫當前格）。
    ring_[cur_slot_] = scene;
    ring_[cur_slot_].complete = true;
    latest_.store(&ring_[cur_slot_], std::memory_order_release);
}

void FlightRecorder::record_incident(const std::string& kind, const std::string& detail,
                                     const std::string& stack, const std::string& src) {
    if (!enabled()) return;

    std::string src_rel = repo_relative(src);   // repo 相對 "檔名:行號"（log → VS Code 跳轉用）

    // [手冊 ch6] 「🌊 持續出事：節流」動畫演的就是這段
    // ── 節流：同 kind 在 kThrottleWindow 內只寫一次完整 incident 檔 ──────────
    // 防「持續性錯誤（如 CFAOI_RDMA_NOCRC 單邊設定）→ 每幀一個 incident 檔」把 _diag
    // 磁碟/inode 灌爆。被抑制期間仍每滿 100 筆補一行 compact jsonl 摘要（有痕跡、不洪水）。
    uint64_t suppressed_since_last = 0;
    {
        std::lock_guard<std::mutex> tlk(throttle_mtx_);
        auto& t = throttle_[kind];
        auto now = std::chrono::steady_clock::now();
        bool first = (t.last_full == std::chrono::steady_clock::time_point{});
        if (!first && now - t.last_full < kThrottleWindow) {
            ++t.suppressed;
            if (t.suppressed % 100 == 1) {
                try {
                    fs::path diag_dir = fs::path(session_.output_dir) / "_diag";
                    json idx{{"type", "incident_suppressed"}, {"ts", time_str(0)},
                             {"kind", kind}, {"suppressed", t.suppressed},
                             {"detail", detail}};
                    if (!src_rel.empty()) idx["src"] = src_rel;
                    std::ofstream jf(diag_dir / (time_str(1) + ".jsonl"), std::ios::app);
                    if (jf) jf << idx.dump() << "\n";
                } catch (...) {}
                std::cerr << "[Incident] kind=" << kind << " 節流中（30s 窗已抑制 "
                          << t.suppressed << " 筆）: " << detail << "\n";
            }
            return;
        }
        suppressed_since_last = t.suppressed;
        t.suppressed = 0;
        t.last_full = now;
    }

    json inc;
    inc["type"] = "incident";
    inc["ts"] = time_str(0);
    inc["kind"] = kind;
    inc["detail"] = detail;
    if (!stack.empty()) inc["stack"] = stack;
    if (!src_rel.empty()) inc["src"] = src_rel;
    if (suppressed_since_last > 0) inc["suppressed_since_last"] = suppressed_since_last;

    inc["session"] = {{"mode", session_.mode}, {"ip_name", session_.ip_name},
                      {"ini", session_.ini},
                      {"recipe", session_.recipe.empty() ? "(none)" : session_.recipe},
                      {"zero_copy", session_.zero_copy}, {"ai_active", session_.ai_active},
                      {"gpu", gpu_info_json()}};

    // 當下現場 + 最近 N 張：一律在 ring_mtx_ 保護下深拷（修 race：舊版在鎖外深拷
    // *latest_ 的 string/vector，與 writer 覆寫同 slot 構成 data race → incident 內容
    // 可能撕裂）。try_lock 失敗（fatal 與 writer 撞，極罕見）→ 只給 POD 摘要，
    // 不碰 string/vector（避免 UB），並註記原因。
    json recent = json::array();
    {
        std::unique_lock<std::mutex> lk(ring_mtx_, std::try_to_lock);
        if (!lk.owns_lock()) {                       // 短暫退讓再試一次（writer 臨界區極短）
            std::this_thread::yield();
            lk.try_lock();
        }
        if (lk.owns_lock()) {
            const FrameScene* cur = latest_.load(std::memory_order_acquire);
            inc["current_frame"] = cur ? scene_full_json(*cur) : json(nullptr);
            // 由舊到新走訪 count_ 筆（head_ 為下一個要寫的位置）。
            for (size_t i = 0; i < count_; ++i) {
                size_t idx = (head_ + kRingSize - count_ + i) % kRingSize;
                recent.push_back(scene_compact_json(ring_[idx]));
            }
        } else {
            const FrameScene* cur = latest_.load(std::memory_order_acquire);
            if (cur) {   // 僅 POD 欄位（int/double 撕裂無 UB；string/vector 不碰）
                inc["current_frame"] = {{"frame_seq", cur->frame_seq},
                                        {"cam_id", cur->cam_id},
                                        {"width", cur->width}, {"height", cur->height},
                                        {"num_defects", cur->num_defects},
                                        {"gpu_ms", cur->gpu_ms},
                                        {"complete", cur->complete}};
            } else {
                inc["current_frame"] = nullptr;
            }
            inc["recent_frames_note"] = "ring 鎖被佔用（fatal 與 writer 撞），僅 POD 摘要";
        }
    }
    inc["recent_frames"] = std::move(recent);

    // 寫檔：incident_<ts>.json（pretty）+ 當日 jsonl 一行（compact 索引）+ console banner。
    std::string ts = time_str(2);
    std::string incident_path;
    try {
        fs::path diag_dir = fs::path(session_.output_dir) / "_diag";
        fs::create_directories(diag_dir);
        fs::path ip = diag_dir / ("incident_" + ts + ".json");
        incident_path = ip.string();
        { std::ofstream f(ip); if (f) f << inc.dump(2) << "\n"; }
        {
            json idx{{"type", "incident"}, {"ts", inc["ts"]}, {"kind", kind},
                     {"detail", detail}, {"file", "incident_" + ts + ".json"}};
            if (!src_rel.empty()) idx["src"] = src_rel;
            const FrameScene* c = latest_.load(std::memory_order_acquire);
            if (c) { idx["panel_id"] = c->panel_id; idx["frame_seq"] = c->frame_seq; }
            std::ofstream jf(diag_dir / (time_str(1) + ".jsonl"), std::ios::app);
            if (jf) jf << idx.dump() << "\n";
        }
    } catch (const std::exception& e) {
        std::cerr << "[FlightRecorder] record_incident 寫檔失敗: " << e.what() << "\n";
    }

    const FrameScene* c = latest_.load(std::memory_order_acquire);
    std::cerr << "[Incident] kind=" << kind
              << " panel=" << (c ? c->panel_id : std::string("(none)"))
              << " seq=" << (c ? c->frame_seq : 0)
              << " : " << detail
              << " → " << incident_path << "\n";
}

void FlightRecorder::record_recipe(const std::string& label,
                                   const std::vector<ZoneSnap>& zones) {
    if (!enabled()) return;
    // LOAD_RECIPE 成功留痕：一行 compact jsonl（守門判定 + 關鍵參數）。
    // 「載錯但合法」事故（守門誤分類/靜默預設/換錯配方）事後可從 _diag 還原
    // 「何時載了什麼、每 zone 判成哪個 algo_mode」。
    json r;
    r["type"] = "recipe";
    r["ts"] = time_str(0);
    r["label"] = label;
    json zs = json::array();
    for (const auto& z : zones) {
        zs.push_back({{"zone_index", z.zone_index}, {"algo_mode", z.algo_mode},
                      {"BTH", z.BTH}, {"DTH", z.DTH},
                      {"pitch", {z.pitch_x, z.pitch_y}},
                      {"pitch_times", z.pitch_times}, {"choose_amount", z.choose_amount},
                      {"multiscale", z.enable_multiscale},
                      {"blob", {z.blob_min, z.blob_max, z.blob_merge}},
                      {"smooth3x3", z.smooth_times2},
                      {"remap", z.preproc_remap}, {"lsc", z.lsc},
                      {"roi", {z.roi_x1, z.roi_y1, z.roi_x2, z.roi_y2}}});
    }
    r["zones"] = std::move(zs);
    try {
        fs::path diag_dir = fs::path(session_.output_dir) / "_diag";
        fs::create_directories(diag_dir);
        std::ofstream jf(diag_dir / (time_str(1) + ".jsonl"), std::ios::app);
        if (jf) jf << r.dump() << "\n";
    } catch (const std::exception& e) {
        std::cerr << "[FlightRecorder] record_recipe 寫檔失敗: " << e.what() << "\n";
    }
}

void FlightRecorder::tick_stats(double gpu_ms, int num_defects, int64_t queue_depth) {
    if (!enabled()) return;
    // single-writer（與 record_frame 同執行緒）；累積視窗，滿 kStatsPeriod 張落一行。
    if (stats_frames_ == 0) {
        stats_t0_ = std::chrono::steady_clock::now();
        stats_gpu_ms_.clear();
        stats_gpu_ms_.reserve(kStatsPeriod);
        stats_defects_sum_ = 0;
        stats_defects_max_ = 0;
        stats_queue_peak_ = -1;
    }
    ++stats_frames_;
    stats_gpu_ms_.push_back(gpu_ms);
    stats_defects_sum_ += (uint64_t)std::max(0, num_defects);
    stats_defects_max_ = std::max(stats_defects_max_, num_defects);
    stats_queue_peak_ = std::max(stats_queue_peak_, queue_depth);
    if (stats_frames_ < kStatsPeriod) return;

    double elapsed_s = std::chrono::duration<double>(
        std::chrono::steady_clock::now() - stats_t0_).count();
    std::vector<double> v = stats_gpu_ms_;
    std::sort(v.begin(), v.end());
    auto pct = [&](double p) {
        if (v.empty()) return 0.0;
        size_t i = (size_t)(p * (double)(v.size() - 1));
        return v[i];
    };
    json s;
    s["type"] = "stats";
    s["ts"] = time_str(0);
    s["frames"] = stats_frames_;
    s["fps"] = elapsed_s > 0 ? (double)stats_frames_ / elapsed_s : 0.0;
    s["gpu_ms_p50"] = pct(0.50);
    s["gpu_ms_p95"] = pct(0.95);
    s["gpu_ms_max"] = v.empty() ? 0.0 : v.back();
    s["defects_sum"] = stats_defects_sum_;
    s["defects_max"] = stats_defects_max_;
    if (stats_queue_peak_ >= 0) s["queue_peak"] = stats_queue_peak_;
    try {
        fs::path diag_dir = fs::path(session_.output_dir) / "_diag";
        std::ofstream jf(diag_dir / (time_str(1) + ".jsonl"), std::ios::app);
        if (jf) jf << s.dump() << "\n";
    } catch (const std::exception& e) {
        std::cerr << "[FlightRecorder] tick_stats 寫檔失敗: " << e.what() << "\n";
    }
    stats_frames_ = 0;   // 開新視窗
}

void FlightRecorder::dump_fatal_once(const std::string& kind, const std::string& detail,
                                     const std::string& stack) {
    if (fatal_dumped_.exchange(true)) return;
    record_incident(kind, detail, stack);
}

void FlightRecorder::on_atexit() {
    if (!enabled()) return;
    // cuda_fatal 判定（CUDA_CHECK 的 exit 路徑）用兩個訊號，任一成立即記：
    //   ① 離開時尚有未清的 CUDA 錯誤（cudaPeekAtLastError）；
    //   ② latest_ 現場 complete=false → set_scene 後 record_frame 未跑 → 在處理影像途中崩潰。
    // 正常結束 / 一般 return（recipe error 時 latest_=null、peek=success）→ 不誤記。
    cudaError_t e = cudaPeekAtLastError();
    const FrameScene* cur = latest_.load(std::memory_order_acquire);
    bool mid_frame = (cur && !cur->complete);
    if (e != cudaSuccess || mid_frame) {
        std::string detail = (e != cudaSuccess)
            ? std::string(cudaGetErrorString(e))
            : std::string("程序在處理影像途中異常結束（record_frame 未完成）");
        dump_fatal_once("cuda_fatal", detail, "");
    }
}

void FlightRecorder::on_terminate() {
    if (enabled()) {
        std::string msg = "unknown";
        if (auto ep = std::current_exception()) {
            try { std::rethrow_exception(ep); }
            catch (const std::exception& e) { msg = e.what(); }
            catch (...) { msg = "non-std exception"; }
        }
        dump_fatal_once("uncaught_exception", msg, "");
    }
    std::abort();  // 保留 crash 行為（terminate handler 不可返回）。
}

}  // namespace diag
