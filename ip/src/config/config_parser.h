#ifndef CONFIG_PARSER_H
#define CONFIG_PARSER_H

#include <string>
#include <map>
#include <fstream>
#include <sstream>
#include <iostream>
#include <algorithm>

class ConfigParser {
public:
    struct Config {
        // Image settings
        int width = 8192;
        int height = 5000;
        int bits = 8;
        
        // Pattern settings
        int pitch_x = 26;
        int pitch_y = 19;
        int search_range_x = 3;
        int search_range_y = 3;
        int fast_search_range = 1;  // For fast kernel: 0=none, 1=±1px, 2=±2px
        int enable_multiscale = 1;  // 0=off, 1=2x, 2=2x+4x
        
        // Lens Shading Correction (LSC) settings
        bool enable_lsc = false;        // Enable lens vignette correction
        float lsc_k1 = 0.15f;           // Quadratic coefficient
        float lsc_k2 = 0.05f;           // Quartic coefficient
        float lsc_k3 = 0.0f;            // Sextic coefficient
        float lsc_max_gain = 1.5f;      // Maximum gain clamp
        bool lsc_auto_calibrate = false; // Auto-calibrate from first image
        
        // Threshold settings
        float BTH = 1.2f;
        float DTH = 0.7f;
        
        // Debug settings
        bool enable_debug_defects = true;
        int num_defects = 50;
        int defect_size = 4;
        int num_bright_defects = 25;
        int num_dark_defects = 25;
        float bright_multiplier = 1.2f;
        float dark_multiplier = 0.7f;
        
        // Output settings
        std::string result_csv = "Result.csv";
        std::string result_image = "result.bmp";
        
        // GPU settings
        int block_dim_x = 32;
        int block_dim_y = 32;

        // Optical resolution (machine-level, not per-zone)
        double opt_res_x = 0.0;   // μm/pixel; 0.0 = not set
        double opt_res_y = 0.0;
        int ccd_index = 0;
    };

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
