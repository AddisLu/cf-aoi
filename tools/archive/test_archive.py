#!/usr/bin/env python3
"""離線測試 cfaoi_archive（不需 Spark、不碰 /srv/cfaoi）：暫存夾當 CFAOI_HOME，ssh/rsync 以本機假資料代替。
涵蓋：init 建夾+README、內容不變不重寫、Spark 行車紀錄改名分日、參數快照只在變更時進 history、
每日摘要統計、保留天數/水位刪除（今天不刪）、參考圖不被清理。
跑法：python3 tools/archive/test_archive.py   預期「全數通過」、exit 0
"""
import datetime as dt
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="cfaoi_archive_test_")
os.environ.update({"CFAOI_HOME": os.path.join(TMP, "home"), "MACHINE_ID": "TEST-01",
                   "GRAB_LOG_DIR": os.path.join(TMP, "grablogs"), "SYNC_DAYS": "2"})
os.makedirs(os.environ["CFAOI_HOME"])
sys.path.insert(0, HERE)
A = importlib.import_module("cfaoi_archive")

FAIL = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + detail) if detail and not cond else ''}")
    if not cond:
        FAIL.append(name)


def touch(path, data="x", days_ago=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(data)
    t = time.time() - days_ago * 86400
    os.utime(path, (t, t))


D0 = A.ymd(A.TODAY)
D1 = A.ymd(A.TODAY - dt.timedelta(days=1))

print("1. init")
A.job_init()
check("8 個類別夾", all(os.path.isdir(A.p(c)) for c in A.CATEGORIES))
check("子夾 00_software/installers、50_raw/align", os.path.isdir(A.p("00_software", "installers"))
      and os.path.isdir(A.p("50_raw", "align")))
readme = open(A.p("README.md"), encoding="utf-8").read()
check("README 帶入機台編號與路徑", "TEST-01" in readme and A.HOME in readme and "{{" not in readme)
m0 = os.path.getmtime(A.p("README.md"))
time.sleep(0.02)
check("內容不變不重寫（mtime 不動）", A.write_if_changed(A.p("README.md"), readme) is False
      and os.path.getmtime(A.p("README.md")) == m0)

print("2. Spark 行車紀錄改名分日")
stage = A.p("10_logs", ".spark_diag")
touch(os.path.join(stage, f"{D0}.jsonl"), '{"type":"session"}\n')
touch(os.path.join(stage, f"incident_{D1}_155022_335.json"), "{}")
touch(os.path.join(stage, "garbage.txt"))
A.rsync = lambda src, dst, extra=(): 0          # 暫存夾已備好，不真的 rsync
A.sync_spark_diag()
check("jsonl → 10_logs/<今天>/spark1_diag.jsonl", os.path.exists(A.p("10_logs", D0, "spark1_diag.jsonl")))
check("incident → 10_logs/<昨天>/spark1_incident_155022_335.json",
      os.path.exists(A.p("10_logs", D1, "spark1_incident_155022_335.json")))
check("不認得的檔不搬", not any("garbage" in f for r, _d, fs in os.walk(A.p("10_logs")) for f in fs
                          if not r.endswith(".spark_diag")))

print("3. 參數快照")
check("第一次 → current + history", A.snapshot("grab", "cam_config.json", '{"exp":70}') == 1
      and os.path.exists(A.p("60_config", "current", "grab_cam_config.json")))
check("內容不變 → 不新增 history", A.snapshot("grab", "cam_config.json", '{"exp":70}') == 0)
hist = os.listdir(A.p("60_config", "history", D0))
check("history 命名 <HHmmss>_<節點>_<名稱>", len(hist) == 1 and hist[0].endswith("_grab_cam_config.json")
      and len(hist[0].split("_")[0]) == 6)

print("4. 每日摘要")
for i, (pid, cnt, ok, flood) in enumerate([("CCD01_000001", 3, True, False), ("CCD01_000002", 1000, False, True),
                                           ("CCD02_000001", 0, True, False)]):
    touch(A.p("40_defects", D0, f"{pid}_DEFAULT", f"{pid}_DEFAULT_ResultInfo.json"),
          json.dumps({"panel_id": pid, "DefectCnt": cnt, "pass": ok, **({"flood_skip": {"n": 5}} if flood else {})}))
with open(A.p("10_logs", D0, "spark1_diag.jsonl"), "a") as f:
    f.write('{"type":"incident","kind":"frame_validation"}\n{"type":"stats","fps":2}\n')
touch(A.p("10_logs", D0, "grab_cfaoi-grab.log"), "10:00 Started cfaoi-grab\n10:01 normal\n10:02 [watchdog] timeout\n")
md = A.summarize_day(A.TODAY)
check("片數 / NG / 爆點停算", "片數（ResultInfo）：3；NG：1；爆點停算：1" in md, md[:400])
check("每台統計列", "| CCD01 | 2 | 1 | 1003 | 1000 |" in md and "| CCD02 | 1 | 0 | 0 | 0 |" in md)
check("incident 依 kind 計數", "incident:frame_validation：1" in md)
check("服務事件抓到重啟與 watchdog", "Started cfaoi-grab" in md and "[watchdog] timeout" in md and "10:01 normal" not in md)
check("參數變更列出", "_grab_cam_config.json" in md)

print("5. 保留天數 / 水位")
old = A.ymd(A.TODAY - dt.timedelta(days=20))
touch(A.p("50_raw", old, "P_source.bin"))
touch(A.p("50_raw", D1, "P_source.bin"))
touch(A.p("40_defects", old, "P_DEFAULT", "x.json"))
A.job_prune()
check("原始圖 > 14 天刪除", not os.path.exists(A.p("50_raw", old)))
check("原始圖 1 天保留", os.path.exists(A.p("50_raw", D1)))
check("檢測結果 20 天保留（90 天）", os.path.exists(A.p("40_defects", old)))
pct = iter([90, 90, 90, 79, 79, 79, 79])
A.used_pct = lambda: next(pct, 79)
A.job_prune()
check("水位：先刪最舊原始圖、到目標就停", not os.path.exists(A.p("50_raw", D1)) and os.path.exists(A.p("40_defects", old)))
check("今天的資料永遠不刪", os.path.exists(A.p("40_defects", D0)))
touch(A.p("50_raw", "reference", "20251202_IP04_x", "a.tif"), days_ago=400)
A.job_prune()
check("50_raw/reference/ 參考圖永不清理", os.path.exists(A.p("50_raw", "reference", "20251202_IP04_x", "a.tif")))

shutil.rmtree(TMP, ignore_errors=True)
print("全數通過" if not FAIL else f"失敗 {len(FAIL)} 項：{FAIL}")
sys.exit(1 if FAIL else 0)
