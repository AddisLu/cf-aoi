#ifndef CFAOI_OVERLAY_WRITER_H
#define CFAOI_OVERLAY_WRITER_H

/**
 * ============================================================================
 * OverlayWriter — overlay 全圖背景寫檔（rdma-process 生產路徑）
 * ============================================================================
 *
 * 問題（2026-10-05 Spark 實測）：overlay = 8192×5000 BGR PNG，**1.05s/張**（PNG 壓縮），
 * 原本在檢測主迴圈同步寫 → 任何一張有缺陷就讓整條管線停 1 秒 → 背壓經 RDMA 一路堵到
 * Grab，37 台一起等。估算一片 1110 張中 >~5 張有缺陷就會掉幀。
 *
 * 作法：
 *   - 主迴圈只做「交出 buffer」：payload vector 以 move 交給本類別（**零拷貝**，只換指標；
 *     DGX Spark 是統一記憶體，影像本來就在 CPU/GPU 共用的同一塊 DRAM，不需要也沒有
 *     「另一塊記憶體」可搬）。
 *   - 背景 N 條低優先權（nice 10）thread 畫框 + 寫 PNG，寫完 buffer 交還 FrameQueue 池。
 *   - **有上限、會丟**：in-flight 已滿 → try_submit 回 false、不動呼叫端的 buffer，
 *     該張 overlay 略過（缺陷清單 ResultInfo + 缺陷小圖照常完整寫出，檢測資訊不少）。
 *     寧可少一張給人看的疊圖，也不能讓檢測管線塞車掉幀。
 *   - 記憶體上限 = max_inflight × 一幀（預設 6 × 41MB ≈ 246MB）。
 * ============================================================================
 */

#include "result_saver.h"

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

class OverlayWriter {
public:
    using ReturnBuf = std::function<void(std::vector<uint8_t>&&)>;

    struct Stats {
        uint64_t submitted = 0;    // 交給背景寫的張數
        uint64_t written   = 0;    // 寫成功
        uint64_t failed    = 0;    // imwrite 失敗
        uint64_t dropped   = 0;    // 忙碌略過（in-flight 已滿）
        unsigned inflight  = 0;    // 目前在手上（排隊 + 寫入中）
        unsigned peak      = 0;    // in-flight 峰值
        double   avg_ms    = 0.0;  // 每張寫檔平均耗時
        double   max_ms    = 0.0;
    };

    // threads：背景寫檔緒數（≥1）；max_inflight：同時持有的幀數上限；
    // ret：寫完交還 buffer（通常 = FrameQueue::recycle）。
    OverlayWriter(unsigned threads, unsigned max_inflight, ReturnBuf ret);
    ~OverlayWriter();

    // 有空位 → 接走 img（move，呼叫端 img 變空）並回 true；
    // 已滿或已停止 → 不動 img、計入 dropped、回 false。
    bool try_submit(std::string path, std::vector<uint8_t>& img, int w, int h,
                    std::vector<ResultSaver::OverlayBox> boxes);

    // 寫完排隊中的再結束（冪等）。
    void stop();

    Stats stats() const;

private:
    struct Job {
        std::string path;
        std::vector<uint8_t> img;
        int w = 0, h = 0;
        std::vector<ResultSaver::OverlayBox> boxes;
    };
    void worker();

    const unsigned max_inflight_;
    ReturnBuf ret_;
    mutable std::mutex mtx_;
    std::condition_variable cv_;
    std::deque<Job> q_;
    bool stopping_ = false;
    unsigned inflight_ = 0;
    Stats st_;
    double sum_ms_ = 0.0;
    std::vector<std::thread> pool_;
};

#endif // CFAOI_OVERLAY_WRITER_H
