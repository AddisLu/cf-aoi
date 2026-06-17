// Stage 0 GenICam 節點探測工具
// 用途：連 raL8192-12gm，確認 ExposureTimeAbs/Gain/AcquisitionLineRate 等節點名稱、
//       單位、min/max/increment、以及 acquisition 中是否可寫（TLParamsLocked）。
// 在 damac 上執行：
//   cmake --build build --target probe_cam_nodes
//   ./build/probe_cam_nodes [serial]   (serial 省略 = auto 第一台)
// 把完整輸出貼給開發者，Gap #2 cam_pylon.cpp 才能填正確節點名稱。

#include <pylon/PylonIncludes.h>
#include <pylon/ParameterIncludes.h>
#include <cstdio>
#include <cstring>
#include <cstdlib>

using namespace Pylon;
using namespace GenApi;

static void probe_float(INodeMap& nm, const char* name) {
    try {
        CFloatParameter p(nm, name);
        EAccessMode acc = p.GetNode()->GetAccessMode();
        printf("  [Float] %-30s  val=%12.4f  min=%12.4f  max=%12.4f  access=%s\n",
               name,
               (double)p.GetValue(),
               (double)p.GetMin(),
               (double)p.GetMax(),
               (acc == RW) ? "RW" : (acc == RO) ? "RO" : (acc == NA) ? "NA" : "??");
    } catch (...) {
        printf("  [Float] %-30s  NOT FOUND\n", name);
    }
}

static void probe_int(INodeMap& nm, const char* name) {
    try {
        CIntegerParameter p(nm, name);
        EAccessMode acc = p.GetNode()->GetAccessMode();
        printf("  [Int  ] %-30s  val=%12lld  min=%12lld  max=%12lld  inc=%6lld  access=%s\n",
               name,
               (long long)p.GetValue(),
               (long long)p.GetMin(),
               (long long)p.GetMax(),
               (long long)p.GetInc(),
               (acc == RW) ? "RW" : (acc == RO) ? "RO" : (acc == NA) ? "NA" : "??");
    } catch (...) {
        printf("  [Int  ] %-30s  NOT FOUND\n", name);
    }
}

int main(int argc, char** argv) {
    const char* serial = (argc > 1) ? argv[1] : nullptr;

    PylonInitialize();
    printf("=== probe_cam_nodes（Gap #2 Stage 0）===\n");

    // cam in explicit scope so destructor runs before PylonTerminate()
    {
        CInstantCamera cam;
        try {
            if (serial) {
                CDeviceInfo want;
                want.SetSerialNumber(serial);
                cam.Attach(CTlFactory::GetInstance().CreateDevice(want));
            } else {
                cam.Attach(CTlFactory::GetInstance().CreateFirstDevice());
            }
            cam.Open();
        } catch (const GenericException& e) {
            fprintf(stderr, "[ERROR] 開相機失敗：%s\n", e.GetDescription());
            PylonTerminate();
            return 1;
        }

        printf("\n相機型號：%s  SN：%s\n",
               cam.GetDeviceInfo().GetModelName().c_str(),
               cam.GetDeviceInfo().GetSerialNumber().c_str());

        INodeMap& nm = cam.GetNodeMap();

        printf("\n=== 曝光（Exposure）===\n");
        probe_float(nm, "ExposureTimeAbs");
        probe_float(nm, "ExposureTime");
        probe_int  (nm, "ExposureTimeRaw");

        printf("\n=== 增益（Gain）===\n");
        probe_float(nm, "Gain");
        probe_int  (nm, "GainRaw");
        probe_float(nm, "GainAuto");

        printf("\n=== 行率（Line Rate）===\n");
        probe_float(nm, "AcquisitionLineRate");
        probe_float(nm, "LineRate");
        probe_float(nm, "ResultingLineRatePeriodAbs");

        printf("\n=== TLParamsLocked（Acquisition 中是否鎖定）===\n");
        probe_int(nm, "TLParamsLocked");

        printf("\n--- Grab 1 幀（確認 acquisition 中的 access mode）---\n");
        cam.StartGrabbing(1, GrabStrategy_OneByOne, GrabLoop_ProvidedByUser);
        {
            CGrabResultPtr r;
            cam.RetrieveResult(3000, r, TimeoutHandling_Return);
            if (r && r->GrabSucceeded()) {
                printf("  成功抓 1 幀：%ux%u  ImageSize=%u\n",
                       r->GetWidth(), r->GetHeight(), (unsigned)r->GetImageSize());
            } else {
                printf("  [WARN] 抓幀失敗或逾時\n");
            }
        }
        cam.StopGrabbing();

        printf("\n=== Acquisition 中的 access mode ===\n");
        probe_float(nm, "ExposureTimeAbs");
        probe_float(nm, "ExposureTime");
        probe_float(nm, "Gain");
        probe_int  (nm, "GainRaw");
        probe_int  (nm, "TLParamsLocked");

        cam.Close();
    }  // cam destructs here, before PylonTerminate

    PylonTerminate();
    printf("\n=== 請把上面完整輸出貼給開發者（Gap #2 Stage 0 結果）===\n");
    fflush(stdout);
    fflush(stderr);
    std::_Exit(0);  // bypass global destructors that crash after PylonTerminate
}
