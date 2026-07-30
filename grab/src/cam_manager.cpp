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
        warn = "找不到 " + path + "：cam_id 退回列舉順序暫派，**重插拔/加減相機後會對到別台**"
               "（cam_config.json 的曝光增益、FrameHeader.camId、IP 端輸出夾 CCD{camId} 皆受影響）。"
               "正式陣列請由 cam_map.example.json 複製並填實際 MAC。";
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
    printf("[cam_manager] cam_map 已載入：%zu 筆 MAC↔cam_id 綁定（嚴格模式：未列於映射的相機將拒開）\n",
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

void CamManager::annotate(std::vector<CamInfo>& infos) const {
    for (size_t i = 0; i < infos.size(); ++i) {
        auto it = mac_map_.find(normalize_mac(infos[i].mac));
        if (it != mac_map_.end()) {
            infos[i].cam_id = it->second.cam_id;
            infos[i].ccd_id = it->second.ccd_id;
            infos[i].bound  = true;
        } else {
            // 無映射/未列於映射：維持列舉 index，但誠實標 bound=false（不假裝已綁定）
            infos[i].cam_id = (int)i;
            infos[i].ccd_id.clear();
            infos[i].bound  = false;
        }
    }
}

bool CamManager::open_all(int want, const std::string& cli_serial,
                          int64_t pkt_size, std::string& err) {
    if (!cams_.empty()) {
        // ARM 冪等：台數符合需求（或 ALL）直接重用已開陣列（每片重 ARM 零冷啟成本）；
        // 台數需求改變才關掉重開。重插拔相機請先 GRAB_STOP 再 ARM。
        if (want <= 0 || cams_.size() == (size_t)want) return true;
        stop_all();
    }

    // 無映射 + 單台：沿用舊語意（auto/指定序號），不需先列舉（保留 legacy 快路徑）。
    // 有映射時即使單台也要列舉，才拿得到 MAC 來查真正的 cam_id。
    if (!has_map() && want == 1) {
        Entry e;
        e.cam = std::make_unique<CamPylon>();
        e.cam_id = 0;
        e.serial = cli_serial;
        if (!e.cam->open(cli_serial, pkt_size)) {
            err = "pylon open failed (serial=" + cli_serial + ")";
            return false;
        }
        cams_.push_back(std::move(e));
        return true;
    }

    auto infos = CamPylon::enumerate_cameras();
    if (infos.empty()) { err = "enumerate 找不到任何相機"; return false; }

    // 決定「開哪些、各自的 cam_id 是多少」
    struct Pick { uint16_t cam_id; std::string serial, mac, ccd_id; };
    std::vector<Pick> picks;

    if (has_map()) {
        // ── 嚴格模式：每台都必須在 cam_map.json 裡有綁定 ────────────────────────
        std::vector<std::string> unmapped;
        for (const auto& ci : infos) {
            auto it = mac_map_.find(normalize_mac(ci.mac));
            if (it == mac_map_.end()) {
                unmapped.push_back((ci.mac.empty() ? std::string("(無MAC)") : ci.mac) +
                                   " SN=" + ci.serial + " " + ci.model);
                continue;
            }
            picks.push_back({it->second.cam_id, ci.serial, ci.mac, it->second.ccd_id});
        }
        if (!unmapped.empty()) {
            // 不默默以列舉順序暫派：未知相機一旦頂用某個 CCD 槽位，配方/曝光/座標全會錯配
            err = "下列相機未列於 cam_map.json，拒絕暫派槽位（請補進映射或移除該相機）：";
            for (size_t k = 0; k < unmapped.size(); ++k) err += (k ? "、" : "") + unmapped[k];
            return false;
        }
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
        // ── 無映射：舊行為（列舉順序暫派）+ 明確警告 ──────────────────────────
        for (size_t i = 0; i < infos.size(); ++i)
            picks.push_back({(uint16_t)i, infos[i].serial, infos[i].mac, std::string()});
        fprintf(stderr,
                "[cam_manager] ⚠ 無 cam_map.json → cam_id 依列舉順序暫派 0..N-1；"
                "重插拔或加減相機後會對到別台（Gap #21）。正式陣列請建立 cam_map.json。\n");
    }

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
        if (!e.cam->open(e.serial, pkt_size)) {
            err = "cam" + std::to_string(e.cam_id) + " (SN=" + e.serial + ") open 失敗";
            stop_all();                     // fail-fast：不留半開陣列
            return false;
        }
        cams_.push_back(std::move(e));
    }
    printf("[cam_manager] 開啟 %zu/%zu 台相機（want=%d，cam_id 來源=%s）\n",
           cams_.size(), infos.size(), want, has_map() ? "cam_map.json(MAC)" : "列舉順序(暫派)");
    for (const auto& e : cams_)
        printf("[cam_manager]   cam%u%s%s  SN=%s  MAC=%s\n",
               e.cam_id, e.ccd_id.empty() ? "" : " = ", e.ccd_id.c_str(),
               e.serial.c_str(), e.mac.empty() ? "-" : e.mac.c_str());
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
    if (!e.cam->open(cli_serial, pkt_size)) return nullptr;
    cams_.push_back(std::move(e));
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
