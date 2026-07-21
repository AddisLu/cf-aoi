#include "cam_manager.h"

#include <algorithm>
#include <cstdio>

bool CamManager::open_all(int want, const std::string& cli_serial,
                          int64_t pkt_size, std::string& err) {
    if (!cams_.empty()) {
        // ARM 冪等：台數符合需求（或 ALL）直接重用已開陣列（每片重 ARM 零冷啟成本）；
        // 台數需求改變才關掉重開。重插拔相機請先 GRAB_STOP 再 ARM。
        if (want <= 0 || cams_.size() == (size_t)want) return true;
        stop_all();
    }

    // 單台：沿用舊語意（auto/指定序號），不需先列舉。
    if (want == 1) {
        Entry e;
        e.cam = std::make_unique<CamPylon>();
        e.cam_id = 0;
        e.serial = cli_serial;
        if (!e.cam->open(cli_serial, pkt_size)) {
            err = "pylon open failed (serial=" + cli_serial + ")";
            return false;
        }
        cams_.push_back(std::move(e));
        return true;
    }

    // 多台/ALL：列舉 → 依序取前 want 台（want<=0 = 全部），各依序號開。
    auto infos = CamPylon::enumerate_cameras();
    if (infos.empty()) { err = "enumerate 找不到任何相機"; return false; }
    size_t n = (want <= 0) ? infos.size() : std::min<size_t>((size_t)want, infos.size());
    if (want > 0 && infos.size() < (size_t)want) {
        err = "列舉到 " + std::to_string(infos.size()) + " 台 < 要求 " + std::to_string(want) + " 台";
        return false;
    }

    for (size_t i = 0; i < n; ++i) {
        Entry e;
        e.cam = std::make_unique<CamPylon>();
        e.cam_id = (uint16_t)i;            // 列舉順序暫派（#21 MAC 穩定映射前的過渡）
        e.serial = infos[i].serial;
        if (!e.cam->open(e.serial, pkt_size)) {
            err = "cam" + std::to_string(i) + " (SN=" + e.serial + ") open 失敗";
            stop_all();                     // fail-fast：不留半開陣列
            return false;
        }
        cams_.push_back(std::move(e));
    }
    printf("[cam_manager] 開啟 %zu/%zu 台相機（want=%d）\n", cams_.size(), infos.size(), want);
    return true;
}

void CamManager::start_all(uint64_t max_frames_per_cam, FrameCb cb) {
    for (auto& e : cams_) {
        e.cam->set_frame_callback(cb);      // 同一 cb，多 thread 併發呼叫（呼叫端 thread-safe）
        e.cam->set_max_frames(max_frames_per_cam);
        e.cam->start(e.cam_id);             // 逐台 arm；skew 由 IP 端玻璃前緣對位吸收
    }
    printf("[cam_manager] 啟動 %zu 台（每台 %llu 張%s）\n",
           cams_.size(), (unsigned long long)max_frames_per_cam,
           max_frames_per_cam == 0 ? "，連續" : "自動停");
}

void CamManager::stop_all() {
    for (auto& e : cams_) if (e.cam) e.cam->stop();
    cams_.clear();
}

CamPylon* CamManager::get(int cam_id) {
    for (auto& e : cams_)
        if ((int)e.cam_id == cam_id) return e.cam.get();
    return nullptr;
}

CamPylon* CamManager::get_or_open_primary(const std::string& cli_serial, int64_t pkt_size) {
    if (!cams_.empty()) return cams_.front().cam.get();
    Entry e;
    e.cam = std::make_unique<CamPylon>();
    e.cam_id = 0;
    e.serial = cli_serial;
    if (!e.cam->open(cli_serial, pkt_size)) return nullptr;
    cams_.push_back(std::move(e));
    return cams_.front().cam.get();
}

int64_t CamManager::max_payload() const {
    int64_t m = 0;
    for (const auto& e : cams_) m = std::max(m, e.cam->payload_size());
    return m;
}

uint64_t CamManager::total_grabbed() const {
    uint64_t s = 0;
    for (const auto& e : cams_) s += e.cam->grabbed();
    return s;
}

uint64_t CamManager::total_dropped() const {
    uint64_t s = 0;
    for (const auto& e : cams_) s += e.cam->dropped();
    return s;
}

size_t CamManager::running_count() const {
    size_t n = 0;
    for (const auto& e : cams_) if (e.cam->is_running()) ++n;
    return n;
}
