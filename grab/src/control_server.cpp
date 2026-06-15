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
                std::string recipe   = req["params"].value("recipe",   "");
                std::string panel_id = req["params"].value("panel_id", "");
                if (recipe_fn_) recipe_fn_(recipe, panel_id);
                resp["status"] = "OK";
                printf("[ctrl] LOAD_RECIPE recipe=%s panel_id=%s\n",
                       recipe.c_str(), panel_id.c_str());

            } else if (cmd == "GRAB_START") {
                int timeout_ms = 40000;
                if (req.contains("params") && req["params"].contains("timeout_ms"))
                    timeout_ms = req["params"]["timeout_ms"].get<int>();

                if (!start_fn_) {
                    resp["status"] = "ERR";
                    resp["error"]  = "no handler";
                } else {
                    std::string err;
                    bool ok = start_fn_(timeout_ms, err);
                    resp["status"] = ok ? "OK" : "ERR";
                    if (!ok) resp["error"] = err;
                }
                printf("[ctrl] GRAB_START timeout_ms=%d → %s\n",
                       timeout_ms, resp["status"].get<std::string>().c_str());

            } else if (cmd == "GRAB_STOP") {
                if (stop_fn_) stop_fn_();
                resp["status"] = "OK";
                printf("[ctrl] GRAB_STOP\n");

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
