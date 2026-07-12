// ═══ 📖 手冊對照（docs/html/cf-aoi-training.html，開啟後 ⌘K 搜章節）═══
// [手冊 ch2] 第三層 RDMA wire 格式（256B/magic/CRC32）＋詞彙表（imm/QP/MR）
// ⚠ 兩端必須同版（不變式2/3）——單邊重編=frame_validation 風暴（p2 破案卡）
// ═══════════════════════════════════════════════════════════════
#ifndef CFAOI_SHARED_FRAMEHEADER_H
#define CFAOI_SHARED_FRAMEHEADER_H

/**
 * ============================================================================
 * CF-AOI 分散式架構 — Capture→Computing RDMA / TCP 線格式（256 bytes）
 * ============================================================================
 *
 * ★ 此版＝ Phase-1 已實機驗證的線格式（Reference/phase1_tests/FrameHeader.h），
 *   2026-06-11 於 damac↔spark-c16f 用 t21/t40 RDMA→GPU 全幀 CRC 驗證通過。
 *   magic = 0xA01CF00D、frameSeq u64、panelId u32、含 sliceIndex/machineCoordX/Y。
 *   兩端（Grab 送出 / IP 接收）必須完全一致，否則解析錯位。
 *
 * ⚠️ 取代舊 repo 版（magic 0xCFA0A001 + panel_id_hash/system_id/flags），該版從未經
 *   RDMA 收發。對齊決策見 docs/HANDOVER_spark_20260615.md / STATUS.md。
 *
 * 不變式：
 *   - sizeof(FrameHeader) == 256（static_assert）
 *   - magic = 0xA01CF00D（合法 hex）
 *   - #pragma pack(1)：兩端 ABI 一致，無 compiler 補洞
 *
 * 相容備註：本檔在 phase1 結構上「附加」便利建構器 make_frame_header() 與 frame_panel_hash()，
 *   供 IP（result/queue）沿用既有呼叫；不改動 wire 結構本身。
 *   phase1 格式「無 system_id / flags(last_frame)」欄位 → make_frame_header 仍接受這些參數但
 *   不寫入 wire（IP 目前未讀）；日後 online 若需 Reflection/Transmission 或 last_frame，
 *   應使用 reserved[] 擴充（勿改既有欄位順序，否則破壞已驗證線格式）。
 * ============================================================================
 */

#include <cstdint>
#include <cstddef>
#include <string>

#pragma pack(push, 1)               // 緊密排列：欄位之間不插入 padding
struct FrameHeader {                // 總長 256 bytes（固定欄位 64 + reserved 192）
    uint32_t magic;                 // 魔術數 0xA01CF00D，接收端確認有效標頭
    uint16_t version;               // 線格式版本 = 2
    uint16_t headerBytes;           // 標頭長度 = 256
    uint64_t frameSeq;              // 全域遞增幀序號（也塞進 RDMA imm）
    uint32_t panelId;               // 面板/玻璃基板編號（IP 端 make_frame_header 暫填 panel 字串 FNV hash）
    uint16_t camId;                 // 相機編號 0..36
    uint16_t sliceIndex;            // 線掃分段索引
    uint16_t totalSlice;            // 總分段數
    uint16_t scanStep;              // 掃描步進
    uint32_t width;                 // 影像寬
    uint32_t height;                // 影像高
    uint16_t bitDepth;              // 位元深度 8/10/12/16
    uint16_t pixelFormat;           // 0=Mono8,1=Mono16,2=BayerRG8,...
    uint64_t ptpTimestampNs;        // PTP 時間戳（奈秒），多相機同步用
    int32_t  machineCoordX;         // 機台座標 X（換算缺陷全域位置用）
    int32_t  machineCoordY;         // 機台座標 Y
    uint32_t payloadBytes;          // 後面影像 payload 位元組數
    uint32_t crc32;                 // payload 的 CRC32（IEEE，驗資料完整性）
    uint8_t  reserved[256 - 64];    // 前面固定欄位共 64 bytes，補滿到 256
};
#pragma pack(pop)

static_assert(sizeof(FrameHeader) == 256, "FrameHeader 必須為 256 bytes");

constexpr uint32_t FRAME_MAGIC   = 0xA01CF00Du;   // 與 magic 欄位比對用
constexpr uint16_t FRAME_VERSION = 2;

// -----------------------------------------------------------------------------
// crc32_ieee — 標準 IEEE 802.3 CRC32（多項式 0xEDB88320，與 zlib 相同）。
//   驗證：crc32_ieee("123456789",9) == 0xCBF43926（標準測試向量）。
// -----------------------------------------------------------------------------
// slice-by-8 表驅動（與逐位元版同 IEEE 結果、wire 相容）。
// 原因：逐位元 CRC 對每幀 40.8MB 在收/送兩端各約 86MB/s，是 RDMA 收圖吞吐的真正瓶頸
// （ib_write_bw 原生 RDMA 達 11.4GB/s）。表初始化用 C++11 thread-safe static local，跨 TU 單例。
struct Crc32Table {
    uint32_t t[8][256];
    Crc32Table() {
        for (uint32_t n = 0; n < 256; ++n) {
            uint32_t c = n;
            for (int k = 0; k < 8; ++k) c = (c >> 1) ^ (0xEDB88320u & (~((c & 1u) - 1u)));
            t[0][n] = c;
        }
        for (uint32_t n = 0; n < 256; ++n) {
            uint32_t c = t[0][n];
            for (int k = 1; k < 8; ++k) { c = t[0][c & 0xff] ^ (c >> 8); t[k][n] = c; }
        }
    }
};
inline const Crc32Table& crc32_tbl() { static const Crc32Table T; return T; }

inline uint32_t crc32_ieee(const void* data, uint64_t len, uint32_t crc = 0xFFFFFFFFu) {
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

// ============================================================================
// 相容層（附加；不屬 wire 結構）——供 IP 既有呼叫沿用
// ============================================================================

// panel_id 字串 → 32-bit FNV-1a hash（暫填入 panelId 欄位；正式上線可改填真實面板編號）
inline uint32_t frame_panel_hash(const std::string& panel_id) {
    uint32_t h = 0x811C9DC5u;       // FNV offset basis
    for (unsigned char c : panel_id) { h ^= c; h *= 0x01000193u; }  // FNV prime
    return h;
}

// 便利建構器：填好固定欄位 + CRC。system_id / last_frame 於 phase1 線格式無對應欄位 →
// 接受但不寫入 wire（IP 目前未讀；見檔頭相容備註）。
inline FrameHeader make_frame_header(const std::string& panel_id,
                                     uint16_t cam_id, uint16_t frame_seq,
                                     uint32_t width, uint32_t height,
                                     const uint8_t* payload, uint32_t payload_bytes,
                                     uint8_t system_id = 0,
                                     uint64_t timestamp_ns = 0,
                                     bool last_frame = false) {
    (void)system_id; (void)last_frame;   // phase1 無對應欄位（日後用 reserved[] 擴充）
    FrameHeader h{};
    h.magic          = FRAME_MAGIC;
    h.version        = FRAME_VERSION;
    h.headerBytes    = 256;
    h.frameSeq       = frame_seq;
    h.panelId        = frame_panel_hash(panel_id);
    h.camId          = cam_id;
    h.sliceIndex     = 0;
    h.totalSlice     = 1;
    h.scanStep       = 0;
    h.width          = width;
    h.height         = height;
    h.bitDepth       = 8;
    h.pixelFormat    = 0;             // Mono8
    h.ptpTimestampNs = timestamp_ns;
    h.machineCoordX  = 0;
    h.machineCoordY  = 0;
    h.payloadBytes   = payload_bytes;
    h.crc32          = (payload && payload_bytes) ? crc32_ieee(payload, payload_bytes) : 0;
    return h;
}

#endif // CFAOI_SHARED_FRAMEHEADER_H
