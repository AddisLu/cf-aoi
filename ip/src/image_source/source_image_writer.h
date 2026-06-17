#ifndef CFAOI_SOURCE_IMAGE_WRITER_H
#define CFAOI_SOURCE_IMAGE_WRITER_H

/**
 * ============================================================================
 * SourceImageWriter — 原始影像非同步存檔（SaveSourceImage 功能）
 * ============================================================================
 *
 * 舊版教訓（Reference/legacy_win/CamProc.cs）：
 *   每幀在 loop 內配一個 MIL buffer → 全部囤在 List 直到偵測完 → 同步 MbufSave → OOM + 阻塞。
 *
 * 新版設計：
 *   ① 固定 N_src 個 ring slot（啟動時一次配置，不動態增大）
 *   ② 獨立 writer thread，條件變數等待，FIFO 消費 → 非同步寫磁碟
 *   ③ ring 滿 → drop + WARN + flight_recorder incident，絕不阻塞主路徑
 *   ④ 格式預設 raw (.bin，width*height bytes Mono8)；比 PNG 快 5-10×
 *
 * 使用：
 *   SourceImageWriter writer;
 *   writer.init(N_src, output_dir);      // 啟動時（計算器決定 N_src）
 *   writer.submit(panel_id, w, h, payload);  // 主路徑（move，不 copy）
 *   writer.stop();                           // 程式結束前
 * ============================================================================
 */

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "../diag/flight_recorder.h"

class SourceImageWriter {
public:
    SourceImageWriter() = default;
    ~SourceImageWriter() { stop(); }

    // 啟動：配置 n_slots 個 ring slot（每個 frame_bytes 大小）+ 啟動 writer thread。
    // frame_bytes = width * height；dir = 輸出目錄（與 ResultSaver 同一個 output 目錄下 source/ 子夾）。
    // test_write_delay_ms：壓力測試用，寫完後人工延遲 N ms（模擬慢 HDD/NAS，觸發 drop WARN）。
    // 呼叫必須在 buffer 計算器之後、第一張影像之前（啟動時一次）。
    void init(size_t n_slots, const std::string& dir, size_t frame_bytes,
              int test_write_delay_ms = 0) {
        if (running_.load()) return;  // 避免重複 init
        out_dir_ = dir + "/source";
        std::filesystem::create_directories(out_dir_);
        frame_bytes_ = frame_bytes;
        // 一次配置所有 ring slot（固定上限，啟動後不可增大）
        slots_.resize(n_slots);
        for (auto& s : slots_) s.data.resize(frame_bytes);
        n_slots_ = n_slots;
        test_write_delay_ms_ = test_write_delay_ms;
        running_ = true;
        thread_ = std::thread([this] { run(); });
        std::cout << "[SourceWriter] 初始化 " << n_slots << " 個 ring slot "
                  << "（各 " << frame_bytes / 1024 / 1024 << " MB），輸出=" << out_dir_ << "\n";
    }

    // 提交一幀（主路徑，payload 以 move 轉入，不 copy）。
    // ring 滿 → drop + WARN（不阻塞）。
    void submit(const std::string& panel_id, uint32_t width, uint32_t height,
                std::vector<uint8_t> payload) {
        if (!running_.load()) return;
        std::lock_guard<std::mutex> lk(mtx_);
        size_t next_head = (head_ + 1) % n_slots_;
        if (next_head == tail_) {
            // ring 滿：drop + incident
            std::cout << "[SourceWriter] WARN ring 滿（" << n_slots_
                      << " 槽），drop panel=" << panel_id << "\n";
            diag::FlightRecorder::instance().record_incident("source_ring_full",
                "panel=" + panel_id + " slots=" + std::to_string(n_slots_));
            drop_count_++;
            return;
        }
        auto& slot = slots_[head_];
        slot.panel_id = panel_id;
        slot.width    = width;
        slot.height   = height;
        // 若 payload 大小與預配 slot 相同，move 進去；否則 resize + copy（防呆）
        if (payload.size() == frame_bytes_) {
            slot.data = std::move(payload);
        } else {
            slot.data.resize(payload.size());
            std::memcpy(slot.data.data(), payload.data(), payload.size());
        }
        head_ = next_head;
        cv_.notify_one();
    }

    void stop() {
        if (!running_.exchange(false)) return;
        cv_.notify_all();
        if (thread_.joinable()) thread_.join();
        if (drop_count_ > 0)
            std::cout << "[SourceWriter] 停止，共 drop " << drop_count_.load() << " 幀\n";
    }

    bool enabled() const { return running_.load(); }

private:
    struct Slot {
        std::string         panel_id;
        uint32_t            width = 0, height = 0;
        std::vector<uint8_t> data;
    };

    void run() {
        while (true) {
            std::unique_lock<std::mutex> lk(mtx_);
            cv_.wait(lk, [this] { return !running_.load() || head_ != tail_; });
            if (!running_.load() && head_ == tail_) break;
            // 取出 tail slot（FIFO）
            Slot& slot = slots_[tail_];
            std::string panel = slot.panel_id;
            uint32_t w = slot.width, h = slot.height;
            // swap data out（避免持鎖做 I/O）
            std::vector<uint8_t> buf;
            buf.swap(slot.data);
            tail_ = (tail_ + 1) % n_slots_;
            lk.unlock();

            // 寫磁碟：{out_dir_}/{panel}_source.bin（raw Mono8）
            std::string path = out_dir_ + "/" + panel + "_source.bin";
            std::ofstream f(path, std::ios::binary | std::ios::trunc);
            if (f) {
                f.write(reinterpret_cast<const char*>(buf.data()), (std::streamsize)buf.size());
            } else {
                std::cerr << "[SourceWriter] 寫檔失敗: " << path << "\n";
            }
            if (test_write_delay_ms_ > 0)
                std::this_thread::sleep_for(std::chrono::milliseconds(test_write_delay_ms_));
        }
    }

    std::vector<Slot>          slots_;
    size_t                     n_slots_     = 0;
    size_t                     frame_bytes_ = 0;
    int                        test_write_delay_ms_ = 0;
    size_t                     head_        = 0;  // producer 寫的位置
    size_t                     tail_        = 0;  // consumer 讀的位置
    std::mutex                 mtx_;
    std::condition_variable    cv_;
    std::thread                thread_;
    std::atomic<bool>          running_{false};
    std::string                out_dir_;
    std::atomic<uint64_t>      drop_count_{0};
};

#endif // CFAOI_SOURCE_IMAGE_WRITER_H
