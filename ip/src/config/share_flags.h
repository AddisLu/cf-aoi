#ifndef CFAOI_SHARE_FLAGS_H
#define CFAOI_SHARE_FLAGS_H

// 共用旗標（由 Control LOAD_RECIPE share_flags 欄位傳入，per-recipe 重置）。
struct ShareFlags {
    bool tuning_recipe   = false;  // true → GPU 跑、結果回 TCP、但完全不寫磁碟（量速/調參模式）
    bool save_source_image = false; // true → 同時把原始 payload 非同步存到 source ring（Step 5）
};

#endif // CFAOI_SHARE_FLAGS_H
