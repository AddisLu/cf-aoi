// =============================================================================
// crc_verify.cpp — 驗證 ARM64 硬體 CRC32 與表格版**位元級等價**（wire 相容性守門）
// -----------------------------------------------------------------------------
// 為什麼一定要驗：CRC 值走在 wire 上（FrameHeader.crc32）。生產拓樸是
//   damac(x86，表格版) 送 → Spark(ARM64，硬體版) 收
// 兩版只要有任何一個長度/對齊不一致，就會變成「每幀都 CRC 失敗」的假故障。
// 這種錯誤在單機測試看不出來（同一版自己對自己一定過），必須逐長度交叉比對。
//
// 用法： crc_verify            （跑全部案例，回傳 0=PASS / 1=FAIL）
// =============================================================================
#include "../../shared/FrameHeader.h"

#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <chrono>
#include <vector>

// 表格版（強制走軟體路徑，供對照）——複製自 FrameHeader.h 的 slice-by-8 實作
static uint32_t crc32_table_ref(const void* data, uint64_t len, uint32_t crc = 0xFFFFFFFFu) {
    const auto& T = crc32_tbl();
    const uint8_t* p = static_cast<const uint8_t*>(data);
    for (; len >= 8; len -= 8, p += 8) {
        uint32_t lo = crc ^ ((uint32_t)p[0] | ((uint32_t)p[1] << 8) |
                             ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24));
        uint32_t hi = (uint32_t)p[4] | ((uint32_t)p[5] << 8) |
                      ((uint32_t)p[6] << 16) | ((uint32_t)p[7] << 24);
        crc = T.t[7][lo & 0xff] ^ T.t[6][(lo >> 8) & 0xff] ^
              T.t[5][(lo >> 16) & 0xff] ^ T.t[4][(lo >> 24) & 0xff] ^
              T.t[3][hi & 0xff] ^ T.t[2][(hi >> 8) & 0xff] ^
              T.t[1][(hi >> 16) & 0xff] ^ T.t[0][(hi >> 24) & 0xff];
    }
    for (; len; --len, ++p) crc = T.t[0][(crc ^ *p) & 0xff] ^ (crc >> 8);
    return crc ^ 0xFFFFFFFFu;
}

int main() {
    int fail = 0, checked = 0;

#if defined(__aarch64__)
    printf("平台：aarch64；硬體 CRC32 %s\n",
           crc32_hw_available() ? "可用（將走硬體路徑）" : "不可用（退回表格版）");
    if (!crc32_hw_available()) {
        printf("[SKIP] 本機無硬體 CRC32，等價性無從比對（表格版自己對自己無意義）\n");
        return 0;
    }
#else
    printf("平台：非 aarch64 → crc32_ieee 本來就走表格版；本測試僅確認自我一致\n");
#endif

    // ── ① 已知答案（外部基準）：CRC-32/ISO-HDLC 對 "123456789" = 0xCBF43926 ──
    {
        const char* s = "123456789";
        uint32_t got = crc32_ieee(s, 9);
        bool ok = (got == 0xCBF43926u);
        printf("%s ① 標準測試向量 \"123456789\" → 0x%08X（期望 0xCBF43926）\n",
               ok ? "[PASS]" : "[FAIL]", got);
        if (!ok) ++fail;
        ++checked;
    }

    // ── ② 逐長度交叉比對 0..2048（涵蓋所有 8-byte 對齊餘數與短尾巴）──
    {
        std::vector<uint8_t> buf(2048);
        for (size_t i = 0; i < buf.size(); ++i)
            buf[i] = (uint8_t)((i * 131 + (i >> 3) * 17 + 7) & 0xFF);
        int bad = 0;
        for (uint64_t n = 0; n <= 2048; ++n) {
            uint32_t a = crc32_ieee(buf.data(), n);
            uint32_t b = crc32_table_ref(buf.data(), n);
            if (a != b) {
                if (bad < 5)
                    printf("      len=%llu 不一致：hw=0x%08X table=0x%08X\n",
                           (unsigned long long)n, a, b);
                ++bad;
            }
            ++checked;
        }
        printf("%s ② 逐長度 0..2048 交叉比對（%d 個長度，不一致 %d）\n",
               bad ? "[FAIL]" : "[PASS]", 2049, bad);
        if (bad) ++fail;
    }

    // ── ③ 非對齊起始位址（RDMA slot 內 payload 起點 = slot+256，但仍要驗各種偏移）──
    {
        std::vector<uint8_t> buf(4096 + 64);
        for (size_t i = 0; i < buf.size(); ++i) buf[i] = (uint8_t)(i * 251 + 13);
        int bad = 0;
        for (int off = 0; off < 16; ++off) {
            for (uint64_t n : {1ull, 7ull, 8ull, 9ull, 63ull, 64ull, 4095ull}) {
                uint32_t a = crc32_ieee(buf.data() + off, n);
                uint32_t b = crc32_table_ref(buf.data() + off, n);
                if (a != b) { ++bad; }
                ++checked;
            }
        }
        printf("%s ③ 非對齊起始位址（offset 0..15 × 7 種長度，不一致 %d）\n",
               bad ? "[FAIL]" : "[PASS]", bad);
        if (bad) ++fail;
    }

    // ── ④ 生產幀尺寸（8160×5000 = 40.8MB）+ 效能比較 ──
    {
        const uint64_t N = 8160ull * 5000ull;
        std::vector<uint8_t> buf(N);
        for (uint64_t i = 0; i < N; i += 1021) buf[i] = (uint8_t)(i & 0xFF);  // 稀疏填值，省初始化時間
        for (uint64_t i = 0; i < 4096; ++i) buf[i] = (uint8_t)(i * 97 + 3);

        auto t0 = std::chrono::steady_clock::now();
        uint32_t a = crc32_ieee(buf.data(), N);
        auto t1 = std::chrono::steady_clock::now();
        uint32_t b = crc32_table_ref(buf.data(), N);
        auto t2 = std::chrono::steady_clock::now();

        double ms_hw = std::chrono::duration<double, std::milli>(t1 - t0).count();
        double ms_tb = std::chrono::duration<double, std::milli>(t2 - t1).count();
        bool ok = (a == b);
        printf("%s ④ 生產幀 8160x5000 (40.8MB)：hw=0x%08X table=0x%08X\n",
               ok ? "[PASS]" : "[FAIL]", a, b);
        printf("      現用路徑 %6.2f ms (%5.2f GB/s) ｜ 表格版 %6.2f ms (%5.2f GB/s) ｜ 加速 %.1fx\n",
               ms_hw, N / ms_hw / 1e6, ms_tb, N / ms_tb / 1e6, ms_tb / ms_hw);
        if (!ok) ++fail;
        ++checked;
    }

    printf("\n結果：%s（共 %d 個比對點，失敗 %d 組）\n",
           fail ? "FAIL ✗" : "ALL PASS ✓", checked, fail);
    return fail ? 1 : 0;
}
