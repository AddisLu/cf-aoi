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

        // ★ 只接受 DIV 模式（gpu_algo kernel 是比例式；SUB 無固定轉換公式）
        std::string cmp;
        bool has_cmp = extract_tag(blk, "AlgorithmCompare", cmp);
        if (cmp != "DIV") {
            std::string actual = has_cmp ? ("\"" + cmp + "\"")
                                         : "(缺少 <AlgorithmCompare> 元素)";
            std::string reason = has_cmp
                ? "SUB 模式的閾值是灰階差（Origin-Reference），gpu_algo kernel 是比例式"
                  "（Origin/mean₈(neighbors) vs BTH/DTH）；灰階差轉比例需依賴局部背景灰階 R"
                  "（BTH=1+ThB_sub/R），無固定常數公式。"
                : "未指定比較模式，無法判定閾值定義域。";
            throw RecipeError(
                "[Recipe] 拒絕載入 DetectRoi[" + std::to_string(idx) + "]"
                "（zone " + std::to_string(idx) + "）：\n"
                "  • 實際 AlgorithmCompare = " + actual + "\n"
                "  • 本系統僅接受 = \"DIV\"\n"
                "  • 拒絕原因：" + reason + "\n"
                "  • 建議解法：將此 zone 的 AlgorithmCompare 改為 \"DIV\"，"
                "並把 BrightThreshold/DarkThreshold 設為比例域值（例如 1.4 / 0.6）。");
        }

        ZoneConfig z = defaults;
        z.recipe_name = recipe_name;
        z.zone_index = idx;

        // 閾值：BrightThreshold→BTH, DarkThreshold→DTH（DIV 域，直接對應）
        tag_double_as_float(blk, "BrightThreshold", z.BTH);
        tag_double_as_float(blk, "DarkThreshold",   z.DTH);

        // 幾何
        tag_int(blk, "PitchX", z.pitch_x);
        tag_int(blk, "PitchY", z.pitch_y);
        tag_int(blk, "SearchX", z.search_range_x);
        tag_int(blk, "SearchY", z.search_range_y);
        z.fast_search_range = clampi(z.search_range_y, 0, 2);  // gpu 局部搜尋是垂直向

        // ROI 範圍（StartX/StartY/EndX/EndY；-1 = 全幅）
        tag_int(blk, "StartX", z.roi_start_x);
        tag_int(blk, "StartY", z.roi_start_y);
        tag_int(blk, "EndX",   z.roi_end_x);
        tag_int(blk, "EndY",   z.roi_end_y);

        std::cout << "[ZoneConfig] zone " << idx << " DIV: BTH=" << z.BTH << " DTH=" << z.DTH
                  << " pitch=(" << z.pitch_x << "," << z.pitch_y << ")"
                  << " search=(" << z.search_range_x << "," << z.search_range_y << ")"
                  << " fast_search=" << z.fast_search_range
                  << " roi=(" << z.roi_start_x << "," << z.roi_start_y << ")-("
                  << z.roi_end_x << "," << z.roi_end_y << ")\n";
        // 註：AlgorithmWay/PitchTime/ChooseAmount/Blob* 在 gpu_algo kernel 無對應，已忽略。

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
