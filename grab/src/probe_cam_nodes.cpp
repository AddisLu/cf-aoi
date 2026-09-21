// Stage 0 GenICam 節點探測工具
// 用途：連 raL8192-12gm，確認 ExposureTimeAbs/Gain/ResultingLineRateAbs 等節點名稱、
//       單位、min/max/increment、以及 acquisition 中是否可寫（TLParamsLocked）。
//
// ⚠️ 本工具**只讀不寫**：全檔只有 probe_* 呼叫，沒有任何 SetValue/TrySetValue/Execute。
//    跑完相機狀態與跑之前完全相同（除了被開關過一次、抓了 1 幀）。
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

static void probe_bool(INodeMap& nm, const char* name) {
    try {
        CBooleanParameter p(nm, name);
        EAccessMode acc = p.GetNode()->GetAccessMode();
        printf("  [Bool ] %-30s  val=%-12s  access=%s\n",
               name, p.GetValue() ? "true" : "false",
               (acc == RW) ? "RW" : (acc == RO) ? "RO" : (acc == NA) ? "NA" : "??");
    } catch (...) {
        printf("  [Bool ] %-30s  NOT FOUND\n", name);
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

        // ⚠️ 2026-09-21 勘誤：原本探的 AcquisitionLineRate / LineRate /
        // ResultingLineRatePeriodAbs 三個名字在 raL8192 的 XML 裡都不存在（少了 Abs
        // 後綴、Rate/Period 位置也顛倒）→ 印 NOT FOUND，被誤記成「此相機無行率節點」
        // 寫進 docs/STATUS.md:107，成了「12kHz 無從驗證」的源頭。正確名稱如下。
        printf("\n=== 行率（Line Rate）===\n");
        // 主角：相機自報的行速率上限，已把 ROI + 曝光 + **頻寬** 三項算進去
        probe_float(nm, "ResultingLineRateAbs");
        probe_float(nm, "ResultingLinePeriodAbs");   // 上者倒數，交叉驗算
        probe_float(nm, "AcquisitionLineRateAbs");   // free-run 設定值（被設死的話看這裡）
        probe_float(nm, "ReadoutTimeAbs");           // 感測器讀出時間 = 物理地板
        probe_float(nm, "ExposureOverlapTimeMaxAbs");// 曝光與讀出是否重疊

        printf("\n=== 幀率（Frame Rate；鎖住幀率的第二條繩子）===\n");
        probe_float(nm, "ResultingFrameRateAbs");
        probe_float(nm, "ResultingFramePeriodAbs");
        probe_float(nm, "AcquisitionFrameRateAbs");
        probe_bool (nm, "AcquisitionFrameRateEnable");

        printf("\n=== PixelFormat（須為 Mono8）===\n");
        probe_enum(nm, "PixelFormat");

        printf("\n=== Auto（須 Off，否則手動曝光/增益被蓋）===\n");
        probe_enum(nm, "ExposureAuto");
        probe_enum(nm, "GainAuto");

        printf("\n=== Trigger（線掃：free-run vs encoder 行觸發）===\n");
        probe_enum(nm, "AcquisitionMode");
        probe_enum(nm, "TriggerSelector");
        probe_enum(nm, "TriggerMode");          // ⚠️ 只反映「當下選中的 selector」
        probe_enum(nm, "TriggerSource");
        probe_enum(nm, "TriggerActivation");
        printf("  ⚠ 上面三項只是 TriggerSelector 當下選中那一個的值。要看 LineStart /\n"
               "    FrameStart / AcquisitionStart 各自的 TriggerMode，必須逐一寫入\n"
               "    TriggerSelector 才讀得到 —— 本工具刻意不寫相機，故不做。\n"
               "    （cam_pylon.cpp:88 的 TriggerMode=Off 同樣只關掉當下那一個。）\n");
        probe_enum(nm, "AcquisitionStatusSelector");
        probe_bool(nm, "AcquisitionStatus");

        printf("\n=== ROI ===\n");
        probe_int(nm, "Width");
        probe_int(nm, "Height");
        probe_int(nm, "OffsetX");
        probe_int(nm, "OffsetY");

        printf("\n=== GigE 傳輸（封包/頻寬）===\n");
        probe_int (nm, "GevSCPSPacketSize");
        probe_int (nm, "GevSCPD");             // inter-packet delay（本拓樸應為 0）
        probe_bool(nm, "GevSCPSDoNotFragment");
        probe_int (nm, "GevSCFTD");            // frame transmission delay
        probe_int (nm, "GevLinkSpeed");
        // 頻寬保留/節流：GevSCDMT（最大可能吞吐）− GevSCBWA（實際分配）= 相機自我節流量，
        // 直接對應 ResultingLineRateAbs 裡的頻寬項。125MB/s 的 10% 保留 ≈ 1.2kHz 行速率。
        probe_int(nm, "GevSCBWR");             // 保留百分比
        probe_int(nm, "GevSCBWRA");            // 保留倍率
        probe_int(nm, "GevSCBWA");             // 實際分配 B/s
        probe_int(nm, "GevSCDCT");             // 目前吞吐 B/s
        probe_int(nm, "GevSCDMT");             // 最大可能吞吐 B/s
        probe_int(nm, "GevTimestampTickFrequency");
        probe_int(nm, "GevHeartbeatTimeout");

        printf("\n=== UserSet（相機 flash 裡的開機預設；只讀）===\n");
        probe_enum(nm, "UserSetSelector");
        probe_enum(nm, "UserSetDefaultSelector");
        printf("  ⚠ 本工具不執行 UserSetSave/UserSetLoad —— UserSetSave 會把當下設定\n"
               "    烤進 flash 變成新的開機預設。\n");

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

        // 注意：這是**另一個 node map**（主機端 stream grabber，不是相機的）。
        // 統計值是本次 open 以來的累計，上面只抓了 1 幀，所以數字很小；
        // 重點在 Resend/Failed 是不是 0 —— 那是唯一能區分「相機送得慢」與
        // 「我們掉包、相機在重送」的證據（重送吃掉的頻寬會表現成行速率變低，
        // 而 grab 的 dropped=0 完全看不見它）。
        printf("\n=== Stream grabber（主機端收流參數與統計）===\n");
        try {
            INodeMap& sg = cam.GetStreamGrabberNodeMap();
            probe_int (sg, "SocketBufferSize");
            probe_int (sg, "MaxNumBuffer");
            probe_int (sg, "MaxBufferSize");
            probe_int (sg, "ReceiveWindowSize");
            probe_bool(sg, "EnableResend");
            probe_int (sg, "PacketTimeout");
            probe_int (sg, "FrameRetention");
            probe_int (sg, "ResendRequestThreshold");
            probe_int (sg, "Statistic_Total_Packet_Count");
            probe_int (sg, "Statistic_Failed_Packet_Count");
            probe_int (sg, "Statistic_Resend_Request_Count");
            probe_int (sg, "Statistic_Resend_Packet_Count");
            probe_int (sg, "Statistic_Failed_Buffer_Count");
            probe_int (sg, "Statistic_Buffer_Underrun_Count");
            probe_int (sg, "Statistic_Missed_Frame_Count");
        } catch (const GenericException& e) {
            printf("  [WARN] 取不到 stream grabber node map：%s\n", e.GetDescription());
        }

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
