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
 * 命令（**15 個**，實作見 control_server.cpp::handle_client 的 if-else 鏈）：
 *
 * ── 基本 / 配方 ──────────────────────────────────────────────────────────────
 *   CHECK_HEALTH             → {"status":"OK","ai":bool}（Control 心跳；每次往返都要快）
 *   GET_STATUS               → 回 status_provider() 的 JSON（各模式自填：queue/recv/edge/zones…）
 *   LOAD_RECIPE              params{recipe, recipe_xml(跨機優先), panel_id,
 *                                   recipe_saving{…}, share_flags{…}, golden_png_base64?}
 *                            → 解析存圖/旗標/對位設定 + 呼叫 load_recipe handler（三域守門）
 *
 * ── 影像注入（**僅 offline-tcp**；rdma 模式由 set_image_ingest_enabled(false) 誠實 ERR 擋掉）──
 *   SEND_IMAGE_STREAM_BEGIN  → 重置 frame_seq，回 OK
 *   SEND_IMAGE_FOR_REVIEW    params{panel_id,cam_id,width,height,frame_seq,payload_bytes,
 *                                   system_id?,last?,no_wait?,debug?,crc32?}
 *                            命令行（\n 結尾）後緊接 payload_bytes 個 raw bytes（Mono8 影像）。
 *                            → 驗證(尺寸/magic/CRC) → 建 FrameHeader 推入 FrameQueue
 *                            → 等 deliver_result 回結果（no_wait=true 則入隊即 ACK）
 *   REVIEW_LOCAL_IMAGE       params{path, panel_id?, debug?}
 *                            → 讀 IP 本機磁碟全解析度影像跑同一條檢測路徑（免傳大圖，bit-exact）
 *
 * ── 對位（Gap #1；每片一次，CF_CHECK_ALIGN/CF_SET_ALIGN 鏈）────────────────────
 *   CHECK_ALIGN              params{width,height,payload_bytes,panel_id} + 緊接搜尋 ROI raw bytes
 *                            → run_align → {shift_x,shift_y,score,angle_deg}；
 *                              score 不足 → **回 ERR（誠實失敗，不回假 0 位移）**，由上位機決策
 *   SET_ALIGN                params{shift_x,shift_y} → 套回所有 zones 的 aligned_*
 *
 * ── 缺陷遠端歸檔 DefectSort（影像在 IP 端硬碟，Control 不假設看得到）──────────
 *   LIST_DEFECT_FOLDERS      params{date} → panel 夾清單 + defect_count（CF_GET_RESULT 鏈也走這條）
 *   SORT_DEFECTS             params{date,output_subdir,by_id_folder,selected_folders[]}
 *   LIST_DEFECT_PATCHES      params{date,folder_name} → 小圖 metadata（座標/型別/current_class）
 *   GET_DEFECT_PATCHES_BATCH params{date,folder_name,patch_ids[]} → PNG bytes(base64) 批次回傳
 *   SAVE_DEFECT_CLASSIFICATION params{date,folder_name,classifications[]} → 分類落地 + classification.json
 *
 * ── 遠端檔案瀏覽（離線調參選圖用）──────────────────────────────────────────
 *   LIST_DIR                 params{path（支援 ~ 展開）} → 子目錄 + 影像檔清單
 *   GET_IMAGE_PREVIEW        params{path,max_width} → 縮圖 PNG(base64) + 全解析度尺寸
 *                            ★ 縮圖是 display-only，**絕不進檢測**（檢測走 REVIEW_LOCAL_IMAGE 全解析度）
 *
 * 未知 cmd → 回 ERR（不靜默忽略）。收到的影像進共用 FrameQueue，由 TcpImageSource 消費 → 與 pipeline 解耦。
 * ⚠️ 單客戶端序列處理：accept 後在同一 thread 跑 handle_client 直到斷線 → Control 佔住連線時，
 *    第二個工具（診斷腳本）只會排隊等待而非收到錯誤（已知限制，非阻斷）。
 * ============================================================================
 */

#include <atomic>
#include <condition_variable>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>

#include <vector>

#include "image_source/image_source.h"  // FrameQueue, FrameHeader
#include "config/recipe_saving_config.h"
#include "config/share_flags.h"
#include "config/zone_config_adapter.h"  // IoiRect（#23）
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
    // rdma 模式：影像來源是 RDMA，TCP 影像注入（SEND_IMAGE_*/REVIEW_LOCAL_IMAGE）必須擋掉——
    // 注入的幀會混進 RDMA 消費迴圈，弄壞 seq/遺失對帳。其餘命令（心跳/查詢/LOAD_RECIPE/對位）照常。
    void set_image_ingest_enabled(bool v) { image_ingest_ = v; }

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

    // #23 LOAD_RECIPE 解析的興趣區（mutex 保護）；offline-tcp 迴圈快照後存圖。
    std::vector<IoiRect> ioi_list() const {
        std::lock_guard<std::mutex> lk(saving_cfg_mtx_);
        return ioi_list_;
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
    std::atomic<bool> image_ingest_{true};           // false = rdma 模式，拒絕 TCP 影像注入

    LoadRecipeFn load_recipe_;
    SetAlignFn   set_align_;
    StatusFn     status_;

    // 存圖設定、共用旗標、對位設定（LOAD_RECIPE 更新；同一把鎖保護三者）
    mutable std::mutex saving_cfg_mtx_;
    RecipeSavingConfig saving_cfg_;
    std::vector<IoiRect> ioi_list_;   // #23 LOAD_RECIPE 解析的興趣區
    ShareFlags         share_flags_;
    AlignRoiConfig     align_roi_cfg_;

    // 結果回傳 rendezvous（key = panel_id；offline review 為循序單一請求）
    std::mutex result_mtx_;
    std::condition_variable result_cv_;
    std::unordered_map<std::string, std::string> results_;
};

#endif // CFAOI_CONTROL_SERVER_H
