#include "zone_config_adapter.h"
#include "config_parser.h"   // ✅ 從 gpu_algo 直接複製來的 INI 解析器

#include <algorithm>
#include <cctype>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

namespace {

// 從 XML 片段取出 <Tag>value</Tag> 內容（第一個出現）。RecipeInfo.xml 的
// DetectRoi 欄位皆為單純標量元素，故用字串搜尋即可，不引入 XML 函式庫。
bool extract_tag(const std::string& xml, const std::string& tag, std::string& out) {
    const std::string open = "<" + tag + ">";
    const std::string close = "</" + tag + ">";
    size_t s = xml.find(open);
    if (s == std::string::npos) return false;
    s += open.size();
    size_t e = xml.find(close, s);
    if (e == std::string::npos) return false;
    out = xml.substr(s, e - s);
    size_t a = out.find_first_not_of(" \t\r\n");
    size_t b = out.find_last_not_of(" \t\r\n");
    if (a == std::string::npos) { out.clear(); return true; }
    out = out.substr(a, b - a + 1);
    return true;
}

bool tag_int(const std::string& xml, const std::string& tag, int& v) {
    std::string s;
    if (!extract_tag(xml, tag, s) || s.empty()) return false;
    try { v = std::stoi(s); return true; } catch (...) { return false; }
}

bool tag_double_as_float(const std::string& xml, const std::string& tag, float& v) {
    std::string s;
    if (!extract_tag(xml, tag, s) || s.empty()) return false;
    try { v = (float)std::stod(s); return true; } catch (...) { return false; }
}

int clampi(int v, int lo, int hi) { return std::max(lo, std::min(hi, v)); }

}  // namespace

namespace ZoneConfigAdapter {

ZoneConfig from_ini(const std::string& path) {
    ConfigParser::Config c = ConfigParser::loadConfig(path);

    ZoneConfig z;
    z.width  = c.width;
    z.height = c.height;
    z.pitch_x = c.pitch_x;
    z.pitch_y = c.pitch_y;
    z.search_range_x = c.search_range_x;
    z.search_range_y = c.search_range_y;
    z.fast_search_range = c.fast_search_range;
    z.enable_multiscale = c.enable_multiscale;
    z.BTH = c.BTH;
    z.DTH = c.DTH;
    z.enable_lsc = c.enable_lsc;
    z.lsc_k1 = c.lsc_k1;
    z.lsc_k2 = c.lsc_k2;
    z.lsc_k3 = c.lsc_k3;
    z.lsc_max_gain = c.lsc_max_gain;
    z.lsc_auto_calibrate = c.lsc_auto_calibrate;
    z.block_dim_x = 16;  // GPU 內部固定，不帶入 INI 的 32×32
    z.block_dim_y = 16;
    // ROI 全部 -1 → 全幅
    return z;
}

OpticalParams load_optical_params(const std::string& ini_path) {
    ConfigParser::Config c = ConfigParser::loadConfig(ini_path);
    OpticalParams p;
    p.opt_res_x = c.opt_res_x;   // 已在 config_parser.h 解析時夾為 >0 或 0.0
    p.opt_res_y = c.opt_res_y;
    p.ccd_index = c.ccd_index;
    return p;
}

std::vector<ZoneConfig> from_recipe_xml(const std::string& xml_path,
                                        const ZoneConfig& defaults) {
    std::ifstream f(xml_path);
    if (!f.is_open())
        throw RecipeError("無法開啟 RecipeInfo.xml: " + xml_path);

    std::stringstream ss;
    ss << f.rdbuf();
    return from_recipe_xml_content(ss.str(), defaults);
}

std::vector<ZoneConfig> from_recipe_xml_content(const std::string& xml,
                                                const ZoneConfig& defaults) {
    std::string recipe_name = defaults.recipe_name;
    { std::string n; if (extract_tag(xml, "RecipeName", n) && !n.empty()) recipe_name = n; }

    // 逐個 <DetectRoi> ... </DetectRoi> 區塊解析（DetectRoiList 內可有多個）。
    std::vector<ZoneConfig> zones;
    const std::string open = "<DetectRoi>";
    const std::string close = "</DetectRoi>";
    size_t pos = 0;
    int idx = 0;
    while (true) {
        size_t s = xml.find(open, pos);
        if (s == std::string::npos) break;
        size_t e = xml.find(close, s);
        if (e == std::string::npos) break;
        const std::string blk = xml.substr(s + open.size(), e - (s + open.size()));
        pos = e + close.size();

        // ★ 演算法域判定 —— 權威欄位是 M_AlgorithmWayCompare（含 _Sub/_Div），
        //   不是 stale 的 <AlgorithmCompare>（legacy CamProc.cs:501-543 即以 enum 為準覆蓋字串）。
        //   舊版只比 <AlgorithmCompare>="DIV" 會被掛 DIV 字串、實為 SUB 的 recipe 騙過 → 靜默假 PASS。
        std::string awc;  bool has_awc = extract_tag(blk, "M_AlgorithmWayCompare", awc);
        std::string cmp;  bool has_cmp = extract_tag(blk, "AlgorithmCompare", cmp);
        auto contains_ci = [](const std::string& h, const char* needle) {
            std::string a = h, b = needle;
            for (auto& c : a) c = (char)std::tolower((unsigned char)c);
            for (auto& c : b) c = (char)std::tolower((unsigned char)c);
            return a.find(b) != std::string::npos;
        };
        bool is_sub = has_awc && contains_ci(awc, "sub");
        bool is_div = (has_awc && contains_ci(awc, "div")) || (cmp == "DIV");

        ZoneConfig z = defaults;
        z.recipe_name = recipe_name;
        z.zone_index = idx;

        // 閾值（兩域同欄位、語意不同）
        tag_double_as_float(blk, "BrightThreshold", z.BTH);
        tag_double_as_float(blk, "DarkThreshold",   z.DTH);

        // 幾何
        tag_int(blk, "PitchX", z.pitch_x);
        tag_int(blk, "PitchY", z.pitch_y);
        tag_int(blk, "SearchX", z.search_range_x);
        tag_int(blk, "SearchY", z.search_range_y);
        z.fast_search_range = clampi(z.search_range_y, 0, 2);  // DIV gpu 局部搜尋是垂直向

        if (is_sub) {
            // SUB（灰階差 8-Way-Star 投票）：BTH/DTH 為灰階差(+17/-16)，配 PitchTime/ChooseAmount。
            z.algo_mode = 1;
            tag_int(blk, "PitchTime",    z.pitch_times);
            tag_int(blk, "ChooseAmount", z.choose_amount);
            if (z.pitch_times   < 1) z.pitch_times   = 1;
            if (z.choose_amount < 1) z.choose_amount = 1;
            if (z.DTH >= 0.0f)
                std::cerr << "[ZoneConfig] WARN zone " << idx
                          << " SUB 但 DarkThreshold=" << z.DTH << " ≥0（SUB 暗閾值通常為負灰階差）\n";
        } else if (is_div) {
            // DIV（比例式）：BTH/DTH 為比例。防呆：DTH<0 是 SUB 域值誤標 DIV（舊靜默假 PASS 漏洞）。
            z.algo_mode = 0;
            if (z.DTH < 0.0f)
                throw RecipeError(
                    "[Recipe] 拒絕載入 DetectRoi[" + std::to_string(idx) + "]：標示 DIV 但 "
                    "DarkThreshold=" + std::to_string(z.DTH) + " <0 = SUB 灰階差域值。"
                    "若實為 SUB，請設 <M_AlgorithmWayCompare> 含 'Sub'；若真 DIV，DarkThreshold 應為比例(>0)。");
        } else {
            throw RecipeError(
                "[Recipe] 拒絕載入 DetectRoi[" + std::to_string(idx) + "]：無法判定演算法域。\n"
                "  • M_AlgorithmWayCompare = " + (has_awc ? ("\"" + awc + "\"") : "(缺)") + "\n"
                "  • AlgorithmCompare = " + (has_cmp ? ("\"" + cmp + "\"") : "(缺)") + "\n"
                "  • 本系統接受：M_AlgorithmWayCompare 含 'Sub'(SUB 投票) 或 'Div'/AlgorithmCompare=\"DIV\"(DIV 比例)。");
        }

        // ROI 範圍（StartX/StartY/EndX/EndY；-1 = 全幅）
        tag_int(blk, "StartX", z.roi_start_x);
        tag_int(blk, "StartY", z.roi_start_y);
        tag_int(blk, "EndX",   z.roi_end_x);
        tag_int(blk, "EndY",   z.roi_end_y);

        std::cout << "[ZoneConfig] zone " << idx << (z.algo_mode == 1 ? " SUB" : " DIV")
                  << ": BTH=" << z.BTH << " DTH=" << z.DTH
                  << " pitch=(" << z.pitch_x << "," << z.pitch_y << ")"
                  << " search=(" << z.search_range_x << "," << z.search_range_y << ")";
        if (z.algo_mode == 1)
            std::cout << " pitch_times=" << z.pitch_times << " choose=" << z.choose_amount;
        else
            std::cout << " fast_search=" << z.fast_search_range;
        std::cout << " roi=(" << z.roi_start_x << "," << z.roi_start_y << ")-("
                  << z.roi_end_x << "," << z.roi_end_y << ")\n";

        zones.push_back(z);
        ++idx;
    }

    if (zones.empty())
        throw RecipeError("RecipeInfo.xml 內找不到任何 <DetectRoi>");

    return zones;
}

// #23 從 recipe 檔讀內容後解析興趣區（offline-file 用；檔不存在/空則回空，不丟例外）。
std::vector<IoiRect> parse_ioi_list_from_file(const std::string& xml_path) {
    std::ifstream f(xml_path);
    if (!f.is_open()) return {};
    std::stringstream ss; ss << f.rdbuf();
    return parse_ioi_list(ss.str());
}

// #23 解析 <DetectIoiList> 內每個 <DetectIoi>（StartX/StartY/EndX/EndY）。無則回空（向下相容）。
std::vector<IoiRect> parse_ioi_list(const std::string& xml) {
    std::vector<IoiRect> out;
    const std::string open = "<DetectIoi>", close = "</DetectIoi>";
    size_t pos = 0;
    while (true) {
        size_t s = xml.find(open, pos);
        if (s == std::string::npos) break;
        size_t e = xml.find(close, s);
        if (e == std::string::npos) break;
        const std::string blk = xml.substr(s + open.size(), e - (s + open.size()));
        pos = e + close.size();
        IoiRect r;
        tag_int(blk, "StartX", r.start_x);
        tag_int(blk, "StartY", r.start_y);
        tag_int(blk, "EndX",   r.end_x);
        tag_int(blk, "EndY",   r.end_y);
        out.push_back(r);
    }
    return out;
}

}  // namespace ZoneConfigAdapter
