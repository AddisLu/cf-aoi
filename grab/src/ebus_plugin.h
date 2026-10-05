#pragma once
// ebus_plugin.h — 載入 eBUS 外掛 libcfaoi_cam_ebus.so（與執行檔同目錄）。cfaoi_grab 與 iport_provision 共用。
// eBUS 函式庫**載入當下**就要 GENICAM_ROOT_V3_4 等環境變數（沒有就 exit）→ 先 setenv 再 dlopen；
// 不用 eBUS 的路徑（pylon）完全不載入。見 grab/CLAUDE.md 不變式 12。
#include "camera.h"

#include <climits>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#include <dlfcn.h>
#include <unistd.h>

// iport_provision 用：一台裝置的 CCD 命名結果
struct EbusProvisionResult {
    int         rc = 1;          // 0 = 成功、1 = 失敗、2 = 參數錯
    std::string log;             // 給人看的訊息（多行）
};

struct EbusPlugin {
    ICamera* (*create)() = nullptr;
    void (*enumerate)(std::vector<CamInfo>*) = nullptr;
    void (*provision)(const char* target, const char* ccd, const char* ip, bool force_ip,
                      EbusProvisionResult* out) = nullptr;
    std::string error;
};

inline const EbusPlugin& ebus_plugin() {
    static EbusPlugin p = [] {
        EbusPlugin r;
#ifdef CFAOI_EBUS_ROOT
        const std::string root = CFAOI_EBUS_ROOT;
        const std::string genicam = root + "/lib/genicam";
        const std::string log = genicam + "/log/config/DefaultLogging.properties";
        const char* home = std::getenv("HOME");
        const std::string cache = std::string(home ? home : "/tmp") + "/.config/Pleora/genicam_cache_v3_4";
        ::setenv("PUREGEV_ROOT", root.c_str(), 0);
        for (const char* k : {"GENICAM_ROOT", "GENICAM_ROOT_V3_4"}) ::setenv(k, genicam.c_str(), 0);
        for (const char* k : {"GENICAM_LOG_CONFIG", "GENICAM_LOG_CONFIG_V3_4"}) ::setenv(k, log.c_str(), 0);
        for (const char* k : {"GENICAM_CACHE", "GENICAM_CACHE_V3_4"}) ::setenv(k, cache.c_str(), 0);
        if (std::system(("mkdir -p '" + cache + "'").c_str()) != 0) { /* 快取建不了只是慢 */ }
        char exe[PATH_MAX] = {0};
        const ssize_t n = ::readlink("/proc/self/exe", exe, sizeof(exe) - 1);
        std::string dir = n > 0 ? std::string(exe, (size_t)n) : std::string(".");
        dir = dir.substr(0, dir.find_last_of('/'));
        const std::string so = dir + "/libcfaoi_cam_ebus.so";
        void* h = ::dlopen(so.c_str(), RTLD_NOW | RTLD_LOCAL);
        if (!h) { r.error = std::string("載入 ") + so + " 失敗：" + ::dlerror(); }
        else {
            r.create = reinterpret_cast<ICamera* (*)()>(::dlsym(h, "cfaoi_ebus_create"));
            r.enumerate = reinterpret_cast<void (*)(std::vector<CamInfo>*)>(::dlsym(h, "cfaoi_ebus_enumerate"));
            r.provision = reinterpret_cast<void (*)(const char*, const char*, const char*, bool, EbusProvisionResult*)>(
                ::dlsym(h, "cfaoi_ebus_provision"));
            if (!r.create || !r.enumerate || !r.provision) r.error = "libcfaoi_cam_ebus.so 缺少進入點（版本不符？重新建置）";
        }
#else
        r.error = "建置時沒有 eBUS SDK（/opt/pleora/ebus）→ eBUS 功能不可用";
#endif
        if (!r.error.empty()) std::fprintf(stderr, "[ebus] ✗ %s\n", r.error.c_str());
        return r;
    }();
    return p;
}
