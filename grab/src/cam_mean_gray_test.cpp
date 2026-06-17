// cam_mean_gray_test — Gap #2 Stage 2+3 驗證
// 直接開相機，調曝光/增益，抓幀，量 mean gray，確認單調性。
// 不需 RDMA / control_server，專注驗 cam_pylon set_params → frame gray 效果。
//
// 用法：
//   ./cam_mean_gray_test [serial_number]
//
// 輸出：exp=A mean=X, exp=B mean=Y, ratio=Y/X（預期 > 1.4）
//       gain_low=L mean=P, gain_high=H mean=Q, ratio=Q/P（預期 > 1.2）

#include "cam_pylon.h"

#include <pylon/PylonIncludes.h>
#include <pylon/ParameterIncludes.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdlib>

using namespace Pylon;

// 抓 1 幀並計算 mean gray（uint8 Mono8）
static double grab_mean(CInstantCamera* c) {
    c->StartGrabbing(1, GrabStrategy_OneByOne, GrabLoop_ProvidedByUser);
    CGrabResultPtr r;
    c->RetrieveResult(5000, r, TimeoutHandling_ThrowException);
    c->StopGrabbing();
    if (!r || !r->GrabSucceeded()) {
        fprintf(stderr, "[mean_gray] 抓幀失敗\n");
        return -1.0;
    }
    const uint8_t* buf = (const uint8_t*)r->GetBuffer();
    size_t n = r->GetImageSize();
    double sum = 0.0;
    for (size_t i = 0; i < n; ++i) sum += buf[i];
    return sum / (double)n;
}

// 設曝光/增益，讀 actual，抓幀
static double set_and_grab(CInstantCamera* c, float exp_us, int gain_raw,
                            float& exp_actual, int& gain_actual) {
    GenApi::INodeMap& nm = c->GetNodeMap();
    CFloatParameter(nm, "ExposureTimeAbs").SetValue((double)exp_us);
    CIntegerParameter(nm, "GainRaw").SetValue((int64_t)gain_raw);
    exp_actual  = (float)CFloatParameter(nm, "ExposureTimeAbs").GetValue();
    gain_actual = (int)  CIntegerParameter(nm, "GainRaw").GetValue();
    return grab_mean(c);
}

int main(int argc, char** argv) {
    const char* serial = (argc > 1) ? argv[1] : nullptr;

    PylonInitialize();
    {   // cam 在內層 scope，確保先於 PylonTerminate() 析構
        CInstantCamera cam;
        bool opened = false;
        try {
            if (serial) {
                CDeviceInfo want;
                want.SetSerialNumber(serial);
                cam.Attach(CTlFactory::GetInstance().CreateDevice(want));
            } else {
                cam.Attach(CTlFactory::GetInstance().CreateFirstDevice());
            }
            cam.Open();
            opened = true;
        } catch (const GenericException& e) {
            fprintf(stderr, "[ERROR] 開相機失敗：%s\n", e.GetDescription());
            // 不在此呼叫 PylonTerminate，讓 cam 先析構
            fflush(stderr);
            // 離開 scope → cam 析構 → 下面的 PylonTerminate 才安全
        }
        if (!opened) goto done;

        printf("相機：%s  SN=%s\n",
               cam.GetDeviceInfo().GetModelName().c_str(),
               cam.GetDeviceInfo().GetSerialNumber().c_str());

        CInstantCamera* cp = &cam;
        float ea, ga_unused;
        int   ga, ea_unused;

        // ===== Stage 2：曝光低 vs 高 → mean gray 單調遞增 =====
        printf("\n=== Stage 2：曝光單調性 ===\n");
        float exp_low  = 70.0f;
        float exp_high = 500.0f;
        // 增益固定 256（最低，避免 gain 干擾）
        double mean_low  = set_and_grab(cp, exp_low,  256, ea, ga);
        printf("  exp=%.0fµs  actual=%.1fµs  mean_gray=%.2f\n", exp_low,  ea, mean_low);
        double mean_high = set_and_grab(cp, exp_high, 256, ea, ga);
        printf("  exp=%.0fµs  actual=%.1fµs  mean_gray=%.2f\n", exp_high, ea, mean_high);
        double exp_ratio = (mean_low > 0) ? mean_high / mean_low : -1.0;
        printf("  ratio=%.3f  %s（預期 > 1.4）\n", exp_ratio,
               exp_ratio > 1.4 ? "PASS" : "FAIL");

        // ===== Stage 3：增益低 vs 高 → mean gray 差異 =====
        printf("\n=== Stage 3：增益差異 ===\n");
        int gain_lo = 256;
        int gain_hi = 1024;
        // 曝光固定 200µs（避免飽和）
        double mean_glo = set_and_grab(cp, 200.0f, gain_lo, ea, ga);
        printf("  gain=%d  actual=%d  mean_gray=%.2f\n", gain_lo, ga, mean_glo);
        double mean_ghi = set_and_grab(cp, 200.0f, gain_hi, ea, ga);
        printf("  gain=%d  actual=%d  mean_gray=%.2f\n", gain_hi, ga, mean_ghi);
        double gain_ratio = (mean_glo > 0) ? mean_ghi / mean_glo : -1.0;
        printf("  ratio=%.3f  %s（預期 > 1.2）\n", gain_ratio,
               gain_ratio > 1.2 ? "PASS" : "FAIL");

        // 恢復 Stage 0 預設值
        set_and_grab(cp, 70.0f, 256, ea, ga);
        printf("\n已恢復 exp=70.0µs gain=256 raw\n");

        cam.Close();
    }
done:
    PylonTerminate();
    fflush(stdout);
    fflush(stderr);
    std::_Exit(0);
}
