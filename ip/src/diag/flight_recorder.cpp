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
#include <fstream>
#include <iostream>
#include <mutex>

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
                      {"BTH", z.BTH}, {"DTH", z.DTH},
                      {"pitch_x", z.pitch_x}, {"pitch_y", z.pitch_y},
                      {"search_x", z.search_x}, {"search_y", z.search_y},
                      {"roi", {z.roi_x1, z.roi_y1, z.roi_x2, z.roi_y2}},
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
                                     const std::string& stack) {
    if (!enabled()) return;

    json inc;
    inc["type"] = "incident";
    inc["ts"] = time_str(0);
    inc["kind"] = kind;
    inc["detail"] = detail;
    if (!stack.empty()) inc["stack"] = stack;

    inc["session"] = {{"mode", session_.mode}, {"ip_name", session_.ip_name},
                      {"ini", session_.ini},
                      {"recipe", session_.recipe.empty() ? "(none)" : session_.recipe},
                      {"zero_copy", session_.zero_copy}, {"ai_active", session_.ai_active},
                      {"gpu", gpu_info_json()}};

    // 當下現場：latest_ 恆可得（atomic 跨執行緒）。
    const FrameScene* cur = latest_.load(std::memory_order_acquire);
    if (cur) inc["current_frame"] = scene_full_json(*cur);
    else     inc["current_frame"] = nullptr;

    // 最近 N 張：try_lock 取 ring 快照；取不到（fatal 與 writer 撞）就只給當下現場。
    json recent = json::array();
    {
        std::unique_lock<std::mutex> lk(ring_mtx_, std::try_to_lock);
        if (lk.owns_lock()) {
            // 由舊到新走訪 count_ 筆（head_ 為下一個要寫的位置）。
            for (size_t i = 0; i < count_; ++i) {
                size_t idx = (head_ + kRingSize - count_ + i) % kRingSize;
                recent.push_back(scene_compact_json(ring_[idx]));
            }
        } else {
            inc["recent_frames_note"] = "ring 鎖被佔用（fatal 與 writer 撞），僅 current_frame";
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
