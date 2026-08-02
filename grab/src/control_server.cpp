#include "control_server.h"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

using json = nlohmann::json;

// ---- 連線讀輔助（與 IP 端 control_server.cpp 相同模式）----
// 阻塞式 recv 累積進緩衝、以 '\n' 切行（尾端 '\r' 相容 CRLF）。
// ⚠️ 已知限制（docs/code_review_20260802.md B16）：buf 無上限——客戶端持續送不含 '\n' 的資料會無限累積。
// ⚠️ 已知限制（B10）：recv 為永久阻塞；stop() 只關 listen fd，解除不了已連線 client 的 recv（溫和退出會卡）。
namespace {

struct ConnReader {
    int fd;
    std::vector<uint8_t> buf;
    explicit ConnReader(int f) : fd(f) {}

    bool fill() {
        uint8_t tmp[4096];
        ssize_t n = ::recv(fd, tmp, sizeof(tmp), 0);
        if (n <= 0) return false;
        buf.insert(buf.end(), tmp, tmp + n);
        return true;
    }

    bool read_line(std::string& line) {
        while (true) {
            auto it = std::find(buf.begin(), buf.end(), (uint8_t)'\n');
            if (it != buf.end()) {
                size_t len = it - buf.begin();
                line.assign((char*)buf.data(), len);
                if (!line.empty() && line.back() == '\r') line.pop_back();
                buf.erase(buf.begin(), it + 1);
                return true;
            }
            if (!fill()) return false;
        }
    }
};

bool send_line(int fd, const std::string& s) {
    std::string msg = s + "\n";
    size_t off = 0;
    while (off < msg.size()) {
        ssize_t n = ::send(fd, msg.data() + off, msg.size() - off, 0);
        if (n <= 0) return false;
        off += (size_t)n;
    }
    return true;
}

} // namespace

// -----------------------------------------------------------------------

ControlServer::ControlServer(int port) : port_(port) {}

ControlServer::~ControlServer() { stop(); }

bool ControlServer::start() {
    listen_fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
    if (listen_fd_ < 0) { perror("[ctrl] socket"); return false; }

    int opt = 1;
    ::setsockopt(listen_fd_, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    sockaddr_in addr{};
    addr.sin_family      = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port        = htons((uint16_t)port_);

    if (::bind(listen_fd_, (sockaddr*)&addr, sizeof(addr)) < 0) {
        perror("[ctrl] bind"); ::close(listen_fd_); listen_fd_ = -1; return false;
    }
    if (::listen(listen_fd_, 1) < 0) {
        perror("[ctrl] listen"); ::close(listen_fd_); listen_fd_ = -1; return false;
    }

    running_ = true;
    thread_  = std::thread(&ControlServer::run, this);
    printf("[ctrl] Grab 命令 server 監聽 port %d\n", port_);
    return true;
}

void ControlServer::stop() {
    running_ = false;
    if (listen_fd_ >= 0) { ::shutdown(listen_fd_, SHUT_RDWR); ::close(listen_fd_); listen_fd_ = -1; }
    if (thread_.joinable()) thread_.join();
}

void ControlServer::run() {
    while (running_) {
        sockaddr_in peer{};
        socklen_t plen = sizeof(peer);
        int fd = ::accept(listen_fd_, (sockaddr*)&peer, &plen);
        if (fd < 0) {
            if (running_) perror("[ctrl] accept");
            // ⚠️ 已知限制（docs/code_review_20260802.md B11）：暫時性錯誤（EINTR/ECONNABORTED/EMFILE）
            // 也會走到這裡永久退出 accept 迴圈 → 8100 靜默死亡而行程續活（取像照跑、命令連不上）。
            break;
        }
        char ip[INET_ADDRSTRLEN];
        ::inet_ntop(AF_INET, &peer.sin_addr, ip, sizeof(ip));
        printf("[ctrl] Control 連入：%s\n", ip);
        handle_client(fd);
        ::close(fd);
        printf("[ctrl] Control 斷線\n");
    }
}

// ---------------------------------------------------------------------------
// handle_client — 單客戶端命令迴圈（11 命令 dispatch；一次服一個 client，序列處理——
// 所有命令 handler 都跑在這條 thread 上，彼此天然序列化，不需對 mac_map_ 等再加鎖）。
// 錯誤語意約定：
//   - 缺 params 而分支直接取 req["params"] 者（LOAD_RECIPE / SET_CAM_PARAMS / TUNE_MEAN）：
//     nlohmann 對剛插入的 null 呼叫 .value() 會擲 type_error → 外層 catch 回 ERR "parse error"
//     （誠實 ERR、不 crash；訊息歸類稍粗但可接受）。
//   - cam_id 邊界：這層只擋明顯非法（<0）；「該 cam_id 是否存在」由 main.cpp handler 對照
//     實際相機清單判定（★5 修法：不可在這層寫死 cam_id==0，否則第二台起全被擋死）。
// ---------------------------------------------------------------------------
void ControlServer::handle_client(int fd) {
    ConnReader reader(fd);
    std::string line;

    while (running_ && reader.read_line(line)) {
        if (line.empty()) continue;

        json resp;
        try {
            json req = json::parse(line);
            int  seq = req.value("seq", 0);
            std::string cmd = req.value("cmd", "");
            resp["seq"] = seq;

            if (cmd == "CHECK_HEALTH") {
                resp["status"] = "OK";
                if (status_fn_) {
                    // status_fn_ 回傳 JSON 物件字串，嵌入 data 欄位
                    try { resp["data"] = json::parse(status_fn_()); }
                    catch (...) { resp["data"] = status_fn_(); }
                }

            } else if (cmd == "LOAD_RECIPE") {
                // 更新 panel_id/panel_hash（不取像）。params 缺席 → 擲例外 → ERR（見函式頭約定）。
                // ⚠️ 已知限制（B18）：無 handler 時仍回 OK（靜默成功反模式；實務上 main.cpp 必接線）。
                std::string recipe   = req["params"].value("recipe",   "");
                std::string panel_id = req["params"].value("panel_id", "");
                if (recipe_fn_) recipe_fn_(recipe, panel_id);
                resp["status"] = "OK";
                printf("[ctrl] LOAD_RECIPE recipe=%s panel_id=%s\n",
                       recipe.c_str(), panel_id.c_str());

            } else if (cmd == "GRAB_ARM") {
                if (!arm_fn_) {
                    resp["status"] = "ERR";
                    resp["error"]  = "no handler";
                } else {
                    std::string err;
                    bool ok = arm_fn_(err);
                    resp["status"] = ok ? "OK" : "ERR";
                    if (!ok) resp["error"] = err;
                }
                printf("[ctrl] GRAB_ARM → %s\n", resp["status"].get<std::string>().c_str());

            } else if (cmd == "GRAB_START") {
                // timeout_ms 這裡有解析、有印 log，但 main.cpp 的 handler 目前忽略它
                // （⚠️ docs/code_review_20260802.md B8：Grab 端無 per-panel watchdog，逾時控管在上位機）。
                int timeout_ms = 40000;
                int frames_per_panel = 0;   // 0 = 連續（legacy）；>0 = 每台收滿 N 張自動停
                if (req.contains("params")) {
                    if (req["params"].contains("timeout_ms"))
                        timeout_ms = req["params"]["timeout_ms"].get<int>();
                    if (req["params"].contains("frames_per_panel"))
                        frames_per_panel = req["params"]["frames_per_panel"].get<int>();
                }

                if (!start_fn_) {
                    resp["status"] = "ERR";
                    resp["error"]  = "no handler";
                } else {
                    std::string err;
                    bool ok = start_fn_(timeout_ms, frames_per_panel, err);
                    resp["status"] = ok ? "OK" : "ERR";
                    if (!ok) resp["error"] = err;
                }
                printf("[ctrl] GRAB_START timeout_ms=%d frames_per_panel=%d → %s\n",
                       timeout_ms, frames_per_panel,
                       resp["status"].get<std::string>().c_str());

            } else if (cmd == "GRAB_STOP") {
                // ⚠️ 已知限制（B18）：無 handler 時仍回 OK（同 LOAD_RECIPE 註記）。
                if (stop_fn_) stop_fn_();
                resp["status"] = "OK";
                printf("[ctrl] GRAB_STOP\n");

            } else if (cmd == "SET_CAM_PARAMS") {
                auto& prms   = req["params"];
                int   cam_id  = prms.value("cam_id",      0);
                float exp_us  = prms.value("exposure_us", 0.0f);
                int   gain_raw= prms.value("gain_raw",    256);

                // cam_id 的有效性由 handler 對照實際相機清單判定（這裡只擋明顯非法值）；
                // 舊版寫死 cam_id != 0 → ERR，導致第二台起完全無法調曝光/增益。
                // 邊界出處（Gap #2 Stage 0：probe_cam_nodes 對 raL8192-12gm 實測）：
                //   exposure_us ∈ [2,10000]µs——下限=相機最小曝光；上限為保護值（線掃 3000 行
                //   × 10000µs = 30s/幀，已遠超 TUNE_MEAN 自適應逾時上限 15s，再大無意義）。
                //   gain_raw ∈ [256,2047]——GainRaw 節點實測 min/max（256 = 0dB 基準）。
                if (cam_id < 0) {
                    resp["status"] = "ERR";
                    resp["error"]  = "invalid cam_id " + std::to_string(cam_id);
                } else if (exp_us < 2.0f || exp_us > 10000.0f) {
                    resp["status"] = "ERR";
                    resp["error"]  = "exposure_us out of range [2.0, 10000.0]";
                } else if (gain_raw < 256 || gain_raw > 2047) {
                    resp["status"] = "ERR";
                    resp["error"]  = "gain_raw out of range [256, 2047]";
                } else if (!set_cam_fn_) {
                    resp["status"] = "ERR";
                    resp["error"]  = "no handler";
                } else {
                    float exp_actual; int gain_actual;
                    std::string err;
                    bool ok = set_cam_fn_(cam_id, exp_us, gain_raw,
                                          exp_actual, gain_actual, err);
                    if (ok) {
                        resp["status"]             = "OK";
                        resp["cam_id"]             = cam_id;
                        resp["exposure_us"]        = exp_us;
                        resp["gain_raw"]           = gain_raw;
                        resp["exposure_us_actual"] = exp_actual;
                        resp["gain_raw_actual"]    = gain_actual;
                    } else {
                        resp["status"] = "ERR";
                        resp["error"]  = err;
                    }
                    printf("[ctrl] SET_CAM_PARAMS cam=%d exp=%.1f gain=%d → %s\n",
                           cam_id, exp_us, gain_raw,
                           resp["status"].get<std::string>().c_str());
                }

            } else if (cmd == "GET_CAM_PARAMS") {
                // 依 cam_id 讀該台實際曝光/增益；該台未開 → handler 回 cam_config.json 條目（main.cpp）。
                int cam_id = req.contains("params")
                             ? req["params"].value("cam_id", 0) : 0;

                if (cam_id < 0) {
                    resp["status"] = "ERR";
                    resp["error"]  = "invalid cam_id " + std::to_string(cam_id);
                } else if (!get_cam_fn_) {
                    resp["status"] = "ERR";
                    resp["error"]  = "no handler";
                } else {
                    float exp_actual; int gain_actual;
                    std::string err;
                    bool ok = get_cam_fn_(cam_id, exp_actual, gain_actual, err);
                    if (ok) {
                        resp["status"]      = "OK";
                        resp["cam_id"]      = cam_id;
                        resp["exposure_us"] = exp_actual;
                        resp["gain_raw"]    = gain_actual;
                    } else {
                        resp["status"] = "ERR";
                        resp["error"]  = err;
                    }
                }

            } else if (cmd == "LIST_CAMERAS") {
                // 唯讀列舉（不開相機、不改相機）+ cam_map annotate；串流中並存已實測不掉幀（STATUS）。
                if (!list_cam_fn_) {
                    resp["status"] = "ERR";
                    resp["error"]  = "no handler";
                } else {
                    resp["status"] = "OK";
                    // list_cam_fn_ 回傳 cameras JSON array 字串，嵌入 cameras 欄位
                    try { resp["cameras"] = json::parse(list_cam_fn_()); }
                    catch (...) { resp["cameras"] = json::array(); }
                }

            } else if (cmd == "GET_CAM_NODES") {
                // ★4 修法：依 cam_id 路由 + 回聲；未知 cam_id 回 ERR（不再靜默回第一台的值）。
                int cam_id = req.contains("params")
                             ? req["params"].value("cam_id", 0) : 0;
                if (!get_nodes_fn_) {
                    resp["status"] = "ERR"; resp["error"] = "no handler";
                } else if (cam_id < 0) {
                    resp["status"] = "ERR"; resp["error"] = "invalid cam_id " + std::to_string(cam_id);
                } else {
                    std::string js, err;
                    resp["cam_id"] = cam_id;          // 回聲 cam_id，讓呼叫端能確認問到的是哪一台
                    if (get_nodes_fn_(cam_id, js, err)) {
                        resp["status"] = "OK";
                        try { resp["nodes"] = json::parse(js); }
                        catch (...) { resp["nodes"] = json::object(); }
                    } else {
                        resp["status"] = "ERR"; resp["error"] = err;
                    }
                }

            } else if (cmd == "SET_CAM_MAP") {
                // Gap #21 綁定動作：Control 送完整映射表 → 寫 cam_map.json 並重載。
                // 刻意要求「完整表」而非增量：增量語意（改一筆、刪一筆）在兩端各自維護狀態時
                // 極易分歧；一次覆蓋全表，Control 畫面上看到什麼就是檔案裡的什麼。
                if (!set_map_fn_) {
                    resp["status"] = "ERR"; resp["error"] = "no handler";
                } else if (!req.contains("params") || !req["params"].contains("entries")
                           || !req["params"]["entries"].is_array()) {
                    resp["status"] = "ERR"; resp["error"] = "params.entries 必須是陣列";
                } else {
                    int written = 0; std::string path, err;
                    if (set_map_fn_(req["params"]["entries"].dump(), written, path, err)) {
                        resp["status"]  = "OK";
                        resp["written"] = written;
                        resp["path"]    = path;
                    } else {
                        resp["status"] = "ERR"; resp["error"] = err;
                    }
                    printf("[ctrl] SET_CAM_MAP %d 筆 → %s\n", written,
                           resp["status"].get<std::string>().c_str());
                }

            } else if (cmd == "TUNE_MEAN") {
                // 邊界同 SET_CAM_PARAMS（出處見該分支註解）。params 缺席 → 擲例外 → ERR（函式頭約定）。
                // ⚠️ 測完曝光/增益會留在相機與 cam_config（runbook §5：調參後務必 SET_CAM_PARAMS 復原）。
                auto& prms    = req["params"];
                int   cam_id  = prms.value("cam_id",      0);
                float exp_us  = prms.value("exposure_us", 0.0f);
                int   gain_raw= prms.value("gain_raw",    256);
                if (cam_id < 0) {
                    resp["status"] = "ERR"; resp["error"] = "invalid cam_id " + std::to_string(cam_id);
                } else if (exp_us < 2.0f || exp_us > 10000.0f) {
                    resp["status"] = "ERR"; resp["error"] = "exposure_us out of range [2.0, 10000.0]";
                } else if (gain_raw < 256 || gain_raw > 2047) {
                    resp["status"] = "ERR"; resp["error"] = "gain_raw out of range [256, 2047]";
                } else if (!tune_mean_fn_) {
                    resp["status"] = "ERR"; resp["error"] = "no handler";
                } else {
                    float exp_actual; int gain_actual; double mean; std::string err;
                    bool ok = tune_mean_fn_(cam_id, exp_us, gain_raw,
                                            exp_actual, gain_actual, mean, err);
                    if (ok) {
                        resp["status"]             = "OK";
                        resp["cam_id"]             = cam_id;
                        resp["exposure_us_actual"] = exp_actual;
                        resp["gain_raw_actual"]    = gain_actual;
                        resp["mean_gray"]          = mean;
                    } else {
                        resp["status"] = "ERR";
                        resp["error"]  = err;
                    }
                    printf("[ctrl] TUNE_MEAN cam=%d exp=%.1f gain=%d → %s\n",
                           cam_id, exp_us, gain_raw,
                           resp["status"].get<std::string>().c_str());
                }

            } else {
                resp["status"] = "ERR";
                resp["error"]  = "unknown command: " + cmd;
            }

        } catch (const std::exception& e) {
            resp["status"] = "ERR";
            resp["error"]  = std::string("parse error: ") + e.what();
        }

        send_line(fd, resp.dump());
    }
}
