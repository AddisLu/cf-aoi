#include "tcp_source.h"

#include <utility>

bool TcpImageSource::next_frame(FrameHeader& hdr, std::vector<uint8_t>& payload) {
    FrameQueue::Item item;
    if (!queue_.pop(item)) return false;  // queue 已關閉且清空
    hdr = item.hdr;
    panel_id_ = item.panel_id;
    payload = std::move(item.payload);
    return true;
}
