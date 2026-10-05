#ifndef CFAOI_SD_WATCHDOG_H
#define CFAOI_SD_WATCHDOG_H
// =============================================================================
// sd_watchdog.h — systemd watchdog（商業化階段 3：卡死自動重啟）
// -----------------------------------------------------------------------------
// 行程「活著但卡住」（命令 handler 卡死、accept 迴圈默默退出、GPU 卡住）時 Restart=always 救不了 ——
// 行程沒死。systemd watchdog：服務 unit 設 WatchdogSec=N，行程要每 <N 秒送一次 "WATCHDOG=1"，
// 逾時 systemd 就砍掉重啟。本檔是不依賴 libsystemd 的最小實作（產線機不多裝套件）。
//
// 用法：
//   sdwd::Pinger wd([&]{ return 健康 ? "" : "原因"; });   // 建構即啟動背景 thread
//   - 不在 systemd watchdog 下（手動執行 / 沒設 WatchdogSec）→ 什麼都不做
//   - 健康檢查回非空字串 → 停止回報（印一次原因），systemd 於 WatchdogSec 後重啟本行程
// unit 需要：WatchdogSec=30  NotifyAccess=all（ExecStart 經 stdbuf exec，同一 PID；all 較保險）
// =============================================================================
#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <string>
#include <thread>

#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

namespace sdwd {

inline int64_t now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

// systemd 要求的回報間隔（µs）；不在 watchdog 下回 0。
inline uint64_t interval_us() {
    const char* us  = std::getenv("WATCHDOG_USEC");
    const char* pid = std::getenv("WATCHDOG_PID");
    if (!us || !*us) return 0;
    if (pid && *pid && std::atol(pid) != (long)::getpid()) return 0;
    return std::strtoull(us, nullptr, 10);
}

// sd_notify 最小版：一個 datagram 送到 $NOTIFY_SOCKET（"@" 開頭 = abstract namespace）。
inline bool notify(const char* msg) {
    const char* path = std::getenv("NOTIFY_SOCKET");
    if (!path || !*path) return false;
    sockaddr_un sa{};
    sa.sun_family = AF_UNIX;
    const size_t len = std::strlen(path);
    if (len >= sizeof(sa.sun_path)) return false;
    std::memcpy(sa.sun_path, path, len);
    if (sa.sun_path[0] == '@') sa.sun_path[0] = '\0';
    const int fd = ::socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (fd < 0) return false;
    const socklen_t sl = (socklen_t)(offsetof(sockaddr_un, sun_path) + len);
    const ssize_t r = ::sendto(fd, msg, std::strlen(msg), MSG_NOSIGNAL, (sockaddr*)&sa, sl);
    ::close(fd);
    return r >= 0;
}

// 背景回報 thread：每 interval/3 檢查一次健康，健康才送 WATCHDOG=1。
class Pinger {
public:
    using Check = std::function<std::string()>;   // "" = 健康；否則 = 不健康原因

    explicit Pinger(Check check) : check_(std::move(check)) {
        const uint64_t us = interval_us();
        if (us == 0) return;
        const auto period = std::chrono::microseconds(us / 3);
        std::printf("[watchdog] systemd watchdog 啟用：每 %.1fs 回報一次（逾時 %.0fs 重啟）\n",
                    us / 3 / 1e6, us / 1e6);
        std::fflush(stdout);
        th_ = std::thread([this, period] {
            bool reported = false;
            while (!stop_) {
                const std::string why = check_();
                if (why.empty()) {
                    notify("WATCHDOG=1");
                    reported = false;
                } else if (!reported) {
                    std::fprintf(stderr, "[watchdog] ⚠ 健康檢查失敗，停止回報 → systemd 將重啟本行程：%s\n",
                                 why.c_str());
                    std::fflush(stderr);
                    reported = true;
                }
                for (int i = 0; i < 10 && !stop_; ++i) std::this_thread::sleep_for(period / 10);
            }
        });
    }
    ~Pinger() { stop_ = true; if (th_.joinable()) th_.join(); }
    Pinger(const Pinger&) = delete;
    Pinger& operator=(const Pinger&) = delete;

private:
    Check check_;
    std::atomic<bool> stop_{false};
    std::thread th_;
};

// 命令 server 健康：accept 迴圈還在、且沒有單一命令處理超過 max_busy_ms。
// ControlServer 持有這兩個 atomic；handle_client 每個命令用 BusyGuard 包起來。
struct ServerLiveness {
    std::atomic<bool>    loop_alive{false};
    std::atomic<int64_t> busy_since_ms{0};

    std::string check(const char* name, int64_t max_busy_ms) const {
        if (!loop_alive) return std::string(name) + " 命令迴圈已退出（連不上命令埠）";
        const int64_t since = busy_since_ms.load();
        if (since != 0 && now_ms() - since > max_busy_ms)
            return std::string(name) + " 單一命令處理已超過 " + std::to_string((now_ms() - since) / 1000) + " 秒";
        return "";
    }
};

struct BusyGuard {
    std::atomic<int64_t>& t;
    explicit BusyGuard(std::atomic<int64_t>& v) : t(v) { t = now_ms(); }
    ~BusyGuard() { t = 0; }
};

}  // namespace sdwd

#endif  // CFAOI_SD_WATCHDOG_H
