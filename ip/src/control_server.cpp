#include "control_server.h"

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <sys/stat.h>
#include <vector>

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <nlohmann/json.hpp>

using json = nlohmann::json;

namespace {

// 連線讀緩衝：支援先讀「一行（\n 結尾）」再讀「剛好 N bytes 二進位」。
struct ConnReader {
    int fd;
    std::vector<uint8_t> buf;  // 已收但未消費的位元組

    explicit ConnReader(int f) : fd(f) {}

    // 從 socket 補資料進 buf；回傳 false 代表連線關閉/錯誤。
    bool fill() {
        uint8_t tmp[8192];
        ssize_t n = ::recv(fd, tmp, sizeof(tmp), 0);
        if (n <= 0) return false;
        buf.insert(buf.end(), tmp, tmp + n);
        return true;
    }

    // 讀一行（不含 \n；自動去掉結尾 \r）。回傳 false = 連線關閉。
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

    // 讀剛好 n bytes（先用 buf 剩餘，再補）。回傳 false = 連線關閉。
    bool read_exact(size_t n, std::vector<uint8_t>& out) {
        while (buf.size() < n) {
            if (!fill()) return false;
        }
        out.assign(buf.begin(), buf.begin() + n);
        buf.erase(buf.begin(), buf.begin() + n);
        return true;
    }
};

bool send_all(int fd, const std::string& s) {
    size_t off = 0;
    while (off < s.size()) {
        ssize_t n = ::send(fd, s.data() + off, s.size() - off, 0);
        if (n <= 0) return false;
        off += (size_t)n;
    }
    return true;
}

void reply(int fd, const json& j) {
    std::string s = j.dump();
    s.push_back('\n');
    send_all(fd, s);
}

namespace fs = std::filesystem;

// By ID Folder：取資料夾名前兩段 '_' token（對齊 legacy frmSortDefect dir.Split('_')[0]_[1]）。
// 不足兩段時退回整個名稱。
std::string id_group(const std::string& folder_name) {
    auto i = folder_name.find('_');
    if (i == std::string::npos) return folder_name;
    auto j = folder_name.find('_', i + 1);
    if (j == std::string::npos) return folder_name;   // 只有一個 '_' → 整名
    return folder_name.substr(0, j);
}

// 名稱是否像 yyyyMMdd 日期夾（8 位數字）。
bool is_date_dir(const std::string& name) {
    if (name.size() != 8) return false;
    for (char c : name) if (c < '0' || c > '9') return false;
    return true;
}

// 讀 panel 夾內 {folder}_ResultInfo.json 的 DefectCnt（讀不到回 -1）。
int read_defect_cnt(const fs::path& panel_dir) {
    try {
        fs::path p = panel_dir / (panel_dir.filename().string() + "_ResultInfo.json");
        std::ifstream f(p);
        if (!f) return -1;
        json j;
        f >> j;
        if (j.contains("DefectCnt")) return j["DefectCnt"].get<int>();
        return -1;
    } catch (...) { return -1; }
}

}  // namespace

ControlServer::ControlServer(int port, FrameQueue& queue)
    : port_(port), queue_(queue) {}

ControlServer::~ControlServer() { stop(); }

bool ControlServer::start() {
    listen_fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
    if (listen_fd_ < 0) {
        std::cerr << "[ControlServer] socket() 失敗\n";
        return false;
    }
    int opt = 1;
    ::setsockopt(listen_fd_, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port = htons((uint16_t)port_);

    if (::bind(listen_fd_, (sockaddr*)&addr, sizeof(addr)) < 0) {
        std::cerr << "[ControlServer] bind(port=" << port_ << ") 失敗: " << std::strerror(errno) << "\n";
        ::close(listen_fd_);
        listen_fd_ = -1;
        return false;
    }
    if (::listen(listen_fd_, 4) < 0) {
        std::cerr << "[ControlServer] listen() 失敗\n";
        ::close(listen_fd_);
        listen_fd_ = -1;
        return false;
    }
    running_ = true;
    thread_ = std::thread(&ControlServer::run, this);
    std::cout << "[ControlServer] 監聽 port " << port_ << "\n";
    return true;
}

void ControlServer::stop() {
    if (!running_.exchange(false)) {
        if (thread_.joinable()) thread_.join();
        return;
    }
    if (listen_fd_ >= 0) {
        ::shutdown(listen_fd_, SHUT_RDWR);
        ::close(listen_fd_);
        listen_fd_ = -1;
    }
    if (thread_.joinable()) thread_.join();
    queue_.close();  // 喚醒 TcpImageSource::next_frame → 結束
}

void ControlServer::deliver_result(const std::string& panel_id, const std::string& result_json) {
    {
        std::lock_guard<std::mutex> lk(result_mtx_);
        results_[panel_id] = result_json;
    }
    result_cv_.notify_all();
}

void ControlServer::run() {
    while (running_) {
        sockaddr_in cli{};
        socklen_t len = sizeof(cli);
        int fd = ::accept(listen_fd_, (sockaddr*)&cli, &len);
        if (fd < 0) {
            if (!running_) break;
            continue;
        }
        std::cout << "[ControlServer] client connected\n";
        handle_client(fd);
        ::close(fd);
        std::cout << "[ControlServer] client disconnected\n";
    }
}

void ControlServer::handle_client(int fd) {
    ConnReader rd(fd);
    std::string line;
    while (running_ && rd.read_line(line)) {
        if (line.empty()) continue;

        json req, resp;
        std::string cmd;
        long seq = 0;
        try {
            req = json::parse(line);
            cmd = req.value("cmd", "");
            seq = req.value("seq", 0);
        } catch (const std::exception& e) {
            reply(fd, {{"status", "ERR"}, {"error", std::string("bad json: ") + e.what()}});
            continue;
        }
        resp["seq"] = seq;
        const json params = req.value("params", json::object());

        if (cmd == "LOAD_RECIPE") {
            std::string recipe = params.value("recipe", "");
            std::string recipe_xml = params.value("recipe_xml", "");  // 跨機：配方內容
            std::string panel = params.value("panel_id", "");
            std::string err;
            bool ok = load_recipe_ ? load_recipe_(recipe, recipe_xml, panel, err) : false;
            resp["status"] = ok ? "OK" : "ERR";
            if (!ok) resp["error"] = err.empty() ? "no load_recipe handler" : err;
            reply(fd, resp);

        } else if (cmd == "GET_STATUS") {
            resp["status"] = "OK";
            try {
                resp["data"] = status_ ? json::parse(status_()) : json::object();
            } catch (...) { resp["data"] = json::object(); }
            reply(fd, resp);

        } else if (cmd == "CHECK_HEALTH") {
            resp["status"] = "OK";
            resp["ai"] = ai_enabled_;
            reply(fd, resp);

        } else if (cmd == "SEND_IMAGE_STREAM_BEGIN") {
            frame_seq_ = 0;
            resp["status"] = "OK";
            reply(fd, resp);

        } else if (cmd == "SEND_IMAGE_FOR_REVIEW") {
            uint32_t width  = params.value("width", 0u);
            uint32_t height = params.value("height", 0u);
            uint32_t payload_bytes = params.value("payload_bytes", 0u);
            std::string panel = params.value("panel_id", "");
            uint16_t cam_id = (uint16_t)params.value("cam_id", 0);
            uint16_t fseq = (uint16_t)params.value("frame_seq", (int)frame_seq_);
            uint8_t sys_id = (uint8_t)params.value("system_id", 0);
            bool last = params.value("last", false);

            if (payload_bytes == 0 || payload_bytes != width * height) {
                reply(fd, {{"seq", seq}, {"status", "ERR"},
                           {"error", "payload_bytes 必須等於 width*height (Mono8)"}});
                continue;
            }
            std::vector<uint8_t> payload;
            if (!rd.read_exact(payload_bytes, payload)) {
                std::cerr << "[ControlServer] 讀取影像 payload 時連線中斷\n";
                break;
            }
            FrameHeader hdr = make_frame_header(panel, cam_id, fseq, width, height,
                                                payload.data(), payload_bytes, sys_id,
                                                /*timestamp*/ 0, last);
            // 清掉同 panel 的舊結果，避免讀到上一張的；再 enqueue 並等本次結果。
            { std::lock_guard<std::mutex> lk(result_mtx_); results_.erase(panel); }
            queue_.push(hdr, panel, std::move(payload));
            ++frame_seq_;
            resp["frame_seq"] = fseq;

            // 等處理端 deliver_result(panel, json)，把結果經 TCP 回傳（跨機器免共用檔案系統）。
            std::string result_json;
            {
                std::unique_lock<std::mutex> lk(result_mtx_);
                bool got = result_cv_.wait_for(lk, std::chrono::seconds(60),
                    [&] { return results_.find(panel) != results_.end(); });
                if (got) { result_json = results_[panel]; results_.erase(panel); }
            }
            if (!result_json.empty()) {
                resp["status"] = "OK";
                try { resp["result"] = json::parse(result_json); }
                catch (...) { resp["result_raw"] = result_json; }
            } else {
                resp["status"] = "TIMEOUT";
                resp["error"] = "等不到處理結果（60s）";
            }
            reply(fd, resp);

        } else if (cmd == "LIST_DEFECT_FOLDERS") {
            // 掃 <output>/<yyyyMMdd>/ 下的 panel 夾（= {panelId}_{recipeName}）。
            // date="" → 掃所有日期夾彙整；否則只掃該日期。每筆 folder_name/panel_id + defect_count。
            std::string date = params.value("date", "");
            json folders = json::array();
            std::error_code ec;
            fs::path out(output_dir_);

            std::vector<fs::path> date_dirs;
            if (!date.empty()) {
                date_dirs.push_back(out / date);
            } else if (fs::exists(out, ec)) {
                for (const auto& e : fs::directory_iterator(out, ec)) {
                    if (ec) break;
                    if (e.is_directory() && is_date_dir(e.path().filename().string()))
                        date_dirs.push_back(e.path());
                }
            }
            for (const auto& dd : date_dirs) {
                if (!fs::exists(dd, ec)) continue;
                for (const auto& e : fs::directory_iterator(dd, ec)) {
                    if (ec) break;
                    if (!e.is_directory()) continue;
                    std::string folder = e.path().filename().string();
                    int cnt = read_defect_cnt(e.path());
                    folders.push_back({{"folder_name", folder},
                                       {"panel_id", folder},
                                       {"date", dd.filename().string()},
                                       {"defect_count", cnt}});
                }
            }
            std::sort(folders.begin(), folders.end(), [](const json& a, const json& b) {
                return a["folder_name"].get<std::string>() < b["folder_name"].get<std::string>();
            });
            resp["status"] = "OK";
            resp["folders"] = folders;
            std::cout << "[ControlServer] LIST_DEFECT_FOLDERS date=" << (date.empty() ? "(all)" : date)
                      << " → " << folders.size() << " 筆\n";
            reply(fd, resp);

        } else if (cmd == "SORT_DEFECTS") {
            // 就地把選中 panel 夾的 Defect* 檔複製到 <output>/<output_subdir>/
            //（by_id → 依資料夾名前兩段建子夾；檔名前綴 {folder}_，對齊 legacy）。
            std::string date = params.value("date", "");
            std::string subdir = params.value("output_subdir", "sorted");
            bool by_id = params.value("by_id_folder", false);
            std::vector<std::string> picked;
            if (params.contains("selected_folders"))
                for (const auto& f : params["selected_folders"]) picked.push_back(f.get<std::string>());

            std::error_code ec;
            fs::path out(output_dir_);
            fs::path dst_root = out / subdir;
            fs::create_directories(dst_root, ec);

            // 找一塊 panel 夾的實際路徑：date 指定 → <out>/<date>/<folder>；否則跨日期夾搜尋第一個命中。
            auto locate = [&](const std::string& folder) -> fs::path {
                if (!date.empty()) {
                    fs::path p = out / date / folder;
                    return fs::exists(p, ec) ? p : fs::path{};
                }
                if (fs::exists(out, ec))
                    for (const auto& e : fs::directory_iterator(out, ec)) {
                        if (!e.is_directory() || !is_date_dir(e.path().filename().string())) continue;
                        fs::path p = e.path() / folder;
                        if (fs::exists(p, ec)) return p;
                    }
                return {};
            };

            json results = json::array();
            int grand_total = 0;
            for (const auto& folder : picked) {
                int copied = 0;
                std::string msg;
                fs::path src = locate(folder);
                if (src.empty()) {
                    msg = "folder not found";
                } else {
                    fs::path dst_dir = by_id ? (dst_root / id_group(folder)) : dst_root;
                    fs::create_directories(dst_dir, ec);
                    for (const auto& e : fs::directory_iterator(src, ec)) {
                        if (ec) break;
                        if (!e.is_regular_file()) continue;
                        std::string fn = e.path().filename().string();
                        if (fn.rfind("Defect", 0) != 0) continue;   // 只複製 Defect* 缺陷圖
                        std::error_code cec;
                        fs::copy_file(e.path(), dst_dir / (folder + "_" + fn),
                                      fs::copy_options::overwrite_existing, cec);
                        if (!cec) copied++;
                    }
                }
                grand_total += copied;
                results.push_back({{"folder", folder}, {"copied", copied}, {"message", msg}});
            }
            resp["status"] = "OK";
            resp["results"] = results;
            resp["total"] = grand_total;
            resp["output_dir"] = dst_root.string();
            std::cout << "[ControlServer] SORT_DEFECTS subdir=" << subdir << " by_id=" << by_id
                      << " panels=" << picked.size() << " → " << grand_total << " 檔\n";
            reply(fd, resp);

        } else {
            resp["status"] = "ERR";
            resp["error"] = "unknown cmd: " + cmd;
            reply(fd, resp);
        }
    }
}
