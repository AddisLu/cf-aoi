#include "file_source.h"

#include <algorithm>
#include <filesystem>
#include <iostream>

#include <opencv2/opencv.hpp>

namespace fs = std::filesystem;

namespace {
bool is_image_ext(std::string ext) {
    std::transform(ext.begin(), ext.end(), ext.begin(), ::tolower);
    return ext == ".tif" || ext == ".tiff" || ext == ".png" ||
           ext == ".bmp" || ext == ".jpg" || ext == ".jpeg";
}
}  // namespace

FileImageSource::FileImageSource(const std::string& path) {
    std::error_code ec;
    if (fs::is_directory(path, ec)) {
        for (const auto& e : fs::directory_iterator(path)) {
            if (e.is_regular_file() && is_image_ext(e.path().extension().string()))
                files_.push_back(e.path().string());
        }
        std::sort(files_.begin(), files_.end());
    } else if (fs::is_regular_file(path, ec)) {
        files_.push_back(path);
    } else {
        std::cerr << "[FileSource] 路徑不存在: " << path << "\n";
    }
    std::cout << "[FileSource] 找到 " << files_.size() << " 個影像檔\n";
}

bool FileImageSource::next_frame(FrameHeader& hdr, std::vector<uint8_t>& payload) {
    while (index_ < files_.size()) {
        const std::string& fpath = files_[index_++];

        // ★ 不變式：IMREAD_UNCHANGED，禁止後處理
        cv::Mat img = cv::imread(fpath, cv::IMREAD_UNCHANGED);
        if (img.empty()) {
            std::cerr << "[FileSource] 讀取失敗，略過: " << fpath << "\n";
            continue;
        }

        // 檢測需要單通道 8-bit grayscale。若來源已是單通道則零拷貝沿用；
        // 多通道時取第一通道（不做色彩混合，保持決定性）。
        cv::Mat gray;
        if (img.channels() == 1) {
            gray = img;
        } else {
            std::vector<cv::Mat> ch;
            cv::split(img, ch);
            gray = ch[0];
        }
        if (gray.depth() != CV_8U) {
            std::cerr << "[FileSource] 非 8-bit 影像，略過: " << fpath << "\n";
            continue;
        }

        const int w = gray.cols, h = gray.rows;
        payload.resize((size_t)w * h);
        if (gray.isContinuous()) {
            std::copy(gray.data, gray.data + payload.size(), payload.begin());
        } else {
            for (int r = 0; r < h; ++r)
                std::copy(gray.ptr(r), gray.ptr(r) + w, payload.begin() + (size_t)r * w);
        }

        current_name_ = fs::path(fpath).stem().string();
        hdr = make_frame_header(current_name_, /*cam_id*/ 0, seq_++,
                                (uint32_t)w, (uint32_t)h,
                                payload.data(), (uint32_t)payload.size());
        return true;
    }
    return false;  // 全部讀完
}
