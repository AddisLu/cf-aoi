#include "control_server.h"

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <map>
#include <string>
#include <utility>
#include <sys/stat.h>
#include <vector>

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <nlohmann/json.hpp>
#include <opencv2/imgcodecs.hpp>

#include "align_engine.h"
#include "diag/flight_recorder.h"

using json = nlohmann::json;

namespace {

// 收圖結構驗證（行車紀錄 frame_validation 用）：magic/version/headerBytes/payloadBytes 一致
// + payload CRC32 比對。回傳空字串=通過，否則=失敗原因（含期望vs實得）。
// offline-tcp 的 header 為本地建構（magic 等恆對）→ 主要擋未來 RDMA wire 損壞 + make_frame_header 迴歸；
// offline-tcp 的真實損壞偵測靠 SEND_IMAGE_FOR_REVIEW 的可選 client 宣告 crc32（見呼叫處）。
std::string validate_frame_header(const FrameHeader& h, const uint8_t* payload, uint32_t n) {
    char b[96];
    if (h.magic != FRAME_MAGIC) {
        std::snprintf(b, sizeof(b), "magic 不符 期望=0x%08X 實得=0x%08X", FRAME_MAGIC, h.magic);
        return b;
    }
    if (h.version != FRAME_VERSION) {
        std::snprintf(b, sizeof(b), "version 不符 期望=%u 實得=%u", FRAME_VERSION, h.version);
        return b;
    }
    if (h.headerBytes != 256) {
        std::snprintf(b, sizeof(b), "headerBytes 不符 期望=256 實得=%u", h.headerBytes);
        return b;
    }
    if (h.payloadBytes != n) {
        std::snprintf(b, sizeof(b), "payloadBytes 不符 header=%u 實收=%u", h.payloadBytes, n);
        return b;
    }
    uint32_t c = crc32_ieee(payload, n);
    if (h.crc32 != c) {
        std::snprintf(b, sizeof(b), "CRC32 不符 header=0x%08X 實算=0x%08X", h.crc32, c);
        return b;
    }
    return "";
}

// 連線讀緩衝：支援先讀「一行（\n 結尾）」再讀「剛好 N bytes 二進位」。
struct ConnReader {
    int fd;
    std::vector<uint8_t> buf;  // 已收但未消費的位元組

    explicit ConnReader(int f) : fd(f) {}

    // 從 socket 補資料進 buf；回傳 false 代表連線關閉/錯誤。
    bool fill() {
        uint8_t tmp[8192];
        ssize_t n = ::recv(fd, tmp, sizeof(tmp), 0);
        if (n <= 0) return false;
        buf.insert(buf.end(), tmp, tmp + n);
        return true;
    }

    // 讀一行（不含 \n；自動去掉結尾 \r）。回傳 false = 連線關閉。
    bool read_line(std::string& line) {
        while (true) {
            auto it = std::find(buf.begin(), buf.end(), (uint8_t)'\n');
            if (it != buf.end()) {
                size_t len = it - buf.begin();
                line.assign((char*)buf.data(), len);
                if (!line.empty() && line.back() == '\r') line.pop_back();
                buf.erase(buf.begin(), it + 1);
                return true;
            }
            if (!fill()) return false;
        }
    }

    // 讀剛好 n bytes（先用 buf 剩餘，再補）。回傳 false = 連線關閉。
    bool read_exact(size_t n, std::vector<uint8_t>& out) {
        while (buf.size() < n) {
            if (!fill()) return false;
        }
        out.assign(buf.begin(), buf.begin() + n);
        buf.erase(buf.begin(), buf.begin() + n);
        return true;
    }
};

bool send_all(int fd, const std::string& s) {
    size_t off = 0;
    while (off < s.size()) {
        ssize_t n = ::send(fd, s.data() + off, s.size() - off, 0);
        if (n <= 0) return false;
        off += (size_t)n;
    }
    return true;
}

void reply(int fd, const json& j) {
    std::string s = j.dump();
    s.push_back('\n');
    send_all(fd, s);
}

namespace fs = std::filesystem;

// By ID Folder：取資料夾名前兩段 '_' token（對齊 legacy frmSortDefect dir.Split('_')[0]_[1]）。
// 不足兩段時退回整個名稱。
std::string id_group(const std::string& folder_name) {
    auto i = folder_name.find('_');
    if (i == std::string::npos) return folder_name;
    auto j = folder_name.find('_', i + 1);
    if (j == std::string::npos) return folder_name;   // 只有一個 '_' → 整名
    return folder_name.substr(0, j);
}

// 名稱是否像 yyyyMMdd 日期夾（8 位數字）。
bool is_date_dir(const std::string& name) {
    if (name.size() != 8) return false;
    for (char c : name) if (c < '0' || c > '9') return false;
    return true;
}

// 讀 panel 夾內 {folder}_ResultInfo.json 的 DefectCnt（讀不到回 -1）。
int read_defect_cnt(const fs::path& panel_dir) {
    try {
        fs::path p = panel_dir / (panel_dir.filename().string() + "_ResultInfo.json");
        std::ifstream f(p);
        if (!f) return -1;
        json j;
        f >> j;
        if (j.contains("DefectCnt")) return j["DefectCnt"].get<int>();
        return -1;
    } catch (...) { return -1; }
}

// 找一塊 panel 夾路徑：date 指定 → <out>/<date>/<folder>；否則跨日期夾搜尋第一個命中。回傳空 = 找不到。
fs::path locate_panel(const fs::path& out, const std::string& date, const std::string& folder) {
    std::error_code ec;
    if (!date.empty()) {
        fs::path p = out / date / folder;
        return fs::exists(p, ec) ? p : fs::path{};
    }
    if (fs::exists(out, ec))
        for (const auto& e : fs::directory_iterator(out, ec)) {
            if (!e.is_directory() || !is_date_dir(e.path().filename().string())) continue;
            fs::path p = e.path() / folder;
            if (fs::exists(p, ec)) return p;
        }
    return {};
}

// 解析缺陷檔名 Defect_{ip}_Slice{ff}_Roi{rr}_Run{nn}_X{xxxx}_Y{yyyyyy}_Dr{reason}.png。
struct DefectName { bool ok = false; int slice = 0, roi = 0, run = 0, x = 0, y = 0; std::string type; };
DefectName parse_defect_name(const std::string& fn) {
    DefectName d;
    auto grab = [&](const char* key, int& out) -> bool {
        auto p = fn.find(key);
        if (p == std::string::npos) return false;
        out = std::atoi(fn.c_str() + p + std::strlen(key));
        return true;
    };
    if (fn.rfind("Defect", 0) != 0) return d;
    bool ok = grab("_Slice", d.slice) & grab("_Roi", d.roi) & grab("_Run", d.run)
            & grab("_X", d.x) & grab("_Y", d.y);
    auto pdr = fn.find("_Dr");
    if (pdr != std::string::npos) {
        std::string rest = fn.substr(pdr + 3);
        auto dot = rest.find('.');
        d.type = (dot == std::string::npos) ? rest : rest.substr(0, dot);  // Bright / Dark
    }
    d.ok = ok;
    return d;
}

// base64 編碼（小圖 PNG bytes 經 JSON 傳給 Control）。
std::string base64_encode(const std::vector<uint8_t>& in) {
    static const char* tbl = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string out;
    out.reserve(((in.size() + 2) / 3) * 4);
    size_t i = 0;
    for (; i + 2 < in.size(); i += 3) {
        uint32_t n = (in[i] << 16) | (in[i + 1] << 8) | in[i + 2];
        out.push_back(tbl[(n >> 18) & 63]); out.push_back(tbl[(n >> 12) & 63]);
        out.push_back(tbl[(n >> 6) & 63]);  out.push_back(tbl[n & 63]);
    }
    if (i < in.size()) {
        uint32_t n = in[i] << 16;
        bool two = (i + 1 < in.size());
        if (two) n |= in[i + 1] << 8;
        out.push_back(tbl[(n >> 18) & 63]);
        out.push_back(tbl[(n >> 12) & 63]);
        out.push_back(two ? tbl[(n >> 6) & 63] : '=');
        out.push_back('=');
    }
    return out;
}

// base64 解碼（golden PNG 由 Control 傳入）。
std::vector<uint8_t> base64_decode(const std::string& in) {
    static const int8_t tbl[256] = {
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,62,-1,-1,-1,63,
        52,53,54,55,56,57,58,59,60,61,-1,-1,-1,-1,-1,-1,
        -1, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9,10,11,12,13,14,
        15,16,17,18,19,20,21,22,23,24,25,-1,-1,-1,-1,-1,
        -1,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,
        41,42,43,44,45,46,47,48,49,50,51,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1
    };
    std::vector<uint8_t> out;
    out.reserve((in.size() / 4) * 3);
    uint32_t acc = 0;
    int bits = 0;
    for (unsigned char c : in) {
        if (c == '=') break;
        int v = tbl[c];
        if (v < 0) continue;
        acc = (acc << 6) | (uint32_t)v;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            out.push_back(uint8_t(acc >> bits));
            acc &= (1u << bits) - 1;
        }
    }
    return out;
}

// XML tag 提取（同 zone_config_adapter.cpp 中的 extract_tag，供解析 AlignRoi 用）。
bool cs_extract_tag(const std::string& xml, const std::string& tag, std::string& out) {
    const std::string open = "<" + tag + ">";
    const std::string close = "</" + tag + ">";
    size_t s = xml.find(open);
    if (s == std::string::npos) return false;
    s += open.size();
    size_t e = xml.find(close, s);
    if (e == std::string::npos) return false;
    out = xml.substr(s, e - s);
    size_t a = out.find_first_not_of(" \t\r\n");
    size_t b = out.find_last_not_of(" \t\r\n");
    if (a == std::string::npos) { out.clear(); return true; }
    out = out.substr(a, b - a + 1);
    return true;
}
bool cs_tag_int(const std::string& xml, const std::string& tag, int& v) {
    std::string s;
    if (!cs_extract_tag(xml, tag, s) || s.empty()) return false;
    try { v = std::stoi(s); return true; } catch (...) { return false; }
}

// 讀整個檔案為 bytes。
std::vector<uint8_t> read_file_bytes(const fs::path& p) {
    std::ifstream f(p, std::ios::binary);
    return std::vector<uint8_t>((std::istreambuf_iterator<char>(f)),
                                 std::istreambuf_iterator<char>());
}

// 讀/寫 panel 夾的 classification.json（patch_filename → "TrueDefect"|"Particle"）。
json read_classification(const fs::path& panel_dir) {
    try {
        std::ifstream f(panel_dir / "classification.json");
        if (!f) return json::object();
        json j; f >> j;
        return j.is_object() ? j : json::object();
    } catch (...) { return json::object(); }
}

}  // namespace

ControlServer::ControlServer(int port, FrameQueue& queue)
    : port_(port), queue_(queue) {}

ControlServer::~ControlServer() { stop(); }

bool ControlServer::start() {
    listen_fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
    if (listen_fd_ < 0) {
        std::cerr << "[ControlServer] socket() 失敗\n";
        return false;
    }
    int opt = 1;
    ::setsockopt(listen_fd_, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port = htons((uint16_t)port_);

    if (::bind(listen_fd_, (sockaddr*)&addr, sizeof(addr)) < 0) {
        std::cerr << "[ControlServer] bind(port=" << port_ << ") 失敗: " << std::strerror(errno) << "\n";
        ::close(listen_fd_);
        listen_fd_ = -1;
        return false;
    }
    if (::listen(listen_fd_, 4) < 0) {
        std::cerr << "[ControlServer] listen() 失敗\n";
        ::close(listen_fd_);
        listen_fd_ = -1;
        return false;
    }
    running_ = true;
    thread_ = std::thread(&ControlServer::run, this);
    std::cout << "[ControlServer] 監聽 port " << port_ << "\n";
    return true;
}

void ControlServer::stop() {
    if (!running_.exchange(false)) {
        if (thread_.joinable()) thread_.join();
        return;
    }
    if (listen_fd_ >= 0) {
        ::shutdown(listen_fd_, SHUT_RDWR);
        ::close(listen_fd_);
        listen_fd_ = -1;
    }
    if (thread_.joinable()) thread_.join();
    queue_.close();  // 喚醒 TcpImageSource::next_frame → 結束
}

void ControlServer::deliver_result(const std::string& panel_id, const std::string& result_json) {
    {
        std::lock_guard<std::mutex> lk(result_mtx_);
        results_[panel_id] = result_json;
    }
    result_cv_.notify_all();
}

void ControlServer::run() {
    while (running_) {
        sockaddr_in cli{};
        socklen_t len = sizeof(cli);
        int fd = ::accept(listen_fd_, (sockaddr*)&cli, &len);
        if (fd < 0) {
            if (!running_) break;
            continue;
        }
        std::cout << "[ControlServer] client connected\n";
        handle_client(fd);
        ::close(fd);
        std::cout << "[ControlServer] client disconnected\n";
    }
}

void ControlServer::handle_client(int fd) {
    ConnReader rd(fd);
    std::string line;
    while (running_ && rd.read_line(line)) {
        if (line.empty()) continue;

        json req, resp;
        std::string cmd;
        long seq = 0;
        try {
            req = json::parse(line);
            cmd = req.value("cmd", "");
            seq = req.value("seq", 0);
        } catch (const std::exception& e) {
            diag::FlightRecorder::instance().record_incident("bad_json", e.what());
            reply(fd, {{"status", "ERR"}, {"error", std::string("bad json: ") + e.what()}});
            continue;
        }
        resp["seq"] = seq;
        const json params = req.value("params", json::object());

        if (cmd == "LOAD_RECIPE") {
            std::string recipe = params.value("recipe", "");
            std::string recipe_xml = params.value("recipe_xml", "");  // 跨機：配方內容
            std::string panel = params.value("panel_id", "");
            // recipe_saving / share_flags / align_roi：選填；缺省 = 保留預設值（向下相容）
            {
                std::lock_guard<std::mutex> slk(saving_cfg_mtx_);
                if (params.contains("recipe_saving")) {
                    const auto& rs = params["recipe_saving"];
                    saving_cfg_.max_save_defect_count = rs.value("max_save_defect_count", -1);
                    saving_cfg_.save_defect_width     = rs.value("save_defect_width",     100);
                    saving_cfg_.save_defect_height    = rs.value("save_defect_height",    100);
                    saving_cfg_.max_defect_count_pass = rs.value("max_defect_count_pass", -1);
                }
                if (params.contains("share_flags")) {
                    const auto& sf = params["share_flags"];
                    share_flags_.tuning_recipe    = sf.value("tuning_recipe",    false);
                    share_flags_.save_source_image = sf.value("save_source_image", false);
                }
                // 解析 AlignRoi（每次 LOAD_RECIPE 覆蓋，對位 aligned_* 由 SET_ALIGN 套回）
                {
                    AlignRoiConfig new_align;
                    new_align.align_enable = false;
                    // 從 recipe_xml 的 <M_AlignRoi> 區塊取座標
                    if (!recipe_xml.empty()) {
                        size_t ar_s = recipe_xml.find("<M_AlignRoi>");
                        size_t ar_e = recipe_xml.find("</M_AlignRoi>");
                        if (ar_s != std::string::npos && ar_e != std::string::npos) {
                            std::string ar = recipe_xml.substr(ar_s, ar_e - ar_s + 13);
                            std::string val;
                            if (cs_extract_tag(ar, "AlignEnable", val))
                                new_align.align_enable = (val == "true" || val == "True" || val == "1");
                            int iv;
                            if (cs_tag_int(ar, "ReferX", iv))       new_align.refer_x       = iv;
                            if (cs_tag_int(ar, "ReferY", iv))       new_align.refer_y       = iv;
                            if (cs_tag_int(ar, "SearchWidth", iv))  new_align.search_width  = iv;
                            if (cs_tag_int(ar, "SearchHeight", iv)) new_align.search_height = iv;
                        }
                    }
                    // 解碼 golden PNG（network-clean：Control 傳 base64，IP 在記憶體持有）
                    if (new_align.align_enable) {
                        std::string b64 = params.value("golden_png_base64", "");
                        if (!b64.empty()) {
                            auto bytes = base64_decode(b64);
                            std::vector<uint8_t> buf(bytes.begin(), bytes.end());
                            cv::Mat golden = cv::imdecode(buf, cv::IMREAD_GRAYSCALE);
                            if (!golden.empty()) {
                                new_align.golden = golden;
                                std::cout << "[AlignRoi] golden loaded "
                                          << golden.cols << "×" << golden.rows
                                          << " ReferX=" << new_align.refer_x
                                          << " SearchW=" << new_align.search_width << "\n";
                            } else {
                                std::cerr << "[AlignRoi] golden_png_base64 解碼失敗，對位停用\n";
                                new_align.align_enable = false;
                            }
                        } else {
                            std::cerr << "[AlignRoi] AlignEnable=true 但未收到 golden_png_base64，對位停用\n";
                            new_align.align_enable = false;
                        }
                    }
                    align_roi_cfg_ = new_align;
                }
            }
            std::string err;
            bool ok = load_recipe_ ? load_recipe_(recipe, recipe_xml, panel, err) : false;
            resp["status"] = ok ? "OK" : "ERR";
            if (!ok) resp["error"] = err.empty() ? "no load_recipe handler" : err;
            reply(fd, resp);

        } else if (cmd == "GET_STATUS") {
            resp["status"] = "OK";
            try {
                resp["data"] = status_ ? json::parse(status_()) : json::object();
            } catch (...) { resp["data"] = json::object(); }
            reply(fd, resp);

        } else if (cmd == "CHECK_HEALTH") {
            resp["status"] = "OK";
            resp["ai"] = ai_enabled_;
            reply(fd, resp);

        } else if (cmd == "SEND_IMAGE_STREAM_BEGIN") {
            frame_seq_ = 0;
            resp["status"] = "OK";
            reply(fd, resp);

        } else if (cmd == "SEND_IMAGE_FOR_REVIEW") {
            uint32_t width  = params.value("width", 0u);
            uint32_t height = params.value("height", 0u);
            uint32_t payload_bytes = params.value("payload_bytes", 0u);
            std::string panel = params.value("panel_id", "");
            uint16_t cam_id = (uint16_t)params.value("cam_id", 0);
            uint16_t fseq = (uint16_t)params.value("frame_seq", (int)frame_seq_);
            uint8_t sys_id = (uint8_t)params.value("system_id", 0);
            bool last = params.value("last", false);
            // no_wait=true → 入隊後立即回 ACK（不等 GPU 結果）；用於壓力測試 / 串流場景。
            // 若 queue 滿仍回 ERR（背壓路徑不變）。正常 offline-tcp 用 false（等結果）。
            bool no_wait = params.value("no_wait", false);
            // debug=true → 本次存全部 patch（調參看小圖）；預設 false → 只存結果+overlay 加速。
            review_save_patches_ = params.value("debug", false);

            // 收圖入口驗證①：尺寸/payload 防呆（bogus 尺寸→巨量配置→OOM(cuda_fatal)）。
            constexpr uint32_t kMaxDim = 16384;
            if (width == 0 || height == 0 || width > kMaxDim || height > kMaxDim ||
                payload_bytes == 0 || payload_bytes != width * height) {
                char eb[176];
                std::snprintf(eb, sizeof(eb),
                    "影像尺寸/payload 非法: width=%u height=%u payload_bytes=%u (需 1..%u 且 payload=w*h)",
                    width, height, payload_bytes, kMaxDim);
                diag::FlightRecorder::instance().record_incident("frame_validation",
                    "panel=" + panel + " : " + eb);
                reply(fd, {{"seq", seq}, {"status", "ERR"}, {"error", eb}});
                continue;
            }
            auto t_recv0 = std::chrono::steady_clock::now();
            std::vector<uint8_t> payload;
            if (!rd.read_exact(payload_bytes, payload)) {
                std::cerr << "[ControlServer] 讀取影像 payload 時連線中斷\n";
                break;
            }
            double recv_ms = std::chrono::duration<double, std::milli>(
                std::chrono::steady_clock::now() - t_recv0).count();
            FrameHeader hdr = make_frame_header(panel, cam_id, fseq, width, height,
                                                payload.data(), payload_bytes, sys_id,
                                                /*timestamp*/ 0, last);
            // 收圖入口驗證②：結構（magic/version/CRC32）+ 可選 client 宣告 crc32（offline-tcp 偵測傳輸損壞）。
            // 失敗 → 記 frame_validation incident + 拒收（不 enqueue）。
            std::string verr = validate_frame_header(hdr, payload.data(), payload_bytes);
            if (verr.empty() && params.contains("crc32")) {
                uint32_t declared = (uint32_t)params.value("crc32", 0u);
                uint32_t actual = crc32_ieee(payload.data(), payload_bytes);
                if (declared != actual) {
                    char eb[96];
                    std::snprintf(eb, sizeof(eb),
                        "CRC32 不符 client宣告=0x%08X 實算=0x%08X", declared, actual);
                    verr = eb;
                }
            }
            if (!verr.empty()) {
                diag::FlightRecorder::instance().record_incident("frame_validation",
                    "panel=" + panel + " seq=" + std::to_string(fseq) + " : " + verr);
                reply(fd, {{"seq", seq}, {"status", "ERR"}, {"error", verr}});
                continue;
            }
            // 清掉同 panel 的舊結果，避免讀到上一張的；再 enqueue 並等本次結果。
            { std::lock_guard<std::mutex> lk(result_mtx_); results_.erase(panel); }
            // 背壓：queue 滿時拒收，回 ERR（不阻塞；防 OOM 鐵律）。
            if (!queue_.push(hdr, panel, std::move(payload))) {
                char eb[160];
                std::snprintf(eb, sizeof(eb),
                    "FrameQueue 滿（上限 %zu 幀）：系統繁忙，請稍後重試",
                    queue_.max_size());
                diag::FlightRecorder::instance().record_incident("queue_overflow",
                    std::string("panel=") + panel +
                    " max=" + std::to_string(queue_.max_size()));
                reply(fd, {{"seq", seq}, {"status", "ERR"}, {"error", eb}});
                continue;
            }
            // 水位監控：push 成功後檢查深度（70% WARN console / 90% 磁碟 incident）。
            if (queue_.max_size() > 0) {
                size_t depth = queue_.size();
                size_t cap   = queue_.max_size();
                int pct = (int)(depth * 100 / cap);
                if (pct >= 90) {
                    char wb[160];
                    std::snprintf(wb, sizeof(wb),
                        "FrameQueue 水位 %d%% (%zu/%zu)：消費端嚴重落後，考慮降速",
                        pct, depth, cap);
                    std::cerr << "[WaterLevel] " << wb << "\n";
                    diag::FlightRecorder::instance().record_incident("queue_high_watermark",
                        std::string("panel=") + panel + " " + wb);
                } else if (pct >= 70) {
                    std::cout << "[WaterLevel] WARN " << pct << "% (" << depth << "/" << cap
                              << " 幀) panel=" << panel << "\n";
                }
            }
            ++frame_seq_;
            resp["frame_seq"] = fseq;

            // no_wait 模式：入隊成功後立即回 ACK，不等 GPU 結果（壓力測試 / 串流場景）。
            // queue 滿時仍回 ERR（背壓路徑不變）。
            if (no_wait) {
                resp["status"] = "OK";
                resp["queued"] = true;
                reply(fd, resp);
                continue;
            }

            // 等處理端 deliver_result(panel, json)，把結果經 TCP 回傳（跨機器免共用檔案系統）。
            auto t_proc0 = std::chrono::steady_clock::now();
            std::string result_json;
            {
                std::unique_lock<std::mutex> lk(result_mtx_);
                bool got = result_cv_.wait_for(lk, std::chrono::seconds(60),
                    [&] { return results_.find(panel) != results_.end(); });
                if (got) { result_json = results_[panel]; results_.erase(panel); }
            }
            double proc_ms = std::chrono::duration<double, std::milli>(
                std::chrono::steady_clock::now() - t_proc0).count();
            std::cout << "[T.T] 收圖傳輸 " << (long)recv_ms << "ms ("
                      << (payload_bytes / (1024 * 1024)) << "MB) | 運算+存圖 "
                      << (long)proc_ms << "ms | debug存patch=" << (review_save_patches_ ? "on" : "off")
                      << "\n";
            if (!result_json.empty()) {
                resp["status"] = "OK";
                try { resp["result"] = json::parse(result_json); }
                catch (...) { resp["result_raw"] = result_json; }
            } else {
                resp["status"] = "TIMEOUT";
                resp["error"] = "等不到處理結果（60s）";
            }
            reply(fd, resp);

        } else if (cmd == "LIST_DEFECT_FOLDERS") {
            // 掃 <output>/<yyyyMMdd>/ 下的 panel 夾（= {panelId}_{recipeName}）。
            // date="" → 掃所有日期夾彙整；否則只掃該日期。每筆 folder_name/panel_id + defect_count。
            std::string date = params.value("date", "");
            json folders = json::array();
            std::error_code ec;
            fs::path out(output_dir_);

            std::vector<fs::path> date_dirs;
            if (!date.empty()) {
                date_dirs.push_back(out / date);
            } else if (fs::exists(out, ec)) {
                for (const auto& e : fs::directory_iterator(out, ec)) {
                    if (ec) break;
                    if (e.is_directory() && is_date_dir(e.path().filename().string()))
                        date_dirs.push_back(e.path());
                }
            }
            for (const auto& dd : date_dirs) {
                if (!fs::exists(dd, ec)) continue;
                for (const auto& e : fs::directory_iterator(dd, ec)) {
                    if (ec) break;
                    if (!e.is_directory()) continue;
                    std::string folder = e.path().filename().string();
                    int cnt = read_defect_cnt(e.path());
                    folders.push_back({{"folder_name", folder},
                                       {"panel_id", folder},
                                       {"date", dd.filename().string()},
                                       {"defect_count", cnt}});
                }
            }
            std::sort(folders.begin(), folders.end(), [](const json& a, const json& b) {
                return a["folder_name"].get<std::string>() < b["folder_name"].get<std::string>();
            });
            resp["status"] = "OK";
            resp["folders"] = folders;
            std::cout << "[ControlServer] LIST_DEFECT_FOLDERS date=" << (date.empty() ? "(all)" : date)
                      << " → " << folders.size() << " 筆\n";
            reply(fd, resp);

        } else if (cmd == "SORT_DEFECTS") {
            // 就地把選中 panel 夾的 Defect* 檔複製到 <output>/<output_subdir>/
            //（by_id → 依資料夾名前兩段建子夾；檔名前綴 {folder}_，對齊 legacy）。
            std::string date = params.value("date", "");
            std::string subdir = params.value("output_subdir", "sorted");
            bool by_id = params.value("by_id_folder", false);
            std::vector<std::string> picked;
            if (params.contains("selected_folders"))
                for (const auto& f : params["selected_folders"]) picked.push_back(f.get<std::string>());

            std::error_code ec;
            fs::path out(output_dir_);
            fs::path dst_root = out / subdir;
            fs::create_directories(dst_root, ec);

            json results = json::array();
            int grand_total = 0;
            for (const auto& folder : picked) {
                int copied = 0;
                std::string msg;
                fs::path src = locate_panel(out, date, folder);
                if (src.empty()) {
                    msg = "folder not found";
                } else {
                    fs::path dst_dir = by_id ? (dst_root / id_group(folder)) : dst_root;
                    fs::create_directories(dst_dir, ec);
                    for (const auto& e : fs::directory_iterator(src, ec)) {
                        if (ec) break;
                        if (!e.is_regular_file()) continue;
                        std::string fn = e.path().filename().string();
                        if (fn.rfind("Defect", 0) != 0) continue;   // 只複製 Defect* 缺陷圖
                        std::error_code cec;
                        fs::copy_file(e.path(), dst_dir / (folder + "_" + fn),
                                      fs::copy_options::overwrite_existing, cec);
                        if (!cec) copied++;
                    }
                }
                grand_total += copied;
                results.push_back({{"folder", folder}, {"copied", copied}, {"message", msg}});
            }
            resp["status"] = "OK";
            resp["results"] = results;
            resp["total"] = grand_total;
            resp["output_dir"] = dst_root.string();
            std::cout << "[ControlServer] SORT_DEFECTS subdir=" << subdir << " by_id=" << by_id
                      << " panels=" << picked.size() << " → " << grand_total << " 檔\n";
            reply(fd, resp);

        } else if (cmd == "LIST_DEFECT_PATCHES") {
            // 列出一塊 panel 夾的所有缺陷小圖 metadata（從檔名解析座標/型別，Size 從 ResultInfo.json，
            // current_class 從 classification.json）。patch_id = 檔名。
            std::string date = params.value("date", "");
            std::string folder = params.value("folder_name", "");
            fs::path out(output_dir_);
            fs::path dir = locate_panel(out, date, folder);
            if (dir.empty()) {
                reply(fd, {{"seq", seq}, {"status", "ERR"}, {"error", "folder not found"}});
                continue;
            }
            // ResultInfo.json → (roi,run) -> Size
            std::map<std::pair<int,int>, int> size_map;
            try {
                std::ifstream rf(dir / (folder + "_ResultInfo.json"));
                if (rf) {
                    json rj; rf >> rj;
                    for (const auto& roi : rj.value("RoiInfoList", json::array())) {
                        int ridx = roi.value("RoiIndex", 0);
                        const auto& dl = roi.value("DefectInfoList", json::array());
                        for (const auto& d : dl) {
                            int run = d.value("RunIndex", 0);
                            size_map[{ridx, run}] = d.value("Size", 0);
                        }
                    }
                }
            } catch (...) {}
            json cls = read_classification(dir);

            std::error_code ec;
            json patches = json::array();
            for (const auto& e : fs::directory_iterator(dir, ec)) {
                if (ec) break;
                if (!e.is_regular_file()) continue;
                std::string fn = e.path().filename().string();
                DefectName dn = parse_defect_name(fn);
                if (!dn.ok) continue;
                auto it = size_map.find({dn.roi, dn.run});
                int size = it != size_map.end() ? it->second : 0;
                std::string cur = cls.contains(fn) ? cls[fn].get<std::string>() : "未分類";
                patches.push_back({
                    {"patch_id", fn}, {"patch_filename", fn},
                    {"run_index", dn.run}, {"roi_index", dn.roi},
                    {"GC_X", dn.x}, {"GC_Y", dn.y}, {"Size", size},
                    {"Type", dn.type}, {"current_class", cur},
                });
            }
            std::sort(patches.begin(), patches.end(), [](const json& a, const json& b) {
                if (a["roi_index"] != b["roi_index"]) return a["roi_index"] < b["roi_index"];
                return a["run_index"] < b["run_index"];
            });
            resp["status"] = "OK";
            resp["folder_name"] = folder;
            resp["patches"] = patches;
            std::cout << "[ControlServer] LIST_DEFECT_PATCHES " << folder << " → "
                      << patches.size() << " 張\n";
            reply(fd, resp);

        } else if (cmd == "GET_DEFECT_PATCHES_BATCH") {
            // 批次回傳多張小圖的 PNG bytes（base64）。
            std::string date = params.value("date", "");
            std::string folder = params.value("folder_name", "");
            fs::path dir = locate_panel(fs::path(output_dir_), date, folder);
            if (dir.empty()) {
                reply(fd, {{"seq", seq}, {"status", "ERR"}, {"error", "folder not found"}});
                continue;
            }
            json arr = json::array();
            if (params.contains("patch_ids"))
                for (const auto& pid : params["patch_ids"]) {
                    std::string id = pid.get<std::string>();
                    fs::path p = dir / id;
                    std::error_code ec;
                    if (!fs::exists(p, ec) || !fs::is_regular_file(p, ec)) continue;
                    auto bytes = read_file_bytes(p);
                    if (bytes.empty()) continue;
                    arr.push_back({{"patch_id", id}, {"png_base64", base64_encode(bytes)}});
                }
            resp["status"] = "OK";
            resp["patches"] = arr;
            std::cout << "[ControlServer] GET_DEFECT_PATCHES_BATCH " << folder << " → "
                      << arr.size() << " 張\n";
            reply(fd, resp);

        } else if (cmd == "SAVE_DEFECT_CLASSIFICATION") {
            // 依分類把小圖複製到 {folder}/TrueDefect|Particle/，並更新 classification.json。
            std::string date = params.value("date", "");
            std::string folder = params.value("folder_name", "");
            fs::path dir = locate_panel(fs::path(output_dir_), date, folder);
            if (dir.empty()) {
                reply(fd, {{"seq", seq}, {"status", "ERR"}, {"error", "folder not found"}});
                continue;
            }
            json cls = read_classification(dir);   // 合併既有分類
            std::error_code ec;
            int n_true = 0, n_part = 0;
            if (params.contains("classifications"))
                for (const auto& c : params["classifications"]) {
                    std::string id = c.value("patch_id", "");
                    std::string klass = c.value("class", "");
                    if (klass != "TrueDefect" && klass != "Particle") continue;
                    fs::path src = dir / id;
                    if (!fs::exists(src, ec)) continue;
                    fs::path sub = dir / klass;
                    fs::create_directories(sub, ec);
                    std::error_code cec;
                    fs::copy_file(src, sub / id, fs::copy_options::overwrite_existing, cec);
                    if (cec) continue;
                    cls[id] = klass;
                    if (klass == "TrueDefect") ++n_true; else ++n_part;
                }
            try {
                std::ofstream of(dir / "classification.json");
                of << cls.dump(2);
            } catch (...) {}
            resp["status"] = "OK";
            resp["TrueDefect"] = n_true;
            resp["Particle"] = n_part;
            resp["total"] = n_true + n_part;
            resp["output_dir"] = dir.string();
            std::cout << "[ControlServer] SAVE_DEFECT_CLASSIFICATION " << folder
                      << " → TrueDefect=" << n_true << " Particle=" << n_part << "\n";
            reply(fd, resp);

        } else if (cmd == "SET_ALIGN") {
            // SET_ALIGN：套回 ShiftX/Y 到所有 zones（由 main.cpp 註冊的 set_align_ callback 執行）。
            // 每片一次：CF_GRAB_START → CF_CHECK_ALIGN → CF_SET_ALIGN → SEND_IMAGE（套回後偵測）。
            double shift_x = params.value("shift_x", 0.0);
            double shift_y = params.value("shift_y", 0.0);
            if (set_align_) {
                set_align_(shift_x, shift_y);
            }
            resp["status"] = "OK";
            std::cout << "[SetAlign] ShiftX=" << shift_x << " ShiftY=" << shift_y << "\n";
            reply(fd, resp);

        } else if (cmd == "CHECK_ALIGN") {
            // CHECK_ALIGN：收搜尋 ROI（Control 端預裁，~250KB），跑 align_engine，回 ShiftX/Y。
            // 失敗（score < threshold）→ 回 ERR（釘點 3：由上位機決策，IP/Control 不自行繼續）。
            uint32_t width        = params.value("width",        0u);
            uint32_t height       = params.value("height",       0u);
            uint32_t payload_bytes = params.value("payload_bytes", 0u);
            std::string panel     = params.value("panel_id", "");

            if (width == 0 || height == 0 || payload_bytes == 0 || payload_bytes != width * height) {
                char eb[128];
                std::snprintf(eb, sizeof(eb),
                    "CHECK_ALIGN: invalid ROI size w=%u h=%u payload=%u", width, height, payload_bytes);
                reply(fd, {{"seq", seq}, {"status", "ERR"}, {"error", eb}});
                continue;
            }
            std::vector<uint8_t> roi_payload;
            if (!rd.read_exact(payload_bytes, roi_payload)) {
                std::cerr << "[ControlServer] CHECK_ALIGN: 讀取 ROI payload 時連線中斷\n";
                break;
            }

            AlignRoiConfig align_cfg;
            { std::lock_guard<std::mutex> lk(saving_cfg_mtx_); align_cfg = align_roi_cfg_; }

            if (!align_cfg.align_enable) {
                // AlignEnable=false → 不對位，直接回 ShiftX=0（不計為失敗）
                resp["status"]    = "OK";
                resp["shift_x"]   = 0.0;
                resp["shift_y"]   = 0.0;
                resp["score"]     = 1.0;
                resp["angle_deg"] = 0.0;
                resp["skipped"]   = true;
                reply(fd, resp);
                continue;
            }

            // 執行對位
            cv::Mat search_roi(height, width, CV_8UC1, roi_payload.data());
            AlignResult ar = run_align(search_roi, align_cfg);

            if (!ar.ok) {
                // 釘點 3：失敗回 ERR + score，由上位機決策
                resp["status"] = "ERR";
                resp["error"]  = ar.error_msg;
                resp["score"]  = ar.score;
                std::cerr << "[CheckAlign] FAIL panel=" << panel << " " << ar.error_msg << "\n";
            } else {
                resp["status"]    = "OK";
                resp["shift_x"]   = ar.shift_x;
                resp["shift_y"]   = ar.shift_y;
                resp["score"]     = ar.score;
                resp["angle_deg"] = ar.angle_deg;
                std::cout << "[CheckAlign] OK panel=" << panel
                          << " ShiftX=" << ar.shift_x << " ShiftY=" << ar.shift_y
                          << " score=" << ar.score << " angle=" << ar.angle_deg << "°\n";
            }
            reply(fd, resp);

        } else {
            resp["status"] = "ERR";
            resp["error"] = "unknown cmd: " + cmd;
            reply(fd, resp);
        }
    }
}
