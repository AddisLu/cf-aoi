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
#include <iostream>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include <opencv2/opencv.hpp>
#include <nlohmann/json.hpp>

#include "config/zone_config_adapter.h"
#include "gpu/gpu_pipeline.h"
#include "image_source/file_source.h"
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
    bool verify_deterministic = false;
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
    "  --verify-deterministic  offline-file：每張圖每個 zone 跑兩次比對 bit-exact，不一致則 fail\n";
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
        else if (k == "--verify-deterministic") a.verify_deterministic = true;
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
InspectionResult process_image(GpuPipeline& pipe, const std::vector<ZoneConfig>& zones,
                               const cv::Mat& gray, const std::string& panel_id,
                               bool verify, bool& verify_failed) {
    InspectionResult agg;
    agg.panel_id = panel_id;
    agg.image_width = gray.cols;
    agg.image_height = gray.rows;
    if (!zones.empty()) agg.recipe_name = zones.front().recipe_name;

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

        ZoneResult zr;
        zr.zone_index = z.zone_index;
        zr.roi_offset_x = r.x;
        zr.roi_offset_y = r.y;
        zr.zone = z;
        zr.result = std::move(dr);
        agg.total_time_ms += zr.result.process_time_ms;
        agg.zones.push_back(std::move(zr));
    }
    return agg;
}

}  // namespace

int main(int argc, char** argv) {
    Args args;
    if (!parse_args(argc, argv, args)) return 1;

    std::cout << "==== CF-AOI IP (mode=" << args.mode << ") ====\n";

    // ---- 建立 ZoneConfig（INI 預設 + 可選 recipe）----
    ZoneConfig base = ZoneConfigAdapter::from_ini(args.ini);
    std::vector<ZoneConfig> zones;
    if (!args.recipe.empty()) {
        try {
            zones = ZoneConfigAdapter::from_recipe_xml(args.recipe, base);
        } catch (const RecipeError& e) {
            std::cerr << "[Recipe] 載入失敗: " << e.what() << "\n";
            return 2;
        }
    } else {
        zones = { base };  // 單一全幅 zone
    }
    std::cout << "[Zone] " << zones.size() << " 個檢測區\n";

    // ---- GPU pipeline ----
    GpuPipeline pipe(args.ai_model_dir);
    std::cout << "[GPU] zero_copy=" << (pipe.is_zero_copy() ? "yes" : "no")
              << " ai=" << (pipe.ai_enabled() ? "on" : "off") << "\n";

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
            InspectionResult res = process_image(pipe, zones, gray, name,
                                                 args.verify_deterministic, verify_failed);
            ResultSaver::save(res, payload.data(), hdr.width, hdr.height, args.output, name);
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
                } catch (const RecipeError& e) { err = e.what(); return false; }
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
                ? ("cam" + std::to_string(hdr.cam_id) + "_seq" + std::to_string(hdr.frame_seq))
                : panel;
            std::vector<ZoneConfig> z_snapshot;
            { std::lock_guard<std::mutex> lk(zones_mtx); z_snapshot = zones; }
            bool vf = false;  // tcp 串流模式不做 deterministic 驗證
            InspectionResult res = process_image(pipe, z_snapshot, gray, name, false, vf);
            ResultSaver::save(res, payload.data(), hdr.width, hdr.height, args.output, name);
            // 把結果經 TCP 回傳給等待中的 Control（跨機器免共用檔案系統）
            server.deliver_result(name, ResultSaver::to_json(res));
            ++processed;
        }
        server.stop();
        std::cout << "[Done] 處理 " << processed << " 張影像\n";

    } else {
        std::cerr << "未知 mode: " << args.mode << "\n";
        usage(argv[0]);
        return 1;
    }

    return 0;
}
