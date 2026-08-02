// ═══ 📖 手冊對照（docs/html/cf-aoi-training.html，開啟後 ⌘K 搜章節）═══
// [手冊 r2] Grab 架構與狀態機 / R2.4 8100 命令表
// [手冊 p2] 「線不動了」破案卡（grabbed 漲、sent_frames 凍=送端斷）
// ═══════════════════════════════════════════════════════════════
// =============================================================================
// cfaoi_grab — Grab 主程式（截取中心 damac，x86 Linux）
// -----------------------------------------------------------------------------
// 狀態機：
//   IDLE → (LOAD_RECIPE) → IDLE（更新 panel_id）
//        → (GRAB_START)  → GRABBING（開相機陣列 + 連 RDMA + 送幀）
//        → (GRAB_STOP)   → IDLE（停相機 + 斷 RDMA）
//
// 軟體觸發架構（docs plan「37 CCD 觸發設計」）：GRAB_START = 觸發。
//   逐台平行 arm（啟動 skew 由 IP 端玻璃前緣對位吸收），每台收滿
//   frames_per_panel 張 × 5000 條自動停；sliceIndex/totalSlice 進 FrameHeader。
//
// 用法：
//   cfaoi_grab --rdma-dest 192.168.3.1:18515 [options]
//
// 選項：
//   --rdma-dest   IP:PORT    Spark IP 端的 RDMA server（必填）
//   --cam-count   N|ALL      啟用相機台數（預設 1；ALL = 列舉到的全部）
//   --frames-per-panel N     每片每台張數（預設 0 = 連續；GRAB_START params 可覆蓋）
//   --cam-id      N          單台模式 cam_id（預設 0；多台依列舉順序 0..N-1）
//   --serial      STRING     pylon 序號；auto = 第一台（僅單台模式，預設 auto）
//   --pkt-size    N          GevSCPSPacketSize（預設 8192）
//   --ctrl-port   N          等 Control 連入的 port（預設 8100）
//   --cam-config  PATH       相機參數 JSON（預設 <exe上一層>/cam_config.json = grab/，
//                            不隨 CWD 漂移；每台一筆 cam_id 條目）
//   --cam-map     PATH       MAC↔cam_id 穩定映射（預設 <exe上一層>/cam_map.json；Gap #21）
//                            有此檔 → 嚴格模式（未列於映射的相機拒開）；無 → 列舉順序暫派 + WARN
// =============================================================================

#include "cam_manager.h"
#include "control_server.h"
#include "rdma_sender.h"
#include "../../shared/FrameHeader.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <limits.h>
#include <sys/stat.h>
#include <unistd.h>

#include <nlohmann/json.hpp>
using json = nlohmann::json;

// ---- cam_config.json helpers（每台一筆 {cam_id, exposure_us, gain_raw}）----

struct CamCfg {
    float exposure_us = 70.0f;   // Stage 0 actual: raL8192-12gm 出廠預設
    int   gain_raw    = 256;     // Stage 0 actual: min raw = 0dB 基準
};

// ⚠️ 已知限制（docs/code_review_20260802.md B12）：檔案存在但 JSON 壞掉時 catch(...) 靜默回
//   出廠預設（70µs/256）——與 cam_map 的 fail-fast 哲學不一致；save_cam_config 寫失敗亦全吞。
static CamCfg load_cam_config(const std::string& path, int cam_id) {
    CamCfg cfg;
    try {
        std::ifstream f(path);
        if (!f.is_open()) return cfg;
        auto j = json::parse(f);
        if (j.contains("cameras") && j["cameras"].is_array()) {
            for (auto& c : j["cameras"]) {
                if (c.value("cam_id", -1) == cam_id) {
                    cfg.exposure_us = c.value("exposure_us", cfg.exposure_us);
                    cfg.gain_raw    = c.value("gain_raw",    cfg.gain_raw);
                    break;
                }
            }
        }
    } catch (...) {}
    return cfg;
}

static void save_cam_config(const std::string& path, int cam_id, float exp_us, int gain_raw) {
    json j;
    try {
        std::ifstream fi(path);
        if (fi.is_open()) j = json::parse(fi);
    } catch (...) {}
    if (!j.contains("cameras") || !j["cameras"].is_array())
        j["cameras"] = json::array();
    bool found = false;
    for (auto& c : j["cameras"]) {
        if (c.value("cam_id", -1) == cam_id) {
            c["exposure_us"] = exp_us;
            c["gain_raw"]    = gain_raw;
            found = true; break;
        }
    }
    if (!found)
        j["cameras"].push_back({{"cam_id",cam_id},{"exposure_us",exp_us},{"gain_raw",gain_raw}});
    try {
        std::ofstream fo(path);
        fo << j.dump(2) << "\n";
    } catch (...) {}
}

// ---- 全域停止旗標（SIGINT/SIGTERM 用）----
static std::atomic<bool> g_shutdown{false};
static void sig_handler(int) { g_shutdown = true; }

// ---- 設定檔路徑錨定（cam_config.json / cam_map.json 勿依賴 CWD）----
// 預設路徑錨定在「執行檔所在目錄的上一層」（二進位固定產出於 grab/build/ → 上一層 = grab/，
// 與 .gitignore 及文件所指位置一致）。從任何 CWD 啟動都讀寫同一份，SET_CAM_PARAMS 回寫、
// GRAB_ARM 重套、SET_CAM_MAP 綁定才不會依啟動位置漂移（2026-07-30 實測 grab/ 與
// grab/build/ 兩份 cam_config 已不同步；cam_map 換 CWD 啟動更會被當成「不存在」而
// 靜默丟失 MAC 綁定）。明確傳 --cam-config/--cam-map 時維持一般 CLI 語意（相對 CWD）。

static std::string exe_dir() {
    char buf[PATH_MAX];
    ssize_t n = ::readlink("/proc/self/exe", buf, sizeof(buf) - 1);
    if (n <= 0) return ".";                     // 取不到（極罕見）→ 退回 CWD
    buf[n] = '\0';
    std::string p(buf);
    auto slash = p.rfind('/');
    return (slash == std::string::npos) ? "." : p.substr(0, slash);
}

// 轉絕對路徑；檔案尚不存在（首次啟動）時正規化其目錄再接檔名
static std::string abs_path(const std::string& p) {
    char buf[PATH_MAX];
    if (::realpath(p.c_str(), buf)) return buf;
    auto slash = p.rfind('/');
    std::string dir  = (slash == std::string::npos) ? "." : p.substr(0, slash);
    std::string base = (slash == std::string::npos) ? p   : p.substr(slash + 1);
    if (::realpath(dir.c_str(), buf)) return std::string(buf) + "/" + base;
    return p;
}

static bool same_file(const std::string& a, const std::string& b) {
    struct stat sa{}, sb{};
    if (::stat(a.c_str(), &sa) != 0 || ::stat(b.c_str(), &sb) != 0) return false;
    return sa.st_dev == sb.st_dev && sa.st_ino == sb.st_ino;
}

// ---- 從 "IP:PORT" 字串切開 ----
static bool parse_host_port(const std::string& s, std::string& host, std::string& port) {
    auto colon = s.rfind(':');
    if (colon == std::string::npos || colon == 0 || colon + 1 >= s.size()) return false;
    host = s.substr(0, colon);
    port = s.substr(colon + 1);
    return true;
}

int main(int argc, char** argv) {
    // ---- 解析 CLI ----
    std::string rdma_dest;
    int         cam_count   = 1;       // 1 = 單台（legacy）；0 = ALL；N = 前 N 台
    int         cli_frames  = 0;       // 每片每台張數預設（0 = 連續；GRAB_START 可覆蓋）
    uint16_t    cam_id      = 0;       // 單台模式使用；多台依列舉順序派 0..N-1
    std::string serial      = "auto";
    int64_t     pkt_size    = 8192;
    int         ctrl_port   = 8100;
    std::string cam_cfg_path;                  // 空 = 預設 exe 上一層/cam_config.json
    std::string cam_map_path;                  // 空 = 預設 exe 上一層/cam_map.json

    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        auto next = [&]() -> const char* {
            return (i + 1 < argc) ? argv[++i] : "";
        };
        if      (a == "--rdma-dest")   rdma_dest    = next();
        else if (a == "--cam-count")   { std::string v = next();
                                         cam_count = (v == "ALL" || v == "all") ? 0 : atoi(v.c_str()); }
        else if (a == "--frames-per-panel") cli_frames = atoi(next());
        else if (a == "--cam-id")      cam_id       = (uint16_t)atoi(next());
        else if (a == "--serial")      serial       = next();
        else if (a == "--pkt-size")    pkt_size     = atoll(next());
        else if (a == "--ctrl-port")   ctrl_port    = atoi(next());
        else if (a == "--cam-config")  cam_cfg_path = next();
        else if (a == "--cam-map")     cam_map_path = next();
        else { fprintf(stderr, "未知參數：%s\n", a.c_str()); return 1; }
    }

    if (rdma_dest.empty()) {
        fprintf(stderr, "必填：--rdma-dest IP:PORT\n"); return 1;
    }
    std::string rdma_host, rdma_port;
    if (!parse_host_port(rdma_dest, rdma_host, rdma_port)) {
        fprintf(stderr, "--rdma-dest 格式錯誤，應為 IP:PORT\n"); return 1;
    }

    // ---- 設定檔路徑解析（見 exe_dir 註解；一律印絕對路徑，漂移可直接從 log 看出）----
    {
        const std::string cfg_root = exe_dir() + "/..";
        if (cam_cfg_path.empty()) cam_cfg_path = cfg_root + "/cam_config.json";
        if (cam_map_path.empty()) cam_map_path = cfg_root + "/cam_map.json";
        cam_cfg_path = abs_path(cam_cfg_path);
        cam_map_path = abs_path(cam_map_path);
        printf("[main] cam_config → %s\n", cam_cfg_path.c_str());
        printf("[main] cam_map    → %s\n", cam_map_path.c_str());
        // 遷移守衛：CWD 下有同名檔但不是採用的那份（多半是舊版從該目錄啟動所留的副本）
        struct { const char* name; const std::string& used; } files[] = {
            {"cam_config.json", cam_cfg_path}, {"cam_map.json", cam_map_path}};
        for (auto& f : files) {
            struct stat st{};
            if (::stat(f.name, &st) == 0 && !same_file(f.name, f.used))
                fprintf(stderr, "[main] ⚠ 目前目錄下的 %s 不會被使用（採用 %s）；"
                        "若為舊版殘留，請人工併值後刪除，避免誤判生效中的參數\n",
                        f.name, f.used.c_str());
        }
    }

    ::signal(SIGINT,  sig_handler);
    ::signal(SIGTERM, sig_handler);

    // ---- 元件 ----
    CamManager    mgr;
    RdmaSender    sender;
    ControlServer ctrl(ctrl_port);

    // ---- Gap #21：MAC↔cam_id 穩定映射（越早載越好，LIST_CAMERAS/ARM 都要用）----
    // 檔案不存在 = 合法的舊行為（warn 提示）；檔案存在但格式錯 = 直接中止，
    // 不可默默退回不穩定的列舉順序（那會讓生產機在無人察覺下把槽位對錯台）。
    {
        std::string map_err, map_warn;
        if (!mgr.load_map(cam_map_path, map_err, map_warn)) {
            fprintf(stderr, "[main] cam_map 載入失敗：%s\n", map_err.c_str());
            return 1;
        }
        if (!map_warn.empty()) fprintf(stderr, "[main] ⚠ %s\n", map_warn.c_str());
    }

    // ---- 狀態（mutex 保護，跨 ControlServer thread 與 N 個 cam grab thread）----
    std::mutex          state_mtx;
    bool                grabbing    = false;   // GRAB_START 後為 true（收滿自動停不清，GRAB_STOP 才清）
    bool                armed       = false;   // GRAB_ARM 後為 true（相機開+參數套+RDMA 連）
    std::string         panel_id    = "PANEL";
    // panel_hash 刻意用 atomic 而非 state_mtx 保護：frame_cb 只讀這一個 uint32，若在回呼裡取
    // state_mtx，會與「GRAB_STOP 持 state_mtx → stop_all() → thread.join()」形成鎖環而死結
    // （2026-07-30 實機 gdb 確認：連續模式必死，state_mtx 永久被持有 → 8100 全掛，只能 kill -9）。
    std::atomic<uint32_t> panel_hash{frame_panel_hash(panel_id)};
    std::atomic<uint64_t> frame_seq{0};

    // ---- 每片切片狀態（GRAB_START 時設定；grab threads 啟動前就緒）----
    std::mutex            send_mtx;            // N 相機 thread 共用一條 RDMA QP → 序列化
    std::atomic<uint32_t> total_slice{1};      // frames_per_panel（0/連續 → 1，legacy 語意）
    std::vector<uint32_t> slice_seq;           // 每台自己的 slice 計數（各 thread 只動自己那格）

    // ---- 每幀回呼（跑在各 CamPylon 的 grab thread 裡，併發）----
    FrameCb frame_cb = [&](uint16_t cid, const uint8_t* data,
                           uint32_t bytes, uint32_t w, uint32_t h) {
        uint32_t total = total_slice.load(std::memory_order_relaxed);
        uint16_t slice = 0;
        if (total > 1) {
            if (cid >= slice_seq.size()) return;      // 防呆：未知 cam
            uint32_t s = slice_seq[cid]++;            // 該台自己的 thread 才會動這格
            if (s >= total) return;                   // 收滿後的殘幀不送（自動停前的競態）
            slice = (uint16_t)s;
        }
        uint32_t phash = panel_hash.load(std::memory_order_relaxed);  // 不取 state_mtx（見宣告處註解）
        // app-CRC **必須在 send_mtx 之外算**：它是送端最貴的一步（40.8MB 實測 ~16ms/幀），
        // 留在鎖內會讓 37 台共用的單一 QP 全部序列化（上限 ~50 幀/s < 需求 88.8 幀/s）。
        // 此處 data 是本相機 thread 自己的 pylon 緩衝，併發計算彼此無關，安全。
        const uint32_t crc = RdmaSender::crc_of(data, bytes);
        std::lock_guard<std::mutex> lk(send_mtx);
        // ⚠️ seq 必須在 send_mtx 內指派：送端 buffer(rdma_sender buf_idx=seq%n_buf) 與遠端
        // slot(slot_id=seq%n_slots) 都由 seq 導出，但流量控制是「計數式」(poll_one / posted-recv
        // credit) → 只有「seq 順序 == wire 順序」時環才安全。若在鎖外指派，多相機 thread 會讓
        // 兩者成為置換關係 → slot 可能在收端 memcpy 前被重寫（2026-07-30 實機：2 台+背壓 err=9/20
        // CRC 損毀，1 台或無背壓皆 0）。
        uint64_t seq = ++frame_seq;  // atomic + send_mtx：全域唯一且單調，順序 == wire 順序
        sender.send_frame(cid, seq, phash, data, bytes, w, h,
                          slice, (uint16_t)(total > 65535u ? 65535u : total), crc);
    };

    // ---- Control 回呼 ----
    ctrl.set_load_recipe([&](const std::string& recipe, const std::string& pid) {
        std::lock_guard<std::mutex> lk(state_mtx);
        panel_id = pid.empty() ? recipe : pid;
        const uint32_t h = frame_panel_hash(panel_id);
        panel_hash.store(h, std::memory_order_relaxed);
        printf("[main] LOAD_RECIPE recipe=%s panel_id=%s hash=0x%08x\n",
               recipe.c_str(), panel_id.c_str(), h);
    });

    // ---- ARM（預熱，冪等）：開陣列 → 套每台曝光/增益 → 連 RDMA。呼叫端須已持 state_mtx。
    // 軟體觸發架構（docs plan「37 CCD 觸發設計」）：冷啟重活（開 37 台=秒級、RDMA connect）
    // 全部提前到這裡（掛在 LOAD_RECIPE 後的空檔）；GRAB_START 觸發本體只剩 ms 級 start_all。
    auto do_arm = [&](std::string& err) -> bool {
        // 開相機陣列（冪等：台數符合直接重用；fail-fast：任一台失敗全關）
        if (!mgr.open_all(cam_count, serial, pkt_size, err)) return false;
        // 單台模式沿用 --cam-id（legacy：FrameHeader.camId 可自訂）；多台依列舉順序 0..N-1
        // ⚠️ 已知限制（docs/code_review_20260802.md B7）：有 cam_map 時這行仍會把 MAC 綁定查回的
        //   cam_id 蓋成 CLI 值（預設 0）→ 單台隔離測試時 camId／cam_config 歸屬對錯台。
        if (cam_count == 1 && !mgr.empty()) mgr.entries().front().cam_id = cam_id;

        // 每台套用 cam_config.json 對應條目的曝光/增益（read-back actual 後 re-save）
        for (auto& e : mgr.entries()) {
            auto cfg = load_cam_config(cam_cfg_path, e.cam_id);
            float exp_actual; int gain_actual;
            if (e.cam->set_params(cfg.exposure_us, cfg.gain_raw, exp_actual, gain_actual)) {
                save_cam_config(cam_cfg_path, e.cam_id, exp_actual, gain_actual);
                printf("[main] cam%u 套用 cam_config：exp=%.1f→%.1fµs  gain=%d→%d raw\n",
                       e.cam_id, cfg.exposure_us, exp_actual, cfg.gain_raw, gain_actual);
            } else {
                printf("[main] 警告：cam%u cam_config 套用失敗，使用相機目前曝光/增益\n",
                       e.cam_id);
            }
        }

        // 連 RDMA（冪等；frame_cap = 陣列最大 PayloadSize）
        if (!sender.is_connected() &&
            !sender.connect(rdma_host.c_str(), rdma_port.c_str(),
                            (size_t)mgr.max_payload())) {
            mgr.stop_all();
            err = "RDMA connect failed";
            return false;
        }
        armed = true;
        printf("[main] GRAB_ARM  %zu 台就緒  rdma→%s:%s（等觸發）\n",
               mgr.size(), rdma_host.c_str(), rdma_port.c_str());
        return true;
    };

    ctrl.set_grab_arm([&](std::string& err) -> bool {
        std::lock_guard<std::mutex> lk(state_mtx);
        if (grabbing && mgr.running_count() > 0) { err = "grabbing 中，不可 ARM"; return false; }
        return do_arm(err);
    });

    // GRAB_START = 觸發本體：已 ARM → 只做切片歸零 + start_all（ms 級）。
    // 未 ARM → 自動先 ARM（相容 nc 手動測試；代價 = 冷啟秒級，產線流程應先 ARM）。
    // frames_per_panel（命令參數優先，其次 --frames-per-panel CLI）>0 → 每台收滿自動停。
    // ⚠️ 已知限制（docs/code_review_20260802.md B8）：timeout_ms 在此被忽略——收滿無 watchdog，
    //   任一台 0 幀時 panel 永不完成（running 永 ≥1），逾時控管完全依賴上位機。
    ctrl.set_grab_start([&](int /*timeout_ms*/, int frames_per_panel,
                            std::string& err) -> bool {
        std::lock_guard<std::mutex> lk(state_mtx);
        // 收滿自動停後 running_count=0 → 視為上一片完成，允許直接開下一片
        if (grabbing && mgr.running_count() > 0) { err = "already grabbing"; return false; }
        if (!armed) {
            printf("[main] GRAB_START 未 ARM → 自動預熱（冷啟；產線請先 GRAB_ARM）\n");
            if (!do_arm(err)) return false;
        }
        const int n_frames = frames_per_panel > 0 ? frames_per_panel : cli_frames;

        // 切片狀態就緒後才 start（grab threads 啟動前 slice_seq 已定，無競態）
        // ⚠️ 不重置 frame_seq：它是「全域唯一且單調」的，跨片歸零會 ①片界連續兩筆打到同一 slot
        //   （slot=seq%n_slots）→ 覆寫風險 ②IP 端 rdma-validate 報 seq 跳躍 ③rdma-process 以
        //   frameSeq 命名 panel 夾 → 下一片與上一片同名，result_saver 清舊檔會抹掉上一片結果。
        //   片內序號請用 sliceIndex/totalSlice（下面 slice_seq 才是 per-panel 狀態）。
        uint16_t max_id = 0;
        for (auto& e : mgr.entries()) max_id = std::max(max_id, e.cam_id);
        slice_seq.assign((size_t)max_id + 1, 0u);   // 以 cam_id 直接索引（含 --cam-id 自訂值）
        total_slice.store(n_frames > 0 ? (uint32_t)n_frames : 1u);

        mgr.start_all((uint64_t)(n_frames > 0 ? n_frames : 0), frame_cb);
        grabbing = true;
        printf("[main] GRAB_START  %zu 台  rdma→%s:%s  panel=%s  frames/panel=%d%s\n",
               mgr.size(), rdma_host.c_str(), rdma_port.c_str(), panel_id.c_str(),
               n_frames, n_frames > 0 ? "" : "(連續)");
        return true;
    });

    ctrl.set_grab_stop([&]() {
        std::lock_guard<std::mutex> lk(state_mtx);
        if (!grabbing && !armed) return;
        mgr.stop_all();
        sender.disconnect();
        grabbing = false;
        armed    = false;
        printf("[main] GRAB_STOP  已送 %llu 幀（%llu bytes）\n",
               (unsigned long long)sender.sent_frames(),
               (unsigned long long)sender.sent_bytes());
    });

    // Gap #2：SET_CAM_PARAMS handler（依 cam_id 路由；該台未開 → 只存 JSON，啟動時套用）
    ctrl.set_cam_params_handler([&](int cid, float exp_us, int gain_raw,
                                     float& exp_actual, int& gain_actual,
                                     std::string& err) -> bool {
        std::lock_guard<std::mutex> lk(state_mtx);
        CamPylon* c = mgr.get(cid);
        // 陣列已開卻找不到該 cam_id → 誠實 ERR（不可默默寫進 cam_config 假裝成功）
        if (!c && !mgr.empty()) { err = "unknown cam_id " + std::to_string(cid); return false; }
        if (!c || !c->is_open()) {
            save_cam_config(cam_cfg_path, cid, exp_us, gain_raw);
            exp_actual  = exp_us;
            gain_actual = gain_raw;
            printf("[main] SET_CAM_PARAMS: cam%d 未開，已存 cam_config exp=%.1fµs gain=%d raw\n",
                   cid, exp_us, gain_raw);
            return true;
        }
        if (!c->set_params(exp_us, gain_raw, exp_actual, gain_actual)) {
            err = "相機寫入失敗";
            return false;
        }
        save_cam_config(cam_cfg_path, cid, exp_actual, gain_actual);
        return true;
    });

    // Gap #2：GET_CAM_PARAMS handler（該台未開 → 回 cam_config.json 對應條目）
    ctrl.get_cam_params_handler([&](int cid,
                                     float& exp_actual, int& gain_actual,
                                     std::string& err) -> bool {
        std::lock_guard<std::mutex> lk(state_mtx);
        CamPylon* c = mgr.get(cid);
        if (!c && !mgr.empty()) { err = "unknown cam_id " + std::to_string(cid); return false; }
        if (!c || !c->is_open()) {
            auto cfg = load_cam_config(cam_cfg_path, cid);
            exp_actual  = cfg.exposure_us;
            gain_actual = cfg.gain_raw;
            return true;
        }
        if (!c->get_params(exp_actual, gain_actual)) {
            err = "相機讀取失敗";
            return false;
        }
        return true;
    });

    // 相機陣列總覽：LIST_CAMERAS（唯讀列舉，不開相機、不改相機）
    ctrl.set_list_cameras_handler([&]() -> std::string {
        auto cams = CamPylon::enumerate_cameras();
        mgr.annotate(cams);   // 依 cam_map.json 填 cam_id/ccd_id/bound（Gap #21）
        json arr = json::array();
        for (const auto& c : cams) {
            arr.push_back({
                {"cam_id",       c.cam_id},
                {"ccd_id",       c.ccd_id},   // 未綁定為空字串
                {"bound",        c.bound},    // false = 未列於 cam_map（不得當成已就位）
                {"mac",          c.mac},
                {"model",        c.model},
                {"serial",       c.serial},
                {"ip",           c.ip},
                {"online",       c.online},
                {"persistent",   c.persistent},
                {"ip_config",    c.ip_config},
                {"device_class", c.device_class}
            });
        }
        size_t nbound = 0;
        for (const auto& c : cams) nbound += c.bound ? 1 : 0;
        printf("[main] LIST_CAMERAS → %zu 台（已綁定 %zu / 未綁定 %zu；cam_map %zu 筆）\n",
               cams.size(), nbound, cams.size() - nbound, mgr.map_size());
        return arr.dump();
    });

    // GET_CAM_NODES：回 GigE 機器層參數（PixelFormat/Auto/Trigger/ROI/封包），供 UI 顯示
    ctrl.set_get_nodes_handler([&](int cid, std::string& js, std::string& err) -> bool {
        std::lock_guard<std::mutex> lk(state_mtx);
        // 依 cam_id 路由；陣列未開時退回 primary（單台 legacy 行為不變）
        // ⚠️ 已知限制（docs/code_review_20260802.md B6）：idle（陣列未開）第一發會開「列舉第一台」
        //   並以請求的 cam_id 回聲——有 cam_map 且該台非第一台時 = 靜默回錯台（★4 的 idle 殘留孔）。
        CamPylon* c = mgr.get(cid);
        if (!c && mgr.empty()) c = mgr.get_or_open_primary(serial, pkt_size);
        if (!c) { err = "unknown cam_id " + std::to_string(cid); return false; }
        MachineParams mp;
        if (!c->read_machine_params(mp, err)) return false;
        json j = {
            {"pixel_format",     mp.pixel_format},
            {"exposure_auto",    mp.exposure_auto},
            {"gain_auto",        mp.gain_auto},
            {"trigger_mode",     mp.trigger_mode},
            {"trigger_selector", mp.trigger_selector},
            {"trigger_source",   mp.trigger_source},
            {"width",            mp.width},
            {"height",           mp.height},
            {"packet_size",      mp.packet_size},
            {"scpd",             mp.scpd}
        };
        js = j.dump();
        return true;
    });

    // Gap #21 綁定動作：SET_CAM_MAP（Control 送完整映射表 → 寫 cam_map.json 並重載）
    ctrl.set_cam_map_handler([&](const std::string& entries_json,
                                 int& written, std::string& path, std::string& err) -> bool {
        std::lock_guard<std::mutex> lk(state_mtx);
        // ⚠️ 取像中一律拒絕：cam_id 決定 FrameHeader.camId 與 IP 端輸出夾，
        // 途中換映射等於同一片面板前後半段的影像被歸給不同 CCD → 資料對錯台。
        if (grabbing || armed) {
            err = "取像中/已 ARM，無法變更相機映射；請先 GRAB_STOP";
            return false;
        }
        path = cam_map_path;
        if (!mgr.write_map(cam_map_path, entries_json, err)) return false;
        written = (int)mgr.map_size();
        printf("[main] SET_CAM_MAP → %s（%d 筆），已重載\n", cam_map_path.c_str(), written);
        return true;
    });

    // 調參效果確認：TUNE_MEAN（開相機免 RDMA → 設曝光/增益 → 抓 1 幀回 mean gray）
    ctrl.set_tune_mean_handler([&](int cid, float exp_us, int gain_raw,
                                   float& ea, int& ga, double& mean,
                                   std::string& err) -> bool {
        std::lock_guard<std::mutex> lk(state_mtx);
        if (grabbing) { err = "取像中，請先 GRAB_STOP 再調參預覽"; return false; }
        CamPylon* c = mgr.get(cid);
        // 陣列已開時不得 fallback 到 primary，否則 cam1 的調參會靜默套到 cam0
        // ⚠️ 已知限制（B6）：idle 第一發同 GET_CAM_NODES 會開列舉第一台；且量測後 save_cam_config(cid)
        //   會把「錯台的實測值」寫進請求 cam_id 的設定槽（下次 ARM 套錯）。
        if (!c && mgr.empty()) c = mgr.get_or_open_primary(serial, pkt_size);
        if (!c) {
            err = mgr.empty() ? "開相機失敗" : ("unknown cam_id " + std::to_string(cid));
            return false;
        }
        if (!c->set_params(exp_us, gain_raw, ea, ga)) { err = "相機寫入失敗"; return false; }
        save_cam_config(cam_cfg_path, cid, ea, ga);
        return c->grab_one_mean(mean, err);
    });

    // ⚠️ 已知限制（docs/code_review_20260802.md B9）：計數仍只回全陣列總和（grabbed/dropped/running），
    //   無 per-cam 幀數明細——6 台時無法從 8100 定位哪台「少幀但還活著」，只能翻 stdout log。
    //   （B1 修法已補 faulted/faulted_cams：**掉線**的台可從 8100 直接定位到 cam_id + 原因。）
    ctrl.set_status_provider([&]() -> std::string {
        bool g, a;
        size_t cams, running;
        uint64_t grabbed, dropped, sent_f, sent_b;
        uint32_t tslice;
        std::vector<CamManager::Fault> faults;
        {
            std::lock_guard<std::mutex> lk(state_mtx);
            g       = grabbing;
            a       = armed;
            cams    = mgr.size();
            running = mgr.running_count();
            grabbed = mgr.total_grabbed();
            dropped = mgr.total_dropped();
            sent_f  = sender.sent_frames();
            sent_b  = sender.sent_bytes();
            tslice  = total_slice.load(std::memory_order_relaxed);
            faults  = mgr.faults();          // B1：拔線/斷電中止的台（本行程仍活著）
        }
        char buf[352];
        snprintf(buf, sizeof(buf),
                 "{\"grabbing\":%s,\"armed\":%s,\"cams\":%zu,\"running\":%zu,"
                 "\"frames_per_panel\":%u,\"grabbed\":%llu,\"dropped\":%llu,"
                 "\"sent_frames\":%llu,\"sent_bytes\":%llu",
                 g ? "true" : "false", a ? "true" : "false", cams, running,
                 tslice > 1 ? tslice : 0,
                 (unsigned long long)grabbed,
                 (unsigned long long)dropped,
                 (unsigned long long)sent_f,
                 (unsigned long long)sent_b);

        // B1：故障台明細另用 nlohmann 組（例外訊息含引號/反斜線，手工拼字串會產出壞 JSON）。
        // faulted>0 = 有相機掉線但行程仍存活；running 少掉的台**不是**收滿，是斷了。
        json fj = json::array();
        for (const auto& f : faults) {
            fj.push_back({{"cam_id", f.cam_id},
                          {"ccd_id", f.ccd_id},
                          {"err",    f.message}});
        }
        std::string out = buf;
        out += ",\"faulted\":" + std::to_string(faults.size());
        out += ",\"faulted_cams\":" + fj.dump() + "}";
        return out;
    });

    // ---- 啟動 ----
    if (!ctrl.start()) {
        fprintf(stderr, "[main] ControlServer 啟動失敗\n");
        return 1;
    }
    {
        auto cfg = load_cam_config(cam_cfg_path, 0);
        printf("[main] cfaoi_grab 就緒  ctrl_port=%d  rdma→%s:%s  cam_count=%s  frames/panel=%d\n",
               ctrl_port, rdma_host.c_str(), rdma_port.c_str(),
               cam_count == 0 ? "ALL" : std::to_string(cam_count).c_str(), cli_frames);
        printf("[main] cam_config=%s  cam0: exp=%.1fµs  gain=%d raw\n",
               cam_cfg_path.c_str(), cfg.exposure_us, cfg.gain_raw);
    }

    // ---- 主迴圈：等信號 ----
    while (!g_shutdown) {
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }

    printf("[main] 收到停止信號，清理中...\n");
    {
        std::lock_guard<std::mutex> lk(state_mtx);
        if (grabbing) {
            mgr.stop_all();
            sender.disconnect();
            grabbing = false;
        }
    }
    ctrl.stop();
    printf("[main] 結束\n");
    return 0;
}
