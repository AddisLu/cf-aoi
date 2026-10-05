// ebus_sim — 軟體 GigE Vision 裝置（eBUS PvSoftDeviceGEV），假扮「L803K 經 iPORT」來測 grab 的
// --camera ebus 取像路徑（沒有實體 iPORT 時用）。2026-10-05。
//
//   ebus_sim <網卡名或 MAC> [寬=8160] [高=5000] [fps=2.4] [序號=SIM0038]
//   例（同一台主機即可，裝置 IP = 該網卡的 IP）：
//     source /opt/pleora/ebus/*/bin/set_puregev_env.sh; grab/build/ebus_sim enp1s0f0np0
//     CFAOI_EBUS_NO_SERIAL=1 grab/build/ebus_frame_check 192.168.3.2 20      （逐張驗內容）
//     CFAOI_EBUS_NO_SERIAL=1 grab/build/cfaoi_grab --camera ebus --line-rate keep --serial 192.168.3.2 …
//
// 每幀內容 = 逐行遞增灰階 + 幀序號寫在第 0 行前 8 bytes（收端可驗證沒錯位、沒重複）。
// ⚠️ 沒有 Camera Link 序列埠 → grab 端要設 CFAOI_EBUS_NO_SERIAL=1 且 --line-rate keep。
// ⚠️ 模擬器整張 40MB 瞬間送出（實體 iPORT 依行頻持續送）→ 同主機時偶有 TOO_MANY_CONSECUTIVE_RESENDS
//    不完整幀（grab 會整張作廢計 dropped）——是模擬器特性，實際掉幀率以實體 iPORT 為準。
#include <PvBuffer.h>
#include <PvFPSStabilizer.h>
#include <PvSoftDeviceGEV.h>
#include <PvStreamingChannelSourceDefault.h>

#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <string>
#include <thread>

static std::atomic<bool> g_stop{false};

class SimSource : public PvStreamingChannelSourceDefault {
public:
    SimSource(uint32_t w, uint32_t h, double fps)
        : PvStreamingChannelSourceDefault(w, h, PvPixelMono8, 8), fps_(fps) {}

    PvResult QueueBuffer(PvBuffer* b) override {
        if (pending_) return PvResult::Code::BUSY;
        pending_ = b;
        return PvResult::Code::OK;
    }
    PvResult RetrieveBuffer(PvBuffer** b) override {
        if (!pending_) return PvResult::Code::NO_AVAILABLE_DATA;
        while (!stab_.IsTimeToDisplay((uint32_t)(fps_ + 0.5))) std::this_thread::sleep_for(std::chrono::milliseconds(1));
        fill(pending_);
        *b = pending_;
        pending_ = nullptr;
        return PvResult::Code::OK;
    }
    void AbortQueuedBuffers() override { pending_ = nullptr; }

private:
    void fill(PvBuffer* b) {
        PvImage* img = b->GetImage();
        const uint32_t w = img->GetWidth(), h = img->GetHeight();
        uint8_t* p = b->GetDataPointer();
        for (uint32_t y = 0; y < h; ++y) std::memset(p + (size_t)y * w, (uint8_t)(y + frame_), w);
        std::memcpy(p, &frame_, sizeof(frame_));   // 幀序號 → 第 0 行前 8 bytes
        ++frame_;
    }
    PvBuffer* pending_ = nullptr;
    PvFPSStabilizer stab_;
    double fps_;
    uint64_t frame_ = 0;
};

int main(int argc, char** argv) {
    if (argc < 2) { std::fprintf(stderr, "用法：%s <網卡名或 MAC> [寬] [高] [fps] [序號]\n", argv[0]); return 1; }
    const uint32_t w = argc > 2 ? (uint32_t)std::atoi(argv[2]) : 8160;
    const uint32_t h = argc > 3 ? (uint32_t)std::atoi(argv[3]) : 5000;
    const double fps = argc > 4 ? std::atof(argv[4]) : 2.4;
    const char* serial = argc > 5 ? argv[5] : "SIM0038";
    std::signal(SIGINT, [](int) { g_stop = true; });
    std::signal(SIGTERM, [](int) { g_stop = true; });

    SimSource src(w, h, fps);
    PvSoftDeviceGEV dev;
    dev.AddStream(&src);
    IPvVirtualDeviceGEVInfo* info = dev.GetInfo();
    info->SetManufacturerName("Pleora Technologies Inc.");
    info->SetModelName("L803K-SIM (iPORT CL-GigE sim)");
    info->SetSerialNumber(serial);
    // Start() 要的是網卡 **MAC**（給網卡名稱它照樣回 OK 但沒綁上，GVCP 完全不回應——2026-10-05 實測）
    std::string mac = argv[1];
    if (mac.find(':') == std::string::npos) {
        std::ifstream f("/sys/class/net/" + mac + "/address");
        if (!(f >> mac)) { std::fprintf(stderr, "找不到網卡 %s\n", argv[1]); return 1; }
    }
    PvResult r = dev.Start(mac.c_str());
    if (!r.IsOK()) {
        std::fprintf(stderr, "啟動失敗：%s %s\n", r.GetCodeString().GetAscii(), r.GetDescription().GetAscii());
        return 1;
    }
    std::printf("[ebus_sim] 已啟動：%s（%s）  %ux%u Mono8  %.1f fps  序號 %s（Ctrl+C 結束）\n", argv[1], mac.c_str(), w, h, fps, serial);
    std::fflush(stdout);
    while (!g_stop) std::this_thread::sleep_for(std::chrono::milliseconds(200));
    dev.Stop();
    return 0;
}
