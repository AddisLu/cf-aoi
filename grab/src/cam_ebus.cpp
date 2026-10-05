// CamEbus — L803K 經 Pleora iPORT CL-GigE（eBUS SDK）。說明見 cam_ebus.h。
#include "cam_ebus.h"

#include <PvBuffer.h>
#include <PvDevice.h>
#include <PvDeviceAdapter.h>
#include <PvDeviceGEV.h>
#include <PvDeviceInfoGEV.h>
#include <PvDeviceSerialPort.h>
#include <PvInterface.h>
#include <PvStream.h>
#include <PvStreamGEV.h>
#include <PvSystem.h>

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <stdexcept>
#include <sys/stat.h>

namespace {

// ── eBUS 執行環境 ────────────────────────────────────────────────────────────
// eBUS 的 GenICam 需要 GENICAM_ROOT 等環境變數（官方 set_puregev_env.sh 設的）。systemd 服務不會
// source 那支腳本 → 第一次碰 eBUS 前自己補（已設就不動）。函式庫路徑由建置時 RPATH 處理。
void ensure_ebus_env() {
    static bool done = false;
    if (done) return;
    done = true;
#ifdef CFAOI_EBUS_ROOT
    const std::string root = CFAOI_EBUS_ROOT;
    const std::string genicam = root + "/lib/genicam";
    const char* home = std::getenv("HOME");
    const std::string cache = std::string(home ? home : "/tmp") + "/.config/Pleora/genicam_cache_v3_4";
    ::setenv("PUREGEV_ROOT", root.c_str(), 0);
    ::setenv("GENICAM_ROOT", genicam.c_str(), 0);
    ::setenv("GENICAM_ROOT_V3_4", genicam.c_str(), 0);
    const std::string log = genicam + "/log/config/DefaultLogging.properties";
    ::setenv("GENICAM_LOG_CONFIG", log.c_str(), 0);
    ::setenv("GENICAM_LOG_CONFIG_V3_4", log.c_str(), 0);
    ::setenv("GENICAM_CACHE", cache.c_str(), 0);
    ::setenv("GENICAM_CACHE_V3_4", cache.c_str(), 0);
    std::string mk = "mkdir -p '" + cache + "'";
    if (std::system(mk.c_str()) != 0) { /* 快取目錄建不了只是慢一點，不致命 */ }
#endif
}

std::string mac_compact(const std::string& m) {   // "00:11:1c:06:00:2b" → "00111C06002B"（同 pylon 列舉格式）
    std::string o;
    for (char c : m) if (std::isxdigit((unsigned char)c)) o += (char)std::toupper((unsigned char)c);
    return o;
}

bool is_pleora(const PvDeviceInfo* di) {
    std::string v = di->GetVendorName().GetAscii();
    std::transform(v.begin(), v.end(), v.begin(), ::tolower);
    return v.find("pleora") != std::string::npos;
}

// ── Basler L800 二進位序列協定（經 iPORT Bulk0 UART）────────────────────────────
// 移植自 tools/cam_align/l800_serial.py（調機工具已實機驗證）。Basler L800 手冊 §4.3：
//   frame = 0x01(BFS) FTF LEN ADDR_lo ADDR_hi [DATA…] BCC 0x03(BFE)
//   FTF：讀 0x0C、寫 0x04（= BCC 存在 + 16-bit 位址）；讀回應 FTF 0x14、**無位址欄**
//   BCC = FTF..最後一個資料 byte 的 XOR（不含 BFS/BCC/BFE）
//   寫 → 回 ACK(0x06) / NAK(0x15)；讀 → ACK + 01 14 n data BCC 03（順序可能顛倒，要掃描）
// CSR（值皆 f32 LE，在 base+1；min +5、max +9）：
//   增益 0x0E00（raw u16 LE 在 +0x0D；256 = 0 dB）、曝光模式 0x1400、曝光 0x1500（µs）、行週期 0x1600（µs）
class L800Serial {
public:
    bool open(PvDevice* dev, std::string& err) {
        PvGenParameterArray* p = dev->GetParameters();
        p->SetEnumValue("BulkSelector", "Bulk0");
        p->SetEnumValue("BulkMode", "UART");
        p->SetEnumValue("BulkBaudRate", "Baud9600");
        p->SetEnumValue("BulkNumOfStopBits", "One");
        p->SetEnumValue("BulkParity", "None");
        p->SetBooleanValue("BulkLoopback", false);
        // SoftReset 是**準位**不是脈衝：寫 1 一定要再寫 0，否則 UART 卡在 reset（調機工具踩過）
        if (p->Get("BulkSoftReset")) {
            p->SetBooleanValue("BulkSoftReset", true);
            p->SetBooleanValue("BulkSoftReset", false);
        }
        adapter_ = std::make_unique<PvDeviceAdapter>(dev);
        PvResult r = port_.Open(adapter_.get(), PvDeviceSerialBulk0);
        if (!r.IsOK()) { err = std::string("Bulk0 序列埠開不了：") + r.GetCodeString().GetAscii(); return false; }
        port_.SetRxBufferSize(4096);
        // 讀一個已知暫存器確認相機有回應（eBUS 剛放手後第一次讀偶爾是空的 → read() 內有重試）
        std::vector<uint8_t> d;
        if (!read(0x0E00, 1, d)) { err = "L800 無回應（Camera Link 序列線？相機電源？）"; port_.Close(); return false; }
        open_ = true;
        return true;
    }
    void close() { if (open_) port_.Close(); open_ = false; }
    bool is_open() const { return open_; }

    bool read(uint16_t addr, uint8_t n, std::vector<uint8_t>& out) {
        std::vector<uint8_t> body = {0x0C, n, (uint8_t)(addr & 0xFF), (uint8_t)(addr >> 8)};
        for (int t = 0; t < 3; ++t) {
            port_.FlushRxBuffer();
            if (!send(body)) continue;
            std::vector<uint8_t> raw;
            const auto until = std::chrono::steady_clock::now() + std::chrono::milliseconds(800);
            while (std::chrono::steady_clock::now() < until) {
                recv_some(raw, 50);
                if (parse_read(raw, n, out)) return true;
            }
        }
        return false;
    }

    bool write(uint16_t addr, const std::vector<uint8_t>& data) {
        std::vector<uint8_t> body = {0x04, (uint8_t)data.size(), (uint8_t)(addr & 0xFF), (uint8_t)(addr >> 8)};
        body.insert(body.end(), data.begin(), data.end());
        for (int t = 0; t < 3; ++t) {
            port_.FlushRxBuffer();
            if (!send(body)) continue;
            std::vector<uint8_t> raw;
            const auto until = std::chrono::steady_clock::now() + std::chrono::milliseconds(800);
            while (std::chrono::steady_clock::now() < until) {
                recv_some(raw, 50);
                for (uint8_t b : raw) {
                    if (b == 0x06) return true;     // ACK
                    if (b == 0x15) break;           // NAK → 重試
                }
                if (std::find(raw.begin(), raw.end(), (uint8_t)0x15) != raw.end()) break;
            }
        }
        return false;
    }

    bool read_f32(uint16_t addr, float& v) {
        std::vector<uint8_t> d;
        if (!read(addr, 4, d)) return false;
        std::memcpy(&v, d.data(), 4);   // LE（x86 / 調機工具實測 L803K 為 LE）
        return std::isfinite(v);
    }
    bool write_f32(uint16_t addr, float v) {
        std::vector<uint8_t> d(4);
        std::memcpy(d.data(), &v, 4);
        return write(addr, d);
    }
    bool read_u16(uint16_t addr, uint16_t& v) {
        std::vector<uint8_t> d;
        if (!read(addr, 2, d)) return false;
        v = (uint16_t)(d[0] | (d[1] << 8));
        return true;
    }
    bool write_u16(uint16_t addr, uint16_t v) { return write(addr, {(uint8_t)(v & 0xFF), (uint8_t)(v >> 8)}); }

    // 高階：曝光 µs、增益 raw、行速率 Hz（行週期 = 1e6/Hz；曝光不可 > 週期-2µs，先壓曝光再改週期）
    bool get_exposure_us(float& v) { return read_f32(0x1501, v); }
    bool set_exposure_us(float v)  { return write_f32(0x1501, v); }
    bool get_gain_raw(uint16_t& v) { return read_u16(0x0E0D, v); }
    bool set_gain_raw(uint16_t v)  { return write_u16(0x0E0D, v); }
    bool get_line_rate_hz(double& hz) {
        float p = 0;
        if (!read_f32(0x1601, p) || p <= 0) return false;
        hz = 1e6 / p;
        return true;
    }
    bool set_line_rate_hz(double hz, double& actual) {
        const float period = (float)std::min(1e6 / std::max(hz, 1.0), 100000.0);
        float exp = 0;
        if (get_exposure_us(exp) && exp > period - 2.0f)
            set_exposure_us(std::max(period - 2.0f, 10.0f));
        if (!write_f32(0x1601, period)) return false;
        return get_line_rate_hz(actual);
    }

private:
    bool send(const std::vector<uint8_t>& body) {
        uint8_t bcc = 0;
        for (uint8_t b : body) bcc ^= b;
        std::vector<uint8_t> f;
        f.push_back(0x01);
        f.insert(f.end(), body.begin(), body.end());
        f.push_back(bcc);
        f.push_back(0x03);
        uint32_t w = 0;
        return port_.Write(f.data(), (uint32_t)f.size(), w).IsOK() && w == f.size();
    }
    void recv_some(std::vector<uint8_t>& raw, uint32_t timeout_ms) {
        uint8_t buf[256];
        uint32_t n = 0;
        port_.Read(buf, sizeof(buf), n, timeout_ms);
        raw.insert(raw.end(), buf, buf + n);
    }
    // 讀回應：掃描 01 FTF(0x10..0x17) n data[n] [BCC] 03；略過 ACK/NAK；BCC 錯 → false（重試）
    static bool parse_read(const std::vector<uint8_t>& raw, uint8_t n, std::vector<uint8_t>& out) {
        for (size_t i = 0; i < raw.size(); ++i) {
            if (raw[i] != 0x01 || i + 3 > raw.size()) continue;
            const uint8_t ftf = raw[i + 1];
            if ((ftf & 0xF8) != 0x10 || raw[i + 2] != n) continue;
            const bool has_bcc = ftf & 0x04;
            const size_t end = i + 3 + n + (has_bcc ? 1 : 0);
            if (end >= raw.size()) return false;           // 還沒收完
            if (has_bcc) {
                uint8_t x = 0;
                for (size_t k = i + 1; k < i + 3 + n; ++k) x ^= raw[k];
                if (x != raw[i + 3 + n]) return false;
            }
            if (raw[end] != 0x03) continue;
            out.assign(raw.begin() + i + 3, raw.begin() + i + 3 + n);
            return true;
        }
        return false;
    }

    PvDeviceSerialPort port_;
    std::unique_ptr<PvDeviceAdapter> adapter_;
    bool open_ = false;
};

// 找 Pleora 裝置：serial 可以是序號或 MAC（含/不含冒號）；"auto"/"" = 第一台
const PvDeviceInfo* find_device(PvSystem& sys, const std::string& want) {
    const std::string want_mac = mac_compact(want);
    for (uint32_t i = 0; i < sys.GetInterfaceCount(); ++i) {
        const PvInterface* itf = sys.GetInterface(i);
        for (uint32_t d = 0; d < itf->GetDeviceCount(); ++d) {
            const PvDeviceInfo* di = itf->GetDeviceInfo(d);
            if (!is_pleora(di)) continue;
            if (want.empty() || want == "auto") return di;
            if (want == di->GetSerialNumber().GetAscii()) return di;
            auto* g = dynamic_cast<const PvDeviceInfoGEV*>(di);
            if (g && want_mac.size() == 12 && mac_compact(g->GetMACAddress().GetAscii()) == want_mac) return di;
            if (g && want == g->GetIPAddress().GetAscii()) return di;
        }
    }
    // 廣播沒找到（跨網段 / 同主機軟體模擬裝置）→ 以 IP / MAC / 名稱單播找
    if (!want.empty() && want != "auto") {
        const PvDeviceInfo* di = nullptr;
        if (sys.FindDevice(want.c_str(), &di).IsOK() && di && is_pleora(di)) return di;
    }
    return nullptr;
}

// CFAOI_EBUS_DEVICES="ip1,ip2"：列舉時額外單播探索這些位址（廣播收不到時用）
std::vector<std::string> extra_devices() {
    std::vector<std::string> out;
    const char* e = std::getenv("CFAOI_EBUS_DEVICES");
    if (!e) return out;
    std::string cur;
    for (const char* c = e; ; ++c) {
        if (*c == ',' || *c == '\0') { if (!cur.empty()) out.push_back(cur); cur.clear(); if (!*c) break; }
        else if (*c != ' ') cur += *c;
    }
    return out;
}

}  // namespace

struct CamEbus::Impl {
    PvDevice* dev = nullptr;
    PvStream* stream = nullptr;
    L800Serial serial;
    std::vector<PvBuffer*> buffers;
    std::string serial_no;
};

CamEbus::CamEbus() : d_(std::make_unique<Impl>()) {}
CamEbus::~CamEbus() { stop(); }

std::vector<CamInfo> CamEbus::enumerate_cameras() {
    ensure_ebus_env();
    std::vector<CamInfo> out;
    PvSystem sys;
    sys.SetDetectionTimeout(1500);
    if (!sys.Find().IsOK()) return out;
    std::map<std::string, bool> seen;   // 同一台會因網卡多個 IP 被列多次 → 依 MAC 去重
    // ⚠️ 每找到一台就立刻轉成 CamInfo，不可先收集 PvDeviceInfo* 再處理：FindDevice 內部會重新探索，
    //    之前拿到的指標全部失效（2026-10-05 實測 segfault 在 GetVendorName）。
    auto add = [&](const PvDeviceInfo* di) {
        if (!di || !is_pleora(di)) return;
        auto* g = dynamic_cast<const PvDeviceInfoGEV*>(di);
        CamInfo ci;
        ci.mac = g ? mac_compact(g->GetMACAddress().GetAscii()) : "";
        if (seen[ci.mac]) return;
        seen[ci.mac] = true;
        ci.cam_id = (int)out.size();
        ci.user_id = di->GetUserDefinedName().GetAscii();
        ci.model = di->GetModelName().GetAscii();
        ci.serial = di->GetSerialNumber().GetAscii();
        ci.device_class = "PleoraGEV";
        ci.ip = g ? g->GetIPAddress().GetAscii() : "";
        ci.persistent = !ci.ip.empty();
        out.push_back(ci);
    };
    for (uint32_t i = 0; i < sys.GetInterfaceCount(); ++i) {
        const PvInterface* itf = sys.GetInterface(i);
        for (uint32_t d = 0; d < itf->GetDeviceCount(); ++d) add(itf->GetDeviceInfo(d));
    }
    for (const auto& ip : extra_devices()) {
        const PvDeviceInfo* di = nullptr;
        if (sys.FindDevice(ip.c_str(), &di).IsOK() && di) add(di);
        else fprintf(stderr, "[cam_ebus] ⚠ CFAOI_EBUS_DEVICES 的 %s 找不到\n", ip.c_str());
    }
    return out;
}

bool CamEbus::open(const std::string& serial, int64_t pkt_size, Roi roi, double line_rate_hz) {
    ensure_ebus_env();
    if (opened_) return true;
    PvSystem sys;
    sys.SetDetectionTimeout(1500);
    sys.Find();
    const PvDeviceInfo* di = find_device(sys, serial);
    if (!di) { fprintf(stderr, "[cam_ebus] 找不到 iPORT（serial/MAC=%s）\n", serial.c_str()); return false; }
    d_->serial_no = di->GetSerialNumber().GetAscii();

    PvResult r;
    d_->dev = PvDevice::CreateAndConnect(di, &r);
    if (!d_->dev) {
        fprintf(stderr, "[cam_ebus] 連不上 %s：%s\n", d_->serial_no.c_str(), r.GetCodeString().GetAscii());
        return false;
    }
    PvGenParameterArray* p = d_->dev->GetParameters();
    p->SetEnumValue("PixelFormat", "Mono8");
    p->SetEnumValue("AcquisitionMode", "Continuous");
    if (roi.width  > 0 && !p->SetIntegerValue("Width",  roi.width).IsOK())
        fprintf(stderr, "[cam_ebus] ⚠ 設 Width=%lld 失敗\n", (long long)roi.width);
    if (roi.height > 0 && !p->SetIntegerValue("Height", roi.height).IsOK())
        fprintf(stderr, "[cam_ebus] ⚠ 設 Height=%lld 失敗\n", (long long)roi.height);
    int64_t w = 0, h = 0;
    p->GetIntegerValue("Width", w);
    p->GetIntegerValue("Height", h);
    if (w <= 0 || h <= 0) {     // Width=0 = iPORT 沒收到 Camera Link 訊號（調機工具同判定）
        fprintf(stderr, "[cam_ebus] ✗ %s Width/Height=%lldx%lld（Camera Link 沒訊號？）\n",
                d_->serial_no.c_str(), (long long)w, (long long)h);
        close_device();
        return false;
    }
    width_ = (uint32_t)w; height_ = (uint32_t)h;

    // 串流：開 PvStream、封包大小（iPORT 預設 1476，上限 9000；設不進退回協商）、目的地
    d_->stream = PvStream::CreateAndOpen(di, &r);
    if (!d_->stream) {
        fprintf(stderr, "[cam_ebus] 開串流失敗：%s\n", r.GetCodeString().GetAscii());
        close_device();
        return false;
    }
    auto* gev = dynamic_cast<PvDeviceGEV*>(d_->dev);
    auto* sgev = dynamic_cast<PvStreamGEV*>(d_->stream);
    if (gev && sgev) {
        if (pkt_size <= 0 || !p->SetIntegerValue("GevSCPSPacketSize", pkt_size).IsOK())
            gev->NegotiatePacketSize();
        gev->SetStreamDestination(sgev->GetLocalIPAddress(), sgev->GetLocalPort());
    }
    payload_ = d_->dev->GetPayloadSize();
    if (payload_ < (int64_t)w * h) payload_ = (int64_t)w * h;

    // 取像緩衝（一次配好；16 張 ≈ 650MB @ 8160×5000）
    for (int i = 0; i < 16; ++i) {
        auto* b = new PvBuffer;
        b->Alloc((uint32_t)payload_);
        d_->buffers.push_back(b);
    }

    // L803K 參數走 CL 序列埠。指定了行速率卻開不了序列埠 → fail-fast（行速率決定影像比例尺，
    // 不能默默沿用相機現值）；CFAOI_EBUS_NO_SERIAL=1 只給軟體模擬裝置測試用。
    std::string serr;
    const bool no_serial = std::getenv("CFAOI_EBUS_NO_SERIAL") != nullptr;
    if (!no_serial && !d_->serial.open(d_->dev, serr)) {
        fprintf(stderr, "[cam_ebus] ✗ %s：%s\n", d_->serial_no.c_str(), serr.c_str());
        if (line_rate_hz != 0) { close_device(); return false; }
    }
    double lr = 0;
    if (d_->serial.is_open() && line_rate_hz > 0) {
        if (!d_->serial.set_line_rate_hz(line_rate_hz, lr)) {
            fprintf(stderr, "[cam_ebus] ✗ %s 設行速率 %.0f Hz 失敗\n", d_->serial_no.c_str(), line_rate_hz);
            close_device();
            return false;
        }
    } else if (d_->serial.is_open()) {
        d_->serial.get_line_rate_hz(lr);
    }
    opened_ = true;
    printf("[cam_ebus] 開啟 %s %s  %ux%u  PayloadSize=%lld  行速率 %s\n",
           di->GetModelName().GetAscii(), d_->serial_no.c_str(), width_, height_, (long long)payload_,
           d_->serial.is_open() ? (std::to_string((long long)lr) + " Hz").c_str()
                                : (no_serial ? "（未開序列埠：CFAOI_EBUS_NO_SERIAL）" : "（序列埠不可用）"));
    return true;
}

void CamEbus::close_device() {
    d_->serial.close();
    if (d_->stream) {
        d_->stream->AbortQueuedBuffers();
        PvBuffer* b = nullptr; PvResult op;
        while (d_->stream->RetrieveBuffer(&b, &op, 0).IsOK()) {}
        d_->stream->Close();
        PvStream::Free(d_->stream);
        d_->stream = nullptr;
    }
    for (auto* b : d_->buffers) delete b;
    d_->buffers.clear();
    if (d_->dev) {
        d_->dev->Disconnect();
        PvDevice::Free(d_->dev);
        d_->dev = nullptr;
    }
    opened_ = false;
    payload_ = 0;
}

void CamEbus::start(uint16_t cam_id) {
    if (!opened_ || !cb_ || running_) return;
    if (thread_.joinable()) thread_.join();
    cam_id_ = cam_id;
    stop_flag_ = false;
    running_ = true;
    faulted_ = false;
    { std::lock_guard<std::mutex> lk(fault_mtx_); fault_msg_.clear(); }
    grabbed_ = 0;
    dropped_ = 0;
    thread_ = std::thread(&CamEbus::grab_loop, this);
}

void CamEbus::stop() {
    stop_flag_ = true;
    if (thread_.joinable()) thread_.join();
    running_ = false;
    if (opened_) close_device();
}

void CamEbus::note_fault(const char* kind, const char* what) {
    try {
        std::lock_guard<std::mutex> lk(fault_mtx_);
        fault_msg_ = std::string(kind) + (what ? what : "?");
    } catch (...) {}
    faulted_ = true;
    fprintf(stderr, "[cam_ebus] ⚠️ cam%u 取像中止（本台故障，其餘相機續跑）：%s%s\n",
            cam_id_, kind, what ? what : "?");
}

void CamEbus::grab_loop() {
    try {
        grab_loop_body();
    } catch (const std::exception& e) {
        note_fault("exception: ", e.what());
    } catch (...) {
        note_fault("unknown: ", "非 std::exception 的未知例外");
    }
    try {
        if (d_->dev) {
            d_->dev->GetParameters()->ExecuteCommand("AcquisitionStop");
            d_->dev->GetParameters()->SetIntegerValue("TLParamsLocked", 0);
            d_->dev->StreamDisable();
        }
        if (d_->stream) {
            d_->stream->AbortQueuedBuffers();
            PvBuffer* b = nullptr; PvResult op;
            while (d_->stream->RetrieveBuffer(&b, &op, 0).IsOK()) {}
        }
    } catch (...) {}
    running_ = false;
}

void CamEbus::grab_loop_body() {
    for (auto* b : d_->buffers) d_->stream->QueueBuffer(b);
    PvGenParameterArray* p = d_->dev->GetParameters();
    p->SetIntegerValue("TLParamsLocked", 1);
    if (!d_->dev->StreamEnable().IsOK()) throw std::runtime_error("StreamEnable 失敗");
    if (!p->ExecuteCommand("AcquisitionStart").IsOK()) throw std::runtime_error("AcquisitionStart 失敗");

    int64_t prev_block = -1;
    int consecutive_timeouts = 0;
    auto t_log = std::chrono::steady_clock::now();
    uint64_t log_frames = 0, incomplete_logged = 0;

    while (!stop_flag_) {
        PvBuffer* buf = nullptr;
        PvResult op;
        PvResult r = d_->stream->RetrieveBuffer(&buf, &op, 1000);
        if (!r.IsOK()) {
            // 逾時 = 還沒收到幀（行速率 12kHz × 5000 行 ≈ 0.42s/幀）；連續 10 秒沒幀 → 視為斷線
            if (r.GetCode() == PvResult::Code::TIMEOUT) {
                if (++consecutive_timeouts >= 10) throw std::runtime_error("連續 10 秒沒有影像（iPORT 斷線/Camera Link 沒訊號？）");
                continue;
            }
            throw std::runtime_error(std::string("RetrieveBuffer: ") + r.GetCodeString().GetAscii());
        }
        consecutive_timeouts = 0;
        const int64_t bid = (int64_t)buf->GetBlockID();
        if (prev_block >= 0 && bid > prev_block + 1) dropped_ += (uint64_t)(bid - prev_block - 1);
        prev_block = bid;

        // 不完整幀（封包缺失 = Camera Link 資料腳接觸不良時「隨機缺行」的樣子）→ 整張作廢不送，
        // 計入 dropped（IP 端 frame_loss 會標 panel_incomplete）。寧可少一張也不送斷層影像。
        if (!op.IsOK()) {
            ++dropped_;
            if (incomplete_logged++ < 5 || incomplete_logged % 100 == 0)
                fprintf(stderr, "[cam_ebus] cam%u 不完整幀 block=%lld：%s（累計 %llu）\n", cam_id_,
                        (long long)bid, op.GetCodeString().GetAscii(), (unsigned long long)incomplete_logged);
            d_->stream->QueueBuffer(buf);
            continue;
        }
        PvImage* img = buf->GetImage();
        const uint32_t w = img ? img->GetWidth() : 0, h = img ? img->GetHeight() : 0;
        if (w == 0 || h == 0) { ++dropped_; d_->stream->QueueBuffer(buf); continue; }
        cb_(cam_id_, buf->GetDataPointer(), w * h, w, h);
        d_->stream->QueueBuffer(buf);
        ++grabbed_;
        ++log_frames;

        const auto now = std::chrono::steady_clock::now();
        if (now - t_log >= std::chrono::seconds(5)) {
            const double s = std::chrono::duration<double>(now - t_log).count();
            printf("[cam_ebus] cam%u  FPS=%.1f  grabbed=%llu  dropped=%llu\n", cam_id_, log_frames / s,
                   (unsigned long long)grabbed_.load(), (unsigned long long)dropped_.load());
            t_log = now; log_frames = 0;
        }
        if (max_frames_ > 0 && grabbed_ >= max_frames_) {
            printf("[cam_ebus] cam%u 收滿 %llu 張，自動停止取像\n", cam_id_, (unsigned long long)grabbed_.load());
            break;
        }
    }
}

bool CamEbus::set_params(float exposure_us, int gain_raw, float& exp_actual, int& gain_actual) {
    if (!opened_ || !d_->serial.is_open()) return false;
    bool ok = d_->serial.set_exposure_us(exposure_us) && d_->serial.set_gain_raw((uint16_t)gain_raw);
    return get_params(exp_actual, gain_actual) && ok;
}

bool CamEbus::get_params(float& exp_actual, int& gain_actual) {
    if (!opened_ || !d_->serial.is_open()) return false;
    uint16_t g = 0;
    if (!d_->serial.get_exposure_us(exp_actual) || !d_->serial.get_gain_raw(g)) return false;
    gain_actual = g;
    return true;
}

bool CamEbus::grab_one_mean(double& mean, std::string& err) {
    if (!opened_) { err = "相機未開啟"; return false; }
    if (running_) { err = "串流中，請先停止"; return false; }
    PvBuffer* first = d_->buffers.front();
    d_->stream->QueueBuffer(first);
    PvGenParameterArray* p = d_->dev->GetParameters();
    d_->dev->StreamEnable();
    p->ExecuteCommand("AcquisitionStart");
    PvBuffer* buf = nullptr; PvResult op;
    PvResult r = d_->stream->RetrieveBuffer(&buf, &op, 5000);
    p->ExecuteCommand("AcquisitionStop");
    d_->dev->StreamDisable();
    d_->stream->AbortQueuedBuffers();
    if (!r.IsOK() || !op.IsOK() || !buf->GetImage()) {
        err = std::string("取像失敗：") + (r.IsOK() ? op.GetCodeString().GetAscii() : r.GetCodeString().GetAscii());
        return false;
    }
    const uint8_t* px = buf->GetDataPointer();
    const uint64_t n = (uint64_t)buf->GetImage()->GetWidth() * buf->GetImage()->GetHeight();
    uint64_t sum = 0;
    for (uint64_t i = 0; i < n; ++i) sum += px[i];
    mean = n ? (double)sum / n : 0.0;
    return true;
}

bool CamEbus::read_machine_params(MachineParams& mp, std::string& err) {
    if (!opened_) { err = "相機未開啟"; return false; }
    PvGenParameterArray* p = d_->dev->GetParameters();
    PvString s;
    if (p->GetEnumValue("PixelFormat", s).IsOK()) mp.pixel_format = s.GetAscii();
    int64_t v = 0;
    if (p->GetIntegerValue("Width", v).IsOK()) mp.width = v;
    if (p->GetIntegerValue("Height", v).IsOK()) mp.height = v;
    if (p->GetIntegerValue("GevSCPSPacketSize", v).IsOK()) mp.packet_size = v;
    if (p->GetIntegerValue("GevSCPD", v).IsOK()) mp.scpd = v;
    mp.trigger_mode = "Off（free-run，L803K 行速率由 CL 序列埠設定）";
    mp.exposure_auto = mp.gain_auto = "N/A（L803K）";
    double lr = 0;
    if (d_->serial.is_open() && d_->serial.get_line_rate_hz(lr)) mp.line_rate_set = mp.line_rate_resulting = lr;
    return true;
}

// ── 外掛進入點（cfaoi_grab 以 dlopen 載入 libcfaoi_cam_ebus.so）────────────────
// eBUS 函式庫在**載入時**就檢查 GENICAM_ROOT_V3_4，沒設直接 exit → 不能直接連進 cfaoi_grab
// （pylon 模式也會被拖死，2026-10-05 實測服務起不來）。改成 --camera ebus 時才 dlopen，
// 載入前由 cam_manager.cpp 先補好環境變數。
extern "C" ICamera* cfaoi_ebus_create() { return new CamEbus(); }
extern "C" void cfaoi_ebus_enumerate(std::vector<CamInfo>* out) { *out = CamEbus::enumerate_cameras(); }
