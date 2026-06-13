#ifndef CFAOI_SHARED_FRAMEHEADER_H
#define CFAOI_SHARED_FRAMEHEADER_H

/**
 * ============================================================================
 * CF-AOI 分散式架構 — RDMA / TCP Wire Format
 * ============================================================================
 *
 * 256-byte fixed-size header prepended to every transmitted frame.
 * Shared by both ends (Grab 端送出 / IP 端接收) — 兩端版本必須一致。
 *
 * 不變式：
 *   - sizeof(FrameHeader) == 256（static_assert 驗證）
 *   - magic 使用合法 hex 0xCFA0A001（注意：O 和 I 不是 hex，故不可用 0xCFAOI001）
 *   - padding 自動算出讓 sizeof == 256
 *   - #pragma pack(1)：兩端 ABI 一致，無 compiler 補洞
 * ============================================================================
 */

#include <cstdint>
#include <cstddef>
#include <string>

#pragma pack(push, 1)
struct FrameHeader {
    uint32_t magic;          // FRAME_MAGIC = 0xCFA0A001
    uint32_t version;        // FRAME_VERSION
    uint64_t timestamp_ns;   // PTP 時間戳（奈秒）
    uint32_t panel_id_hash;  // panel_id 的 32-bit hash（FNV-1a）
    uint16_t cam_id;         // 來源 CCD 編號（0~33）
    uint16_t frame_seq;      // 幀序號
    uint32_t width;          // 影像寬度（例：8192）
    uint32_t height;         // 影像高度（例：5000）
    uint8_t  pixel_format;   // 0 = Mono8
    uint8_t  system_id;      // 0 = Reflection（反射）, 1 = Transmission（穿透）
    uint16_t flags;          // bit0 = last_frame
    uint32_t payload_bytes;  // 影像資料位元組數（= width*height for Mono8）
    uint32_t crc32;          // payload 的 CRC32（IEEE）

    // 補齊到 256 bytes：固定欄位合計 44 bytes → padding 212 bytes
    uint8_t  padding[256 - (4 + 4 + 8 + 4 + 2 + 2 + 4 + 4 + 1 + 1 + 2 + 4 + 4)];
};
#pragma pack(pop)

static_assert(sizeof(FrameHeader) == 256, "FrameHeader must be exactly 256 bytes");

// ============================================================================
// 常數
// ============================================================================

constexpr uint32_t FRAME_MAGIC   = 0xCFA0A001u;  // 合法 hex（非 0xCFAOI001）
constexpr uint32_t FRAME_VERSION = 1u;

// pixel_format 列舉
enum FramePixelFormat : uint8_t {
    PIXFMT_MONO8 = 0,
};

// system_id 列舉
enum FrameSystemId : uint8_t {
    SYS_REFLECTION   = 0,
    SYS_TRANSMISSION = 1,
};

// flags bit 定義
enum FrameFlags : uint16_t {
    FLAG_LAST_FRAME = 0x0001,
};

// ============================================================================
// CRC32（IEEE 802.3，table-less，與多數工具相容）
// ============================================================================

inline uint32_t frame_crc32(const uint8_t* data, size_t len) {
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < len; ++i) {
        crc ^= data[i];
        for (int b = 0; b < 8; ++b) {
            uint32_t mask = -(crc & 1u);
            crc = (crc >> 1) ^ (0xEDB88320u & mask);
        }
    }
    return ~crc;
}

// ============================================================================
// panel_id → 32-bit hash（FNV-1a）
// ============================================================================

inline uint32_t frame_panel_hash(const std::string& panel_id) {
    uint32_t h = 0x811C9DC5u;  // FNV offset basis
    for (unsigned char c : panel_id) {
        h ^= c;
        h *= 0x01000193u;      // FNV prime
    }
    return h;
}

// 便利建構器：填好固定欄位 + CRC，回傳 header。
inline FrameHeader make_frame_header(const std::string& panel_id,
                                     uint16_t cam_id, uint16_t frame_seq,
                                     uint32_t width, uint32_t height,
                                     const uint8_t* payload, uint32_t payload_bytes,
                                     uint8_t system_id = SYS_REFLECTION,
                                     uint64_t timestamp_ns = 0,
                                     bool last_frame = false) {
    FrameHeader h{};
    h.magic         = FRAME_MAGIC;
    h.version       = FRAME_VERSION;
    h.timestamp_ns  = timestamp_ns;
    h.panel_id_hash = frame_panel_hash(panel_id);
    h.cam_id        = cam_id;
    h.frame_seq     = frame_seq;
    h.width         = width;
    h.height        = height;
    h.pixel_format  = PIXFMT_MONO8;
    h.system_id     = system_id;
    h.flags         = last_frame ? FLAG_LAST_FRAME : 0;
    h.payload_bytes = payload_bytes;
    h.crc32         = (payload && payload_bytes) ? frame_crc32(payload, payload_bytes) : 0;
    return h;
}

#endif // CFAOI_SHARED_FRAMEHEADER_H
