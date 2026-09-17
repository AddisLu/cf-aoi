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
#include <string>
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

static void probe_enum(INodeMap& nm, const char* name) {
    try {
        CEnumParameter p(nm, name);
        EAccessMode acc = p.GetNode()->GetAccessMode();
        GenApi::StringList_t syms;
        try { p.GetSettableValues(syms); } catch (...) {}
        std::string opts;
        for (size_t i = 0; i < syms.size(); ++i) { if (i) opts += ","; opts += syms[i].c_str(); }
        printf("  [Enum ] %-30s  val=%-16s  access=%s  options={%s}\n",
               name, p.GetValue().c_str(),
               (acc == RW) ? "RW" : (acc == RO) ? "RO" : (acc == NA) ? "NA" : "??",
               opts.c_str());
    } catch (...) {
        printf("  [Enum ] %-30s  NOT FOUND\n", name);
    }
}

static void probe_ip(INodeMap& nm, const char* name) {
    try {
        CIntegerParameter p(nm, name);
        EAccessMode acc = p.GetNode()->GetAccessMode();
        long long v = (long long)p.GetValue();
        printf("  [IP   ] %-30s  %lld.%lld.%lld.%lld  access=%s\n",
               name, (v >> 24) & 0xFF, (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF,
               (acc == RW) ? "RW" : (acc == RO) ? "RO" : (acc == NA) ? "NA" : "??");
    } catch (...) {
        printf("  [IP   ] %-30s  NOT FOUND\n", name);
    }
}

int main(int argc, char** argv) {
    const char* serial = (argc > 1) ? argv[1] : nullptr;

    PylonInitialize();
    printf("=== probe_cam_nodes（Gap #2 Stage 0）===\n");

    // cam in explicit scope so destructor runs before PylonTerminate()
    {
        CInstantCamera cam;
        std::string open_err;
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
            // PylonTerminate 不可在 catch 內呼叫：它卸載傳輸層 .so，而例外物件的解構碼
            // 就在那裡 → catch 結束時 SIGSEGV（2026-09-18 實機：相機被別的程式佔用時重現）
            open_err = e.GetDescription();
        }
        if (!open_err.empty()) {
            fprintf(stderr, "[ERROR] 開相機失敗：%s\n", open_err.c_str());
            fflush(stderr);
            std::_Exit(1);         // 同下方成功路徑：略過會在 pylon 收尾後爆掉的全域解構
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

        printf("\n=== PixelFormat（須為 Mono8）===\n");
        probe_enum(nm, "PixelFormat");

        printf("\n=== Auto（須 Off，否則手動曝光/增益被蓋）===\n");
        probe_enum(nm, "ExposureAuto");
        probe_enum(nm, "GainAuto");

        printf("\n=== Trigger（線掃：free-run vs encoder 行觸發）===\n");
        probe_enum(nm, "AcquisitionMode");
        probe_enum(nm, "TriggerSelector");
        probe_enum(nm, "TriggerMode");
        probe_enum(nm, "TriggerSource");

        printf("\n=== ROI ===\n");
        probe_int(nm, "Width");
        probe_int(nm, "Height");
        probe_int(nm, "OffsetX");
        probe_int(nm, "OffsetY");

        printf("\n=== GigE 傳輸（封包/頻寬）===\n");
        probe_int(nm, "GevSCPSPacketSize");
        probe_int(nm, "GevSCPD");

        printf("\n=== GigE 網路 / 綁定（persistent IP）===\n");
        probe_ip(nm, "GevCurrentIPAddress");
        probe_ip(nm, "GevPersistentIPAddress");
        probe_ip(nm, "GevPersistentSubnetMask");
        probe_ip(nm, "GevPersistentDefaultGateway");
        probe_int(nm, "GevCurrentIPConfiguration");

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
