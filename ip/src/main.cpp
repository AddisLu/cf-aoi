/**
 * ============================================================================
 * CF-AOI IP 程式 — 進入點（Step 1：offline-tcp / offline-file）
 * ============================================================================
 *
 * 流程：
 *   1. 解析參數，建立 ZoneConfig（INI 預設 + 可選 recipe 覆蓋，多 zone）。
 *   2. 建 GpuPipeline（移植自 gpu_algo batch_detector）。
 *   3. 依 mode 建 IImageSource：
 *        offline-file → FileImageSource
 *        offline-tcp  → ControlServer(餵 FrameQueue) + TcpImageSource(消費)
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

#include "config/recipe_saving_config.h"
#include "config/share_flags.h"
#include "config/zone_config_adapter.h"
#include "diag/flight_recorder.h"
#include "gpu/gpu_pipeline.h"
#include "image_source/file_source.h"
#include "image_source/source_image_writer.h"
#include "image_source/tcp_source.h"
#include "control_server.h"
#include "result_saver.h"

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
    // 驗證用覆寫（取代 buffer 計算器結果；-1 = 用計算器）
    int  max_queue_size    = -1;   // --max-queue-size N：覆寫 FrameQueue 上限（壓力測試用）
    int  max_src_ring_size = -1;   // --max-src-ring-size N：覆寫 SourceRing 上限（OOM 測試用）
    // MaxDefectCountPass CLI 覆寫（offline-file 驗決定性用；offline-tcp 由 LOAD_RECIPE 覆蓋）
    int  max_defect_count_pass = -1;  // --max-defect-count-pass N
    // 壓力測試用：消費端人工延遲（模擬慢 GPU/慢磁碟，讓 queue 積累到滿觸發背壓 ERR）
    int  test_consumer_delay_ms = 0;  // --test-consumer-delay-ms N
    // 壓力測試用：SourceWriter 寫檔後人工延遲（模擬慢 HDD/NAS，讓 ring 積累到滿觸發 drop WARN）
    int  test_source_writer_delay_ms = 0;  // --test-source-writer-delay-ms N
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
    "  --test-source-writer-delay-ms <n> SourceWriter：每幀寫完後延遲 N ms（模擬慢 HDD，觸發 ring drop WARN 測試）\n";
}

bool parse_args(int argc, char** argv, Args& a) {
    for (int i = 1; i < argc; ++i) {
        std::string k = argv[i];
        auto next = [&](const char* name) -> std::string {
            if (i + 1 >= argc) { std::cerr << name << " 需要參數\n"; return ""; }
            return argv[++i];
        };
        if (k == "--mode") a.mode = next("--mode");
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
        else if (k == "--verify-deterministic") a.verify_deterministic = true;
        else if (k == "--max-queue-size") a.max_queue_size = std::stoi(next("--max-queue-size"));
        else if (k == "--max-src-ring-size") a.max_src_ring_size = std::stoi(next("--max-src-ring-size"));
        else if (k == "--max-defect-count-pass") a.max_defect_count_pass = std::stoi(next("--max-defect-count-pass"));
        else if (k == "--test-consumer-delay-ms") a.test_consumer_delay_ms = std::stoi(next("--test-consumer-delay-ms"));
        else if (k == "--test-source-writer-delay-ms") a.test_source_writer_delay_ms = std::stoi(next("--test-source-writer-delay-ms"));
        else if (k == "-h" || k == "--help") { usage(argv[0]); return false; }
        else { std::cerr << "未知參數: " << k << "\n"; usage(argv[0]); return false; }
    }
    return true;
}

// 計算 zone 在影像內的有效 ROI rect（全幅或夾在影像範圍內）。
cv::Rect zone_rect(const ZoneConfig& z, int w, int h) {
    if (z.is_full_frame()) return cv::Rect(0, 0, w, h);
    int x1 = std::clamp(z.roi_start_x, 0, w - 1);
    int y1 = std::clamp(z.roi_start_y, 0, h - 1);
    int x2 = std::clamp(z.roi_end_x, x1 + 1, w);
    int y2 = std::clamp(z.roi_end_y, y1 + 1, h);
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
                               const RecipeSavingConfig& saving_cfg = {}) {
    InspectionResult agg;
    agg.panel_id = panel_id;
    agg.image_width = gray.cols;
    agg.image_height = gray.rows;
    agg.ai_used = pipe.ai_enabled();   // 有效 AI 狀態（停用時缺陷標待人工複核）
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
    for (const auto& z : zones) {
        diag::ZoneSnap zs;
        zs.zone_index = z.zone_index;
        zs.BTH = z.BTH; zs.DTH = z.DTH;
        zs.pitch_x = z.pitch_x; zs.pitch_y = z.pitch_y;
        zs.search_x = z.search_range_x; zs.search_y = z.search_range_y;
        zs.roi_x1 = z.roi_start_x; zs.roi_y1 = z.roi_start_y;
        zs.roi_x2 = z.roi_end_x;   zs.roi_y2 = z.roi_end_y;
        s.zones.push_back(zs);
    }
    return s;
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
            diag::FlightRecorder::instance().record_incident("recipe_load", e.what());
            return 2;
        }
    } else {
        zones = { base };  // 單一全幅 zone
    }
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

    if (args.mode == "offline-file") {
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
                                                 cli_saving_cfg);
            fill_scene_results(scene, res);
            diag::FlightRecorder::instance().record_frame(scene);  // process 後：補結果（timed region 外）
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
                    return true;
                } catch (const RecipeError& e) {
                    err = e.what();
                    diag::FlightRecorder::instance().record_incident("recipe_load", err);
                    return false;
                }
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
            InspectionResult res = process_image(pipe, z_snapshot, gray, name, false, vf, saving_cfg);
            fill_scene_results(scene, res);
            diag::FlightRecorder::instance().record_frame(scene);  // process 後：補結果
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

    } else {
        std::cerr << "未知 mode: " << args.mode << "\n";
        usage(argv[0]);
        return 1;
    }

    return 0;
}
