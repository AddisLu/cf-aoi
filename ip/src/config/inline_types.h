#ifndef INLINE_TYPES_H
#define INLINE_TYPES_H

/**
 * ============================================================================
 * LCD CF AOI 系統 - 共用型別定義
 * ============================================================================
 * 
 * 定義整個 in-line 系統使用的核心資料結構。
 * 所有模組 (Rivermax 接收器、排程器、控制器、GUI) 共用這些型別。
 * 
 * 硬體架構：
 *   34 隻 L803K Line Scan CCD
 *   → iPort CL-GigE (Camera Link to GigE 轉換)
 *   → NVIDIA SN2200 交換器 (100GbE)
 *   → 多台 NVIDIA Spark (GB10, ConnectX-7)
 * ============================================================================
 */

#include <string>
#include <vector>
#include <cstdint>
#include <atomic>
#include <chrono>
#include <mutex>
#include <functional>

// ============================================================================
// 系統常數
// ============================================================================

constexpr int MAX_CCD_COUNT      = 34;     // 最大 CCD 數量
constexpr int MAX_SPARK_NODES    = 8;      // 最大 Spark 節點數
constexpr int DEFAULT_FRAME_LINES = 5000;  // 每幀行數（line scan 5000 條）
constexpr int DEFAULT_TACT_TIME_SEC = 35;  // 標準 Tact Time (秒)
constexpr int MAX_DEFECTS_PER_IMAGE = 10000;
constexpr int DEFAULT_PATCH_SIZE = 100;

// ============================================================================
// 系統運行模式
// ============================================================================

enum class SystemMode {
    INLINE,     // In-line 即時跑貨模式
    OFFLINE,    // Off-line 離線檔案檢測模式
    SETUP       // 設定模式 (GUI 參數調整)
};

inline const char* systemModeToString(SystemMode mode) {
    switch (mode) {
        case SystemMode::INLINE:  return "IN-LINE";
        case SystemMode::OFFLINE: return "OFF-LINE";
        case SystemMode::SETUP:   return "SETUP";
        default: return "UNKNOWN";
    }
}

// ============================================================================
// CCD 參數結構
// ============================================================================

struct CCDParams {
    int     id = 0;                     // CCD 編號 (0~33)
    std::string name = "CCD-XX";        // CCD 名稱
    std::string description;            // 描述
    bool    enabled = true;             // 是否啟用
    
    // === 影像參數 ===
    int     image_width = 8192;         // 影像寬度（像素）
    int     image_height = 5000;        // 影像高度（行數 = lines_per_frame）
    
    // === ROI 參數 ===
    int     roi_x = 0;                  // ROI 起始 X（0=不裁切）
    int     roi_y = 0;                  // ROI 起始 Y
    int     roi_width = 0;              // ROI 寬度（0=使用整張影像）
    int     roi_height = 0;             // ROI 高度
    
    // === 檢測參數 ===
    int     pitch_x = 26;              // CF Pattern X 方向週期
    int     pitch_y = 19;              // CF Pattern Y 方向週期
    float   BTH = 1.20f;              // 亮點閾值 (Bright Threshold)
    float   DTH = 0.70f;              // 暗點閾值 (Dark Threshold)
    int     fast_search_range = 1;     // 搜尋範圍 (0/1/2)
    int     enable_multiscale = 1;     // 多尺度檢測 (0=off, 1=2x, 2=2x+4x)
    
    // === LSC 參數 ===
    bool    enable_lsc = false;        // Lens Shading Correction
    float   lsc_k1 = 0.15f;
    float   lsc_k2 = 0.05f;
    float   lsc_k3 = 0.00f;
    float   lsc_max_gain = 1.5f;
    
    // === GPU 參數 ===
    int     block_dim_x = 16;
    int     block_dim_y = 16;
    
    // === CCD 硬體參數 ===
    float   exposure_us = 50.0f;       // 曝光時間 (微秒)
    float   gain_db = 0.0f;            // 增益 (dB)
    int     line_rate_hz = 142857;     // 行頻 (Hz)，5000行/35s ≈ 142857
    
    // 取得有效 ROI（0 表示使用整張影像）
    int getEffectiveWidth() const { return roi_width > 0 ? roi_width : image_width; }
    int getEffectiveHeight() const { return roi_height > 0 ? roi_height : image_height; }
};

// ============================================================================
// Spark 節點資訊
// ============================================================================

struct SparkNodeInfo {
    std::string id = "spark-01";        // 節點 ID
    std::string ip = "192.168.1.101";   // IP 位址
    int         gpu_id = 0;             // GPU 裝置編號
    bool        enabled = true;         // 是否啟用
    std::vector<int> ccd_assignments;   // 分配的 CCD 編號
    
    // === 運行狀態 ===
    bool    connected = false;          // 連線狀態
    float   avg_process_time_ms = 0;    // 平均處理時間
    int     images_processed = 0;       // 已處理影像數
    int     total_defects = 0;          // 累計缺陷數
    float   gpu_utilization = 0;        // GPU 使用率 (%)
    float   gpu_temperature = 0;        // GPU 溫度 (°C)
};

// ============================================================================
// 網路參數
// ============================================================================

struct NetworkConfig {
    bool        rivermax_enabled = true;
    std::string switch_ip = "192.168.1.1";
    std::string switch_model = "SN2200";
    int         mtu = 9000;             // Jumbo Frame
    int         ptp_domain = 0;         // PTP 時間同步域
    std::string multicast_group = "239.1.1.1";
    int         base_port = 50000;      // 基礎端口號（每隻 CCD 遞增）
    std::vector<std::string> nic_interfaces;  // ConnectX-7 NIC 介面名稱
};

// ============================================================================
// Frame 資料結構 — 從 CCD 接收的影像幀
// ============================================================================

struct FrameData {
    int         ccd_id = -1;            // 來源 CCD 編號
    uint64_t    frame_number = 0;       // 幀序號
    uint64_t    timestamp_ns = 0;       // PTP 時間戳 (奈秒)
    int         width = 0;              // 影像寬度
    int         height = 0;             // 影像高度
    std::vector<uint8_t> data;          // 影像資料 (grayscale 8-bit)
    std::string panel_id;               // 面板 ID（由 MES 傳入或自動編號）
    
    bool isValid() const { return ccd_id >= 0 && !data.empty() && width > 0 && height > 0; }
    size_t dataSize() const { return (size_t)width * height; }
};

// ============================================================================
// 檢測結果結構
// ============================================================================

struct InspectionResult {
    // 來源資訊
    int         ccd_id = -1;
    uint64_t    frame_number = 0;
    std::string panel_id;
    std::string spark_node_id;          // 處理此影像的 Spark 節點
    
    // 檢測結果
    int         num_defects = 0;
    int         num_bright = 0;         // 亮點缺陷數
    int         num_dark = 0;           // 暗點缺陷數
    bool        pass = true;            // OK/NG 判定
    
    // 計時
    double      upload_ms = 0;
    double      detection_ms = 0;       // 8-way + multiscale + CCL + blob
    double      ai_filter_ms = 0;
    double      total_ms = 0;
    
    // 時間戳
    uint64_t    start_timestamp_ns = 0;
    uint64_t    end_timestamp_ns = 0;
    
    // 影像尺寸
    int         image_width = 0;
    int         image_height = 0;
};

// ============================================================================
// 系統狀態（統計）
// ============================================================================

struct SystemStats {
    // 全域統計
    std::atomic<int>      panels_inspected{0};
    std::atomic<int>      total_defects{0};
    std::atomic<int>      panels_pass{0};
    std::atomic<int>      panels_ng{0};
    std::atomic<int>      frames_received{0};
    std::atomic<int>      frames_processed{0};
    std::atomic<int>      frames_dropped{0};
    
    // 計時統計
    std::atomic<double>   avg_process_ms{0};
    std::atomic<double>   max_process_ms{0};
    std::atomic<double>   min_process_ms{999999};
    
    // 系統狀態
    SystemMode            current_mode = SystemMode::OFFLINE;
    bool                  is_running = false;
    std::string           last_error;
    
    void reset() {
        panels_inspected = 0;
        total_defects = 0;
        panels_pass = 0;
        panels_ng = 0;
        frames_received = 0;
        frames_processed = 0;
        frames_dropped = 0;
        avg_process_ms = 0;
        max_process_ms = 0;
        min_process_ms = 999999;
        last_error.clear();
    }
};

// ============================================================================
// 回呼函式型別
// ============================================================================

// 收到影像幀時的回呼
using FrameCallback = std::function<void(const FrameData& frame)>;

// 檢測完成時的回呼
using ResultCallback = std::function<void(const InspectionResult& result)>;

// 狀態更新回呼
using StatusCallback = std::function<void(const std::string& message, int level)>;

#endif // INLINE_TYPES_H
