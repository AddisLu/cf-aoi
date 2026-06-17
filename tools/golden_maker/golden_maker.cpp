/**
 * golden_maker — CF-AOI Golden Pattern 製作工具
 *
 * 使用（GUI 模式，需桌面）：
 *   ./golden_maker --image <frame.png> [--output mark.png] [--search-margin 3]
 *
 * 使用（CLI fallback，SSH server / headless）：
 *   ./golden_maker --image <frame.png> --mark-rect x,y,w,h [--output mark.png] [--search-margin 3]
 *   ex: ./golden_maker --image frame.png --mark-rect 1170,770,60,60
 *
 * GUI 操作：
 *   滑鼠拖拽 → 框選 Mark 區域
 *   S         → 存 8-bit 灰階 PNG + 印 AlignRoi XML 到 stdout
 *   R         → 重設框選
 *   Q / ESC   → 離開
 *
 * stdout（貼入 RecipeInfo.xml <M_AlignRoi>）：
 *   <PatternPath>mark.png</PatternPath>
 *   <ReferX>1200</ReferX>
 *   ...
 */

#include <opencv2/core.hpp>
#include <opencv2/highgui.hpp>
#include <opencv2/imgcodecs.hpp>
#include <opencv2/imgproc.hpp>

#include <cstdio>
#include <cstring>
#include <iostream>
#include <string>

// ─── 全域狀態（mouse callback 用）───────────────────────────────────────────
static cv::Mat g_disp;           // 顯示用（彩色）
static cv::Mat g_gray;           // 原始灰階影像
static cv::Point g_pt0, g_pt1;  // 拖拽起點/終點
static bool g_dragging = false;
static bool g_has_sel  = false;

static void draw_overlay() {
    g_gray.copyTo(g_disp);
    cv::cvtColor(g_disp, g_disp, cv::COLOR_GRAY2BGR);
    if (g_has_sel) {
        cv::Rect r(g_pt0, g_pt1);
        r = r & cv::Rect(0, 0, g_gray.cols, g_gray.rows);
        cv::rectangle(g_disp, r, cv::Scalar(0, 255, 0), 2);
        char buf[64];
        std::snprintf(buf, sizeof(buf), "(%d,%d) %dx%d",
                      r.x, r.y, r.width, r.height);
        cv::putText(g_disp, buf, cv::Point(r.x, std::max(r.y - 6, 12)),
                    cv::FONT_HERSHEY_SIMPLEX, 0.55, cv::Scalar(0, 255, 0), 1);
    }
    cv::imshow("golden_maker", g_disp);
}

static void on_mouse(int event, int x, int y, int /*flags*/, void* /*userdata*/) {
    if (event == cv::EVENT_LBUTTONDOWN) {
        g_pt0 = g_pt1 = cv::Point(x, y);
        g_dragging = true;
        g_has_sel  = false;
    } else if (event == cv::EVENT_MOUSEMOVE && g_dragging) {
        g_pt1 = cv::Point(x, y);
        g_has_sel = true;
        draw_overlay();
    } else if (event == cv::EVENT_LBUTTONUP && g_dragging) {
        g_pt1 = cv::Point(x, y);
        g_dragging = false;
        g_has_sel  = true;
        draw_overlay();
    }
}

// ─── 引數解析 ────────────────────────────────────────────────────────────────
static std::string get_arg(int argc, char** argv, const char* flag, const char* def = "") {
    for (int i = 1; i + 1 < argc; ++i)
        if (std::strcmp(argv[i], flag) == 0) return argv[i + 1];
    return def;
}
static bool has_flag(int argc, char** argv, const char* flag) {
    for (int i = 1; i < argc; ++i)
        if (std::strcmp(argv[i], flag) == 0) return true;
    return false;
}

// ─── AlignRoi XML 輸出（GUI/CLI 共用）──────────────────────────────────────
static void print_align_roi_xml(const std::string& output_path,
                                int orig_x, int orig_y, int orig_w, int orig_h,
                                int search_margin)
{
    int refer_x = orig_x + orig_w / 2;
    int refer_y = orig_y + orig_h / 2;
    int sw      = orig_w * search_margin;
    int sh      = orig_h * search_margin;
    std::printf("<!-- 貼入 RecipeInfo.xml <M_AlignRoi> -->\n");
    std::printf("<PatternPath>%s</PatternPath>\n",   output_path.c_str());
    std::printf("<ReferX>%d</ReferX>\n",             refer_x);
    std::printf("<ReferY>%d</ReferY>\n",             refer_y);
    std::printf("<SearchWidth>%d</SearchWidth>\n",   sw);
    std::printf("<SearchHeight>%d</SearchHeight>\n", sh);
    std::fflush(stdout);
}

// ─── main ────────────────────────────────────────────────────────────────────
int main(int argc, char** argv) {
    if (argc < 2 || has_flag(argc, argv, "--help") || has_flag(argc, argv, "-h")) {
        std::cerr <<
            "用法 (GUI):  golden_maker --image <frame.png> [--output mark.png] [--search-margin 3]\n"
            "用法 (CLI):  golden_maker --image <frame.png> --mark-rect x,y,w,h [--output mark.png] [--search-margin 3]\n"
            "\n"
            "  --image <path>         來源影像（任何 OpenCV 支援格式）\n"
            "  --output <path>        輸出 Golden PNG（預設 mark.png）\n"
            "  --mark-rect x,y,w,h    CLI fallback（無桌面/SSH server 用）：直接指定 Mark 矩形\n"
            "  --search-margin <N>    SearchWidth/Height = Mark尺寸 × N（預設 3）\n";
        return 1;
    }

    std::string image_path   = get_arg(argc, argv, "--image");
    std::string output_path  = get_arg(argc, argv, "--output", "mark.png");
    std::string mark_rect_s  = get_arg(argc, argv, "--mark-rect");
    int search_margin = std::atoi(get_arg(argc, argv, "--search-margin", "3").c_str());
    if (search_margin < 1) search_margin = 1;

    if (image_path.empty()) {
        std::cerr << "錯誤：請指定 --image <path>\n";
        return 1;
    }

    cv::Mat gray_full = cv::imread(image_path, cv::IMREAD_GRAYSCALE);
    if (gray_full.empty()) {
        std::cerr << "無法讀取影像：" << image_path << "\n";
        return 1;
    }

    // ── CLI fallback：--mark-rect x,y,w,h ─────────────────────────────────
    if (!mark_rect_s.empty()) {
        int x = 0, y = 0, w = 0, h = 0;
        if (std::sscanf(mark_rect_s.c_str(), "%d,%d,%d,%d", &x, &y, &w, &h) != 4
            || w <= 0 || h <= 0) {
            std::cerr << "錯誤：--mark-rect 格式應為 x,y,w,h（整數）\n";
            return 1;
        }
        cv::Rect rect(x, y, w, h);
        rect &= cv::Rect(0, 0, gray_full.cols, gray_full.rows);
        if (rect.width < 4 || rect.height < 4) {
            std::cerr << "矩形越界或太小（< 4×4 px）\n";
            return 1;
        }
        cv::Mat crop = gray_full(rect).clone();
        if (!cv::imwrite(output_path, crop)) {
            std::cerr << "存檔失敗：" << output_path << "\n";
            return 1;
        }
        std::cerr << "已存 " << output_path
                  << " (" << crop.cols << "×" << crop.rows << " px)\n";
        print_align_roi_xml(output_path, x, y, w, h, search_margin);
        return 0;
    }

    // ── GUI 模式 ────────────────────────────────────────────────────────────
    cv::Mat disp_src = gray_full;
    double scale = 1.0;
    const int MAX_DISP = 2000;
    if (gray_full.cols > MAX_DISP || gray_full.rows > MAX_DISP) {
        scale = static_cast<double>(MAX_DISP) / std::max(gray_full.cols, gray_full.rows);
        cv::resize(gray_full, disp_src, cv::Size(), scale, scale, cv::INTER_AREA);
    }
    g_gray = disp_src;
    double inv_scale = 1.0 / scale;

    cv::namedWindow("golden_maker", cv::WINDOW_AUTOSIZE);
    cv::setMouseCallback("golden_maker", on_mouse);
    draw_overlay();

    std::cerr << "影像尺寸：" << gray_full.cols << "×" << gray_full.rows
              << (scale < 1.0 ? " (縮放顯示)" : "") << "\n"
              << "操作：拖拽框選 → S 存檔，R 重設，Q/ESC 離開\n";

    while (true) {
        int key = cv::waitKey(30) & 0xFF;
        if (key == 'q' || key == 27) break;
        else if (key == 'r' || key == 'R') {
            g_has_sel = false; draw_overlay();
            std::cerr << "已重設框選\n";
        } else if (key == 's' || key == 'S') {
            if (!g_has_sel) { std::cerr << "尚未框選任何區域\n"; continue; }
            cv::Rect sel(g_pt0, g_pt1);
            sel &= cv::Rect(0, 0, g_gray.cols, g_gray.rows);
            if (sel.width < 4 || sel.height < 4) {
                std::cerr << "框選區域太小（< 4×4 px），請重新框選\n"; continue;
            }
            cv::Mat crop = g_gray(sel).clone();
            if (!cv::imwrite(output_path, crop)) {
                std::cerr << "存檔失敗：" << output_path << "\n"; continue;
            }
            int orig_x = (int)(sel.x      * inv_scale);
            int orig_y = (int)(sel.y      * inv_scale);
            int orig_w = (int)(sel.width  * inv_scale);
            int orig_h = (int)(sel.height * inv_scale);
            print_align_roi_xml(output_path, orig_x, orig_y, orig_w, orig_h, search_margin);
            std::cerr << "已存 " << output_path
                      << " (" << crop.cols << "×" << crop.rows << " px)\n"
                      << "AlignRoi XML 已印到 stdout\n";
        }
    }
    cv::destroyAllWindows();
    return 0;
}
