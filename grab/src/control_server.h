#pragma once
// ControlServer — Grab 端 TCP JSON server（Control → Grab，預設 port 8100）
// 協議與 IP 端（port 8200）相同格式：newline-delimited JSON，每行一個命令。
//
// 支援命令：
//   CHECK_HEALTH      → {"seq":N,"status":"OK","grabbing":bool,"frames":N,"drops":N}
//   LOAD_RECIPE       params{recipe, panel_id}  → load_recipe callback
//   GRAB_START        params{timeout_ms?, frames_per_panel?} → grab_start callback
//                     frames_per_panel：每台收滿 N 張自動停（0/缺省 = 連續，legacy）
//   GRAB_STOP                                    → grab_stop callback
//   SET_CAM_PARAMS    params{cam_id,exposure_us,gain_raw} → set_cam callback
//   GET_CAM_PARAMS    params{cam_id}             → get_cam callback
//   LIST_CAMERAS      （唯讀列舉）→ {"status":"OK","cameras":[{cam_id,mac,model,ip,online,persistent,...}]}

#include <atomic>
#include <functional>
#include <string>
#include <thread>

class ControlServer {
public:
    // frames_per_panel：每台收滿 N 張自動停（0 = 連續取像，legacy 行為）
    using GrabStartFn  = std::function<bool(int timeout_ms, int frames_per_panel,
                                            std::string& err)>;
    using GrabStopFn   = std::function<void()>;
    using LoadRecipeFn = std::function<void(const std::string& recipe,
                                            const std::string& panel_id)>;
    using StatusFn     = std::function<std::string()>;  // 回傳 JSON 物件字串

    // cam_id / requested values → exp_actual / gain_actual（read-back）→ true/false
    using SetCamFn = std::function<bool(int cam_id,
                                        float exp_us, int gain_raw,
                                        float& exp_actual, int& gain_actual,
                                        std::string& err)>;
    // cam_id → exp_actual / gain_actual → true/false
    using GetCamFn = std::function<bool(int cam_id,
                                        float& exp_actual, int& gain_actual,
                                        std::string& err)>;
    // LIST_CAMERAS：回傳 cameras JSON array 字串（唯讀列舉，不開相機）
    using ListCamFn = std::function<std::string()>;
    // TUNE_MEAN：開相機(免 RDMA)+ 設曝光/增益 + 抓 1 幀回 mean gray（調參效果確認）
    using TuneMeanFn = std::function<bool(int cam_id, float exp_us, int gain_raw,
                                          float& exp_actual, int& gain_actual,
                                          double& mean, std::string& err)>;
    // GET_CAM_NODES：回 GigE 機器層參數 JSON 物件字串（需開相機）
    using GetNodesFn = std::function<bool(std::string& json_out, std::string& err)>;

    explicit ControlServer(int port);
    ~ControlServer();

    void set_grab_start(GrabStartFn fn)    { start_fn_   = std::move(fn); }
    void set_grab_stop(GrabStopFn fn)      { stop_fn_    = std::move(fn); }
    void set_load_recipe(LoadRecipeFn fn)  { recipe_fn_  = std::move(fn); }
    void set_status_provider(StatusFn fn)  { status_fn_  = std::move(fn); }
    void set_cam_params_handler(SetCamFn fn) { set_cam_fn_ = std::move(fn); }
    void get_cam_params_handler(GetCamFn fn) { get_cam_fn_ = std::move(fn); }
    void set_list_cameras_handler(ListCamFn fn) { list_cam_fn_ = std::move(fn); }
    void set_tune_mean_handler(TuneMeanFn fn) { tune_mean_fn_ = std::move(fn); }
    void set_get_nodes_handler(GetNodesFn fn) { get_nodes_fn_ = std::move(fn); }

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
    SetCamFn     set_cam_fn_;
    GetCamFn     get_cam_fn_;
    ListCamFn    list_cam_fn_;
    TuneMeanFn   tune_mean_fn_;
    GetNodesFn   get_nodes_fn_;
};
