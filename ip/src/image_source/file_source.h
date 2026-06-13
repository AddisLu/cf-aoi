#ifndef CFAOI_FILE_SOURCE_H
#define CFAOI_FILE_SOURCE_H

/**
 * ============================================================================
 * FileImageSource — offline-file（Step 1 批次離線檢測）
 * ============================================================================
 *
 * 從單一檔案或目錄載入 tif/bmp/png。
 * 不變式：cv::IMREAD_UNCHANGED，禁止任何後處理 / 色彩轉換。
 *   - 若影像為多通道，僅在最後一步取單通道（檢測需 grayscale），
 *     但載入本身保持 UNCHANGED 以符合「同影像兩次結果 bit-exact」。
 * ============================================================================
 */

#include <string>
#include <vector>
#include "image_source.h"

class FileImageSource : public IImageSource {
public:
    // path 可為單一影像檔或目錄（目錄則依檔名排序逐張輸出）。
    explicit FileImageSource(const std::string& path);

    bool next_frame(FrameHeader& hdr, std::vector<uint8_t>& payload) override;

    size_t total() const { return files_.size(); }
    // 目前已輸出影像對應的原始檔名（供 result_saver 命名用）。
    const std::string& current_name() const { return current_name_; }

private:
    std::vector<std::string> files_;
    size_t index_ = 0;
    uint16_t seq_ = 0;
    std::string current_name_;
};

#endif // CFAOI_FILE_SOURCE_H
