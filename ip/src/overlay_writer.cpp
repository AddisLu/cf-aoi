#include "overlay_writer.h"

#include <chrono>
#include <cstdio>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <unistd.h>

OverlayWriter::OverlayWriter(unsigned threads, unsigned max_inflight, ReturnBuf ret)
    : max_inflight_(max_inflight > 0 ? max_inflight : 1), ret_(std::move(ret)) {
    if (threads == 0) threads = 1;
    for (unsigned i = 0; i < threads; ++i) pool_.emplace_back(&OverlayWriter::worker, this);
}

OverlayWriter::~OverlayWriter() { stop(); }

bool OverlayWriter::try_submit(std::string path, std::vector<uint8_t>& img, int w, int h,
                               std::vector<ResultSaver::OverlayBox> boxes) {
    {
        std::lock_guard<std::mutex> lk(mtx_);
        if (stopping_ || inflight_ >= max_inflight_) { ++st_.dropped; return false; }
        ++inflight_;
        if (inflight_ > st_.peak) st_.peak = inflight_;
        ++st_.submitted;
        q_.push_back(Job{std::move(path), std::move(img), w, h, std::move(boxes)});
    }
    img = {};   // 明確交出（move 後狀態未指定 → 統一成空，呼叫端 next_frame 會拿新的）
    cv_.notify_one();
    return true;
}

void OverlayWriter::worker() {
    // 低優先權：Linux 的 nice 是 per-thread（以 tid 設）。CPU 吃緊時讓檢測/收包先跑，
    // 但不用 SCHED_IDLE —— 那會在滿載時完全餓死，buffer 卡在手上反而更早開始丟。
    setpriority(PRIO_PROCESS, (id_t)syscall(SYS_gettid), 10);
    for (;;) {
        Job job;
        {
            std::unique_lock<std::mutex> lk(mtx_);
            cv_.wait(lk, [&] { return stopping_ || !q_.empty(); });
            if (q_.empty()) return;   // stopping 且已清空
            job = std::move(q_.front());
            q_.pop_front();
        }
        const auto t0 = std::chrono::steady_clock::now();
        const bool ok = ResultSaver::write_overlay(job.img.data(), job.w, job.h, job.boxes, job.path);
        const double ms = std::chrono::duration<double, std::milli>(
            std::chrono::steady_clock::now() - t0).count();
        if (!ok) fprintf(stderr, "[overlay] ⚠ 寫檔失敗：%s\n", job.path.c_str());
        if (ret_) ret_(std::move(job.img));
        std::lock_guard<std::mutex> lk(mtx_);
        --inflight_;
        if (ok) ++st_.written; else ++st_.failed;
        sum_ms_ += ms;
        if (ms > st_.max_ms) st_.max_ms = ms;
    }
}

void OverlayWriter::stop() {
    {
        std::lock_guard<std::mutex> lk(mtx_);
        if (stopping_ && pool_.empty()) return;
        stopping_ = true;
    }
    cv_.notify_all();
    for (auto& t : pool_) if (t.joinable()) t.join();
    pool_.clear();
}

OverlayWriter::Stats OverlayWriter::stats() const {
    std::lock_guard<std::mutex> lk(mtx_);
    Stats s = st_;
    s.inflight = inflight_;
    const uint64_t n = st_.written + st_.failed;
    s.avg_ms = n ? sum_ms_ / (double)n : 0.0;
    return s;
}
