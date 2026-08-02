#ifndef CONFIG_PARSER_H
#define CONFIG_PARSER_H

// ============================================================================
// ConfigParser — default_zone.ini 解析（✅ 自 Reference/Demo 直接複製，勿改邏輯）
// ----------------------------------------------------------------------------
// 定位：**machine-level 預設值**，只在啟動時讀一次（main.cpp → ZoneConfigAdapter::from_ini
// 產生「單一全幅 base zone」＋ load_optical_params）。recipe（RecipeInfo.xml）載入時以
// 此 base 為底、逐 DetectRoi 覆寫 → **recipe 沒寫的欄位就沿用這裡的 INI 預設**（重要：
// 見下方 enable_multiscale 註）。三個 INI 檔（default_zone / config_real / config_optimized）
// 皆同格式，由 --ini 指定。
//
// INI section → ZoneConfig 欄位對應（實際搬運在 zone_config_adapter.cpp::from_ini）：
//   [Image]       width/height        → z.width/z.height（僅作 buffer 計算器估幀大小的預設；
//                                       真正尺寸每幀由影像/FrameHeader 決定）
//   [Pattern]     pitch_*/search_*    → 同名欄位；fast_search_range → mode0 kernel 局部搜尋
//   [LensShading] enable_lsc/lsc_k*   → 同名欄位（recipe 的 LscEnable/Lsc* 可覆寫）
//   [Threshold]   BTH/DTH             → 同名欄位（recipe 的 Bright/DarkThreshold 可覆寫）
//   [GPU]         block_dim_x/y       → ⚠️ **不搬運**：from_ini 硬寫死 16×16（不變式 15），
//                                       INI 的 32×32 是死設定，改它不影響 GPU block 維度
//   [Optical]     opt_res_x/y,ccd_index → OpticalParams（機器層，不進 ZoneConfig；Gap #5 μm 換算）
//   [Debug]/[Output] → **完全不搬運**：Demo 的合成缺陷注入與 CSV/BMP 輸出在 IP 已移除
//                     （欄位保留只為讓舊 INI 不報錯），改設定不會有任何效果
//   [EdgeCheck]   → 不由本解析器處理，另由 EdgeCheck::load_config 讀（見 edge_check.cpp）
// ============================================================================

#include <string>
#include <map>
#include <fstream>
#include <sstream>
#include <iostream>
#include <algorithm>

class ConfigParser {
public:
    struct Config {
        // Image settings — 幀尺寸預設；實際檢測尺寸每幀由來源決定，這裡只影響啟動期
        // buffer 計算器（FrameQueue/SourceRing 上限）與 RDMA slot 大小估算（main.cpp）。
        // ⚠️ 設得比實際相機幀小 → RDMA slot 不夠大，收端會以 WC error 收場（rdma_source）。
        int width = 8192;
        int height = 5000;
        int bits = 8;                   // 目前僅支援 Mono8；非 8-bit 影像在來源端就被擋掉

        // Pattern settings — 網格週期與搜尋範圍（缺陷判定的幾何前提）
        // ⚠️ pitch 必須精確匹配面板網格週期：偏 1~4px 就會把正常網格當缺陷而爆量
        //    （實測 26→30 → 缺陷 561→10000 觸頂，不變式 14）。新面板務必先 FFT 估算。
        int pitch_x = 26;
        int pitch_y = 19;
        int search_range_x = 3;
        int search_range_y = 3;
        int fast_search_range = 1;  // For fast kernel: 0=none, 1=±1px, 2=±2px
                                    // 線掃相機 skew 容忍（垂直向）；recipe 走 clamp(SearchY,0,2)
        int enable_multiscale = 1;  // 0=off, 1=2x, 2=2x+4x
                                    // ⚠️ 預設 1（沿自 Demo）：多尺度**僅 mode2(DIV-voting) 會執行**
                                    //   （gpu_pipeline.cpp Step 4.5），mode0/mode1 讀了不用。
                                    //   連帶效應：recipe 未寫 <EnableMultiscale> 時沿用此預設 →
                                    //   凡被守門判成 mode2 的 recipe 就**默默開了多尺度**（多一輪
                                    //   downsample+redetect，結果與純單尺度不同）。要關請在 INI 設 0
                                    //   或 recipe 顯式寫 0。（docs/code_review_20260802.md I5 連帶效應）

        // Lens Shading Correction (LSC) settings — 徑向暗角校正 gain(r)=1+k1r²+k2r⁴+k3r⁶
        // 預設 false：**開啟會改變像素值 → 影響缺陷判定**，調參基準線一律先關（保 bit-exact 可比性）。
        bool enable_lsc = false;        // Enable lens vignette correction
        float lsc_k1 = 0.15f;           // Quadratic coefficient
        float lsc_k2 = 0.05f;           // Quartic coefficient
        float lsc_k3 = 0.0f;            // Sextic coefficient
        float lsc_max_gain = 1.5f;      // Maximum gain clamp（防過度放大暗角雜訊）
        bool lsc_auto_calibrate = false; // Auto-calibrate from first image
                                         // ⚠️ 死欄位：IP 無 caller（Demo 的自動校準未接線）

        // Threshold settings — **語意隨演算法域改變**（守門判定，見 zone_config_adapter.h）：
        //   mode0 DIV / mode2 DIV-voting → 比值域（BTH>1>DTH>0，如 1.20/0.70）
        //   mode1 SUB                    → 灰階差域（如 +17/−16，DTH 為負）
        // 這裡的 INI 預設是**比值域**；跑 SUB recipe 時一定會被 recipe 值覆寫，不會混用。
        float BTH = 1.2f;
        float DTH = 0.7f;

        // Debug settings — ⚠️ 全部**死設定**：Demo 用來合成假缺陷做自測，IP 已移除該路徑。
        // 欄位保留只為相容舊 INI（不會因未知鍵報錯）；改這些值對檢測結果零影響。
        bool enable_debug_defects = true;
        int num_defects = 50;
        int defect_size = 4;
        int num_bright_defects = 25;
        int num_dark_defects = 25;
        float bright_multiplier = 1.2f;
        float dark_multiplier = 0.7f;

        // Output settings — ⚠️ 同為**死設定**：IP 輸出走 ResultSaver（ResultInfo.json/.xml + PNG），
        // 不再產生 Demo 的 Result.csv / result.bmp。
        std::string result_csv = "Result.csv";
        std::string result_image = "result.bmp";

        // GPU settings — ⚠️ **死設定**：from_ini 無條件覆寫成 16×16（不變式 15，GB10 實測最佳），
        // INI 寫 32×32 從未被執行。改這裡不會改變 GPU block 維度。
        int block_dim_x = 32;
        int block_dim_y = 32;

        // Optical resolution (machine-level, not per-zone)
        // Gap #5：缺陷 pixel→μm 換算來源（GlobalPos*_um = GlobalPos*_px × opt_res_*）。
        // 機器層屬性，不隨 recipe 換（LOAD_RECIPE 只換 zones，不碰這裡）。
        // 0.0 = 未設定 sentinel → μm 欄位輸出 0.0（不猜、不用預設值硬算）。
        double opt_res_x = 0.0;   // μm/pixel; 0.0 = not set
        double opt_res_y = 0.0;
        int ccd_index = 0;        // 預留多 CCD 拼接，目前恆 0（μm 公式無拼接項）
    };

    // 讀 INI；**檔案不存在不算錯**（印 Warning 後回內建預設值繼續跑）——
    // 理由：INI 只是 machine-level 預設，真正決定檢測行為的是 recipe；缺 INI 不該擋開機。
    // ⚠️ 但這也代表「路徑打錯」會靜默降級成預設值（pitch 26/19、BTH/DTH 1.2/0.7），
    //    調參時務必確認 log 沒有這行 Warning，否則等於在錯的基準上調。
    static Config loadConfig(const std::string& filename) {
        Config config;
        std::ifstream file(filename);

        if (!file.is_open()) {
            std::cerr << "Warning: Could not open config file: " << filename
                      << ". Using default values." << std::endl;
            return config;
        }
        
        std::string line;
        std::string current_section;
        
        while (std::getline(file, line)) {
            // Remove comments and trim
            size_t comment_pos = line.find('#');
            if (comment_pos != std::string::npos) {
                line = line.substr(0, comment_pos);
            }
            line = trim(line);
            
            if (line.empty()) continue;
            
            // Check for section header
            if (line[0] == '[' && line.back() == ']') {
                current_section = line.substr(1, line.length() - 2);
                continue;
            }
            
            // Parse key-value pair
            size_t eq_pos = line.find('=');
            if (eq_pos != std::string::npos) {
                std::string key = trim(line.substr(0, eq_pos));
                std::string value = trim(line.substr(eq_pos + 1));
                
                parseValue(config, current_section, key, value);
            }
        }
        
        return config;
    }

private:
    static std::string trim(const std::string& str) {
        size_t start = str.find_first_not_of(" \t\r\n");
        if (start == std::string::npos) return "";
        size_t end = str.find_last_not_of(" \t\r\n");
        return str.substr(start, end - start + 1);
    }
    
    // 逐鍵派送。⚠️ **未知 section/key 一律靜默忽略**（無 typo 偵測）：
    // 例如把 `pitch_x` 打成 `pitchx`、或寫在錯的 section 下 → 不報錯、值不生效、沿用預設。
    // 調參對不上時第一個要懷疑這點（可對照 main.cpp 啟動印出的實際生效值）。
    // [Optical] 三鍵另有 try-catch + 夾值（垃圾值→0，不 crash）；其餘鍵用裸 stoi/stof，
    // 格式錯會丟例外（由呼叫端 main 的例外路徑處理，不會靜默吃掉）。
    static void parseValue(Config& config, const std::string& section,
                          const std::string& key, const std::string& value) {
        if (section == "Image") {
            if (key == "width") config.width = std::stoi(value);
            else if (key == "height") config.height = std::stoi(value);
            else if (key == "bits") config.bits = std::stoi(value);
        }
        else if (section == "Pattern") {
            if (key == "pitch_x") config.pitch_x = std::stoi(value);
            else if (key == "pitch_y") config.pitch_y = std::stoi(value);
            else if (key == "search_range_x") config.search_range_x = std::stoi(value);
            else if (key == "search_range_y") config.search_range_y = std::stoi(value);
            else if (key == "fast_search_range") config.fast_search_range = std::stoi(value);
            else if (key == "enable_multiscale") config.enable_multiscale = std::stoi(value);
        }
        else if (section == "LensShading") {
            if (key == "enable_lsc") config.enable_lsc = (std::stoi(value) != 0);
            else if (key == "lsc_k1") config.lsc_k1 = std::stof(value);
            else if (key == "lsc_k2") config.lsc_k2 = std::stof(value);
            else if (key == "lsc_k3") config.lsc_k3 = std::stof(value);
            else if (key == "lsc_max_gain") config.lsc_max_gain = std::stof(value);
            else if (key == "lsc_auto_calibrate") config.lsc_auto_calibrate = (std::stoi(value) != 0);
        }
        else if (section == "Threshold") {
            if (key == "BTH") config.BTH = std::stof(value);
            else if (key == "DTH") config.DTH = std::stof(value);
        }
        else if (section == "Debug") {
            if (key == "enable_debug_defects") config.enable_debug_defects = (std::stoi(value) != 0);
            else if (key == "num_defects") config.num_defects = std::stoi(value);
            else if (key == "defect_size") config.defect_size = std::stoi(value);
            else if (key == "num_bright_defects") config.num_bright_defects = std::stoi(value);
            else if (key == "num_dark_defects") config.num_dark_defects = std::stoi(value);
            else if (key == "bright_multiplier") config.bright_multiplier = std::stof(value);
            else if (key == "dark_multiplier") config.dark_multiplier = std::stof(value);
        }
        else if (section == "Output") {
            if (key == "result_csv") config.result_csv = value;
            else if (key == "result_image") config.result_image = value;
        }
        else if (section == "GPU") {
            if (key == "block_dim_x") config.block_dim_x = std::stoi(value);
            else if (key == "block_dim_y") config.block_dim_y = std::stoi(value);
        }
        else if (section == "Optical") {
            if (key == "opt_res_x") {
                try { double v = std::stod(value); config.opt_res_x = (v > 0.0) ? v : 0.0; } catch (...) {}
            }
            else if (key == "opt_res_y") {
                try { double v = std::stod(value); config.opt_res_y = (v > 0.0) ? v : 0.0; } catch (...) {}
            }
            else if (key == "ccd_index") {
                try { int v = std::stoi(value); config.ccd_index = (v >= 0) ? v : 0; } catch (...) {}
            }
        }
    }
};

#endif // CONFIG_PARSER_H
