#pragma once
// ControlServer — Grab 端 TCP JSON server（Control → Grab，預設 port 8100）
// 協議與 IP 端（port 8200）相同格式：newline-delimited JSON，每行一個命令。
//
// 支援命令（共 11 個；dispatch 鏈見 control_server.cpp handle_client，未知 cmd → ERR unknown command）：
//   CHECK_HEALTH      → OK + data={grabbing,armed,cams,running,frames_per_panel,grabbed,dropped,
//                       sent_frames,sent_bytes}（皆為全陣列總和，無 per-cam 明細）
//   LOAD_RECIPE       params{recipe, panel_id}  → load_recipe callback（更新 panel_hash，不取像）
//   GRAB_ARM          → grab_arm callback（預熱：開相機陣列+套參數+RDMA connect，冪等）
//                     軟體觸發架構：重活提前到 ARM，GRAB_START 只剩 ms 級 start
//   GRAB_START        params{timeout_ms?, frames_per_panel?} → grab_start callback
//                     frames_per_panel：每台收滿 N 張自動停（0/缺省 = 連續，legacy）
//                     未 ARM 時自動先 ARM（相容 nc 手動測試，但觸發延遲=冷啟秒級）
//   GRAB_STOP                                    → grab_stop callback
//   SET_CAM_PARAMS    params{cam_id,exposure_us,gain_raw} → set_cam callback（回 actual read-back）
//   GET_CAM_PARAMS    params{cam_id}             → get_cam callback（未開 → 回 cam_config 條目）
//   LIST_CAMERAS      （唯讀列舉，不開相機）→ {"status":"OK","cameras":[{cam_id,ccd_id,bound,mac,
//                     model,serial,ip,online,persistent,ip_config,device_class}]}
//   GET_CAM_NODES     params{cam_id} → GigE 機器層參數 JSON + cam_id 回聲（★4 修法：錯 cam_id 回 ERR）
//   SET_CAM_MAP       params{entries:[{mac,cam_id,ccd_id}]} → 寫 cam_map.json 並重載（Gap #21；
//                     取像中/已 ARM 拒絕；壞資料全擋、原檔不動）
//   TUNE_MEAN         params{cam_id,exposure_us,gain_raw} → 設參數 + 抓 1 幀回 mean_gray（調參預覽）

#include <atomic>
#include <functional>
#include <string>
#include <thread>

class ControlServer {
public:
    // frames_per_panel：每台收滿 N 張自動停（0 = 連續取像，legacy 行為）
    using GrabStartFn  = std::function<bool(int timeout_ms, int frames_per_panel,
                                            std::string& err)>;
    // GRAB_ARM：預熱（開相機陣列+套曝光增益+RDMA connect），冪等可重呼
    using GrabArmFn    = std::function<bool(std::string& err)>;
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
    // cam_id 必填：舊版簽章沒有它 → 一律回第一台，多相機時「靜默回錯相機」（不是 ERR，更危險）
    using GetNodesFn = std::function<bool(int cam_id, std::string& json_out, std::string& err)>;
    // SET_CAM_MAP：寫入 MAC↔cam_id 映射（Gap #21 綁定動作）。entries = JSON 陣列字串。
    // 取像中必須拒絕（改映射 = 改相機身分）。回 written = 寫入筆數、path = 實際檔案路徑。
    using SetCamMapFn = std::function<bool(const std::string& entries_json,
                                           int& written, std::string& path,
                                           std::string& err)>;

    explicit ControlServer(int port);
    ~ControlServer();

    void set_grab_start(GrabStartFn fn)    { start_fn_   = std::move(fn); }
    void set_grab_arm(GrabArmFn fn)        { arm_fn_     = std::move(fn); }
    void set_grab_stop(GrabStopFn fn)      { stop_fn_    = std::move(fn); }
    void set_load_recipe(LoadRecipeFn fn)  { recipe_fn_  = std::move(fn); }
    void set_status_provider(StatusFn fn)  { status_fn_  = std::move(fn); }
    void set_cam_params_handler(SetCamFn fn) { set_cam_fn_ = std::move(fn); }
    void get_cam_params_handler(GetCamFn fn) { get_cam_fn_ = std::move(fn); }
    void set_list_cameras_handler(ListCamFn fn) { list_cam_fn_ = std::move(fn); }
    void set_tune_mean_handler(TuneMeanFn fn) { tune_mean_fn_ = std::move(fn); }
    void set_get_nodes_handler(GetNodesFn fn) { get_nodes_fn_ = std::move(fn); }
    void set_cam_map_handler(SetCamMapFn fn) { set_map_fn_ = std::move(fn); }

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
    GrabArmFn    arm_fn_;
    GrabStopFn   stop_fn_;
    LoadRecipeFn recipe_fn_;
    StatusFn     status_fn_;
    SetCamFn     set_cam_fn_;
    GetCamFn     get_cam_fn_;
    ListCamFn    list_cam_fn_;
    TuneMeanFn   tune_mean_fn_;
    GetNodesFn   get_nodes_fn_;
    SetCamMapFn  set_map_fn_;
};
