#include "result_saver.h"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sstream>
#include <thread>
#include <vector>

#include <opencv2/opencv.hpp>
#include <nlohmann/json.hpp>

namespace fs = std::filesystem;
using json = nlohmann::json;

namespace {

// gpu DefectInfo（ROI-local）→ legacy DefectInfo 欄位集。
// 全域座標 = ROI offset + local。GPU 無的欄位（CV_*/Sigma）填 0/-1，Filter 固定 NoFilter。
struct LegacyDefect {
    int RunIndex = 0;
    int GlobalPosX = 0, GlobalPosY = 0;
    int Size = 0, Width = 0, Height = 0;
    std::string Type;     // PointBright / PointDark
    std::string Filter = "NoFilter";
    int X_Min = 0, X_Max = 0, Y_Min = 0, Y_Max = 0;
    int GC_X = 0, GC_Y = 0;        // ROI-local 重心
    float CV_Sigma = 0, CV_Mean = 0; int CV_Min = 0, CV_Max = 0;
    float GL_Sigma = 0, GL_Mean = 0; int GL_Min = 0, GL_Max = 0;
    int AiIndex = -1; std::string AiType; float AiScore = -1;
    std::string RuleType;
    float MeanValue = -1;
    std::string DetectReason;
    std::string ImagePath;
};

LegacyDefect to_legacy(const DefectInfo& d, int off_x, int off_y, int run_index,
                       bool ai_used = false) {
    LegacyDefect L;
    L.RunIndex = run_index;
    // AI 停用時缺陷一律標待人工複核（保留架構，資料足夠後重啟 AI 才會有 AiOK/SizeNG 等）。
    L.AiType = ai_used ? "NoSet" : "待人工複核";
    L.GC_X = (int)d.center_x;
    L.GC_Y = (int)d.center_y;
    L.GlobalPosX = off_x + (int)d.center_x;
    L.GlobalPosY = off_y + (int)d.center_y;
    L.Size = d.size;
    L.Width  = d.max_x - d.min_x + 1;
    L.Height = d.max_y - d.min_y + 1;
    L.X_Min = off_x + d.min_x; L.X_Max = off_x + d.max_x;
    L.Y_Min = off_y + d.min_y; L.Y_Max = off_y + d.max_y;
    L.Type = d.is_bright ? "PointBright" : "PointDark";
    L.DetectReason = d.is_bright ? "Bright" : "Dark";  // gpu_algo 比例式 → 亮/暗點
    L.GL_Mean = d.avg_brightness;
    return L;
}

// 一塊 panel 的資料夾名 = legacy "{panelId}_{recipeName}"（recipe 空則只用 panelId）。
std::string panel_folder_name(const InspectionResult& r) {
    if (r.recipe_name.empty()) return r.panel_id;
    return r.panel_id + "_" + r.recipe_name;
}

// 今日 yyyyMMdd（本地時間，對齊 legacy DateTime.Now.ToString("yyyyMMdd")）。
std::string today_yyyymmdd() {
    std::time_t t = std::time(nullptr);
    std::tm tm {};
    ::localtime_r(&t, &tm);
    char buf[16];
    std::strftime(buf, sizeof(buf), "%Y%m%d", &tm);
    return buf;
}

// legacy 缺陷檔名：Defect_{Ip}_Slice{ff}_Roi{rr}_Run{nn}_X{xxxx}_Y{yyyyyy}_Dr{reason}.png
std::string defect_filename(const std::string& ip_name, int slice, int roi, int run,
                            int gx, int gy, const std::string& reason) {
    char buf[256];
    std::snprintf(buf, sizeof(buf),
                  "Defect_%s_Slice%02d_Roi%02d_Run%02d_X%04d_Y%06d_Dr%s.png",
                  ip_name.c_str(), slice, roi, run, gx, gy, reason.c_str());
    return buf;
}

void xml_escape(std::ostream& os, const std::string& s) {
    for (char c : s) {
        switch (c) {
            case '&': os << "&amp;"; break;
            case '<': os << "&lt;"; break;
            case '>': os << "&gt;"; break;
            case '"': os << "&quot;"; break;
            default: os << c;
        }
    }
}

void write_defect_xml(std::ostream& os, const LegacyDefect& d) {
    os << "        <DefectInfo>\n";
    os << "          <RunIndex>" << d.RunIndex << "</RunIndex>\n";
    os << "          <GlobalPosX>" << d.GlobalPosX << "</GlobalPosX>\n";
    os << "          <GlobalPosY>" << d.GlobalPosY << "</GlobalPosY>\n";
    os << "          <Size>" << d.Size << "</Size>\n";
    os << "          <Width>" << d.Width << "</Width>\n";
    os << "          <Height>" << d.Height << "</Height>\n";
    os << "          <Type>"; xml_escape(os, d.Type); os << "</Type>\n";
    os << "          <Filter>"; xml_escape(os, d.Filter); os << "</Filter>\n";
    os << "          <Y_Min>" << d.Y_Min << "</Y_Min>\n";
    os << "          <Y_Max>" << d.Y_Max << "</Y_Max>\n";
    os << "          <X_Min>" << d.X_Min << "</X_Min>\n";
    os << "          <X_Max>" << d.X_Max << "</X_Max>\n";
    os << "          <GC_Y>" << d.GC_Y << "</GC_Y>\n";
    os << "          <GC_X>" << d.GC_X << "</GC_X>\n";
    os << "          <CV_Sigma>" << d.CV_Sigma << "</CV_Sigma>\n";
    os << "          <CV_Mean>" << d.CV_Mean << "</CV_Mean>\n";
    os << "          <CV_Min>" << d.CV_Min << "</CV_Min>\n";
    os << "          <CV_Max>" << d.CV_Max << "</CV_Max>\n";
    os << "          <GL_Sigma>" << d.GL_Sigma << "</GL_Sigma>\n";
    os << "          <GL_Mean>" << d.GL_Mean << "</GL_Mean>\n";
    os << "          <GL_Min>" << d.GL_Min << "</GL_Min>\n";
    os << "          <GL_Max>" << d.GL_Max << "</GL_Max>\n";
    os << "          <AiIndex>" << d.AiIndex << "</AiIndex>\n";
    os << "          <AiType>"; xml_escape(os, d.AiType); os << "</AiType>\n";
    os << "          <AiScore>" << d.AiScore << "</AiScore>\n";
    os << "          <RuleType>"; xml_escape(os, d.RuleType); os << "</RuleType>\n";
    os << "          <MeanValue>" << d.MeanValue << "</MeanValue>\n";
    os << "          <DetectReason>"; xml_escape(os, d.DetectReason); os << "</DetectReason>\n";
    os << "          <ImagePath>"; xml_escape(os, d.ImagePath); os << "</ImagePath>\n";
    os << "        </DefectInfo>\n";
}

json defect_to_json(const LegacyDefect& d) {
    return json{
        {"RunIndex", d.RunIndex},
        {"GlobalPosX", d.GlobalPosX}, {"GlobalPosY", d.GlobalPosY},
        {"Size", d.Size}, {"Width", d.Width}, {"Height", d.Height},
        {"Type", d.Type}, {"Filter", d.Filter},
        {"X_Min", d.X_Min}, {"X_Max", d.X_Max}, {"Y_Min", d.Y_Min}, {"Y_Max", d.Y_Max},
        {"GC_X", d.GC_X}, {"GC_Y", d.GC_Y},
        {"CV_Sigma", d.CV_Sigma}, {"CV_Mean", d.CV_Mean}, {"CV_Min", d.CV_Min}, {"CV_Max", d.CV_Max},
        {"GL_Sigma", d.GL_Sigma}, {"GL_Mean", d.GL_Mean}, {"GL_Min", d.GL_Min}, {"GL_Max", d.GL_Max},
        {"AiIndex", d.AiIndex}, {"AiType", d.AiType}, {"AiScore", d.AiScore},
        {"RuleType", d.RuleType}, {"MeanValue", d.MeanValue},
        {"DetectReason", d.DetectReason}, {"ImagePath", d.ImagePath},
    };
}

}  // namespace

namespace ResultSaver {

std::string to_json(const InspectionResult& r) {
    json j;
    j["panel_id"]     = r.panel_id;
    j["recipe_name"]  = r.recipe_name;
    j["DefectCnt"]    = r.total_defects();
    j["AiOkCnt"]      = 0;
    j["RuleOkCnt"]    = 0;
    j["pass"]         = r.pass();
    j["total_time_ms"]= r.total_time_ms;
    j["image_width"]  = r.image_width;
    j["image_height"] = r.image_height;

    json roi_list = json::array();
    for (const auto& z : r.zones) {
        json defs = json::array();
        int ri = 0;
        for (const auto& d : z.result.defects)
            defs.push_back(defect_to_json(to_legacy(d, z.roi_offset_x, z.roi_offset_y, ri++, r.ai_used)));
        roi_list.push_back({
            {"RoiIndex", z.zone_index},
            {"roi_offset_x", z.roi_offset_x},
            {"roi_offset_y", z.roi_offset_y},
            {"num_defects", z.result.num_defects},
            {"process_time_ms", z.result.process_time_ms},
            {"DefectInfoList", defs},
        });
    }
    j["RoiInfoList"] = roi_list;
    return j.dump(2);
}

int save(const InspectionResult& r,
         const uint8_t* img, int w, int h,
         const std::string& out_dir,
         const std::string& ip_name,
         const SaveOptions& opt,
         std::string* out_panel_dir) {
    const int patch_size = opt.patch_size;
    // ---- 資料夾結構：<out>/<yyyyMMdd>/<panelId>_<recipeName>/（對齊 legacy）----
    const std::string date = today_yyyymmdd();
    const std::string basename = panel_folder_name(r);   // ResultInfo 檔名前綴 = panel 夾名
    const std::string panel_dir = out_dir + "/" + date + "/" + basename;
    fs::create_directories(panel_dir);
    if (out_panel_dir) *out_panel_dir = panel_dir;
    const std::string& dst = panel_dir;   // 缺陷圖 / ResultInfo 都落在 panel 夾

    // ---- 0. 多 ROI 邊界死區可見化 ----
    // gpu_algo 8-Way kernel 會跳過距 ROI 邊緣 margin 內的像素（cuda_kernels.cu:135-145）：
    //   margin_x = pitch_x*2 + fast_search_range, margin_y = pitch_y*2 + fast_search_range
    // 這些邊界像素「未被檢測」。逐 ROI 印出有效檢測區與死區寬度，讓使用者知道未檢測範圍。
    for (const auto& z : r.zones) {
        const int roiW = z.result.image_width;
        const int roiH = z.result.image_height;
        const int mx = z.zone.pitch_x * 2 + z.zone.fast_search_range;
        const int my = z.zone.pitch_y * 2 + z.zone.fast_search_range;
        const int ox = z.roi_offset_x, oy = z.roi_offset_y;
        if (roiW <= 2 * mx || roiH <= 2 * my) {
            std::cout << "[DeathMargin] zone " << z.zone_index << " ⚠ ROI " << roiW << "x" << roiH
                      << " 小於死區 2*(" << mx << "," << my << ") → 整個 ROI 幾乎/完全未檢測！\n";
        } else {
            std::cout << "[DeathMargin] zone " << z.zone_index
                      << " ROI=(" << ox << "," << oy << ")+" << roiW << "x" << roiH
                      << " death_margin=(x:" << mx << ", y:" << my << ")"
                      << " → 有效檢測區(global)=[" << (ox + mx) << "," << (oy + my) << "]..["
                      << (ox + roiW - mx) << "," << (oy + roiH - my) << "]"
                      << "（四邊各 " << mx << "/" << my << " px 未檢測）\n";
        }
    }

    // ---- 1. 新版 JSON（legacy 欄位名）----
    {
        std::ofstream f(dst + "/" + basename + "_ResultInfo.json");
        f << to_json(r);
    }

    // ---- 2. legacy JudgeResult XML ----
    {
        std::ofstream os(dst + "/" + basename + "_ResultInfo.xml");
        os << "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n";
        os << "<JudgeResult>\n";
        os << "  <DefectCnt>" << r.total_defects() << "</DefectCnt>\n";
        os << "  <AiOkCnt>0</AiOkCnt>\n";
        os << "  <RuleOkCnt>0</RuleOkCnt>\n";
        os << "  <SaveDefectWidth>" << patch_size << "</SaveDefectWidth>\n";
        os << "  <SaveDefectHeight>" << patch_size << "</SaveDefectHeight>\n";
        os << "  <RoiInfoList>\n";
        for (const auto& z : r.zones) {
            os << "    <RoiInfo>\n";
            os << "      <RoiIndex>" << z.zone_index << "</RoiIndex>\n";
            os << "      <DefectInfoList>\n";
            int ri = 0;
            for (const auto& d : z.result.defects)
                write_defect_xml(os, to_legacy(d, z.roi_offset_x, z.roi_offset_y, ri++, r.ai_used));
            os << "      </DefectInfoList>\n";
            os << "    </RoiInfo>\n";
        }
        os << "  </RoiInfoList>\n";
        os << "  <IoiInfoList />\n";
        os << "</JudgeResult>\n";
    }

    // ---- 3. 缺陷小圖（legacy Defect_ 命名）+ 4. overlay ----
    // 效能：crop（單緒、快）→ 平行寫檔（imwrite 是瓶頸，對不同檔案 thread-safe）。
    //       overlay 改 PNG（BMP 全圖未壓縮 ~163MB 寫太慢）；調參階段可關圖/限張數。
    using clk = std::chrono::steady_clock;
    auto ms = [](clk::time_point a, clk::time_point b) {
        return std::chrono::duration<double, std::milli>(b - a).count();
    };
    cv::Mat gray(h, w, CV_8UC1, const_cast<uint8_t*>(img));
    const int frame_h = r.frame_height > 0 ? r.frame_height : h;  // Slice = GlobalPosY / frame 高
    const int half = patch_size / 2;
    const std::vector<int> png_fast = {cv::IMWRITE_PNG_COMPRESSION, 1};  // 低壓縮 = 快

    // -- crop 階段：產生 (檔名, patch) 清單（受 save_patches / max_patches 限制）--
    auto t_crop0 = clk::now();
    std::vector<std::pair<std::string, cv::Mat>> tasks;
    int total_defects = 0;
    if (opt.save_patches) {
        for (const auto& z : r.zones) {
            int run = 0;
            for (const auto& d : z.result.defects) {
                ++total_defects;
                if (opt.max_patches >= 0 && (int)tasks.size() >= opt.max_patches) { ++run; continue; }
                int cx = z.roi_offset_x + (int)d.center_x;
                int cy = z.roi_offset_y + (int)d.center_y;
                int slice = frame_h > 0 ? cy / frame_h : 0;
                int x1 = std::max(0, cx - half), y1 = std::max(0, cy - half);
                int x2 = std::min(w, cx + half),  y2 = std::min(h, cy + half);
                ++run;
                if (x2 <= x1 || y2 <= y1) continue;
                cv::Mat patch = gray(cv::Rect(x1, y1, x2 - x1, y2 - y1)).clone();
                if (patch.cols < patch_size || patch.rows < patch_size) {
                    cv::Mat padded = cv::Mat::zeros(patch_size, patch_size, patch.type());
                    int ox = (patch_size - patch.cols) / 2, oy = (patch_size - patch.rows) / 2;
                    patch.copyTo(padded(cv::Rect(ox, oy, patch.cols, patch.rows)));
                    patch = padded;
                }
                std::string reason = d.is_bright ? "Bright" : "Dark";
                tasks.emplace_back(dst + "/" + defect_filename(ip_name, slice, z.zone_index,
                                                               run - 1, cx, cy, reason),
                                   std::move(patch));
            }
        }
    } else {
        total_defects = r.total_defects();
    }
    double crop_ms = ms(t_crop0, clk::now());

    // -- 平行寫缺陷小圖 --
    auto t_patch0 = clk::now();
    std::atomic<int> saved{0};
    if (!tasks.empty()) {
        unsigned nthreads = opt.threads > 0 ? (unsigned)opt.threads
                                            : std::max(1u, std::thread::hardware_concurrency());
        nthreads = std::min<unsigned>(nthreads, (unsigned)tasks.size());
        std::atomic<size_t> next{0};
        auto worker = [&]() {
            size_t i;
            while ((i = next.fetch_add(1)) < tasks.size()) {
                if (cv::imwrite(tasks[i].first, tasks[i].second, png_fast)) ++saved;
            }
        };
        std::vector<std::thread> pool;
        for (unsigned t = 0; t < nthreads; ++t) pool.emplace_back(worker);
        for (auto& th : pool) th.join();
    }
    double patch_ms = ms(t_patch0, clk::now());

    // -- overlay（PNG，低壓縮）--
    auto t_ov0 = clk::now();
    if (opt.save_overlay) {
        cv::Mat overlay;
        cv::cvtColor(gray, overlay, cv::COLOR_GRAY2BGR);
        for (const auto& z : r.zones) {
            for (const auto& d : z.result.defects) {
                cv::Scalar color = d.is_bright ? cv::Scalar(0, 0, 255) : cv::Scalar(255, 0, 0);
                int mx = std::max(0, z.roi_offset_x + d.min_x - 2);
                int my = std::max(0, z.roi_offset_y + d.min_y - 2);
                int Mx = std::min(w - 1, z.roi_offset_x + d.max_x + 2);
                int My = std::min(h - 1, z.roi_offset_y + d.max_y + 2);
                cv::rectangle(overlay, cv::Point(mx, my), cv::Point(Mx, My), color, 2);
            }
        }
        cv::imwrite(dst + "/" + basename + "_result.png", overlay, png_fast);
    }
    double ov_ms = ms(t_ov0, clk::now());

    const char* ai_note = r.ai_used ? "" : "(待人工複核)";
    std::cout << "[ResultSaver] " << basename << ": " << total_defects << " 缺陷" << ai_note
              << " (" << r.zones.size() << " zones), 存 " << saved.load() << "/" << tasks.size()
              << " 缺陷圖 → " << dst << "\n";
    std::cout << "[ResultSaver] 存圖耗時: crop " << (long)crop_ms << "ms, patches "
              << (long)patch_ms << "ms (" << saved.load() << " 張), overlay "
              << (long)ov_ms << "ms"
              << (opt.save_patches ? "" : " [跳過 patch]")
              << (opt.save_overlay ? "" : " [跳過 overlay]")
              << (opt.max_patches >= 0 ? (" [限 " + std::to_string(opt.max_patches) + " 張]") : "")
              << "\n";
    return saved.load();
}

}  // namespace ResultSaver
