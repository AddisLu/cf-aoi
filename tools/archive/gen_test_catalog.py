#!/usr/bin/env python3
"""
gen_test_catalog.py — 產生「unit test / 驗證腳本 檔案說明」（Markdown + JSON），給人看也給 RAG 收錄。

說明文字直接取自每支測試的檔頭註解（改測試時順手改檔頭 = 說明自動更新）；
執行方式、需要的硬體、跑在哪台由下方 CATALOG 人工維護。
repo 裡看起來像測試、卻不在 CATALOG 的檔案 → 列在「未分類」並 exit 1（--check），避免目錄悄悄過期。

用法：
  gen_test_catalog.py --out <目錄>        寫 <目錄>/tests_catalog.md + tests_catalog.json
  gen_test_catalog.py --check            只檢查有沒有未分類的測試檔（CI / bootstrap 用）
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# kind：unit=純邏輯、不需硬體 / sim=軟體模擬裝置 / e2e=需要程式在跑（offline-tcp 等）/ hw=需要實機
# host：grab / spark / control / any
CATALOG = [
    # ── grab（C++）──
    ("grab/test/b1_fault_containment/b1_fault_test.cpp", "unit", "grab",
     "tools/grab_setup/verify_grab_host.sh（第 6 節自動編譯執行）"),
    ("grab/test/stitch/stitch_test.cpp", "unit", "grab", "tools/grab_setup/verify_grab_host.sh"),
    ("grab/test/ccd_identity/ccd_identity_test.cpp", "unit", "grab", "tools/grab_setup/verify_grab_host.sh"),
    ("grab/test/ebus_sim/ebus_sim.cpp", "sim", "grab",
     "source /opt/pleora/ebus/*/bin/set_puregev_env.sh; grab/build/ebus_sim <網卡>"),
    ("grab/test/ebus_sim/ebus_frame_check.cpp", "sim", "grab",
     "CFAOI_EBUS_NO_SERIAL=1 grab/build/ebus_frame_check <裝置IP> 20"),
    ("grab/test/ebus_sim/run_sim_netns.sh", "sim", "grab", "sudo grab/test/ebus_sim/run_sim_netns.sh start|stop"),
    ("grab/src/rdma_nslot_test.cpp", "e2e", "grab",
     "grab/build/rdma_nslot_test <spark_ip> 18515 <張數> [寬] [高] [delay_ms] [threads]"),
    ("grab/src/cam_mean_gray_test.cpp", "hw", "grab", "grab/build/cam_mean_gray_test [序號]"),
    # ── ip（C++，Spark）──
    ("ip/src/crc_verify.cpp", "unit", "spark", "ip/build/crc_verify"),
    ("ip/src/align_verify.cpp", "unit", "spark", "ip/build/align_verify"),
    ("ip/src/coord_verify.cpp", "unit", "spark", "ip/build/coord_verify"),
    ("ip/src/edge_verify.cpp", "unit", "spark", "ip/build/edge_verify"),
    ("ip/src/rules_verify.cpp", "unit", "spark", "ip/build/rules_verify"),
    # ── control（C# xUnit）──
    ("control/tests/SpeedVerificationTests.cs", "unit", "control", "dotnet test control/tests"),
    # ── 端到端驗證腳本（Python）──
    ("scripts/verify_step3_trigger.py", "hw", "grab", "python3 scripts/verify_step3_trigger.py 127.0.0.1 8100 5 <台數>"),
    ("scripts/verify_list_during_grab.py", "hw", "grab", "python3 scripts/verify_list_during_grab.py"),
    ("scripts/verify_rdma_replay.py", "e2e", "grab", "python3 scripts/verify_rdma_replay.py --sender grab/build/image_replay_sender …"),
    ("scripts/verify_alignment.py", "e2e", "spark", "python3 scripts/verify_alignment.py --port 8200"),
    ("scripts/verify_coord.py", "e2e", "spark", "python3 scripts/verify_coord.py"),
    ("scripts/verify_flight_src.py", "e2e", "spark", "python3 scripts/verify_flight_src.py"),
    ("scripts/verify_flight_v2.py", "e2e", "spark", "python3 scripts/verify_flight_v2.py"),
    ("scripts/verify_recipe_roundtrip.py", "e2e", "spark", "python3 scripts/verify_recipe_roundtrip.py"),
    ("scripts/verify_remote_image.py", "e2e", "spark", "python3 scripts/verify_remote_image.py"),
    ("scripts/verify_rules_edge.py", "e2e", "spark", "python3 scripts/verify_rules_edge.py --port 8200"),
    ("scripts/verify_sprint_存圖控制.py", "e2e", "spark", "python3 scripts/verify_sprint_存圖控制.py backpressure …"),
    ("scripts/control_test.py", "e2e", "any", "python3 scripts/control_test.py --ip <IP> --image <圖>"),
    ("scripts/compare_results.py", "unit", "any", "python3 scripts/compare_results.py <輸出A> <輸出B>"),
    ("scripts/doctor.py", "unit", "any", "python3 scripts/doctor.py"),
    # ── 相機工具 ──
    ("tools/cam_align/test_offline.py", "unit", "grab", "python3 tools/cam_align/test_offline.py"),
    ("tools/cam_align/test_multiframe.py", "hw", "grab", "python3 tools/cam_align/test_multiframe.py --frames 27"),
    ("tools/cam_align/gvsp_testpacket.py", "hw", "grab", "python3 tools/cam_align/gvsp_testpacket.py"),
    ("tools/grab_setup/verify_grab_host.sh", "hw", "grab", "tools/grab_setup/verify_grab_host.sh [--arm]"),
    ("tools/archive/test_archive.py", "unit", "grab", "python3 tools/archive/test_archive.py"),
    ("tools/node_agent/test_agent_loop.py", "unit", "any", "python3 tools/node_agent/test_agent_loop.py"),
    ("tools/triage/test_triage.py", "unit", "grab", "python3 tools/triage/test_triage.py"),
]
KIND_ZH = {"unit": "單元（不需硬體）", "sim": "模擬裝置", "e2e": "端到端（需程式在跑）", "hw": "實機"}
TEST_LIKE = re.compile(r"(^|/)(test_[^/]+|[^/]*_test\.(cpp|py)|verify_[^/]+|[^/]*_verify\.cpp|[^/]*Tests\.cs)$")
SKIP_DIRS = {".git", "build", "bin", "obj", "node_modules", "__pycache__", "Reference", "pylon_stub"}


def header_doc(path, max_lines=14):
    """取檔頭註解（// 、# 、\"\"\" 、/** */、/// <summary>），去掉框線與空行。"""
    try:
        with open(os.path.join(REPO, path), encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()[:60]
    except OSError:
        return "（檔案不存在）"
    out, in_doc = [], False
    cmt = r"#" if path.endswith((".py", ".sh")) else r"///?|\*|/\*\*"   # C++/C# 的 # 是前處理器，不是註解
    for ln in lines:
        s = ln.strip()
        if s.startswith("#!") or s.startswith("# -*-"):
            continue
        if s.startswith('"""'):
            if in_doc:
                break
            in_doc = True
            s = s[3:]
            if s.endswith('"""') and len(s) >= 3:
                out.append(s[:-3])
                break
        elif in_doc and s.endswith('"""'):
            out.append(s[:-3])
            break
        elif not in_doc:
            m = re.match(rf"^({cmt})\s?(.*)$", s)
            if not m:
                if out:
                    break           # 註解區塊結束
                continue            # 還沒進註解（using/import…）
            s = m.group(2)
        s = re.sub(r"</?summary>", "", s).strip(" */")
        if re.fullmatch(r"[─=\-━*/ ]*", s):
            continue
        out.append(s)
        if len(out) >= max_lines:
            break
    return "\n".join(x for x in out if x).strip() or "（無檔頭說明）"


def scan_unlisted():
    listed = {p for p, *_ in CATALOG}
    found = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), REPO)
            if TEST_LIKE.search(rel) and rel not in listed and not rel.endswith((".csproj", ".md")):
                found.append(rel)
    return sorted(found)


def git_rev():
    try:
        return subprocess.check_output(["git", "-C", REPO, "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def build():
    items = []
    for path, kind, host, run in CATALOG:
        items.append({"path": path, "kind": kind, "host": host, "run": run,
                      "exists": os.path.exists(os.path.join(REPO, path)), "description": header_doc(path)})
    return items


def to_md(items, unlisted, rev):
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    md = [f"# CF-AOI 測試檔案說明（unit test / 驗證腳本）",
          "",
          f"> 自動產生：`tools/archive/gen_test_catalog.py`｜git `{rev}`｜{now}。說明取自各檔檔頭註解。",
          "> 種類：" + "、".join(f"**{k}**={v}" for k, v in KIND_ZH.items()),
          "",
          "| 檔案 | 種類 | 在哪跑 | 執行 |",
          "|---|---|---|---|"]
    for it in items:
        flag = "" if it["exists"] else " ⚠缺檔"
        md.append(f"| `{it['path']}`{flag} | {it['kind']} | {it['host']} | `{it['run']}` |")
    md.append("")
    for it in items:
        md += [f"## {it['path']}", "", f"- 種類：{KIND_ZH[it['kind']]}；在哪跑：{it['host']}",
               f"- 執行：`{it['run']}`", "", "```text", it["description"], "```", ""]
    if unlisted:
        md += ["## ⚠ 未分類（像測試但不在目錄裡 → 請補進 gen_test_catalog.py 的 CATALOG）", ""]
        md += [f"- `{p}`" for p in unlisted]
    return "\n".join(md) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    unlisted = scan_unlisted()
    if a.check:
        for p in unlisted:
            print("未分類：", p)
        return 1 if unlisted else 0
    if not a.out:
        ap.error("需要 --out 或 --check")
    os.makedirs(a.out, exist_ok=True)
    items, rev = build(), git_rev()
    with open(os.path.join(a.out, "tests_catalog.md"), "w", encoding="utf-8") as f:
        f.write(to_md(items, unlisted, rev))
    with open(os.path.join(a.out, "tests_catalog.json"), "w", encoding="utf-8") as f:
        json.dump({"git": rev, "generated": dt.datetime.now().isoformat(timespec="seconds"),
                   "tests": items, "unlisted": unlisted}, f, ensure_ascii=False, indent=1)
    print(f"測試目錄：{len(items)} 項" + (f"，未分類 {len(unlisted)} 項" if unlisted else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
