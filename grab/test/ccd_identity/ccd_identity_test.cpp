// ─────────────────────────────────────────────────────────────────────────────
// CCD 身分解析測試 — CamManager::resolve()（DeviceUserID 優先 > cam_map MAC > 未綁定）
//
// 純邏輯、不需相機：直接餵合成的 CamInfo 列舉結果。借用 b1 的 pylon stub 只為了讓
// cam_manager.cpp / cam_pylon.cpp 能連結（本測試不呼叫任何 pylon 路徑）。
// 真相機的 DeviceUserID 讀取（CDeviceInfo::GetUserDefinedName）仍需 damac 驗。
// ─────────────────────────────────────────────────────────────────────────────
#include "cam_manager.h"

#include <cstdio>
#include <fstream>
#include <string>
#include <unistd.h>

static int g_fail = 0;

static void check(bool ok, const std::string& name, const std::string& detail = "") {
    printf("  %s  %s%s\n", ok ? "PASS" : "FAIL", name.c_str(),
           detail.empty() ? "" : ("  [" + detail + "]").c_str());
    if (!ok) ++g_fail;
}

static CamInfo cam(const std::string& sn, const std::string& mac, const std::string& uid) {
    CamInfo c;
    c.serial = sn;
    c.mac = mac;
    c.user_id = uid;
    return c;
}

static std::string write_map(const std::string& body) {
    const std::string path = "/tmp/ccd_identity_test_" + std::to_string(getpid()) + ".json";
    std::ofstream(path) << body;
    return path;
}

int main() {
    printf("== parse_ccd_name\n");
    {
        uint16_t id = 99;
        check(CamManager::parse_ccd_name("CCD00", id) && id == 0, "CCD00 → 0");
        check(CamManager::parse_ccd_name("CCD36", id) && id == 36, "CCD36 → 36");
        check(!CamManager::parse_ccd_name("", id), "空字串拒絕");
        check(!CamManager::parse_ccd_name("CCD1", id), "一位數拒絕");
        check(!CamManager::parse_ccd_name("CCD001", id), "三位數拒絕");
        check(!CamManager::parse_ccd_name("ccd01", id), "小寫拒絕");
        check(!CamManager::parse_ccd_name("CCD0A", id), "非數字拒絕");
    }

    printf("== 無 UserID、無映射 → 非嚴格（舊行為）\n");
    {
        CamManager m;
        std::vector<CamInfo> v{cam("A", "00:30:53:00:00:01", ""), cam("B", "00:30:53:00:00:02", "")};
        std::string err;
        check(m.resolve(v, err), "回 true", err);
        check(!v[0].bound && !v[1].bound, "皆 bound=false");
        check(v[0].cam_id == 0 && v[1].cam_id == 1, "cam_id = 列舉 index");
    }

    printf("== UserID 決定身分（與列舉順序無關）\n");
    {
        CamManager m;
        std::vector<CamInfo> v{cam("A", "m1", "CCD03"), cam("B", "m2", "CCD00"),
                               cam("C", "m3", "CCD02"), cam("D", "m4", "CCD01")};
        std::string err;
        check(m.resolve(v, err), "回 true", err);
        check(v[0].cam_id == 3 && v[1].cam_id == 0 && v[2].cam_id == 2 && v[3].cam_id == 1,
              "cam_id 取自 UserID");
        check(v[0].ccd_id == "CCD03" && v[0].bind_source == "user_id", "ccd_id/bind_source");
    }

    printf("== 換相機：新機 UserID 設好即接手槽位（MAC 不同也不影響）\n");
    {
        CamManager m;
        std::vector<CamInfo> v{cam("NEW", "00:30:53:ff:ff:ff", "CCD02")};
        std::string err;
        check(m.resolve(v, err) && v[0].cam_id == 2, "新機 → cam2", err);
    }

    printf("== 嚴格：有一台已命名、另一台未命名 → 拒開\n");
    {
        CamManager m;
        std::vector<CamInfo> v{cam("A", "m1", "CCD00"), cam("NEW", "m9", "")};
        std::string err;
        check(!m.resolve(v, err), "回 false");
        check(err.find("SN=NEW") != std::string::npos, "錯誤訊息點名未命名那台", err);
        check(v[0].bound && !v[1].bound, "已命名者仍標 bound，未命名者 bound=false");
    }

    printf("== 嚴格：UserID 格式錯誤（打錯字）→ 拒開並顯示該名稱\n");
    {
        CamManager m;
        std::vector<CamInfo> v{cam("A", "m1", "CCD00"), cam("B", "m2", "CCD1")};
        std::string err;
        check(!m.resolve(v, err), "回 false");
        check(err.find("CCD1") != std::string::npos, "錯誤訊息帶出錯誤名稱", err);
    }

    printf("== 嚴格：UserID 重複（舊機沒改名就接回）→ 拒開\n");
    {
        CamManager m;
        std::vector<CamInfo> v{cam("OLD", "m1", "CCD01"), cam("NEW", "m2", "CCD01")};
        std::string err;
        check(!m.resolve(v, err), "回 false");
        check(err.find("SN=OLD") != std::string::npos && err.find("SN=NEW") != std::string::npos,
              "錯誤訊息點名兩台", err);
    }

    printf("== cam_map 備援 + 混用\n");
    {
        CamManager m;
        const std::string p = write_map(
            R"({"cameras":[{"mac":"00:30:53:00:00:01","cam_id":5,"ccd_id":"CCD05"},)"
            R"({"mac":"00:30:53:00:00:02","cam_id":7,"ccd_id":"CCD07"}]})");
        std::string err, warn;
        check(m.load_map(p, err, warn) && m.has_map(), "load_map ok", err);

        std::vector<CamInfo> v{cam("A", "003053000001", ""), cam("B", "m2", "CCD00")};
        err.clear();
        check(m.resolve(v, err), "UserID + MAC 混用回 true", err);
        check(v[0].cam_id == 5 && v[0].bind_source == "mac" && v[0].ccd_id == "CCD05", "無 UserID → MAC 映射");
        check(v[1].cam_id == 0 && v[1].bind_source == "user_id", "有 UserID → UserID");

        std::vector<CamInfo> c{cam("X", "00:30:53:00:00:02", "CCD03")};
        err.clear();
        check(m.resolve(c, err) && c[0].cam_id == 3 && c[0].bind_source == "user_id",
              "UserID 與 MAC 映射衝突 → UserID 優先", err);

        std::vector<CamInfo> d{cam("M", "00:30:53:00:00:02", ""), cam("U", "m3", "CCD07")};
        err.clear();
        check(!m.resolve(d, err), "MAC 映射與 UserID 撞同一 cam_id → 拒開", err);

        std::vector<CamInfo> u{cam("Z", "00:30:53:00:00:99", "")};
        err.clear();
        check(!m.resolve(u, err), "有映射檔但相機不在映射、也無 UserID → 拒開（原嚴格模式不變）");
        std::remove(p.c_str());
    }

    printf("== annotate 不回報錯誤但照樣填欄位\n");
    {
        CamManager m;
        std::vector<CamInfo> v{cam("A", "m1", "CCD04"), cam("B", "m2", "")};
        m.annotate(v);
        check(v[0].bound && v[0].cam_id == 4 && !v[1].bound && v[1].cam_id == 1, "欄位正確");
    }

    printf(g_fail ? "\n%d 項失敗\n" : "\n全數通過\n", g_fail);
    return g_fail ? 1 : 0;
}
