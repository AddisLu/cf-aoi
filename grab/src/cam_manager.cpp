#include "cam_manager.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <fstream>
#include <set>

#include <nlohmann/json.hpp>
using json = nlohmann::json;

// ---------------------------------------------------------------------------
// Gap #21：MAC ↔ cam_id 穩定映射
// ---------------------------------------------------------------------------
std::string CamManager::normalize_mac(const std::string& s) {
    std::string out;
    out.reserve(12);
    for (char c : s) {
        if (c == ':' || c == '-' || c == '.' || c == ' ') continue;
        out += (char)std::toupper((unsigned char)c);
    }
    return out;
}

bool CamManager::load_map(const std::string& path, std::string& err, std::string& warn) {
    mac_map_.clear();

    std::ifstream f(path);
    if (!f) {
        warn = "找不到 " + path + "：CCD 身分僅由相機 DeviceUserID（CCDnn）決定；"
               "相機未設 UserID 時 cam_id 退回列舉順序暫派，**重插拔/加減相機後會對到別台**。";
        return true;      // 沒有檔案 = 合法的舊行為，不算錯誤
    }

    json j;
    try {
        f >> j;
    } catch (const std::exception& e) {
        err = path + " 解析失敗：" + e.what();
        return false;
    }
    if (!j.contains("cameras") || !j["cameras"].is_array()) {
        err = path + " 缺少 cameras 陣列";
        return false;
    }

    std::set<uint16_t>    seen_id;
    for (const auto& c : j["cameras"]) {
        if (!c.contains("mac") || !c.contains("cam_id")) {
            err = path + " 有條目缺少 mac 或 cam_id";
            return false;
        }
        const std::string key = normalize_mac(c["mac"].get<std::string>());
        if (key.size() != 12) {
            err = path + " MAC 格式不正確：" + c["mac"].get<std::string>() + "（正規化後應為 12 碼 hex）";
            return false;
        }
        const int id = c["cam_id"].get<int>();
        if (id < 0 || id > 65535) {
            err = path + " cam_id 超出範圍：" + std::to_string(id);
            return false;
        }
        if (!mac_map_.emplace(key, MacBinding{(uint16_t)id,
                                              c.value("ccd_id", std::string())}).second) {
            err = path + " MAC 重複：" + c["mac"].get<std::string>();
            mac_map_.clear();
            return false;
        }
        if (!seen_id.insert((uint16_t)id).second) {
            err = path + " cam_id 重複：" + std::to_string(id) + "（一個槽位只能綁一台）";
            mac_map_.clear();
            return false;
        }
    }
    if (mac_map_.empty()) {
        warn = path + " 的 cameras 為空 → 等同無映射，cam_id 退回列舉順序暫派。";
        return true;
    }
    printf("[cam_manager] cam_map 已載入：%zu 筆 MAC↔cam_id 備援綁定（DeviceUserID 優先；無身分的相機將拒開）\n",
           mac_map_.size());
    return true;
}

bool CamManager::write_map(const std::string& path, const std::string& entries_json,
                           std::string& err) {
    // ── ① 先組出完整檔案內容（帶說明，讓人直接看檔也懂）──────────────────────
    json entries;
    try {
        entries = json::parse(entries_json);
    } catch (const std::exception& e) {
        err = std::string("entries JSON 解析失敗：") + e.what();
        return false;
    }
    if (!entries.is_array()) { err = "entries 必須是陣列"; return false; }

    json doc;
    doc["_comment"] = "MAC ↔ cam_id（CCD 槽位）穩定映射 — Gap #21。由 Control 的 SET_CAM_MAP 寫入。";
    doc["_note"]    = "手動編輯亦可；格式與規則見 cam_map.example.json。cam_id/MAC 皆不可重複。";
    doc["cameras"]  = entries;

    // ── ② 寫暫存檔 → **用 load_map 自己驗一次** → 過了才 rename ────────────────
    // 為什麼要這樣：驗證規則只有一份（load_map），不會出現「寫得進去但下次開機載不起來」。
    const std::string tmp = path + ".tmp";
    {
        std::ofstream f(tmp, std::ios::trunc);
        if (!f) { err = "無法寫入暫存檔 " + tmp; return false; }
        f << doc.dump(2) << "\n";
        if (!f.good()) { err = "寫入暫存檔失敗 " + tmp; return false; }
    }

    // 用一個暫時的 CamManager 驗（不動自己的 mac_map_，驗失敗時現況完全不受影響）
    {
        CamManager probe;
        std::string verr, vwarn;
        if (!probe.load_map(tmp, verr, vwarn)) {
            std::remove(tmp.c_str());
            err = "新映射未通過驗證（已放棄，原檔不動）：" + verr;
            return false;
        }
        if (!probe.has_map()) {
            std::remove(tmp.c_str());
            err = "新映射為空（至少要有一筆；若要停用映射請直接刪檔）";
            return false;
        }
    }

    // ── ③ 備份原檔（有的話）→ rename 上線（rename 為原子操作，不會留半寫的檔）──
    {
        std::ifstream cur(path);
        if (cur.good()) {
            cur.close();
            std::remove((path + ".bak").c_str());
            std::rename(path.c_str(), (path + ".bak").c_str());   // 失敗不阻斷，只是少一份備份
        }
    }
    if (std::rename(tmp.c_str(), path.c_str()) != 0) {
        std::remove(tmp.c_str());
        err = "rename 失敗，映射未更新：" + path;
        return false;
    }

    // ── ④ 重載進本實例（此後 open_all / annotate 立即使用新映射）─────────────
    std::string lerr, lwarn;
    if (!load_map(path, lerr, lwarn)) {
        err = "已寫檔但重載失敗（下次啟動會中止，請檢查 " + path + "）：" + lerr;
        return false;
    }
    printf("[cam_manager] cam_map 已更新：%zu 筆（備份 %s.bak）\n", mac_map_.size(), path.c_str());
    return true;
}

bool CamManager::parse_ccd_name(const std::string& s, uint16_t& cam_id) {
    if (s.size() != 5 || s.compare(0, 3, "CCD") != 0) return false;
    if (!std::isdigit((unsigned char)s[3]) || !std::isdigit((unsigned char)s[4])) return false;
    cam_id = (uint16_t)((s[3] - '0') * 10 + (s[4] - '0'));
    return true;
}

bool CamManager::resolve(std::vector<CamInfo>& infos, std::string& err) const {
    bool strict = has_map();
    std::vector<std::string> unbound;
    for (size_t i = 0; i < infos.size(); ++i) {
        CamInfo& ci = infos[i];
        uint16_t uid = 0;
        auto it = mac_map_.find(normalize_mac(ci.mac));
        if (parse_ccd_name(ci.user_id, uid)) {
            // ① 相機自帶身分（權威）
            strict = true;
            ci.cam_id = uid;
            ci.ccd_id = ci.user_id;
            ci.bound = true;
            ci.bind_source = "user_id";
            if (it != mac_map_.end() && it->second.cam_id != uid)
                fprintf(stderr, "[cam_manager] ⚠ SN=%s DeviceUserID=%s 與 cam_map（MAC→cam%u）不一致，"
                                "以 DeviceUserID 為準；請更新或刪除 cam_map.json 該筆\n",
                        ci.serial.c_str(), ci.user_id.c_str(), it->second.cam_id);
        } else if (it != mac_map_.end()) {
            // ② MAC 映射（過渡/備援）
            ci.cam_id = it->second.cam_id;
            ci.ccd_id = it->second.ccd_id;
            ci.bound = true;
            ci.bind_source = "mac";
        } else {
            // 未綁定：維持列舉 index，但誠實標 bound=false（不假裝已就位）
            ci.cam_id = (int)i;
            ci.ccd_id.clear();
            ci.bound = false;
            ci.bind_source.clear();
            unbound.push_back((ci.mac.empty() ? std::string("(無MAC)") : ci.mac) + " SN=" + ci.serial +
                              (ci.user_id.empty() ? "" : " UserID=\"" + ci.user_id + "\"（非 CCDnn 格式）"));
        }
    }
    if (!strict) return true;

    std::vector<std::string> problems;
    if (!unbound.empty()) {
        // 不默默以列舉順序暫派：未知相機一旦頂用某個 CCD 槽位，配方/曝光/座標全會錯配
        std::string m = "下列相機沒有 CCD 身分（請在 pylon Viewer 設 Device User ID = CCDnn，"
                        "或補進 cam_map.json）：";
        for (size_t k = 0; k < unbound.size(); ++k) m += (k ? "、" : "") + unbound[k];
        problems.push_back(m);
    }
    std::map<int, std::string> seen;
    for (const auto& ci : infos) {
        if (!ci.bound) continue;
        auto ins = seen.emplace(ci.cam_id, ci.serial);
        if (!ins.second)
            problems.push_back("cam_id " + std::to_string(ci.cam_id) + "（" + ci.ccd_id + "）重複：SN=" +
                               ins.first->second + " 與 SN=" + ci.serial + "（換相機後舊機請改名或移除）");
    }
    if (problems.empty()) return true;
    err.clear();
    for (size_t k = 0; k < problems.size(); ++k) err += (k ? "；" : "") + problems[k];
    return false;
}

void CamManager::annotate(std::vector<CamInfo>& infos) const {
    std::string ignored;
    resolve(infos, ignored);
}

bool CamManager::open_all(int want, const std::string& cli_serial,
                          int64_t pkt_size, std::string& err) {
    if (!cams_.empty()) {
        // ⚠️ 2026-07-30 實機抓到的靜默失效：
        //   idle 調參路徑（GET_CAM_NODES / TUNE_MEAN）會經 get_or_open_primary 開「一台」進 cams_。
        //   舊碼在 want<=0（ALL）時**無條件重用** cams_ → 之後的 GRAB_ARM 回 OK、armed=true，
        //   但實際只有那一台在取像（實測 CHECK_HEALTH cams=1）。37 台時等於整片面板少 36 顆 CCD，
        //   且沒有任何錯誤訊息。→ 只要目前這組是 idle 路徑開的，一律關掉重開。
        if (primary_only_) {
            stop_all();
        } else {
            // ARM 冪等：台數符合需求（或 ALL）直接重用已開陣列（每片重 ARM 零冷啟成本）；
            // 台數需求改變才關掉重開。重插拔相機請先 GRAB_STOP 再 ARM。
            if (want <= 0 || cams_.size() == (size_t)want) return true;
            stop_all();
        }
    }
    primary_only_ = false;   // 以下為正式開陣列路徑

    auto infos = CamPylon::enumerate_cameras();
    if (infos.empty()) { err = "enumerate 找不到任何相機"; return false; }

    std::string rerr;
    if (!resolve(infos, rerr)) { err = rerr; return false; }
    bool strict = false;
    for (const auto& ci : infos) strict = strict || ci.bound;

    // 非嚴格 + 單台：沿用舊語意（auto/指定序號）。
    if (!strict && want == 1) {
        Entry e;
        e.cam = std::make_unique<CamPylon>();
        e.cam_id = 0;
        e.serial = cli_serial;
        if (!e.cam->open(cli_serial, pkt_size, roi_, line_rate_hz_)) {
            err = "pylon open failed (serial=" + cli_serial + ")";
            return false;
        }
        cams_.push_back(std::move(e));
        return true;
    }

    // 決定「開哪些、各自的 cam_id 是多少」
    struct Pick { uint16_t cam_id; std::string serial, mac, ccd_id, source; };
    std::vector<Pick> picks;
    for (const auto& ci : infos)
        picks.push_back({(uint16_t)ci.cam_id, ci.serial, ci.mac, ci.ccd_id, ci.bind_source});

    if (strict) {
        // 依 cam_id 由小到大（--cam-count N 取前 N 台時才是決定性的，不隨列舉順序飄）
        std::sort(picks.begin(), picks.end(),
                  [](const Pick& a, const Pick& b) { return a.cam_id < b.cam_id; });
        // 單台且指定序號 → 只留該台（--serial 語意不變）
        if (want == 1 && !cli_serial.empty() && cli_serial != "auto") {
            picks.erase(std::remove_if(picks.begin(), picks.end(),
                                       [&](const Pick& p) { return p.serial != cli_serial; }),
                        picks.end());
            if (picks.empty()) { err = "列舉中找不到序號 " + cli_serial; return false; }
        }
    } else {
        // ── 無任何身分來源：舊行為（列舉順序暫派）+ 明確警告 ──────────────────
        fprintf(stderr,
                "[cam_manager] ⚠ 相機皆無 DeviceUserID（CCDnn）且無 cam_map.json → cam_id 依列舉順序暫派 0..N-1；"
                "重插拔或加減相機後會對到別台。請在 pylon Viewer 設 Device User ID。\n");
    }

    // ⚠️ 已知限制（docs/code_review_20260802.md B4）：want<=0（ALL）無「應到幾台」基準——
    //   即使 cam_map 有 6 筆、只列舉到 5 台也照開照回 OK（fail-fast 只保護 want>0 的路徑）。
    size_t n = (want <= 0) ? picks.size() : std::min<size_t>((size_t)want, picks.size());
    if (want > 0 && picks.size() < (size_t)want) {
        err = "可用相機 " + std::to_string(picks.size()) + " 台 < 要求 " + std::to_string(want) + " 台";
        return false;
    }

    for (size_t i = 0; i < n; ++i) {
        Entry e;
        e.cam = std::make_unique<CamPylon>();
        e.cam_id = picks[i].cam_id;
        e.serial = picks[i].serial;
        e.mac    = picks[i].mac;
        e.ccd_id = picks[i].ccd_id;
        e.bind_source = picks[i].source;
        if (!e.cam->open(e.serial, pkt_size, roi_, line_rate_hz_)) {
            err = "cam" + std::to_string(e.cam_id) + " (SN=" + e.serial + ") open 失敗";
            stop_all();                     // fail-fast：不留半開陣列
            return false;
        }
        cams_.push_back(std::move(e));
    }
    printf("[cam_manager] 開啟 %zu/%zu 台相機（want=%d）\n", cams_.size(), infos.size(), want);
    for (const auto& e : cams_)
        printf("[cam_manager]   cam%u%s%s  SN=%s  MAC=%s  來源=%s\n",
               e.cam_id, e.ccd_id.empty() ? "" : " = ", e.ccd_id.c_str(),
               e.serial.c_str(), e.mac.empty() ? "-" : e.mac.c_str(),
               e.bind_source.empty() ? "列舉順序(暫派)" : e.bind_source.c_str());
    return true;
}

void CamManager::start_all(uint64_t max_frames_per_cam, FrameCb cb) {
    for (auto& e : cams_) {
        e.cam->set_frame_callback(cb);      // 同一 cb，多 thread 併發呼叫（呼叫端 thread-safe）
        e.cam->set_max_frames(max_frames_per_cam);
        e.cam->start(e.cam_id);             // 逐台 arm；skew 由 IP 端玻璃前緣對位吸收
    }
    printf("[cam_manager] 啟動 %zu 台（每台 %llu 張%s）\n",
           cams_.size(), (unsigned long long)max_frames_per_cam,
           max_frames_per_cam == 0 ? "，連續" : "自動停");
}

void CamManager::stop_all() {
    for (auto& e : cams_) if (e.cam) e.cam->stop();
    cams_.clear();
    primary_only_ = false;
}

CamPylon* CamManager::get(int cam_id) {
    for (auto& e : cams_)
        if ((int)e.cam_id == cam_id) return e.cam.get();
    return nullptr;
}

CamPylon* CamManager::get_or_open_primary(const std::string& cli_serial, int64_t pkt_size) {
    if (!cams_.empty()) return cams_.front().cam.get();
    Entry e;
    e.cam = std::make_unique<CamPylon>();
    e.cam_id = 0;
    e.serial = cli_serial;
    if (!e.cam->open(cli_serial, pkt_size, roi_, line_rate_hz_)) return nullptr;
    cams_.push_back(std::move(e));
    // 標記「這組是 idle 調參路徑開的、不是完整陣列」→ 下次 open_all 必須重開，
    // 否則 GRAB_ARM(ALL) 會把這一台當成整個陣列（靜默少台，見 open_all 註解）。
    primary_only_ = true;
    return cams_.front().cam.get();
}

int64_t CamManager::max_payload() const {
    int64_t m = 0;
    for (const auto& e : cams_) m = std::max(m, e.cam->payload_size());
    return m;
}

uint64_t CamManager::total_grabbed() const {
    uint64_t s = 0;
    for (const auto& e : cams_) s += e.cam->grabbed();
    return s;
}

uint64_t CamManager::total_dropped() const {
    uint64_t s = 0;
    for (const auto& e : cams_) s += e.cam->dropped();
    return s;
}

size_t CamManager::running_count() const {
    size_t n = 0;
    for (const auto& e : cams_) if (e.cam->is_running()) ++n;
    return n;
}

// B1：故障台彙總。故障後該台 is_running()==false，與「收滿自動停」外觀相同 →
// 必須靠 is_faulted() 區分，否則斷線會被當成正常收完（靜默假完成）。
size_t CamManager::faulted_count() const {
    size_t n = 0;
    for (const auto& e : cams_) if (e.cam->is_faulted()) ++n;
    return n;
}

std::vector<CamManager::Fault> CamManager::faults() const {
    std::vector<Fault> out;
    for (const auto& e : cams_) {
        if (!e.cam->is_faulted()) continue;
        out.push_back(Fault{e.cam_id, e.ccd_id, e.cam->fault_message()});
    }
    return out;
}
