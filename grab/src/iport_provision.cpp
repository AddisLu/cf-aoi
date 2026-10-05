// iport_provision — 下方陣列 iPORT（L803K）CCD 身分配置工具（同 raL8192 的 cam_provision 規則）
//
//   iport_provision list                         列出所有 Pleora iPORT（SN/MAC/IP/名稱）
//   iport_provision set <SN|MAC|IP> CCDnn [ip]   寫 GigE Vision 使用者名稱 CCDnn + persistent IP（預設
//                                                192.168.4.nn），讀回比對，並 ForceIP 立即生效
//        --no-force-ip                           只寫 persistent（下次開機生效），不立即改目前 IP
// 名稱/IP 撞到別台（含 Basler 相機）→ 拒絕；要交換編號時先把其中一台設到暫時位址（例 .101）。
// ⚠️ 設定時 iPORT 不可被其他程式控制（cfaoi_grab 先停、eBUS Player / 調機工具先關）。
// eBUS 經外掛 libcfaoi_cam_ebus.so 載入（eBUS 函式庫載入時就要 GenICam 環境變數，見 grab/CLAUDE.md 不變式 12）。
#include "ebus_plugin.h"

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

int main(int argc, char** argv) {
    const std::string cmd = argc > 1 ? argv[1] : "";
    if (cmd != "list" && cmd != "set") {
        std::fprintf(stderr, "用法：%s list | set <SN|MAC|IP> CCDnn [ip] [--no-force-ip]\n", argv[0]);
        return 2;
    }
    const EbusPlugin& p = ebus_plugin();
    if (!p.error.empty()) return 1;
    if (cmd == "list") {
        std::vector<CamInfo> v;
        p.enumerate(&v);
        std::printf("%-12s %-36s %-13s %-16s %s\n", "SERIAL", "MODEL", "MAC", "IP", "USER_ID");
        for (const auto& c : v)
            std::printf("%-12s %-36s %-13s %-16s %s\n", c.serial.c_str(), c.model.c_str(), c.mac.c_str(),
                        c.ip.c_str(), c.user_id.c_str());
        if (v.empty()) std::printf("（找不到 Pleora iPORT；跨網段時可設 CFAOI_EBUS_DEVICES=ip,… 單播補找）\n");
        return 0;
    }
    std::vector<std::string> pos;
    bool force_ip = true;
    for (int i = 2; i < argc; ++i) {
        if (std::strcmp(argv[i], "--no-force-ip") == 0) force_ip = false;
        else pos.push_back(argv[i]);
    }
    if (pos.size() < 2) { std::fprintf(stderr, "set 需要 <SN|MAC|IP> CCDnn [ip]\n"); return 2; }
    EbusProvisionResult r;
    p.provision(pos[0].c_str(), pos[1].c_str(), pos.size() > 2 ? pos[2].c_str() : "", force_ip, &r);
    std::fputs(r.log.c_str(), r.rc == 0 ? stdout : stderr);
    return r.rc;
}
