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
            if (di.IsMacAddressAvailable()) ci.mac = di.GetMacAddress().c_str();
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

        payload_ = CIntegerParameter(nm, "PayloadSize").GetValue();
        opened_  = true;

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
    cam_id_    = cam_id;
    stop_flag_ = false;
    running_   = true;
    grabbed_   = 0;
    dropped_   = 0;
    thread_    = std::thread(&CamPylon::grab_loop, this);
}

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

void CamPylon::grab_loop() {
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

            // drop 偵測：兩種取較大
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

    c->StopGrabbing();
    running_ = false;
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
