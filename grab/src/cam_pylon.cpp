#include "cam_pylon.h"

#include <pylon/PylonIncludes.h>
#include <pylon/ParameterIncludes.h>

#include <chrono>
#include <cstdio>

using namespace Pylon;

// camera_ptr_ 的真實型別
static CInstantCamera* cam(void* p) { return static_cast<CInstantCamera*>(p); }

// 列舉所有相機（唯讀，不開相機）。EnumerateDevices 後讀 CDeviceInfo 的網路欄位。
// PylonInitialize/Terminate 為 ref-counted：與已開相機共存安全（grabbing 中並存須實測，見 STATUS）。
std::vector<CamInfo> CamPylon::enumerate_cameras() {
    std::vector<CamInfo> out;
    PylonInitialize();
    try {
        DeviceInfoList_t devices;
        CTlFactory::GetInstance().EnumerateDevices(devices);
        for (size_t i = 0; i < devices.size(); ++i) {
            const CDeviceInfo& di = devices[i];
            CamInfo ci;
            ci.cam_id       = (int)i;                       // 暫派；MAC 穩定映射 = #21
            ci.model        = di.GetModelName().c_str();
            ci.serial       = di.GetSerialNumber().c_str();
            ci.device_class = di.GetDeviceClass().c_str();
            ci.online       = true;                          // 出現在列舉 = online
            if (di.IsMacAddressAvailable()) {
                std::string raw = di.GetMacAddress().c_str();   // 例 "003053531941"
                // 12 碼純 hex → 補冒號 "00:30:53:53:19:41"（顯示用）
                if (raw.size() == 12 && raw.find(':') == std::string::npos) {
                    std::string m;
                    for (size_t k = 0; k < raw.size(); k += 2) {
                        if (k) m += ':';
                        m += raw.substr(k, 2);
                    }
                    ci.mac = m;
                } else {
                    ci.mac = raw;
                }
            }
            if (di.IsIpAddressAvailable())  ci.ip  = di.GetIpAddress().c_str();
            if (di.IsIpConfigCurrentAvailable())
                ci.ip_config = di.GetIpConfigCurrent().c_str();
            // persistent 狀態：非 GigE 可能無此屬性 → try/catch 守。
            try { ci.persistent = di.IsPersistentIpActive(); } catch (...) {}
            out.push_back(std::move(ci));
        }
    } catch (const GenericException& e) {
        fprintf(stderr, "[cam_pylon] enumerate 失敗：%s\n", e.GetDescription());
    }
    PylonTerminate();   // ref-counted：相機已開時 ref 仍 >0，相機不受影響
    return out;
}

bool CamPylon::open(const std::string& serial, int64_t pkt_size) {
    if (opened_) return true;

    PylonInitialize();
    auto* c = new CInstantCamera();
    camera_ptr_ = c;

    try {
        if (serial == "auto") {
            c->Attach(CTlFactory::GetInstance().CreateFirstDevice());
        } else {
            CDeviceInfo want;
            want.SetSerialNumber(serial.c_str());
            c->Attach(CTlFactory::GetInstance().CreateDevice(want));
        }
        c->Open();

        GenApi::INodeMap& nm = c->GetNodeMap();
        if (pkt_size > 0)
            CIntegerParameter(nm, "GevSCPSPacketSize").TrySetValue(pkt_size);

        // GigE 無 .dcf：機器層基本參數須顯式設定（全 TrySetValue/guard，失敗不中斷）。
        // 1) PixelFormat 鎖 Mono8（可被改成 Mono12/YUV → 資料格式錯）
        // 2) ExposureAuto/GainAuto 關（否則自動曝光蓋掉手動值 = 設了沒用）
        // 3) TriggerMode 顯式 Off = free-run（現況；encoder 行觸發為未來生產配置）
        CEnumParameter(nm, "PixelFormat").TrySetValue("Mono8");
        CEnumParameter(nm, "ExposureAuto").TrySetValue("Off");
        CEnumParameter(nm, "GainAuto").TrySetValue("Off");
        CEnumParameter(nm, "TriggerMode").TrySetValue("Off");

        payload_ = CIntegerParameter(nm, "PayloadSize").GetValue();
        opened_  = true;

        // 印出實際生效值（確認真的設成功，非只送指令）
        auto enumOr = [&](const char* n) -> std::string {
            try { return std::string(CEnumParameter(nm, n).GetValue().c_str()); }
            catch (...) { return "?"; }
        };
        printf("[cam_pylon] 機器層: PixelFormat=%s ExposureAuto=%s GainAuto=%s TriggerMode=%s\n",
               enumOr("PixelFormat").c_str(), enumOr("ExposureAuto").c_str(),
               enumOr("GainAuto").c_str(), enumOr("TriggerMode").c_str());

        printf("[cam_pylon] 開啟 %s SN=%s  PayloadSize=%lld\n",
               c->GetDeviceInfo().GetModelName().c_str(),
               c->GetDeviceInfo().GetSerialNumber().c_str(),
               (long long)payload_);
        return true;

    } catch (const GenericException& e) {
        fprintf(stderr, "[cam_pylon] open 失敗：%s\n", e.GetDescription());
        delete c;
        camera_ptr_ = nullptr;
        PylonTerminate();
        return false;
    }
}

void CamPylon::start(uint16_t cam_id) {
    if (!opened_ || !cb_ || running_) return;
    if (thread_.joinable()) thread_.join();   // 回收前次自動停止（收滿 N 張）殘留的 thread
    cam_id_    = cam_id;
    stop_flag_ = false;
    running_   = true;
    faulted_   = false;                       // B1：新一輪取像清掉上次的故障標記
    { std::lock_guard<std::mutex> lk(fault_mtx_); fault_msg_.clear(); }
    grabbed_   = 0;
    dropped_   = 0;
    thread_    = std::thread(&CamPylon::grab_loop, this);
}

// stop — 順序：① 豎 stop_flag_ ② join 取像 thread ③ StopGrabbing/Close/delete + PylonTerminate
//（ref-counted，其他已開相機不受影響）。stop 後同一物件可再 open/start。
// join 無逾時：若 frame_cb 正阻塞在 RDMA 背壓（rdma_sender poll_one），此處會等到它解除為止
//（⚠️ 收端 wedge 時 = docs/code_review_20260802.md B5 卡死鏈的一環，stop_flag_ 解不了 poll_one）。
void CamPylon::stop() {
    stop_flag_ = true;
    if (thread_.joinable()) thread_.join();
    running_ = false;

    if (opened_ && camera_ptr_) {
        try { cam(camera_ptr_)->StopGrabbing(); } catch (...) {}
        try { cam(camera_ptr_)->Close(); }       catch (...) {}
        delete cam(camera_ptr_);
        camera_ptr_ = nullptr;
        PylonTerminate();
        opened_  = false;
        payload_ = 0;
    }
}

// B1：記錄故障（grab thread 內呼叫；控制 thread 經 fault_message() 讀）。
// 刻意收 const char* 而非 std::string：呼叫端在 catch handler 裡，任何配置記憶體的動作
// （字串串接）若丟 bad_alloc 就會逸出 thread → 仍然 terminate。字串組裝改在這裡 try 起來，
// 最壞情況只是丟失訊息文字，**faulted_ 旗標一定豎得起來**。
void CamPylon::note_fault(const char* kind, const char* what) {
    try {
        std::lock_guard<std::mutex> lk(fault_mtx_);
        fault_msg_ = std::string(kind) + (what ? what : "?");
    } catch (...) {}
    faulted_ = true;
    fprintf(stderr, "[cam_pylon] ⚠️ cam%u 取像中止（本台故障，其餘相機續跑）：%s%s\n",
            cam_id_, kind, what ? what : "?");
}

// grab_loop — 取像 thread 本體（每台一條；std::thread 進入點）。停止方式：stop() 豎 stop_flag_ 後 join。
//
// B1 修法（docs/code_review_20260802.md，2026-08-02）：全函式包 try/catch。
//   拔線/斷電/交換機掉埠時 StartGrabbing/RetrieveResult 會擲 GenericException；
//   例外若逸出 thread 進入點 → std::terminate → **整個 cfaoi_grab 死亡（6 台陪葬 + 8100/RDMA 全斷）**。
//   攔下來後：本台標記 faulted_ 並乾淨退出，其餘相機的 thread 不受影響繼續送幀。
// ⚠️ 攔到例外後**不自動重連**——重連要重跑 open/參數/RDMA 全鏈，靜默重連會讓「線鬆了」變成無人察覺的
//   間歇掉幀。恢復路徑仍是人為 GRAB_STOP → 排除線路 → GRAB_ARM（見 docs/6cam_setup_runbook.md）。
void CamPylon::grab_loop() {
    try {
        grab_loop_body();
    } catch (const GenericException& e) {
        // 相機層例外：拔線/斷電/掉埠，以及逾時以外的傳輸錯誤
        // （GenericException 繼承 std::exception → 必須排在下面那個 catch 之前）
        note_fault("pylon: ", e.GetDescription());
    } catch (const std::exception& e) {
        // 非 pylon 例外（例如 frame_cb 內部：RDMA 送幀、記憶體配置）
        note_fault("exception: ", e.what());
    } catch (...) {
        note_fault("unknown: ", "非 std::exception 的未知例外");
    }

    // 正常結束與故障結束共用收尾：StopGrabbing 在斷線後自己也會擲例外，必須吞掉，
    // 否則 catch 完又在這裡逸出 thread 進入點 → 仍然 std::terminate（等於沒修）。
    if (camera_ptr_) { try { cam(camera_ptr_)->StopGrabbing(); } catch (...) {} }
    running_ = false;
}

// grab_loop_body — 原本的取像迴圈本體。**允許擲例外**，由 grab_loop() 統一攔。
void CamPylon::grab_loop_body() {
    CInstantCamera* c = cam(camera_ptr_);
    c->MaxNumBuffer = 16;
    c->StartGrabbing(GrabStrategy_OneByOne);   // 持續，不限幀數

    CGrabResultPtr res;
    int64_t prev_block = -1;
    auto t_log = std::chrono::steady_clock::now();
    uint64_t log_frames = 0;

    while (!stop_flag_ && c->IsGrabbing()) {
        c->RetrieveResult(2000, res, TimeoutHandling_Return);
        if (!res) continue;

        if (res->GrabSucceeded()) {
            ++grabbed_;
            ++log_frames;

            // drop 偵測：BlockID 缺口 + GetNumberOfSkippedImages **相加**（舊註解「兩種取較大」有誤）。
            // 兩來源語意不同：BlockID 缺口=相機端跳號（線路/相機丟幀）；SkippedImages=pylon 驅動端
            // 因緩衝滿丟棄。極端情境同一幀可能被兩邊各計一次（偏保守=寧多報勿漏報）。
            uint64_t skipped = res->GetNumberOfSkippedImages();
            int64_t  bid     = (int64_t)res->GetBlockID();
            if (prev_block >= 0 && bid > prev_block + 1)
                dropped_ += (uint64_t)(bid - prev_block - 1);
            dropped_ += skipped;
            prev_block = bid;

            cb_(cam_id_,
                (const uint8_t*)res->GetBuffer(),
                (uint32_t)res->GetImageSize(),
                (uint32_t)res->GetWidth(),
                (uint32_t)res->GetHeight());

            // 每片 N 張：收滿自動結束（thread 自然退出；同舊系統 M_FRAMES_PER_TRIGGER(N) 語意）
            if (max_frames_ > 0 && grabbed_ >= max_frames_) {
                printf("[cam_pylon] cam%u 收滿 %llu 張，自動停止取像\n",
                       cam_id_, (unsigned long long)grabbed_);
                break;
            }

        } else {
            fprintf(stderr, "[cam_pylon] GrabFailed: %s\n",
                    res->GetErrorDescription().c_str());
        }

        // 每 5 秒印一次 FPS
        auto now = std::chrono::steady_clock::now();
        double secs = std::chrono::duration<double>(now - t_log).count();
        if (secs >= 5.0) {
            printf("[cam_pylon] cam%u  FPS=%.1f  grabbed=%llu  dropped=%llu\n",
                   cam_id_, log_frames / secs,
                   (unsigned long long)grabbed_,
                   (unsigned long long)dropped_);
            log_frames = 0;
            t_log = now;
        }
    }
}

// ---------------------------------------------------------------------------
// Gap #2：曝光 / 增益 get/set
// ---------------------------------------------------------------------------

bool CamPylon::set_params(float exposure_us, int gain_raw,
                           float& exp_actual, int& gain_actual) {
    if (!opened_ || !camera_ptr_) return false;
    try {
        GenApi::INodeMap& nm = cam(camera_ptr_)->GetNodeMap();
        CFloatParameter(nm, "ExposureTimeAbs").SetValue((double)exposure_us);
        CIntegerParameter(nm, "GainRaw").SetValue((int64_t)gain_raw);
        exp_actual  = (float)CFloatParameter(nm, "ExposureTimeAbs").GetValue();
        gain_actual = (int)  CIntegerParameter(nm, "GainRaw").GetValue();
        printf("[cam_pylon] set_params: exp %.1f→%.1fµs  gain %d→%d raw\n",
               exposure_us, exp_actual, gain_raw, gain_actual);
        return true;
    } catch (const GenericException& e) {
        fprintf(stderr, "[cam_pylon] set_params 失敗：%s\n", e.GetDescription());
        return false;
    }
}

bool CamPylon::get_params(float& exp_actual, int& gain_actual) {
    if (!opened_ || !camera_ptr_) return false;
    try {
        GenApi::INodeMap& nm = cam(camera_ptr_)->GetNodeMap();
        exp_actual  = (float)CFloatParameter(nm, "ExposureTimeAbs").GetValue();
        gain_actual = (int)  CIntegerParameter(nm, "GainRaw").GetValue();
        return true;
    } catch (const GenericException& e) {
        fprintf(stderr, "[cam_pylon] get_params 失敗：%s\n", e.GetDescription());
        return false;
    }
}

// 讀回 GigE 機器層參數(open() 設的東西),供 UI 顯示。需相機已 open。
bool CamPylon::read_machine_params(MachineParams& mp, std::string& err) {
    if (!opened_ || !camera_ptr_) { err = "相機未開"; return false; }
    try {
        GenApi::INodeMap& nm = cam(camera_ptr_)->GetNodeMap();
        auto en = [&](const char* n) -> std::string {
            try { return std::string(CEnumParameter(nm, n).GetValue().c_str()); }
            catch (...) { return std::string("?"); }
        };
        auto it = [&](const char* n) -> long long {
            try { return (long long)CIntegerParameter(nm, n).GetValue(); }
            catch (...) { return -1; }
        };
        mp.pixel_format     = en("PixelFormat");
        mp.exposure_auto    = en("ExposureAuto");
        mp.gain_auto        = en("GainAuto");
        mp.trigger_mode     = en("TriggerMode");
        mp.trigger_selector = en("TriggerSelector");
        mp.trigger_source   = en("TriggerSource");
        mp.width       = it("Width");
        mp.height      = it("Height");
        mp.packet_size = it("GevSCPSPacketSize");
        mp.scpd        = it("GevSCPD");
        return true;
    } catch (const GenericException& e) {
        err = e.GetDescription();
        return false;
    }
}

// 抓 1 幀算 uint8 平均灰階（調參效果確認）。需相機已 open 且非串流中。
bool CamPylon::grab_one_mean(double& mean, std::string& err) {
    if (!opened_ || !camera_ptr_) { err = "相機未開"; return false; }
    if (running_.load())          { err = "取像中，無法單張抓幀（請先停止取像）"; return false; }
    auto* c = cam(camera_ptr_);
    try {
        // 線掃幀時間 ≈ 曝光 × 行數(free-run),故 timeout 隨曝光/Height 自適應(夾 3~15s)。
        GenApi::INodeMap& nm = c->GetNodeMap();
        double  exp_us = 0; int64_t lines = 0;
        try { exp_us = CFloatParameter(nm, "ExposureTimeAbs").GetValue(); } catch (...) {}
        try { lines  = CIntegerParameter(nm, "Height").GetValue(); } catch (...) {}
        double est_ms = exp_us * (double)lines / 1000.0 * 1.3 + 500.0;
        int timeout_ms = (int)(est_ms < 3000.0 ? 3000.0 : (est_ms > 15000.0 ? 15000.0 : est_ms));
        c->StartGrabbing(1, GrabStrategy_OneByOne, GrabLoop_ProvidedByUser);
        CGrabResultPtr r;
        c->RetrieveResult(timeout_ms, r, TimeoutHandling_Return);
        if (!r || !r->GrabSucceeded()) {
            c->StopGrabbing();
            err = "抓幀失敗/逾時";
            return false;
        }
        const uint8_t* p = (const uint8_t*)r->GetBuffer();
        size_t n = (size_t)r->GetImageSize();
        uint64_t sum = 0;
        for (size_t i = 0; i < n; ++i) sum += p[i];
        mean = n ? (double)sum / (double)n : 0.0;
        c->StopGrabbing();
        printf("[cam_pylon] grab_one_mean: %ux%u mean=%.2f\n",
               r->GetWidth(), r->GetHeight(), mean);
        return true;
    } catch (const GenericException& e) {
        try { c->StopGrabbing(); } catch (...) {}
        err = e.GetDescription();
        return false;
    }
}
