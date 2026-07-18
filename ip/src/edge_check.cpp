#include "edge_check.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <sstream>
#include <vector>

namespace {

// 帶間隔差分的間隔（行）：diff[y] = smooth[y+kGap] - smooth[y-kGap]。
// 邊緣過渡帶（散射/景深）通常數行內完成，8 行間隔可吃到完整躍變量。
constexpr int kGap = 8;
// 列平均的欄下採樣步距（8192 欄取 1/8 已極穩定，成本 ~1/8）。
constexpr int kColStep = 8;
// 剖面平滑窗（box，行）：壓雜訊但不糊掉邊緣位置。
constexpr int kSmooth = 5;

// 列平均亮度剖面（rows [y0, y1)）。
std::vector<float> row_profile(const uint8_t* img, int width, int height, int y0, int y1) {
    y0 = std::max(0, y0);
    y1 = std::min(height, y1);
    std::vector<float> prof;
    if (y1 <= y0) return prof;
    prof.resize(y1 - y0);
    const int n_cols = (width + kColStep - 1) / kColStep;
    for (int y = y0; y < y1; ++y) {
        const uint8_t* row = img + (size_t)y * width;
        long sum = 0;
        for (int x = 0; x < width; x += kColStep) sum += row[x];
        prof[y - y0] = (float)sum / (float)n_cols;
    }
    return prof;
}

// box 平滑（in-place 語意；回傳新 vector）。
std::vector<float> smooth_profile(const std::vector<float>& p) {
    const int n = (int)p.size();
    std::vector<float> out(n);
    const int half = kSmooth / 2;
    for (int i = 0; i < n; ++i) {
        int a = std::max(0, i - half), b = std::min(n - 1, i + half);
        float s = 0;
        for (int j = a; j <= b; ++j) s += p[j];
        out[i] = s / (float)(b - a + 1);
    }
    return out;
}

// 在剖面中找邊緣躍變：|smooth[y+kGap] - smooth[y-kGap]| >= min_contrast 的局部峰。
// first=true → 掃描方向由前往後取第一個峰（前緣）；false → 由後往前取最後一個峰（尾緣）。
// 回傳剖面內 index（-1 = 未找到）。
int find_edge(const std::vector<float>& sm, int min_contrast, bool first) {
    const int n = (int)sm.size();
    if (n < 2 * kGap + 1) return -1;
    auto diff_at = [&](int i) { return std::fabs(sm[i + kGap] - sm[i - kGap]); };

    const int lo = kGap, hi = n - kGap;  // 有效 diff 範圍 [lo, hi)
    if (first) {
        for (int i = lo; i < hi; ++i) {
            if (diff_at(i) < (float)min_contrast) continue;
            // 前進到局部峰（躍變最大處 = 邊緣中心）
            int peak = i;
            while (peak + 1 < hi && diff_at(peak + 1) >= diff_at(peak)) ++peak;
            return peak;
        }
    } else {
        for (int i = hi - 1; i >= lo; --i) {
            if (diff_at(i) < (float)min_contrast) continue;
            int peak = i;
            while (peak - 1 >= lo && diff_at(peak - 1) >= diff_at(peak)) --peak;
            return peak;
        }
    }
    return -1;
}

std::string trim(const std::string& s) {
    size_t a = s.find_first_not_of(" \t\r\n");
    if (a == std::string::npos) return "";
    size_t b = s.find_last_not_of(" \t\r\n");
    return s.substr(a, b - a + 1);
}

}  // namespace

namespace EdgeCheck {

EdgeCheckConfig load_config(const std::string& ini_path) {
    EdgeCheckConfig cfg;
    std::ifstream f(ini_path);
    if (!f.is_open()) return cfg;  // 無 INI → 預設（disabled）

    std::string line, section;
    while (std::getline(f, line)) {
        // 去註解（# 與 ;）再修剪
        size_t c = line.find_first_of("#;");
        if (c != std::string::npos) line = line.substr(0, c);
        line = trim(line);
        if (line.empty()) continue;
        if (line.front() == '[' && line.back() == ']') {
            section = line.substr(1, line.size() - 2);
            continue;
        }
        if (section != "EdgeCheck") continue;
        size_t eq = line.find('=');
        if (eq == std::string::npos) continue;
        const std::string key = trim(line.substr(0, eq));
        const std::string val = trim(line.substr(eq + 1));
        try {
            if      (key == "enabled")               cfg.enabled = (std::stoi(val) != 0);
            else if (key == "frame_lines")           cfg.frame_lines = std::max(1, std::stoi(val));
            else if (key == "min_contrast")          cfg.min_contrast = std::max(1, std::stoi(val));
            else if (key == "expected_leading_line") cfg.expected_leading_line = std::stoi(val);
            else if (key == "leading_tolerance")     cfg.leading_tolerance = std::max(0, std::stoi(val));
            else if (key == "expected_panel_lines")  cfg.expected_panel_lines = std::max(0L, std::stol(val));
            else if (key == "drift_warn_pct")        cfg.drift_warn_pct = std::max(0.0, std::stod(val));
            else if (key == "tail_search_frames")    cfg.tail_search_frames = std::max(1, std::stoi(val));
        } catch (...) { /* 垃圾值 → 保留預設，不 crash */ }
    }
    return cfg;
}

EdgeCheckResult run(const uint8_t* img, int width, int height, const EdgeCheckConfig& cfg) {
    EdgeCheckResult r;
    if (!cfg.enabled || !img || width <= 0 || height <= 0) return r;
    r.checked = true;
    r.expected_lines = cfg.expected_panel_lines;

    // ── 前緣：第一張窗口（[0, frame_lines)）──
    {
        const int y1 = std::min(height, cfg.frame_lines);
        auto sm = smooth_profile(row_profile(img, width, height, 0, y1));
        int idx = find_edge(sm, cfg.min_contrast, /*first=*/true);
        if (idx >= 0) {
            r.leading_found = true;
            r.leading_line = idx;  // 窗從 0 起 → 剖面 index 即全域行號
            if (cfg.expected_leading_line >= 0) {
                r.leading_in_range =
                    std::abs(r.leading_line - cfg.expected_leading_line) <= cfg.leading_tolerance;
            }
        } else {
            r.leading_in_range = false;
        }
    }

    // ── 尾緣：最後 tail_search_frames 張窗口 ──
    {
        const int y0 = std::max(0, height - cfg.tail_search_frames * cfg.frame_lines);
        auto sm = smooth_profile(row_profile(img, width, height, y0, height));
        int idx = find_edge(sm, cfg.min_contrast, /*first=*/false);
        if (idx >= 0) {
            const int global = y0 + idx;
            // 兩窗重疊（短影像）時，最後一個躍變可能就是前緣本身 → 不算找到尾緣
            if (!r.leading_found || global > r.leading_line + kGap) {
                r.tail_found = true;
                r.tail_line = global;
            }
        }
    }

    // ── 量測：前後緣皆有才算 ──
    if (r.leading_found && r.tail_found) {
        r.measured_lines = (long)r.tail_line - (long)r.leading_line;
        if (r.expected_lines > 0) {
            r.drift_pct = 100.0 * ((double)r.measured_lines - (double)r.expected_lines)
                                / (double)r.expected_lines;
        }
    }
    return r;
}

std::string summary(const EdgeCheckResult& r, const EdgeCheckConfig& cfg) {
    if (!r.checked) return "edge_check: off";
    std::ostringstream os;
    os << "前緣=" << (r.leading_found ? std::to_string(r.leading_line) : std::string("未找到"));
    if (r.leading_found && cfg.expected_leading_line >= 0)
        os << (r.leading_in_range ? "(範圍內)" : "(超出預期範圍)");
    os << " 尾緣=" << (r.tail_found ? std::to_string(r.tail_line) : std::string("未找到"));
    if (r.leading_found && r.tail_found) {
        os << " 實測行數=" << r.measured_lines;
        if (r.expected_lines > 0) {
            char buf[64];
            std::snprintf(buf, sizeof(buf), " 理論=%ld drift=%+.3f%%", r.expected_lines, r.drift_pct);
            os << buf;
        }
    }
    return os.str();
}

}  // namespace EdgeCheck
