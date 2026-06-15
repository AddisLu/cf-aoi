#pragma once
// ControlServer — Grab 端 TCP JSON server（Control → Grab，預設 port 8100）
// 協議與 IP 端（port 8200）相同格式：newline-delimited JSON，每行一個命令。
//
// 支援命令：
//   CHECK_HEALTH  → {"seq":N,"status":"OK","grabbing":bool,"frames":N,"drops":N}
//   LOAD_RECIPE   params{recipe, panel_id} → 觸發 load_recipe callback
//   GRAB_START    params{timeout_ms?}      → 觸發 grab_start callback
//   GRAB_STOP                              → 觸發 grab_stop callback

#include <atomic>
#include <functional>
#include <string>
#include <thread>

class ControlServer {
public:
    using GrabStartFn  = std::function<bool(int timeout_ms, std::string& err)>;
    using GrabStopFn   = std::function<void()>;
    using LoadRecipeFn = std::function<void(const std::string& recipe,
                                            const std::string& panel_id)>;
    using StatusFn     = std::function<std::string()>;  // 回傳 JSON 物件字串

    explicit ControlServer(int port);
    ~ControlServer();

    void set_grab_start(GrabStartFn fn)   { start_fn_  = std::move(fn); }
    void set_grab_stop(GrabStopFn fn)     { stop_fn_   = std::move(fn); }
    void set_load_recipe(LoadRecipeFn fn) { recipe_fn_ = std::move(fn); }
    void set_status_provider(StatusFn fn) { status_fn_ = std::move(fn); }

    bool start();   // 建立 listener，開接受 thread
    void stop();    // 關閉 listener，join thread

private:
    void run();
    void handle_client(int fd);

    int  port_;
    int  listen_fd_ = -1;
    std::thread       thread_;
    std::atomic<bool> running_{false};

    GrabStartFn  start_fn_;
    GrabStopFn   stop_fn_;
    LoadRecipeFn recipe_fn_;
    StatusFn     status_fn_;
};
