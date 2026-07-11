/**
 * ============================================================================
 * CF-AOI IP 程式 — 進入點
 * ============================================================================
 *
 * 流程：
 *   1. 解析參數，建立 ZoneConfig（INI 預設 + 可選 recipe 覆蓋，多 zone）。
 *   2. 建 GpuPipeline（移植自 gpu_algo batch_detector）。
 *   3. 依 mode 建 IImageSource：
 *        offline-file → FileImageSource
 *        offline-tcp  → ControlServer(餵 FrameQueue) + TcpImageSource(消費)
 *        rdma-validate → RdmaImageSource（Step 3 N-slot ring buffer，需 CFAOI_HAS_RDMA）
 *   4. 迴圈：next_frame → 對每個 zone 裁切+process_frame+offset → 合併 → ResultSaver::save
 * ============================================================================
 */

#include <algorithm>
#include <chrono>
#include <thread>
#include <cmath>
#include <cstdio>
#include <iostream>
#include <memory>
#include <mutex>
#include <string>
#include <vector>
#include <sys/sysinfo.h>

#include <opencv2/opencv.hpp>
#include <nlohmann/json.hpp>

#include "align_engine.h"
#include "config/recipe_saving_config.h"
#include "config/share_flags.h"
#include "config/zone_config_adapter.h"
#include "defect_rules.h"
#include "diag/flight_recorder.h"
#include "gpu/gpu_pipeline.h"
#include "image_source/file_source.h"
#include "image_source/source_image_writer.h"
#include "image_source/tcp_source.h"
#include "control_server.h"
#include "result_saver.h"

#ifdef CFAOI_HAS_RDMA
#include "image_source/rdma_source.h"
#endif

using json = nlohmann::json;

namespace {

struct Args {
    std::string mode = "offline-file";
    int control_port = 8200;
    std::string input;
    std::string output = "output";
    std::string recipe;            // RecipeInfo.xml 路徑（可選）
    std::string ini = "config/default_zone.ini";
    std::string ai_model_dir = "models/gpu_model";
    std::string ip_name = "IP01";  // 缺陷檔名 Defect_{IpName}_... 用
    bool use_ai = false;           // AI 分類過濾（預設停用：訓練資料不足）
    bool verify_deterministic = false;
    bool stitch = false;           // offline-file：把整個目錄的 slice 在 Y 方向拼成整片 panel 再偵測
                                   // （legacy 全 panel 拼接座標 recipe 用；Step B：跨 slice 鄰域 + remap 全域尺度）
    // 存圖選項（調參加速）
    bool save_patches = true;
    bool save_overlay = true;
    int  max_patches = -1;         // >=0 → 只存前 N 張缺陷小圖
    int  save_threads = 0;         // 0 → 自動
    // bench 模式（量純 GPU 運算速度）
    int  bench_iters = 100;
    int  bench_warmup = 10;
    // 單一全幅 zone 的覆寫（bench 用來掃缺陷負載上下界；<0/<=0 = 不覆寫）
    float ov_bth = -1.f, ov_dth = -1.f;
    int  ov_pitch_x = -1, ov_pitch_y = -1;
    int  ov_algo_mode = -1;        // --algo-mode 0/1/2：強制覆寫 zone 演算法(0 DIV / 1 SUB / 2 DIV-voting)
    int  ov_dark_eps  = -1;        // --dark-eps N：覆寫 DIV-voting 暗區棄權門檻
    int  ov_multiscale = -1;       // --multiscale 0/1/2：覆寫多尺度(0 關 / 1 +2× / 2 +2×+4×)大顆 Defect 補強
    // 驗證用覆寫（取代 buffer 計算器結果；-1 = 用計算器）
    int  max_queue_size    = -1;   // --max-queue-size N：覆寫 FrameQueue 上限（壓力測試用）
    int  max_src_ring_size = -1;   // --max-src-ring-size N：覆寫 SourceRing 上限（OOM 測試用）
    // MaxDefectCountPass CLI 覆寫（offline-file 驗決定性用；offline-tcp 由 LOAD_RECIPE 覆蓋）
    int  max_defect_count_pass = -1;  // --max-defect-count-pass N
    // 壓力測試用：消費端人工延遲（模擬慢 GPU/慢磁碟，讓 queue 積累到滿觸發背壓 ERR）
    int  test_consumer_delay_ms = 0;  // --test-consumer-delay-ms N
    // 壓力測試用：SourceWriter 寫檔後人工延遲（模擬慢 HDD/NAS，讓 ring 積累到滿觸發 drop WARN）
    int  test_source_writer_delay_ms = 0;  // --test-source-writer-delay-ms N
    // RDMA（Step 3 rdma-validate 模式）
    std::string rdma_bind = "0.0.0.0";     // --rdma-bind
    std::string rdma_port = "18515";       // --rdma-port
    uint32_t    rdma_slots = 4;            // --rdma-slots N（N-slot ring 深度 = 初始 credit 數）
};

void usage(const char* prog) {
    std::cout <<
    "用法: " << prog << " --mode offline-tcp|offline-file [options]\n"
    "  --mode <m>            offline-file（讀檔批次）或 offline-tcp（Control 傳影像）\n"
    "  --input <path>        offline-file：影像檔或目錄\n"
    "  --output <dir>        結果輸出目錄（預設 output）\n"
    "  --recipe <xml>        legacy RecipeInfo.xml（多 ROI；只接受 DIV 模式）\n"
    "  --ini <path>          預設參數 INI（預設 config/default_zone.ini）\n"
    "  --control-port <n>    offline-tcp 監聽 port（預設 8200）\n"
    "  --ai-model-dir <dir>  AI 模型目錄（預設 models/gpu_model；找不到則停用 AI）\n"
    "  --ip-name <name>      本機 IP 名稱（缺陷檔名 Defect_{IpName}_...，預設 IP01）\n"
    "  --use-ai              啟用 AI 分類過濾（預設停用：訓練資料不足，缺陷標待人工複核）\n"
    "  --no-save-images      只存 ResultInfo，不存任何缺陷小圖/overlay（調參加速）\n"
    "  --no-overlay          不存 overlay 全圖（仍存缺陷小圖）\n"
    "  --max-patches <n>     只存前 n 張缺陷小圖（調參加速）\n"
    "  --save-threads <n>    缺陷小圖平行寫入緒數（0=自動）\n"
    "  --verify-deterministic  offline-file：每張圖每個 zone 跑兩次比對 bit-exact，不一致則 fail\n"
    "  --mode bench           量純 GPU process_image 速度：一張圖重複跑，報 gpu_ms/wall_ms 統計\n"
    "  --bench-iters <n>      bench 量測張數（預設 100）\n"
    "  --bench-warmup <n>     bench 暖機張數（丟棄，預設 10；吸收 CUDA init/JIT）\n"
    "  --bth/--dth <f>        覆寫單一全幅 zone 的 BTH/DTH（bench 掃缺陷負載用）\n"
    "  --pitch-x/--pitch-y <n> 覆寫 pitch（bench 掃缺陷負載用）\n"
    "  --max-queue-size <n>   覆寫 FrameQueue 上限（取代 buffer 計算器；驗證背壓用）\n"
    "  --max-src-ring-size <n> 覆寫 SourceRing 上限（取代計算器；驗證 OOM 防護用）\n"
    "  --max-defect-count-pass <n> offline-file 模式設 MaxDefectCountPass 截斷（驗決定性用）\n"
    "  --test-consumer-delay-ms <n> offline-tcp：每幀處理後人工延遲 N ms（模擬慢消費，觸發背壓 ERR 測試）\n"
    "  --test-source-writer-delay-ms <n> SourceWriter：每幀寫完後延遲 N ms（模擬慢 HDD，觸發 ring drop WARN 測試）\n"
    "\n[rdma-validate 模式（需 CFAOI_HAS_RDMA）]\n"
    "  --rdma-bind <ip>      RDMA server 綁定 IP（預設 0.0.0.0）\n"
    "  --rdma-port <port>    RDMA server port（預設 18515）\n"
    "  --rdma-slots <n>      N-slot ring 深度 = 初始 credit（預設 4，4×40MB=160MB）\n"
    "  --test-consumer-delay-ms 同樣有效：故意拖慢消費→ credit 耗盡→ Grab RNR 背壓\n";
}

bool parse_args(int argc, char** argv, Args& a) {
    for (int i = 1; i < argc; ++i) {
        std::string k = argv[i];
        auto next = [&](const char* name) -> std::string {
            if (i + 1 >= argc) { std::cerr << name << " 需要參數\n"; return ""; }
            return argv[++i];
        };
        if (k == "--mode") a.mode = next("--mode");
        else if (k == "--stitch") a.stitch = true;
        else if (k == "--input") a.input = next("--input");
        else if (k == "--output") a.output = next("--output");
        else if (k == "--recipe") a.recipe = next("--recipe");
        else if (k == "--ini") a.ini = next("--ini");
        else if (k == "--control-port") a.control_port = std::stoi(next("--control-port"));
        else if (k == "--ai-model-dir") a.ai_model_dir = next("--ai-model-dir");
        else if (k == "--ip-name") a.ip_name = next("--ip-name");
        else if (k == "--use-ai") a.use_ai = true;
        else if (k == "--no-save-images") { a.save_patches = false; a.save_overlay = false; }
        else if (k == "--no-overlay") a.save_overlay = false;
        else if (k == "--max-patches") a.max_patches = std::stoi(next("--max-patches"));
        else if (k == "--save-threads") a.save_threads = std::stoi(next("--save-threads"));
        else if (k == "--bench-iters") a.bench_iters = std::stoi(next("--bench-iters"));
        else if (k == "--bench-warmup") a.bench_warmup = std::stoi(next("--bench-warmup"));
        else if (k == "--bth") a.ov_bth = std::stof(next("--bth"));
        else if (k == "--dth") a.ov_dth = std::stof(next("--dth"));
        else if (k == "--pitch-x") a.ov_pitch_x = std::stoi(next("--pitch-x"));
        else if (k == "--pitch-y") a.ov_pitch_y = std::stoi(next("--pitch-y"));
        else if (k == "--algo-mode") a.ov_algo_mode = std::stoi(next("--algo-mode"));
        else if (k == "--dark-eps") a.ov_dark_eps = std::stoi(next("--dark-eps"));
        else if (k == "--multiscale") a.ov_multiscale = std::stoi(next("--multiscale"));
        else if (k == "--verify-deterministic") a.verify_deterministic = true;
        else if (k == "--max-queue-size") a.max_queue_size = std::stoi(next("--max-queue-size"));
        else if (k == "--max-src-ring-size") a.max_src_ring_size = std::stoi(next("--max-src-ring-size"));
        else if (k == "--max-defect-count-pass") a.max_defect_count_pass = std::stoi(next("--max-defect-count-pass"));
        else if (k == "--test-consumer-delay-ms") a.test_consumer_delay_ms = std::stoi(next("--test-consumer-delay-ms"));
        else if (k == "--test-source-writer-delay-ms") a.test_source_writer_delay_ms = std::stoi(next("--test-source-writer-delay-ms"));
        else if (k == "--rdma-bind")  a.rdma_bind  = next("--rdma-bind");
        else if (k == "--rdma-port")  a.rdma_port  = next("--rdma-port");
        else if (k == "--rdma-slots") a.rdma_slots = (uint32_t)std::stoul(next("--rdma-slots"));
        else if (k == "-h" || k == "--help") { usage(argv[0]); return false; }
        else { std::cerr << "未知參數: " << k << "\n"; usage(argv[0]); return false; }
    }
    return true;
}

// 計算 zone 在影像內的有效 ROI rect（對位後用 eff_*，全幅或夾在影像範圍內）。
cv::Rect zone_rect(const ZoneConfig& z, int w, int h) {
    if (z.is_full_frame()) return cv::Rect(0, 0, w, h);
    int x1 = std::clamp(z.eff_start_x(), 0, w - 1);
    int y1 = std::clamp(z.eff_start_y(), 0, h - 1);
    int x2 = std::clamp(z.eff_end_x(), x1 + 1, w);
    int y2 = std::clamp(z.eff_end_y(), y1 + 1, h);
    return cv::Rect(x1, y1, x2 - x1, y2 - y1);
}

// 比對兩次 process_frame 結果是否 bit-exact。一致回傳空字串；否則回傳第一個差異點描述。
std::string first_determinism_diff(const DetectionResult& a, const DetectionResult& b) {
    if (a.num_defects != b.num_defects)
        return "缺陷數不同: run1=" + std::to_string(a.num_defects) +
               " run2=" + std::to_string(b.num_defects);
    for (size_t i = 0; i < a.defects.size(); ++i) {
        const auto& d1 = a.defects[i];
        const auto& d2 = b.defects[i];
        auto neq = [&](const char* f, double v1, double v2) {
            return std::string("defect[") + std::to_string(i) + "]." + f +
                   ": run1=" + std::to_string(v1) + " run2=" + std::to_string(v2);
        };
        if (d1.center_x != d2.center_x) return neq("center_x", d1.center_x, d2.center_x);
        if (d1.center_y != d2.center_y) return neq("center_y", d1.center_y, d2.center_y);
        if (d1.size     != d2.size)     return neq("size", d1.size, d2.size);
        if (d1.is_bright!= d2.is_bright)return neq("is_bright", d1.is_bright, d2.is_bright);
        if (d1.min_x != d2.min_x) return neq("min_x", d1.min_x, d2.min_x);
        if (d1.max_x != d2.max_x) return neq("max_x", d1.max_x, d2.max_x);
        if (d1.min_y != d2.min_y) return neq("min_y", d1.min_y, d2.min_y);
        if (d1.max_y != d2.max_y) return neq("max_y", d1.max_y, d2.max_y);
        if (d1.avg_brightness != d2.avg_brightness)
            return neq("avg_brightness", d1.avg_brightness, d2.avg_brightness);
    }
    return "";
}

// 對一張影像跑所有 zone，回傳聚合結果。
// verify=true 時每個 zone 跑兩次比對 bit-exact，不一致則把 verify_failed 設 true 並印第一個差異。
// saving_cfg.max_defect_count_pass >= 0 時：累計缺陷超過上限後停止後續 zone（整數比較，zone 完成後才 break）。
InspectionResult process_image(GpuPipeline& pipe, const std::vector<ZoneConfig>& zones,
                               const cv::Mat& gray, const std::string& panel_id,
                               bool verify, bool& verify_failed,
                               const RecipeSavingConfig& saving_cfg = {},
                               const OpticalParams& optical = {}) {
    InspectionResult agg;
    agg.panel_id = panel_id;
    agg.image_width = gray.cols;
    agg.image_height = gray.rows;
    agg.ai_used = pipe.ai_enabled();   // 有效 AI 狀態（停用時缺陷標待人工複核）
    agg.opt_res_x = optical.opt_res_x;
    agg.opt_res_y = optical.opt_res_y;
    agg.ccd_index = optical.ccd_index;
    if (!zones.empty()) agg.recipe_name = zones.front().recipe_name;

    int total_defects_so_far = 0;
    for (const auto& z : zones) {
        cv::Rect r = zone_rect(z, gray.cols, gray.rows);
        cv::Mat sub = gray(r);
        cv::Mat sub_cont = sub.isContinuous() ? sub : sub.clone();

        ZoneConfig zc = z;  // 帶入實際影像尺寸
        zc.width = sub_cont.cols;
        zc.height = sub_cont.rows;
        zc.panel_id = panel_id;

        DetectionResult dr = pipe.process_frame(sub_cont.data, sub_cont.cols, sub_cont.rows, zc);

        if (verify) {
            DetectionResult dr2 = pipe.process_frame(sub_cont.data, sub_cont.cols, sub_cont.rows, zc);
            std::string diff = first_determinism_diff(dr, dr2);
            if (!diff.empty()) {
                verify_failed = true;
                std::cerr << "[Verify] ✗ NON-DETERMINISTIC panel=" << panel_id
                          << " zone=" << z.zone_index << " 第一個差異: " << diff << "\n";
            } else {
                std::cout << "[Verify] ✓ panel=" << panel_id << " zone=" << z.zone_index
                          << " bit-exact (" << dr.num_defects << " defects)\n";
            }
        }

        // Step E Blob 過濾（size 範圍 + 鄰近合併；CPU 後處理，預設 0=關 → 不改結果）。
        // 置於 verify 之後、BypassEdge/Rule 之前（blob 連通元件過濾為最先的後處理層）。
        defect_rules::apply_blob(dr.defects, z.blob_min_size, z.blob_max_size, z.blob_merge_distance);

        // #32 邊界略過 + #16 Rule 改判（CPU 後處理；預設全關 → 不改結果）。
        // 置於 verify 之後：verify 比的是 GPU 兩跑決定性（未過濾）；過濾本身亦決定性。
        defect_rules::apply(dr.defects, r.x, r.y, gray, gray.cols, gray.rows, saving_cfg);
        dr.num_defects = defect_rules::recount(dr.defects, dr.num_bright, dr.num_dark);
        dr.pass = (dr.num_defects == 0);

        total_defects_so_far += dr.num_defects;
        ZoneResult zr;
        zr.zone_index = z.zone_index;
        zr.roi_offset_x = r.x;
        zr.roi_offset_y = r.y;
        zr.zone = z;
        zr.result = std::move(dr);
        agg.total_time_ms += zr.result.process_time_ms;
        agg.zones.push_back(std::move(zr));

        // MaxDefectCountPass：整數比較，zone GPU 完成後才 break（決定性不變式 #5）
        if (saving_cfg.max_defect_count_pass >= 0 &&
            total_defects_so_far > saving_cfg.max_defect_count_pass) {
            std::cout << "[MaxDefectCountPass] 累計缺陷=" << total_defects_so_far
                      << " > 上限=" << saving_cfg.max_defect_count_pass
                      << "，跳過剩餘 zone（panel=" << panel_id << "）\n";
            break;
        }
    }
    return agg;
}

// 行車紀錄：ZoneConfig → ZoneSnap（含 SUB/融合欄位——出事時能回答「用哪個演算法跑的」）。
// make_scene_params（每幀現場）與 record_recipe（LOAD_RECIPE 留痕）共用。
std::vector<diag::ZoneSnap> zones_to_snaps(const std::vector<ZoneConfig>& zones) {
    std::vector<diag::ZoneSnap> out;
    out.reserve(zones.size());
    for (const auto& z : zones) {
        diag::ZoneSnap zs;
        zs.zone_index = z.zone_index;
        zs.BTH = z.BTH; zs.DTH = z.DTH;
        zs.pitch_x = z.pitch_x; zs.pitch_y = z.pitch_y;
        zs.search_x = z.search_range_x; zs.search_y = z.search_range_y;
        zs.roi_x1 = z.roi_start_x; zs.roi_y1 = z.roi_start_y;
        zs.roi_x2 = z.roi_end_x;   zs.roi_y2 = z.roi_end_y;
        zs.algo_mode = z.algo_mode;
        zs.enable_multiscale = z.enable_multiscale;
        zs.pitch_times = z.pitch_times;
        zs.choose_amount = z.choose_amount;
        zs.mean_low_threshold = z.mean_low_threshold;
        zs.blob_min = z.blob_min_size; zs.blob_max = z.blob_max_size;
        zs.blob_merge = z.blob_merge_distance;
        zs.smooth_times2 = z.smooth_times2;
        zs.preproc_remap = z.preproc_remap;
        zs.lsc = z.enable_lsc;
        out.push_back(zs);
    }
    return out;
}

// 行車紀錄：從 zones + FrameHeader 組「僅參數」現場（process 前 set_scene 用，
// 使 CUDA fatal 在運算中途也抓得到當下 panel/zone 參數）。
diag::FrameScene make_scene_params(const std::vector<ZoneConfig>& zones,
                                   const std::string& panel, const FrameHeader& hdr) {
    diag::FrameScene s;
    s.panel_id  = panel;
    s.frame_seq = hdr.frameSeq;
    s.cam_id    = hdr.camId;
    s.width     = (int)hdr.width;
    s.height    = (int)hdr.height;
    s.zones = zones_to_snaps(zones);
    return s;
}

// 行車紀錄：defect_flood 觸發器——「結果錯但不 crash」盲區的爆量半邊。
// 任一 zone 觸頂 GPU cap（MAX_DEFECTS=10000，ip/CLAUDE.md 不變式 6；觸頂結果非
// bit-exact、大概率為 Pitch 設錯/守門誤路由）→ 記 incident，自動把最近 64 張現場
// （含正常張的缺陷數基線 + 本幀完整 zone 參數）落地。節流由 recorder 統一處理。
void maybe_record_defect_flood(const InspectionResult& res) {
    constexpr int kDefectCap = 10000;   // = GPU MAX_DEFECTS（不變式 6）
    int zone_capped = -1, zone_defects = 0, total = 0;
    for (const auto& zr : res.zones) {
        total += zr.result.num_defects;
        if (zr.result.num_defects >= kDefectCap && zone_capped < 0) {
            zone_capped = zr.zone_index;
            zone_defects = zr.result.num_defects;
        }
    }
    if (zone_capped < 0 && total < kDefectCap) return;
    std::string detail = "缺陷爆量 panel=" + res.panel_id +
        " total=" + std::to_string(total);
    if (zone_capped >= 0)
        detail += " zone" + std::to_string(zone_capped) + "=" +
                  std::to_string(zone_defects) + "(觸頂cap，結果已非bit-exact)";
    detail += "；第一懷疑：Pitch 設錯（不變式14）或演算法模式/閾值域錯配";
    FR_RECORD_INCIDENT("defect_flood", detail);
}

// 把結果補進現場（process 後 record_frame 用）。
void fill_scene_results(diag::FrameScene& s, const InspectionResult& res) {
    s.gpu_ms = res.total_time_ms;
    int total = 0, bright = 0, dark = 0;
    for (size_t i = 0; i < res.zones.size(); ++i) {
        if (i < s.zones.size()) s.zones[i].defects = res.zones[i].result.num_defects;
        total  += res.zones[i].result.num_defects;
        bright += res.zones[i].result.num_bright;
        dark   += res.zones[i].result.num_dark;
    }
    s.num_defects = total; s.num_bright = bright; s.num_dark = dark;
    s.pass = (total == 0);
}

}  // namespace

int main(int argc, char** argv) {
    Args args;
    if (!parse_args(argc, argv, args)) return 1;

    std::cout << "==== CF-AOI IP (mode=" << args.mode << ") ====\n";

    // ---- INI 預設參數 ----
    ZoneConfig base = ZoneConfigAdapter::from_ini(args.ini);
    const auto machine_optical = ZoneConfigAdapter::load_optical_params(args.ini);
    std::cout << "[Optical] opt_res=(" << machine_optical.opt_res_x
              << "," << machine_optical.opt_res_y
              << ") ccd_index=" << machine_optical.ccd_index << "\n";
    // bench 覆寫單一全幅 zone（掃缺陷負載上下界用；只對無 recipe 的單 zone 生效）
    if (args.ov_bth >= 0.f)  base.BTH = args.ov_bth;
    if (args.ov_dth >= 0.f)  base.DTH = args.ov_dth;
    if (args.ov_pitch_x > 0) base.pitch_x = args.ov_pitch_x;
    if (args.ov_pitch_y > 0) base.pitch_y = args.ov_pitch_y;

    // ---- GPU pipeline（先建：使行車紀錄拿得到 zero_copy/ai 狀態；CUDA init 也在 recipe 解析前）----
    GpuPipeline pipe(args.ai_model_dir);
    pipe.set_ai_active(args.use_ai);   // 預設停用 AI（訓練資料不足）
    std::cout << "[GPU] zero_copy=" << (pipe.is_zero_copy() ? "yes" : "no")
              << " ai=" << (pipe.ai_enabled() ? "on" : "off")
              << (pipe.ai_model_loaded() && !args.use_ai ? "（模型已載入但停用）" : "") << "\n";

    // ---- 行車紀錄 begin_session（bench 不啟用 → recorder 全程 no-op，gpu_ms 零擾動）----
    if (args.mode != "bench") {
        diag::SessionInfo si;
        si.mode       = args.mode;
        si.ip_name    = args.ip_name;
        si.ini        = args.ini;
        si.recipe     = args.recipe;   // 空 → offline-tcp 由 LOAD_RECIPE 帶 inline xml
        si.output_dir = args.output;
        si.zero_copy  = pipe.is_zero_copy();
        si.ai_active  = pipe.ai_enabled();
        diag::FlightRecorder::instance().begin_session(si);
    }

    // ---- 建立 ZoneConfig（INI 預設 + 可選 recipe）----
    std::vector<ZoneConfig> zones;
    if (!args.recipe.empty()) {
        try {
            zones = ZoneConfigAdapter::from_recipe_xml(args.recipe, base);
        } catch (const RecipeError& e) {
            std::cerr << "[Recipe] 載入失敗: " << e.what() << "\n";
            FR_RECORD_INCIDENT("recipe_load", e.what());
            return 2;
        }
    } else {
        zones = { base };  // 單一全幅 zone
    }
    // CLI 演算法/閾值覆寫（offline 驗證/比較用：在既有 recipe 上強制換 algo_mode / 比值閾值 / 暗區門檻）。
    if (args.ov_algo_mode >= 0 || args.ov_bth >= 0.f || args.ov_dth >= 0.f ||
        args.ov_dark_eps >= 0 || args.ov_multiscale >= 0) {
        for (auto& z : zones) {
            if (args.ov_algo_mode >= 0)  z.algo_mode = args.ov_algo_mode;
            if (args.ov_bth >= 0.f)      z.BTH = args.ov_bth;
            if (args.ov_dth >= 0.f)      z.DTH = args.ov_dth;
            if (args.ov_dark_eps >= 0)   z.mean_low_threshold = args.ov_dark_eps;
            if (args.ov_multiscale >= 0) z.enable_multiscale = args.ov_multiscale;
        }
        std::cout << "[CLI override] algo_mode=" << args.ov_algo_mode
                  << " BTH=" << args.ov_bth << " DTH=" << args.ov_dth
                  << " dark_eps=" << args.ov_dark_eps << " → 套用至 " << zones.size() << " zones\n";
    }
    // 行車紀錄：配方載入成功留痕（offline-file/stitch 的啟動配方；含 CLI 覆寫後的實效值）。
    if (!args.recipe.empty())
        diag::FlightRecorder::instance().record_recipe(args.recipe, zones_to_snaps(zones));
    // #23 興趣區（offline-file：從 recipe 檔解析一次；offline-tcp 由 LOAD_RECIPE 解析）
    std::vector<IoiRect> file_ioi = args.recipe.empty()
        ? std::vector<IoiRect>{}
        : ZoneConfigAdapter::parse_ioi_list_from_file(args.recipe);
    std::cout << "[Zone] " << zones.size() << " 個檢測區\n";

    // 存圖選項（調參加速）
    SaveOptions save_opt;
    save_opt.save_patches = args.save_patches;
    save_opt.save_overlay = args.save_overlay;
    save_opt.max_patches  = args.max_patches;
    save_opt.threads      = args.save_threads;

    // CLI 覆寫 RecipeSavingConfig（offline-file 驗決定性；offline-tcp 由 LOAD_RECIPE 覆蓋）
    RecipeSavingConfig cli_saving_cfg;
    cli_saving_cfg.max_defect_count_pass = args.max_defect_count_pass;

    int processed = 0;

    if (args.mode == "offline-file" && args.stitch) {
        // ── Step B：全 panel 拼接 ──────────────────────────────────────────────
        // 把目錄內所有 slice 依檔名序在 Y 方向 vconcat 成整片 panel（8160×ΣH），
        // 再以 legacy 全 panel 座標 recipe（ROI Y 可達 146k）偵測一次。
        // 目的：① remap 的 min/max 取「整個 ROI（全 panel）」尺度（非單 slice）
        //       ② 8-Way 比對的鄰域可跨 slice（單 slice 邊界像素不再落 margin 被漏）。
        if (args.input.empty()) { std::cerr << "offline-file --stitch 需要 --input（目錄）\n"; return 1; }
        FileImageSource src(args.input);
        FrameHeader hdr;
        std::vector<uint8_t> payload;
        std::vector<cv::Mat> slices;
        std::string first_name;
        int W = 0;
        while (src.next_frame(hdr, payload)) {
            cv::Mat m(hdr.height, hdr.width, CV_8UC1, payload.data());
            slices.push_back(m.clone());   // payload 會被重用 → 必須 clone
            if (first_name.empty()) first_name = src.current_name();
            W = hdr.width;
        }
        if (slices.empty()) { std::cerr << "[stitch] 找不到影像\n"; return 1; }
        cv::Mat panel;
        cv::vconcat(slices, panel);
        std::cout << "[stitch] 拼接 " << slices.size() << " 張 → panel "
                  << panel.cols << "x" << panel.rows << "（" << first_name << " ...）\n";
        std::string pname = "PANEL_" + first_name;
        bool vf = false;
        InspectionResult res = process_image(pipe, zones, panel, pname,
                                             args.verify_deterministic, vf,
                                             cli_saving_cfg, machine_optical);
        res.ioi_list = file_ioi;
        maybe_record_defect_flood(res);   // 爆量自動落地（拼接 panel 亦適用）
        ResultSaver::save(res, panel.data, panel.cols, panel.rows, args.output, args.ip_name, save_opt);
        std::cout << "[Done] 拼接 panel 偵測完成，缺陷 " << res.total_defects() << "\n";

    } else if (args.mode == "offline-file") {
        if (args.input.empty()) { std::cerr << "offline-file 需要 --input\n"; return 1; }
        FileImageSource src(args.input);
        FrameHeader hdr;
        std::vector<uint8_t> payload;
        bool verify_failed = false;
        while (src.next_frame(hdr, payload)) {
            cv::Mat gray(hdr.height, hdr.width, CV_8UC1, payload.data());
            std::string name = src.current_name();
            diag::FrameScene scene = make_scene_params(zones, name, hdr);
            diag::FlightRecorder::instance().set_scene(scene);  // process 前：抓參數現場
            InspectionResult res = process_image(pipe, zones, gray, name,
                                                 args.verify_deterministic, verify_failed,
                                                 cli_saving_cfg, machine_optical);
            res.ioi_list = file_ioi;   // #23 興趣區
            fill_scene_results(scene, res);
            diag::FlightRecorder::instance().record_frame(scene);  // process 後：補結果（timed region 外）
            maybe_record_defect_flood(res);                        // 爆量自動落地（觸頂/總數異常）
            diag::FlightRecorder::instance().tick_stats(scene.gpu_ms, scene.num_defects,
                                                        scene.queue_depth);
            ResultSaver::save(res, payload.data(), hdr.width, hdr.height, args.output, args.ip_name, save_opt);
            ++processed;
        }
        std::cout << "[Done] 處理 " << processed << " 張影像\n";
        if (args.verify_deterministic) {
            if (verify_failed) {
                std::cerr << "[Verify] ✗ 偵測到非決定性結果，驗證失敗\n";
                return 4;
            }
            std::cout << "[Verify] ✓ 全部影像兩次執行 bit-exact\n";
        }

    } else if (args.mode == "offline-tcp") {
        FrameQueue queue;
        SourceImageWriter src_writer;

        // ── Buffer 安全計算器 ─────────────────────────────────────────────────
        // host RAM 只算 FrameQueue payload（vector<uint8_t>）；GPU 持久 buffer
        // 用 cudaMalloc 配 device RAM，不影響 sysinfo().freeram，兩者不混用。
        // 保留一個 pinned slot（~幀大小）給 GPU pipeline 首幀分配（h_pinned/h_mapped_input）。
        {
            struct ::sysinfo si{};
            ::sysinfo(&si);
            const size_t frame_bytes =
                (size_t)(base.width  > 0 ? base.width  : 8192) *
                (size_t)(base.height > 0 ? base.height : 5000);
            const size_t host_free  = (size_t)si.freeram * si.mem_unit;
            const size_t host_avail = host_free > frame_bytes ? host_free - frame_bytes : 0;
            // 用 50% 可用 host RAM 給 FrameQueue，另 30% 給 SourceImageWriter ring（各不超過 8/4 幀）
            // --max-queue-size / --max-src-ring-size 可覆寫計算結果（驗證背壓 / OOM 防護用）。
            const size_t n_queue = args.max_queue_size > 0
                ? (size_t)args.max_queue_size
                : std::max<size_t>(1, std::min<size_t>(host_avail / 2 / frame_bytes, 8));
            const size_t n_src = args.max_src_ring_size > 0
                ? (size_t)args.max_src_ring_size
                : std::max<size_t>(1, std::min<size_t>(host_avail / 3 / frame_bytes, 4));
            queue.set_max_size(n_queue);
            std::cout << "[BufferCalc] host可用RAM=" << host_free / 1024 / 1024 << "MB"
                      << "  幀大小~" << frame_bytes / 1024 / 1024 << "MB"
                      << "  FrameQueue上限=" << n_queue << "幀"
                      << "  SourceRing上限=" << n_src << "幀\n";
            // SourceImageWriter 固定上限：啟動時一次配置，save_source_image 旗標決定是否呼叫 submit
            src_writer.init(n_src, args.output, frame_bytes, args.test_source_writer_delay_ms);
        }
        // ─────────────────────────────────────────────────────────────────────

        ControlServer server(args.control_port, queue);
        server.set_ai_enabled(pipe.ai_enabled());
        server.set_output_dir(args.output);   // 供 LIST_DEFECT_FOLDERS / SORT_DEFECTS 掃描

        std::mutex zones_mtx;  // LOAD_RECIPE 可在串流中更新 zones
        server.set_load_recipe_handler(
            [&](const std::string& recipe, const std::string& recipe_xml,
                const std::string& panel, std::string& err) -> bool {
                try {
                    // 跨機器：優先用配方 XML 內容（免共用檔案系統）；否則退回路徑。
                    auto z = recipe_xml.empty()
                        ? ZoneConfigAdapter::from_recipe_xml(recipe, base)
                        : ZoneConfigAdapter::from_recipe_xml_content(recipe_xml, base);
                    std::lock_guard<std::mutex> lk(zones_mtx);
                    zones = std::move(z);
                    std::cout << "[Recipe] LOAD_RECIPE "
                              << (recipe_xml.empty() ? ("'" + recipe + "'") : "(inline xml)")
                              << " panel=" << panel << " → " << zones.size() << " zones\n";
                    // 行車紀錄：LOAD_RECIPE 成功留痕（守門判定後每 zone 的 algo_mode/參數）。
                    // 「載錯但合法」（守門誤分類/靜默預設/換錯配方）事後可從 _diag 還原。
                    diag::FlightRecorder::instance().record_recipe(
                        (recipe_xml.empty() ? recipe : recipe + " (inline xml)") +
                            " panel=" + panel,
                        zones_to_snaps(zones));
                    return true;
                } catch (const RecipeError& e) {
                    err = e.what();
                    FR_RECORD_INCIDENT("recipe_load", err);
                    return false;
                }
            });
        // SET_ALIGN 命令：套回 ShiftX/Y 到所有 zones（aligned_* 欄位）。
        // 每片一次：LOAD_RECIPE 清 aligned_*，SET_ALIGN 後偵測使用 eff_*。
        server.set_set_align_handler([&](double shift_x, double shift_y) {
            std::lock_guard<std::mutex> lk(zones_mtx);
            apply_align_shift(zones, shift_x, shift_y);  // F1：全幅 zone 跳過，不塌成 1px
        });

        server.set_status_provider([&]() -> std::string {
            json s;
            s["processed"] = processed;
            s["ai"] = pipe.ai_enabled();
            std::lock_guard<std::mutex> lk(zones_mtx);
            s["zones"] = zones.size();
            return s.dump();
        });

        if (!server.start()) return 3;

        TcpImageSource src(queue);
        FrameHeader hdr;
        std::vector<uint8_t> payload;
        while (src.next_frame(hdr, payload)) {
            cv::Mat gray(hdr.height, hdr.width, CV_8UC1, payload.data());
            std::string panel = src.current_panel_id();
            std::string name = panel.empty()
                ? ("cam" + std::to_string(hdr.camId) + "_seq" + std::to_string(hdr.frameSeq))
                : panel;
            std::vector<ZoneConfig> z_snapshot;
            { std::lock_guard<std::mutex> lk(zones_mtx); z_snapshot = zones; }
            bool vf = false;  // tcp 串流模式不做 deterministic 驗證
            RecipeSavingConfig saving_cfg = server.saving_config();
            ShareFlags sflags = server.share_flags();
            // SaveSourceImage：copy payload → SourceImageWriter ring（非同步寫磁碟，主路徑不阻塞）。
            if (sflags.save_source_image) {
                src_writer.submit(name, hdr.width, hdr.height,
                                  std::vector<uint8_t>(payload));  // copy（payload 仍供 gray mat 用）
            }
            diag::FrameScene scene = make_scene_params(z_snapshot, name, hdr);
            scene.queue_depth = (int64_t)queue.size();  // 水位快照（進 ring，incident 時可查）
            diag::FlightRecorder::instance().set_scene(scene);  // process 前：抓參數現場
            InspectionResult res = process_image(pipe, z_snapshot, gray, name, false, vf, saving_cfg, machine_optical);
            res.ioi_list = server.ioi_list();   // #23 興趣區（LOAD_RECIPE 解析）→ 存圖時裁切
            fill_scene_results(scene, res);
            diag::FlightRecorder::instance().record_frame(scene);  // process 後：補結果
            maybe_record_defect_flood(res);                        // 爆量自動落地
            diag::FlightRecorder::instance().tick_stats(scene.gpu_ms, scene.num_defects,
                                                        scene.queue_depth);
            // TuningRecipe：GPU 跑、結果仍回 TCP，但完全不寫磁碟（量速/調參模式）。
            if (sflags.tuning_recipe) {
                std::cout << "[TuningRecipe] 跳過存圖（結果仍回傳）panel=" << name << "\n";
            } else {
                // 調參(review)：預設只存結果+overlay；SEND_IMAGE_FOR_REVIEW debug=true 時才存全部 patch。
                // save_width/height/max_patches 來自 LOAD_RECIPE recipe_saving（-1 = 向下相容無上限）。
                SaveOptions review_opt = save_opt;
                review_opt.save_patches  = server.review_save_patches();
                review_opt.save_width    = saving_cfg.save_defect_width;
                review_opt.save_height   = saving_cfg.save_defect_height;
                review_opt.max_patches   = saving_cfg.max_save_defect_count;
                ResultSaver::save(res, payload.data(), hdr.width, hdr.height, args.output, args.ip_name, review_opt);
            }
            // 把結果經 TCP 回傳給等待中的 Control（跨機器免共用檔案系統）
            server.deliver_result(name, ResultSaver::to_json(res));
            if (args.test_consumer_delay_ms > 0)
                std::this_thread::sleep_for(std::chrono::milliseconds(args.test_consumer_delay_ms));
            ++processed;
        }
        server.stop();
        std::cout << "[Done] 處理 " << processed << " 張影像\n";

    } else if (args.mode == "bench") {
        // 速度量測：一張圖載入後重複跑 process_image（所有 zone），量純 GPU 與 wall 時間。
        if (args.input.empty()) { std::cerr << "bench 需要 --input\n"; return 1; }
        FileImageSource src(args.input);
        FrameHeader hdr;
        std::vector<uint8_t> payload;
        if (!src.next_frame(hdr, payload)) { std::cerr << "[Bench] 讀不到影像\n"; return 1; }
        cv::Mat gray(hdr.height, hdr.width, CV_8UC1, payload.data());
        const int w = hdr.width, h = hdr.height;
        std::string name = src.current_name();
        bool vf = false;

        std::cout << "[Bench] 影像 " << w << "x" << h << " | zones=" << zones.size()
                  << " | BTH=" << zones[0].BTH << " DTH=" << zones[0].DTH
                  << " pitch=(" << zones[0].pitch_x << "," << zones[0].pitch_y << ")"
                  << " | warmup=" << args.bench_warmup << " iters=" << args.bench_iters
                  << " | ai=" << (pipe.ai_enabled() ? "on" : "off") << "\n";

        auto now = [] { return std::chrono::steady_clock::now(); };
        auto ms = [](auto a, auto b) {
            return std::chrono::duration<double, std::milli>(b - a).count();
        };

        for (int i = 0; i < args.bench_warmup; ++i)
            process_image(pipe, zones, gray, name, false, vf);   // 暖機（吸收 CUDA init/JIT/malloc）

        std::vector<double> gpu, wall;
        gpu.reserve(args.bench_iters); wall.reserve(args.bench_iters);
        int ndef = 0;
        for (int i = 0; i < args.bench_iters; ++i) {
            auto t0 = now();
            InspectionResult res = process_image(pipe, zones, gray, name, false, vf);
            auto t1 = now();
            double g = 0; for (const auto& z : res.zones) g += z.result.process_time_ms;
            gpu.push_back(g);
            wall.push_back(ms(t0, t1));
            ndef = res.total_defects();
        }

        // 含存圖的 wall（full save 到暫存，5 次取樣）
        std::vector<double> wall_save;
        SaveOptions full; full.save_patches = true; full.save_overlay = true;
        for (int i = 0; i < 5; ++i) {
            auto t0 = now();
            InspectionResult res = process_image(pipe, zones, gray, name, false, vf);
            ResultSaver::save(res, payload.data(), w, h, "/tmp/bench_out", args.ip_name, full);
            wall_save.push_back(ms(t0, now()));
        }

        auto rep = [](const char* tag, std::vector<double> v) {
            std::sort(v.begin(), v.end());
            double s = 0; for (double x : v) s += x;
            double mean = s / v.size(), med = v[v.size() / 2];
            double p99 = v[(size_t)std::ceil(0.99 * v.size()) - 1];
            std::printf("  %-22s mean=%.2f  median=%.2f  P99=%.2f  min=%.2f  max=%.2f ms (n=%zu)\n",
                        tag, mean, med, p99, v.front(), v.back(), v.size());
        };
        std::cout << "[Bench] 缺陷數=" << ndef << "\n";
        rep("gpu_ms(cudaEvent)", gpu);
        rep("wall_ms(no save)", wall);
        rep("wall_ms(incl save)", wall_save);

        std::sort(gpu.begin(), gpu.end());
        double g_med = gpu[gpu.size() / 2];
        double total_s = 1110.0 * g_med / 1000.0;
        std::printf("[Bench] 容量換算：1110 張 × %.2fms(gpu median) = %.1f s/面板"
                    "（30s 節拍）→ N_spark = ceil(%.2f) = %d\n",
                    g_med, total_s, total_s / 30.0, (int)std::ceil(total_s / 30.0));

    } else if (args.mode == "rdma-validate") {
#ifndef CFAOI_HAS_RDMA
        std::cerr << "[rdma-validate] 未編譯 RDMA（找不到 libibverbs），無法使用此模式\n";
        return 1;
#else
        // ── Step 3 驗證模式：N-slot RDMA ring buffer + credit 背壓 ──────────────
        // 1. 連續流：Grab 連送 100+ 幀，IP N-slot 連續接，CRC 全對
        // 2. 背壓：--test-consumer-delay-ms → credit 耗盡 → Grab poll_one() 阻塞
        // 3. slot race：繞回（seq>N）時 CRC 仍正確
        // 4. flight_recorder：CRC/magic 驗證失敗 → frame_validation incident
        //    （credit 水位無低水位 incident——credit 隱含在 post_recv 深度，無法廉價觀測；
        //      塞車徵兆改由 scene.queue_depth + stats line 的 queue_peak 提供）
        // ────────────────────────────────────────────────────────────────────────

        // max_payload = 一幀最大 payload（以 INI/recipe 的 width×height 估算）
        const uint32_t img_w = (base.width  > 0) ? (uint32_t)base.width  : 8192u;
        const uint32_t img_h = (base.height > 0) ? (uint32_t)base.height : 5000u;
        const uint32_t max_payload = img_w * img_h;

        FrameQueue queue;
        // FrameQueue 上限：--max-queue-size 或 2×rdma_slots（rdma-validate 不需太大）
        {
            const size_t n = args.max_queue_size > 0
                ? (size_t)args.max_queue_size
                : std::max<size_t>(2, (size_t)args.rdma_slots * 2);
            queue.set_max_size(n);
            std::cout << "[rdma-validate] FrameQueue 上限=" << n << " 幀"
                      << "  rdma_slots=" << args.rdma_slots
                      << "  bind=" << args.rdma_bind << ":" << args.rdma_port << "\n";
        }

        RdmaImageSource rdma_src;
        if (!rdma_src.init(args.rdma_bind, args.rdma_port,
                           args.rdma_slots, max_payload, queue)) {
            std::cerr << "[rdma-validate] RDMA 初始化失敗\n";
            return 3;
        }

        FrameHeader hdr;
        std::vector<uint8_t> payload;
        uint64_t ok_count = 0, err_count = 0;
        uint64_t last_seq = UINT64_MAX;

        while (rdma_src.next_frame(hdr, payload)) {
            // 驗收：seq 必須遞增（RC QP 保序）
            if (last_seq != UINT64_MAX && hdr.frameSeq != last_seq + 1) {
                fprintf(stderr, "[rdma-validate] ERR seq 跳躍：expected=%llu got=%llu\n",
                        (unsigned long long)(last_seq + 1),
                        (unsigned long long)hdr.frameSeq);
                ++err_count;
            }
            last_seq = hdr.frameSeq;

            // 二次驗 CRC（rdma_source 已驗過一次；此處再驗確認 payload copy 正確）
            uint32_t crc2 = crc32_ieee(payload.data(), hdr.payloadBytes);
            if (crc2 != hdr.crc32) {
                fprintf(stderr, "[rdma-validate] ERR 二次 CRC 失敗 seq=%llu"
                        " slot=%llu（got=0x%08x want=0x%08x）\n",
                        (unsigned long long)hdr.frameSeq,
                        (unsigned long long)(hdr.frameSeq % args.rdma_slots),
                        crc2, hdr.crc32);
                FR_RECORD_INCIDENT("rdma_validate",
                    "double-check crc seq=" + std::to_string(hdr.frameSeq));
                ++err_count;
            } else {
                ++ok_count;
                if (ok_count <= 5 || ok_count % 20 == 0)
                    printf("[rdma-validate] seq=%-6llu slot=%-2llu  CRC=OK  ok/err=%llu/%llu\n",
                           (unsigned long long)hdr.frameSeq,
                           (unsigned long long)(hdr.frameSeq % args.rdma_slots),
                           (unsigned long long)ok_count,
                           (unsigned long long)err_count);
            }

            if (args.test_consumer_delay_ms > 0)
                std::this_thread::sleep_for(
                    std::chrono::milliseconds(args.test_consumer_delay_ms));

            ++processed;
        }

        rdma_src.stop();
        printf("[rdma-validate] 完成：ok=%llu err=%llu total=%llu\n",
               (unsigned long long)ok_count,
               (unsigned long long)err_count,
               (unsigned long long)processed);
        if (err_count > 0) {
            std::cerr << "[rdma-validate] ✗ 有 " << err_count << " 幀錯誤，驗證失敗\n";
            return 4;
        }
        std::cout << "[rdma-validate] ✓ 全部 " << ok_count << " 幀 CRC 正確\n";
#endif  // CFAOI_HAS_RDMA

    } else if (args.mode == "rdma-process") {
#ifndef CFAOI_HAS_RDMA
        std::cerr << "[rdma-process] 未編譯 RDMA（找不到 libibverbs），無法使用此模式\n";
        return 1;
#else
        // ── 檔案→RDMA 回放「運算」模式（Gap #27 收端 / Gap #17 離線子集）────────────
        // RdmaImageSource → FrameQueue → process_image（AOI）→ ResultSaver，與 offline-tcp 同處理路徑；
        // 來源換 RDMA、配方靜態載入（--recipe/--ini）。塞車監控：每幀 proc_ms + FrameQueue 深度 + 背壓幀數。
        // CRC 已於 rdma_source recv_thread（push 前）驗過，此處不重驗。
        const uint32_t img_w = (base.width  > 0) ? (uint32_t)base.width  : 8192u;
        const uint32_t img_h = (base.height > 0) ? (uint32_t)base.height : 5000u;
        const uint32_t max_payload = img_w * img_h;

        FrameQueue queue;
        {
            const size_t n = args.max_queue_size > 0
                ? (size_t)args.max_queue_size
                : std::max<size_t>(2, (size_t)args.rdma_slots * 2);
            queue.set_max_size(n);
            std::cout << "[rdma-process] FrameQueue 上限=" << n << " 幀  rdma_slots="
                      << args.rdma_slots << "  bind=" << args.rdma_bind << ":" << args.rdma_port
                      << "  zones=" << zones.size() << "\n";
        }

        RdmaImageSource rdma_src;
        if (!rdma_src.init(args.rdma_bind, args.rdma_port,
                           args.rdma_slots, max_payload, queue)) {
            std::cerr << "[rdma-process] RDMA 初始化失敗\n";
            return 3;
        }

        FrameHeader hdr;
        std::vector<uint8_t> payload;
        bool verify_failed = false;
        double sum_proc_ms = 0.0, max_proc_ms = 0.0;
        size_t max_queue_depth = 0, backpressure_hits = 0;
        auto t_start = std::chrono::steady_clock::now();

        while (rdma_src.next_frame(hdr, payload)) {
            size_t depth = queue.size();                      // 取出本幀後佇列殘量（塞車徵兆）
            if (depth > max_queue_depth) max_queue_depth = depth;
            if (depth >= queue.max_size()) ++backpressure_hits;

            cv::Mat gray(hdr.height, hdr.width, CV_8UC1, payload.data());
            // 命名 CCD{camId}_{seq}（camId 由 sender 設為 CCD 編號；可逆對回來源/ground-truth）
            char nm[40];
            std::snprintf(nm, sizeof(nm), "CCD%02u_%06llu",
                          (unsigned)hdr.camId, (unsigned long long)hdr.frameSeq);
            std::string name = nm;

            diag::FrameScene scene = make_scene_params(zones, name, hdr);
            scene.queue_depth = (int64_t)depth;   // 水位快照（原漏填 → incident 時查不到塞車徵兆）
            diag::FlightRecorder::instance().set_scene(scene);
            auto t0 = std::chrono::steady_clock::now();
            InspectionResult res = process_image(pipe, zones, gray, name,
                                                 /*verify*/false, verify_failed,
                                                 cli_saving_cfg, machine_optical);
            res.ioi_list = file_ioi;
            double proc_ms = std::chrono::duration<double, std::milli>(
                std::chrono::steady_clock::now() - t0).count();
            sum_proc_ms += proc_ms;
            if (proc_ms > max_proc_ms) max_proc_ms = proc_ms;
            fill_scene_results(scene, res);
            diag::FlightRecorder::instance().record_frame(scene);
            maybe_record_defect_flood(res);                        // 爆量自動落地
            diag::FlightRecorder::instance().tick_stats(scene.gpu_ms, scene.num_defects,
                                                        scene.queue_depth);
            ResultSaver::save(res, payload.data(), hdr.width, hdr.height,
                              args.output, args.ip_name, save_opt);
            ++processed;
            if (processed <= 5 || processed % 20 == 0)
                printf("[rdma-process] #%d %s defects=%d proc=%.1fms queue=%zu/%zu recv_ok=%llu\n",
                       processed, name.c_str(), res.total_defects(), proc_ms,
                       queue.size(), queue.max_size(),
                       (unsigned long long)rdma_src.recv_ok());
        }

        rdma_src.stop();
        double total_s = std::chrono::duration<double>(
            std::chrono::steady_clock::now() - t_start).count();
        printf("[rdma-process] 完成：%d 幀  全程 %.1fs（%.2f fps）  proc avg/max=%.1f/%.1fms"
               "  佇列峰值=%zu/%zu  背壓幀=%zu  recv ok/err=%llu/%llu\n",
               processed, total_s, total_s > 0 ? processed / total_s : 0.0,
               processed ? sum_proc_ms / processed : 0.0, max_proc_ms,
               max_queue_depth, queue.max_size(), backpressure_hits,
               (unsigned long long)rdma_src.recv_ok(),
               (unsigned long long)rdma_src.recv_err());
        if (processed == 0) { std::cerr << "[rdma-process] ✗ 未收到任何幀\n"; return 4; }
#endif  // CFAOI_HAS_RDMA

    } else {
        std::cerr << "未知 mode: " << args.mode << "\n";
        usage(argv[0]);
        return 1;
    }

    return 0;
}
