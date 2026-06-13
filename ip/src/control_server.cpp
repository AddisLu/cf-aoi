#include "control_server.h"

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <iostream>
#include <string>
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

        } else {
            resp["status"] = "ERR";
            resp["error"] = "unknown cmd: " + cmd;
            reply(fd, resp);
        }
    }
}
