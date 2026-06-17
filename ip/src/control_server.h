#ifndef CFAOI_CONTROL_SERVER_H
#define CFAOI_CONTROL_SERVER_H

/**
 * ============================================================================
 * ControlServer — TCP JSON 命令 server（Control → IP，預設 port 8200）
 * ============================================================================
 *
 * 協議：newline-delimited JSON 命令，每行一個 {"cmd":..,"seq":..,"params":{..}}。
 * 回應：一行 JSON {"seq":..,"status":"OK"|"ERR",...}。
 *
 * 命令：
 *   LOAD_RECIPE              params{recipe, panel_id}     → 呼叫 load_recipe handler
 *   GET_STATUS               → 回 status_provider() 的 JSON
 *   CHECK_HEALTH             → {"status":"OK","ai":bool}
 *   SEND_IMAGE_STREAM_BEGIN  params{panel_id, count?}     → 重置序號，回 OK
 *   SEND_IMAGE_FOR_REVIEW    params{panel_id,cam_id,width,height,frame_seq,payload_bytes,
 *                                   pixel_format?,system_id?,last?}
 *                            命令行（\n 結尾）後緊接 payload_bytes 個 raw bytes（Mono8 影像）。
 *                            → 建 FrameHeader 推入 FrameQueue，回 OK。
 *
 * 收到的影像進共用 FrameQueue，由 TcpImageSource 消費 → 與 pipeline 解耦。
 * ============================================================================
 */

#include <atomic>
#include <condition_variable>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>

#include "image_source/image_source.h"  // FrameQueue, FrameHeader
#include "config/recipe_saving_config.h"
#include "config/share_flags.h"
#include "align_engine.h"               // AlignRoiConfig

class ControlServer {
public:
    // load_recipe handler：recipe 為名稱/路徑（同機）、recipe_xml 為配方內容（跨機，優先）。
    // 回傳是否成功，失敗時填 err。
    using LoadRecipeFn = std::function<bool(const std::string& recipe,
                                            const std::string& recipe_xml,
                                            const std::string& panel_id,
                                            std::string& err)>;
    // set_align handler：SET_ALIGN 命令到達時，對所有 zones 套回 ShiftX/Y。
    using SetAlignFn = std::function<void(double shift_x, double shift_y)>;
    // status_provider：回傳一個 JSON 物件字串（GET_STATUS 用）。
    using StatusFn = std::function<std::string()>;

    ControlServer(int port, FrameQueue& queue);
    ~ControlServer();

    void set_load_recipe_handler(LoadRecipeFn fn) { load_recipe_ = std::move(fn); }
    void set_set_align_handler(SetAlignFn fn)   { set_align_  = std::move(fn); }
    void set_status_provider(StatusFn fn) { status_ = std::move(fn); }
    void set_ai_enabled(bool v) { ai_enabled_ = v; }
    void set_output_dir(const std::string& d) { output_dir_ = d; }   // 供 LIST/SORT_DEFECTS 掃描

    // LOAD_RECIPE 解析的存圖設定（mutex 保護）；offline-tcp 迴圈快照後傳給 process_image。
    RecipeSavingConfig saving_config() const {
        std::lock_guard<std::mutex> lk(saving_cfg_mtx_);
        return saving_cfg_;
    }

    // LOAD_RECIPE 解析的共用旗標（mutex 保護）；offline-tcp 迴圈快照後決定是否存圖。
    ShareFlags share_flags() const {
        std::lock_guard<std::mutex> lk(saving_cfg_mtx_);
        return share_flags_;
    }

    // LOAD_RECIPE 解析的對位設定（mutex 保護）；CHECK_ALIGN handler 使用。
    AlignRoiConfig align_roi_config() const {
        std::lock_guard<std::mutex> lk(saving_cfg_mtx_);
        return align_roi_cfg_;
    }

    // SEND_IMAGE_FOR_REVIEW 的 debug 旗標：true → 該次存全部 patch（調參需要看小圖）；
    // false（預設）→ 只存結果+overlay 加速。由主處理迴圈讀取決定 SaveOptions。
    bool review_save_patches() const { return review_save_patches_.load(); }

    bool start();   // 開 listener thread
    void stop();    // 關閉並 join；同時 queue.close()

    // 由處理端（main loop）在影像處理完成後呼叫，把結果 JSON 交回給等待中的
    // SEND_IMAGE_FOR_REVIEW，使結果經 TCP 回傳（跨機器不需共用檔案系統）。
    void deliver_result(const std::string& panel_id, const std::string& result_json);

private:
    void run();     // accept loop
    void handle_client(int fd);

    int port_;
    FrameQueue& queue_;
    int listen_fd_ = -1;
    std::thread thread_;
    std::atomic<bool> running_{false};
    bool ai_enabled_ = false;
    uint16_t frame_seq_ = 0;
    std::string output_dir_ = "output";
    std::atomic<bool> review_save_patches_{false};   // SEND_IMAGE_FOR_REVIEW debug 旗標

    LoadRecipeFn load_recipe_;
    SetAlignFn   set_align_;
    StatusFn     status_;

    // 存圖設定、共用旗標、對位設定（LOAD_RECIPE 更新；同一把鎖保護三者）
    mutable std::mutex saving_cfg_mtx_;
    RecipeSavingConfig saving_cfg_;
    ShareFlags         share_flags_;
    AlignRoiConfig     align_roi_cfg_;

    // 結果回傳 rendezvous（key = panel_id；offline review 為循序單一請求）
    std::mutex result_mtx_;
    std::condition_variable result_cv_;
    std::unordered_map<std::string, std::string> results_;
};

#endif // CFAOI_CONTROL_SERVER_H
