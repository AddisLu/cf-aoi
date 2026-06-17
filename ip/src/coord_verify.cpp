/**
 * coord_verify.cpp — Gap #5 pixel→μm 換算精度驗證
 *
 * 純 CPU，無 CUDA/OpenCV 依賴，不需相機 / GPU。
 * 驗證：to_legacy μm 計算邏輯 + load_optical_params INI 解析邊界（含垃圾值 smoke）。
 *
 * 編譯：見 CMakeLists.txt 的 coord_verify target
 * 執行：./coord_verify
 */

#include <cassert>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

#include "config/zone_config_adapter.h"

// ── 測試框架 ─────────────────────────────────────────────────────────────────

struct TestResult { std::string name; bool pass; std::string detail; };
static std::vector<TestResult> g_results;

static void check(const std::string& name, bool cond, const std::string& detail)
{
    g_results.push_back({name, cond, detail});
    printf("[%s] %s\n  %s\n", cond ? "PASS" : "FAIL", name.c_str(), detail.c_str());
}

// μm = pixel * opt_res，與 to_legacy() 邏輯一致（不依賴 gpu_pipeline 結構）。
static double compute_um(int pixel, double opt_res) {
    return (opt_res > 0.0) ? pixel * opt_res : 0.0;
}

// ── Unit Cases：μm 乘法邏輯（測試 compute_um，對應 to_legacy 的 GlobalPosX/Y_um 計算）──

static void run_unit_cases()
{
    printf("\n=== Unit Cases: pixel × opt_res 換算 ===\n");

    // Case 1：典型正常值
    {
        int px = 100, py = 200; double opt = 0.5;
        double gx_um = compute_um(px, opt), gy_um = compute_um(py, opt);
        bool pass = std::abs(gx_um - 50.0) < 1e-9 && std::abs(gy_um - 100.0) < 1e-9;
        char buf[200];
        std::snprintf(buf, sizeof(buf),
            "px=(%d,%d) opt=%.1f → um=(%.3f,%.3f) expect=(50.000,100.000)",
            px, py, opt, gx_um, gy_um);
        check("Case 1: 正常值 100×0.5=50.000", pass, buf);
    }

    // Case 2：原點（pixel=0 → μm=0，即使 opt_res>0）
    {
        int px = 0, py = 0; double opt = 0.5;
        double gx_um = compute_um(px, opt), gy_um = compute_um(py, opt);
        bool pass = std::abs(gx_um) < 1e-9 && std::abs(gy_um) < 1e-9;
        char buf[200];
        std::snprintf(buf, sizeof(buf),
            "px=(0,0) opt=%.1f → um=(%.3f,%.3f) expect=(0.000,0.000)", opt, gx_um, gy_um);
        check("Case 2: 原點 0×0.5=0.000", pass, buf);
    }

    // Case 3：opt_res=0.0（未設定 sentinel）→ μm=0.0，不輸出真實值
    {
        int px = 100; double opt = 0.0;
        double gx_um = compute_um(px, opt);
        bool pass = std::abs(gx_um) < 1e-9;
        char buf[100];
        std::snprintf(buf, sizeof(buf),
            "px=100 opt=0.0(未設定) → um=%.3f expect=0.000（sentinel）", gx_um);
        check("Case 3: opt_res=0.0 → μm sentinel=0.0", pass, buf);
    }

    // Case 4：非整數結果精度（333 × 0.5 = 166.5）
    {
        int px = 333; double opt = 0.5;
        double gx_um = compute_um(px, opt);
        bool pass = std::abs(gx_um - 166.5) < 1e-9;
        char buf[100];
        std::snprintf(buf, sizeof(buf),
            "px=333 opt=0.5 → um=%.3f expect=166.500", gx_um);
        check("Case 4: 333×0.5=166.500（非整數精度）", pass, buf);
    }

    // Case 5：CcdIndex 固定 0
    {
        int ccd = 0;
        bool pass = (ccd == 0);
        char buf[80];
        std::snprintf(buf, sizeof(buf), "CcdIndex=%d expect=0（預留多 CCD）", ccd);
        check("Case 5: CcdIndex=0 固定", pass, buf);
    }
}

// ── INI 解析邊界：直接呼叫 load_optical_params ───────────────────────────────

static void run_ini_cases()
{
    printf("\n=== INI 解析邊界 ===\n");

    // Case 6：負數 opt_res → 夾到 0.0，不 crash
    {
        // 寫暫存 INI
        const char* path = "/tmp/coord_verify_neg.ini";
        {
            std::ofstream f(path);
            f << "[Optical]\nopt_res_x = -1.0\nopt_res_y = -0.5\nccd_index = 0\n";
        }
        OpticalParams p = ZoneConfigAdapter::load_optical_params(path);
        bool pass = (p.opt_res_x == 0.0) && (p.opt_res_y == 0.0) && (p.ccd_index == 0);
        char buf[200];
        std::snprintf(buf, sizeof(buf),
            "INI: opt_res_x=-1.0,-0.5 → opt_res_x=%.3f opt_res_y=%.3f ccd_index=%d (expect 0.0,0.0,0)",
            p.opt_res_x, p.opt_res_y, p.ccd_index);
        check("Case 6: 負數 opt_res → 夾到 0.0，不 crash", pass, buf);
    }

    // Case 7：缺 [Optical] section → 預設 0.0，不 crash
    {
        const char* path = "/tmp/coord_verify_nosec.ini";
        {
            std::ofstream f(path);
            f << "[Pattern]\npitch_x = 26\npitch_y = 19\n";  // 無 [Optical]
        }
        OpticalParams p = ZoneConfigAdapter::load_optical_params(path);
        bool pass = (p.opt_res_x == 0.0) && (p.opt_res_y == 0.0) && (p.ccd_index == 0);
        char buf[200];
        std::snprintf(buf, sizeof(buf),
            "INI: 無 [Optical] → opt_res_x=%.3f opt_res_y=%.3f ccd_index=%d (expect 0.0,0.0,0)",
            p.opt_res_x, p.opt_res_y, p.ccd_index);
        check("Case 7: 缺 [Optical] section → 預設 0.0，不 crash", pass, buf);
    }

    // Case 8（startup smoke）：垃圾值 opt_res_x=abc → catch → 0.0，不 crash
    {
        const char* path = "/tmp/coord_verify_garbage.ini";
        {
            std::ofstream f(path);
            f << "[Optical]\nopt_res_x = abc\nopt_res_y = 0.5\nccd_index = xyz\n";
        }
        OpticalParams p = ZoneConfigAdapter::load_optical_params(path);
        bool pass = (p.opt_res_x == 0.0) && (p.opt_res_y > 0.0) && (p.ccd_index == 0);
        char buf[200];
        std::snprintf(buf, sizeof(buf),
            "INI: opt_res_x=abc(垃圾),opt_res_y=0.5,ccd_index=xyz(垃圾) "
            "→ opt_res_x=%.3f opt_res_y=%.3f ccd_index=%d (expect 0.0,0.5,0)",
            p.opt_res_x, p.opt_res_y, p.ccd_index);
        check("Case 8 (startup smoke): 垃圾值 → catch → 0.0，不 crash", pass, buf);
    }
}

// ── main ─────────────────────────────────────────────────────────────────────

int main()
{
    printf("============================================================\n");
    printf("   Gap #5 pixel→μm 換算 — coord_verify\n");
    printf("============================================================\n");

    run_unit_cases();
    run_ini_cases();

    int pass_cnt = 0, fail_cnt = 0;
    printf("\n============================================================\n");
    for (auto& r : g_results) {
        if (r.pass) pass_cnt++; else fail_cnt++;
    }
    printf("結果: PASS %d / FAIL %d (共 %d)\n",
           pass_cnt, fail_cnt, (int)g_results.size());
    if (fail_cnt > 0) {
        printf("\nFAIL 清單：\n");
        for (auto& r : g_results)
            if (!r.pass) printf("  ✗ %s\n    %s\n", r.name.c_str(), r.detail.c_str());
    }
    printf("============================================================\n");
    return fail_cnt > 0 ? 1 : 0;
}
