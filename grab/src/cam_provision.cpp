// cam_provision — 相機身分配置工具（CCD 槽位 = DeviceUserID + persistent IP）
//
// 身分規則（2026-09-17 決策，取代以 MAC 為主的綁定）：
//   相機 DeviceUserID = "CCDnn" → cam_id = nn（存在相機 flash，pylon Viewer 直接顯示此名稱）
//   persistent IP      = 192.168.5.nn/24（CCD01→.1 … CCD37→.37；.200 = 截取中心主機）
//   ⚠️ 2026-09-18 起編號 **1 開頭**（CCD01–CCD37）且 IP 尾碼 = CCD 編號，兩者一眼對得上。
// 換相機 SOP：pylon Viewer 設 Device User ID + IP Configurator 設 IP 即可；本工具是 CLI 等效做法。
//
// 用法：
//   cam_provision list                       列出所有相機（SN/MAC/IP/UserID，不開相機）
//   cam_provision set <serial> CCDnn [ip]    寫 DeviceUserID + persistent IP，並 ForceIp 立即生效
//                                            （ip 省略 = 192.168.5.nn；換 IP 時可顯式指定暫時位址避開衝突）
//
// ⚠️ set 時相機不可被其他程式開著（cfaoi_grab 先 GRAB_STOP / pylon Viewer 先關）。

#include <pylon/PylonIncludes.h>
#include <pylon/ParameterIncludes.h>
#include <pylon/gige/GigETransportLayer.h>
#include <pylon/gige/PylonGigEDevice.h>

#include <cstdio>
#include <regex>
#include <string>

using namespace Pylon;

static int do_list(DeviceInfoList_t& devs) {
    printf("%-10s %-14s %-17s %-16s %-10s %s\n", "SERIAL", "MODEL", "MAC", "IP", "IPCONFIG", "USER_ID");
    for (size_t i = 0; i < devs.size(); ++i) {
        const CDeviceInfo& d = devs[i];
        printf("%-10s %-14s %-17s %-16s %-10s %s\n",
               d.GetSerialNumber().c_str(), d.GetModelName().c_str(),
               d.IsMacAddressAvailable() ? d.GetMacAddress().c_str() : "-",
               d.IsIpAddressAvailable() ? d.GetIpAddress().c_str() : "-",
               d.IsIpConfigCurrentAvailable() ? d.GetIpConfigCurrent().c_str() : "-",
               d.IsUserDefinedNameAvailable() ? d.GetUserDefinedName().c_str() : "");
    }
    return 0;
}

static int do_set(DeviceInfoList_t& devs, const std::string& serial,
                  const std::string& ccd, std::string ip) {
    std::smatch m;
    if (!std::regex_match(ccd, m, std::regex("CCD([0-9]{2})"))) {
        fprintf(stderr, "CCD 名稱須為 CCDnn（例 CCD03），收到 %s\n", ccd.c_str());
        return 2;
    }
    const int n = std::stoi(m[1].str());
    if (n == 0) {   // 編號 1 開頭；CCD00 會算出 192.168.5.0（網段位址，不可用）
        fprintf(stderr, "CCD 編號自 CCD01 起（IP 尾碼 = 編號；CCD00 對到無效的 192.168.5.0）\n");
        return 2;
    }
    if (ip.empty()) ip = "192.168.5." + std::to_string(n);   // CCDnn → .nn（1 開頭）

    const CDeviceInfo* target = nullptr;
    for (size_t i = 0; i < devs.size(); ++i) {
        const CDeviceInfo& d = devs[i];
        if (d.GetSerialNumber() == serial.c_str()) { target = &devs[i]; continue; }
        // 名稱/IP 撞到別台 → 拒絕（grab 端遇到重複也會拒開，這裡先擋）
        if (d.IsUserDefinedNameAvailable() && d.GetUserDefinedName() == ccd.c_str()) {
            fprintf(stderr, "%s 已被 SN=%s 使用，請先改掉那台（或拔除舊相機）\n",
                    ccd.c_str(), d.GetSerialNumber().c_str());
            return 3;
        }
        if (d.IsIpAddressAvailable() && d.GetIpAddress() == ip.c_str()) {
            fprintf(stderr, "IP %s 已被 SN=%s 使用\n", ip.c_str(), d.GetSerialNumber().c_str());
            return 3;
        }
    }
    if (!target) { fprintf(stderr, "找不到序號 %s（先跑 list）\n", serial.c_str()); return 4; }
    const String_t mac = target->GetMacAddress();

    {
        IPylonDevice* dev = CTlFactory::GetInstance().CreateDevice(*target);
        auto* gd = dynamic_cast<IPylonGigEDevice*>(dev);
        if (!gd) {
            CTlFactory::GetInstance().DestroyDevice(dev);
            fprintf(stderr, "SN=%s 不是 GigE 裝置\n", serial.c_str());
            return 5;
        }
        try {
            dev->Open();
            CStringParameter(dev->GetNodeMap(), "DeviceUserID").SetValue(ccd.c_str());
            gd->ChangeIpConfiguration(/*persistent*/ true, /*dhcp*/ false);
            gd->SetPersistentIpAddress(ip.c_str(), "255.255.255.0", "0.0.0.0");
            dev->Close();
        } catch (...) {
            if (dev->IsOpen()) dev->Close();
            CTlFactory::GetInstance().DestroyDevice(dev);
            throw;
        }
        CTlFactory::GetInstance().DestroyDevice(dev);
    }

    // persistent IP 要重開機才用；ForceIp 讓目前 IP 立即換成同一個值（相機需未開啟）
    ITransportLayer* tl = CTlFactory::GetInstance().CreateTl(BaslerGigEDeviceClass);
    auto* gtl = dynamic_cast<IGigETransportLayer*>(tl);
    if (gtl) gtl->ForceIp(mac, ip.c_str(), "255.255.255.0", "0.0.0.0");
    CTlFactory::GetInstance().ReleaseTl(tl);

    printf("SN=%s MAC=%s → DeviceUserID=%s persistent IP=%s/24（已 ForceIp）\n",
           serial.c_str(), mac.c_str(), ccd.c_str(), ip.c_str());
    return 0;
}

int main(int argc, char** argv) {
    const std::string cmd = argc > 1 ? argv[1] : "";
    if (cmd != "list" && !(cmd == "set" && (argc == 4 || argc == 5))) {
        fprintf(stderr, "用法：%s list | set <serial> CCDnn [ip]\n", argv[0]);
        return 1;
    }
    PylonAutoInitTerm init;
    try {
        DeviceInfoList_t devs;
        CTlFactory::GetInstance().EnumerateDevices(devs);
        if (cmd == "list") return do_list(devs);
        return do_set(devs, argv[2], argv[3], argc == 5 ? argv[4] : "");
    } catch (const GenericException& e) {
        fprintf(stderr, "pylon 錯誤：%s\n", e.GetDescription());
        return 10;
    }
}
