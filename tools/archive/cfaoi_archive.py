#!/usr/bin/env python3
"""
cfaoi_archive — 機台資料歸檔到 Grab 固定資料夾（CFAOI_HOME，預設 /srv/cfaoi）。

cfaoi-archive.timer 每 10 分鐘跑一次（也可手動）。各工作互相獨立：Spark 連不到只影響要拉 Spark 的那幾項，
結果寫進 <CFAOI_HOME>/STATUS.json。命名規則全文見 tools/archive/README_CFAOI_HOME.md（init 時複製成
<CFAOI_HOME>/README.md）。

  00_software/   相關軟體：installers/ control_windows/ updates/ firmware/
  10_logs/       機台 log：<yyyyMMdd>/<節點>_<來源>.<ext>
  20_docs/       參考資料：cf-aoi/（repo 文件鏡像，自動）vendor/ sop/（人工放）
  30_tests/      測試說明 tests_catalog.{md,json}；results/<yyyyMMdd>/<yyyyMMdd>_<HHmmss>_<節點>_<項目>.log
  40_defects/    檢測結果 + defect 小圖：<yyyyMMdd>/<panelId>_<recipe>/（與 Spark 輸出同名，不改名）
  50_raw/        原始圖檔：<yyyyMMdd>/<panelId>_source.bin；align/<yyyyMMdd_HHmmss>/CCDnn.*（相機工具快照）；
                 reference/<yyyyMMdd>_<來源>_<說明>/（驗證用參考圖，永久保留、不自動清）
  60_config/     參數快照：current/<節點>_<名稱>；history/<yyyyMMdd>/<HHmmss>_<節點>_<名稱>（內容有變才存）
  70_knowledge/  給 LoopEngineering（RAG）的每日摘要：<yyyyMMdd>_daily.md
LoopEngineering（Spark）直接以遠端路徑 grab:/srv/cfaoi 讀這個資料夾（SSH，不複製），登錄見 loop_register.sh。

用法：
  cfaoi_archive.py                 全部工作
  cfaoi_archive.py logs defects    只跑指定工作（init logs defects raw docs tests config software knowledge prune）
  cfaoi_archive.py --dry-run       列出會做什麼，不寫檔
設定：/etc/default/cfaoi-archive（systemd EnvironmentFile；沒有就用預設，見 DEFAULTS）
"""
import datetime as dt
import fcntl
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = {
    "CFAOI_HOME": "/srv/cfaoi",
    "MACHINE_ID": socket.gethostname(),
    "REPO": os.path.abspath(os.path.join(HERE, "..", "..")),
    "GRAB_LOG_DIR": os.path.expanduser("~/cfaoi_logs"),
    "GRAB_UNITS": "cfaoi-grab cfaoi-agent cfaoi-cleanup cfaoi-archive chrony",
    "SPARK": "auo001@192.168.3.1",           # RDMA 直連網段（fab 內唯一通路；不走 Tailscale）
    "SPARK_NODE": "spark1",
    "SPARK_OUTPUT": "/home/auo001/cfaoi_output",
    "SPARK_REPO": "/home/auo001/Addis/cf-aoi",
    "SPARK_UNITS": "cfaoi-ip-production cfaoi-ip-offline cfaoi-agent cfaoi-cleanup",
    "SYNC_DAYS": "2",                        # 每輪重掃最近幾天（今天 + 昨天收尾）；第一次跑會補齊全部
    "RAW_BWLIMIT_KB": "300000",              # 原始圖拉取限速（KB/s；~2.4Gbps，遠低於 100G 鏈路，不擠 RDMA）
    "KEEP_LOG_DAYS": "365",
    "KEEP_DEFECT_DAYS": "90",
    "KEEP_RAW_DAYS": "14",
    "KEEP_TEST_DAYS": "365",
    "KEEP_CONFIG_DAYS": "365",
    "KEEP_KNOWLEDGE_DAYS": "730",
    "KEEP_SOFTWARE_UPDATES": "5",            # 00_software/updates、control_windows 各保留最新幾版
    "MAX_USED_PCT": "85",
    "TARGET_USED_PCT": "80",
}
CFG = {k: os.environ.get(k, v) for k, v in DEFAULTS.items()}
HOME = CFG["CFAOI_HOME"]
DRY = "--dry-run" in sys.argv
NOW = dt.datetime.now()
TODAY = NOW.date()

CATEGORIES = {
    "00_software": ["installers", "control_windows", "updates", "firmware"],
    "10_logs": [],
    "20_docs": ["cf-aoi", "vendor", "sop"],
    "30_tests": ["results"],
    "40_defects": [],
    "50_raw": ["align", "reference"],
    "60_config": ["current", "history"],
    "70_knowledge": [],
}
DATE_RE = re.compile(r"^(\d{8})$")
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=15"]


# ───────────────────────── 共用 ─────────────────────────
def log(msg):
    print(f"[archive] {msg}", flush=True)


def ymd(d):
    return d.strftime("%Y%m%d")


def recent_days(n):
    return [TODAY - dt.timedelta(days=i) for i in range(int(n))]


def p(*parts):
    return os.path.join(HOME, *parts)


def run(cmd, timeout=600, check=True, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}… → {r.returncode}: {(r.stderr or r.stdout).strip()[-300:]}")
    return r


def ssh(cmd, timeout=120, check=True):
    return run(["ssh", *SSH_OPTS, CFG["SPARK"], cmd], timeout=timeout, check=check)


def write_if_changed(path, data):
    """內容一樣就不寫（mtime 不動 → rsync/RAG 不會當成新檔）。回傳是否寫入。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    try:
        with open(path, "rb") as f:
            if f.read() == data:
                return False
    except OSError:
        pass
    if DRY:
        log(f"(dry-run) 寫 {path}（{len(data)} bytes）")
        return True
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return True


def rsync(src, dst, extra=()):
    os.makedirs(dst if dst.endswith("/") else os.path.dirname(dst), exist_ok=True)
    cmd = ["rsync", "-a", "--partial", "--stats", "-e", "ssh " + " ".join(SSH_OPTS), *extra, src, dst]
    if DRY:
        cmd.insert(1, "--dry-run")
    r = run(["nice", "-n", "10", "ionice", "-c3", *cmd], timeout=3600)
    m = re.search(r"Number of regular files transferred: ([\d,]+)", r.stdout)
    return int(m.group(1).replace(",", "")) if m else 0


# ───────────────────────── 工作 ─────────────────────────
def job_init():
    """建資料夾 + README（命名規則）。"""
    for cat, subs in CATEGORIES.items():
        for s in [""] + subs:
            d = p(cat, s)
            if not os.path.isdir(d) and not DRY:
                os.makedirs(d, exist_ok=True)
    tpl = os.path.join(HERE, "README_CFAOI_HOME.md")
    with open(tpl, encoding="utf-8") as f:
        text = f.read().replace("{{MACHINE_ID}}", CFG["MACHINE_ID"]).replace("{{CFAOI_HOME}}", HOME)
    write_if_changed(p("README.md"), text)
    return "OK"


def journal_day(day, units, remote=False):
    """某天某些 unit 的 journald（short-iso）；回傳 {unit: text}。"""
    since, until = f"{day:%Y-%m-%d} 00:00:00", f"{day + dt.timedelta(days=1):%Y-%m-%d} 00:00:00"
    out = {}
    for u in units.split():
        cmd = ["journalctl", "--no-pager", "-q", "-o", "short-iso", "-u", u + ".service" if "." not in u else u,
               "--since", since, "--until", until]
        r = ssh(" ".join(shlex.quote(c) for c in cmd), check=False) if remote else run(cmd, check=False)
        if r.returncode == 0 and r.stdout.strip():
            out[u] = r.stdout
    return out


def log_days():
    """今天 + 最近 SYNC_DAYS 天每輪重做；第一次跑（沒有 .backfill_done）補到 journald 最舊那天為止。"""
    days = set(recent_days(CFG["SYNC_DAYS"]))
    if not os.path.exists(p("10_logs", ".backfill_done")):
        r = subprocess.run("journalctl --no-pager -q -o short-iso | head -1", shell=True,
                           capture_output=True, text=True, timeout=60)
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", r.stdout)
        first = dt.date(*map(int, m.groups())) if m else TODAY
        first = max(first, TODAY - dt.timedelta(days=int(CFG["KEEP_LOG_DAYS"])))
        days |= {first + dt.timedelta(days=i) for i in range((TODAY - first).days + 1)}
    return sorted(days)


def job_logs():
    n = 0
    days = log_days()
    # Grab 本機 journald（舊日期沒資料的 journal_day 回空 → 不建資料夾）
    for day in days:
        for unit, text in journal_day(day, CFG["GRAB_UNITS"]).items():
            n += write_if_changed(p("10_logs", ymd(day), f"grab_{unit}.log"), text)
        if day >= TODAY - dt.timedelta(days=int(CFG["SYNC_DAYS"])):
            k = run(["journalctl", "--no-pager", "-q", "-k", "-o", "short-iso", "--since", f"{day:%Y-%m-%d}",
                     "--until", f"{day + dt.timedelta(days=1):%Y-%m-%d}"], check=False)
            if k.returncode == 0 and k.stdout.strip():
                n += write_if_changed(p("10_logs", ymd(day), "grab_kernel.log"), k.stdout)
    # Grab 前景執行（桌面圖示）的 log：~/cfaoi_logs/grab_YYYYMMDD.log
    for f in glob.glob(os.path.join(CFG["GRAB_LOG_DIR"], "grab_*.log")):
        m = re.match(r"grab_(\d{8})\.log$", os.path.basename(f))
        if m:
            dst = p("10_logs", m.group(1), "grab_foreground.log")
            if not os.path.exists(dst) or os.path.getmtime(f) > os.path.getmtime(dst):
                if not DRY:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(f, dst)
                n += 1
    # Spark journald（只做最近幾天；Spark 也有 journald 4GB 上限）+ 行車紀錄 _diag
    err = []
    try:
        for day in recent_days(CFG["SYNC_DAYS"]):
            for unit, text in journal_day(day, CFG["SPARK_UNITS"], remote=True).items():
                n += write_if_changed(p("10_logs", ymd(day), f"{CFG['SPARK_NODE']}_{unit}.log"), text)
        n += sync_spark_diag()
    except Exception as e:  # noqa: BLE001 — Spark 斷線不影響本機部分
        err.append(f"Spark：{e}")
    if not DRY:
        write_if_changed(p("10_logs", ".backfill_done"), f"{NOW.isoformat(timespec='seconds')}\n")
    return f"{n} 檔更新" + (f"；⚠ {'；'.join(err)}" if err else "")


def sync_spark_diag():
    """_diag/<yyyyMMdd>.jsonl → 10_logs/<d>/spark1_diag.jsonl；incident_<yyyyMMdd>_<HHmmss>_<ms>.json →
    10_logs/<d>/spark1_incident_<HHmmss>_<ms>.json。先 rsync 到暫存夾再改名分日。"""
    stage = p("10_logs", ".spark_diag")
    got = rsync(f"{CFG['SPARK']}:{CFG['SPARK_OUTPUT']}/_diag/", stage + "/")
    if DRY or not os.path.isdir(stage):
        return got
    node = CFG["SPARK_NODE"]
    for f in os.listdir(stage):
        src = os.path.join(stage, f)
        m1 = re.match(r"^(\d{8})\.jsonl$", f)
        m2 = re.match(r"^incident_(\d{8})_(\d{6})_?(\d*)\.json$", f)
        if m1:
            dst = p("10_logs", m1.group(1), f"{node}_diag.jsonl")
        elif m2:
            tail = m2.group(2) + (f"_{m2.group(3)}" if m2.group(3) else "")
            dst = p("10_logs", m2.group(1), f"{node}_incident_{tail}.json")
        else:
            continue
        if not os.path.exists(dst) or os.path.getmtime(src) > os.path.getmtime(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    return got


def spark_dates(sub=""):
    r = ssh(f"ls -1 {shlex.quote(CFG['SPARK_OUTPUT'] + '/' + sub)} 2>/dev/null", check=False)
    return sorted(x for x in r.stdout.split() if DATE_RE.match(x))


def job_defects():
    """Spark <OUTPUT>/<yyyyMMdd>/<panelId>_<recipe>/ → 40_defects/<yyyyMMdd>/（不 --delete：Spark 30 天清掉，Grab 留更久）。"""
    have = set(os.listdir(p("40_defects"))) if os.path.isdir(p("40_defects")) else set()
    recent = {ymd(d) for d in recent_days(CFG["SYNC_DAYS"])}
    n = 0
    for d in spark_dates():
        if d in recent or d not in have:
            n += rsync(f"{CFG['SPARK']}:{CFG['SPARK_OUTPUT']}/{d}/", p("40_defects", d) + "/")
    return f"{n} 檔新增"


def job_raw():
    """Spark <OUTPUT>/source/<panelId>_source.bin（平放）→ 50_raw/<修改日>/。限速 + 低優先權。"""
    r = ssh(f"cd {shlex.quote(CFG['SPARK_OUTPUT'] + '/source')} 2>/dev/null && "
            "find . -maxdepth 1 -type f -printf '%TY%Tm%Td %s %f\\n'", check=False)
    by_day = {}
    for ln in r.stdout.splitlines():
        parts = ln.split(" ", 2)
        if len(parts) == 3 and DATE_RE.match(parts[0]):
            dst = p("50_raw", parts[0], parts[2])
            if not (os.path.exists(dst) and os.path.getsize(dst) == int(parts[1])):
                by_day.setdefault(parts[0], []).append(parts[2])
    n = 0
    for day, files in sorted(by_day.items()):
        lst = p("50_raw", f".files_{day}.txt")
        if not DRY:
            os.makedirs(p("50_raw", day), exist_ok=True)
            with open(lst, "w") as f:
                f.write("\n".join(files) + "\n")
        n += rsync(f"{CFG['SPARK']}:{CFG['SPARK_OUTPUT']}/source/", p("50_raw", day) + "/",
                   [f"--bwlimit={CFG['RAW_BWLIMIT_KB']}", f"--files-from={lst}"])
        if not DRY:
            os.remove(lst)
    return f"{n} 檔新增"


def job_docs():
    """repo 文件鏡像 → 20_docs/cf-aoi/（--delete：這一夾只放自動鏡像，人工文件放 vendor/ sop/）。"""
    repo = CFG["REPO"]
    inc = ["--include=*/", "--include=*.md", "--include=*.html", "--include=*.txt", "--include=*.pdf",
           "--include=*.png", "--include=*.svg", "--exclude=*"]
    excl = ["--exclude=.git/", "--exclude=build/", "--exclude=bin/", "--exclude=obj/", "--exclude=publish/",
            "--exclude=node_modules/", "--exclude=Reference/", "--exclude=recipes/", "--exclude=__pycache__/"]
    n = rsync(repo + "/", p("20_docs", "cf-aoi") + "/", [*excl, *inc, "--prune-empty-dirs", "--delete"])
    rev = run(["git", "-C", repo, "log", "-1", "--format=%h %ci %s"], check=False).stdout.strip()
    write_if_changed(p("20_docs", "cf-aoi", "VERSION.txt"), f"{rev}\n")
    return f"{n} 檔更新（git {rev.split(' ')[0] if rev else '?'}）"


def job_tests():
    r = run([sys.executable, os.path.join(HERE, "gen_test_catalog.py"), "--out", p("30_tests")], check=False) \
        if not DRY else None
    return (r.stdout.strip() or r.stderr.strip()[-200:]) if r else "(dry-run)"


def snapshot(node, name, data):
    """60_config/current/<節點>_<名稱>；內容有變 → 另存 history/<yyyyMMdd>/<HHmmss>_<節點>_<名稱>。"""
    cur = p("60_config", "current", f"{node}_{name}")
    if write_if_changed(cur, data):
        write_if_changed(p("60_config", "history", ymd(TODAY), f"{NOW:%H%M%S}_{node}_{name}"), data)
        return 1
    return 0


def job_config():
    repo, n = CFG["REPO"], 0
    local = {
        "cam_config.json": os.path.join(repo, "grab", "cam_config.json"),
        "etc-default-cfaoi-grab": "/etc/default/cfaoi-grab",
        "etc-default-cfaoi-agent": "/etc/default/cfaoi-agent",
        "etc-default-cfaoi-cleanup": "/etc/default/cfaoi-cleanup",
        "etc-default-cfaoi-archive": "/etc/default/cfaoi-archive",
    }
    for name, path in local.items():
        try:
            with open(path, "rb") as f:
                n += snapshot("grab", name, f.read())
        except OSError:
            pass
    n += snapshot("grab", "git-version.txt", run(["git", "-C", repo, "log", "-1", "--format=%h %ci %s"],
                                                 check=False).stdout)
    err = ""
    try:
        sr = CFG["SPARK_REPO"]
        files = {"etc-default-cfaoi-agent": "/etc/default/cfaoi-agent",
                 "etc-default-cfaoi-cleanup": "/etc/default/cfaoi-cleanup",
                 "default_zone.ini": f"{sr}/ip/config/default_zone.ini",
                 "cfaoi-ip-production.service": "/etc/systemd/system/cfaoi-ip-production.service"}
        for name, path in files.items():
            r = ssh(f"cat {shlex.quote(path)}", check=False)
            if r.returncode == 0:
                n += snapshot(CFG["SPARK_NODE"], name, r.stdout)
        r = ssh(f"git -C {shlex.quote(sr)} log -1 --format='%h %ci %s'", check=False)
        n += snapshot(CFG["SPARK_NODE"], "git-version.txt", r.stdout)
        # 配方：整個 recipes/ 打成一份清單 + 雜湊（配方本身在 git 裡；這裡記「現場實際用的是哪一版」）
        r = ssh(f"cd {shlex.quote(sr)}/recipes 2>/dev/null && find . -type f -name '*.xml' | sort | xargs -r sha1sum",
                check=False)
        n += snapshot(CFG["SPARK_NODE"], "recipes-sha1.txt", r.stdout)
    except Exception as e:  # noqa: BLE001
        err = f"；⚠ Spark：{e}"
    return f"{n} 項有變{err}"


def job_software():
    """安裝檔（只補缺的）＋ Windows Control 包 ＋ 離線更新包（各留最新 N 版）。"""
    n = 0
    for pat in ["~/下載/*.deb", "~/下載/pylon/*.deb", "~/Downloads/*.deb", "~/Downloads/pylon/*.deb"]:
        for f in glob.glob(os.path.expanduser(pat)):
            b = os.path.basename(f)
            if re.match(r"(?i)(pylon|ebus|codemeter)", b) and not os.path.exists(p("00_software", "installers", b)):
                if not DRY:
                    shutil.copy2(f, p("00_software", "installers", b))
                n += 1
    for f in glob.glob(os.path.join(CFG["REPO"], "control", "publish", "cfaoi-control-win-x64-*.zip")):
        dst = p("00_software", "control_windows", os.path.basename(f))
        if not os.path.exists(dst):
            if not DRY:
                shutil.copy2(f, dst)
            n += 1
    for d in glob.glob(os.path.expanduser("~/cfaoi_updates/cfaoi-update-*")):
        dst = p("00_software", "updates", os.path.basename(d))
        if os.path.isdir(d) and not os.path.exists(dst):
            if not DRY:
                shutil.copytree(d, dst)
            n += 1
    keep = int(CFG["KEEP_SOFTWARE_UPDATES"])
    for sub in ["control_windows", "updates"]:
        items = sorted(glob.glob(p("00_software", sub, "cfaoi-*")), key=os.path.getmtime)
        for old in items[:-keep] if len(items) > keep else []:
            remove(old, f"只保留最新 {keep} 版")
    return f"{n} 項新增"


# ───── 每日摘要（RAG 用：把當天發生的事寫成「可讀的文字」，比丟原始 log 好檢索）─────
def summarize_day(day):
    d = ymd(day)
    lines = [f"# 機台 {CFG['MACHINE_ID']} 每日摘要 {day:%Y-%m-%d}", "",
             f"> 自動產生（tools/archive/cfaoi_archive.py knowledge）於 {NOW:%Y-%m-%d %H:%M}。"
             f"原始資料：10_logs/{d}/、40_defects/{d}/、60_config/history/{d}/。", ""]
    # 檢測結果
    per_cam, panels, flood, fails = {}, 0, 0, 0
    for rj in glob.glob(p("40_defects", d, "*", "*_ResultInfo.json")):
        try:
            with open(rj, encoding="utf-8") as f:
                r = json.load(f)
        except (OSError, ValueError):
            continue
        panels += 1
        m = re.search(r"CCD(\d{2})", r.get("panel_id", "") or os.path.basename(rj))
        cam = f"CCD{m.group(1)}" if m else "?"
        c = per_cam.setdefault(cam, {"n": 0, "defects": 0, "max": 0, "ng": 0})
        cnt = int(r.get("DefectCnt", 0) or 0)
        c["n"] += 1
        c["defects"] += cnt
        c["max"] = max(c["max"], cnt)
        if not r.get("pass", True):
            c["ng"] += 1
            fails += 1
        if r.get("flood_skip"):
            flood += 1
    lines += ["## 檢測結果", "", f"- 片數（ResultInfo）：{panels}；NG：{fails}；爆點停算：{flood}", ""]
    if per_cam:
        lines += ["| 相機 | 片數 | NG | 缺陷總數 | 單片最大 |", "|---|---|---|---|---|"]
        for cam in sorted(per_cam):
            c = per_cam[cam]
            lines.append(f"| {cam} | {c['n']} | {c['ng']} | {c['defects']} | {c['max']} |")
        lines.append("")
    # 行車紀錄（Spark _diag）
    kinds, stats = {}, []
    diag = p("10_logs", d, f"{CFG['SPARK_NODE']}_diag.jsonl")
    if os.path.exists(diag):
        with open(diag, encoding="utf-8", errors="replace") as f:
            for ln in f:
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                t = e.get("type", "?")
                key = t if t != "incident" else f"incident:{e.get('kind', '?')}"
                kinds[key] = kinds.get(key, 0) + 1
                if t == "stats":
                    stats.append(e)
    incidents = sorted(glob.glob(p("10_logs", d, f"{CFG['SPARK_NODE']}_incident_*.json")))
    lines += ["## IP 行車紀錄（Spark _diag）", ""]
    lines += [f"- {k}：{v}" for k, v in sorted(kinds.items())] or ["- （無）"]
    lines += [f"- incident 完整檔：{len(incidents)} 個" + (f"（例：{os.path.basename(incidents[-1])}）" if incidents else "")]
    lines.append("")
    # 服務事件（重啟 / watchdog / 錯誤行）
    lines += ["## 服務事件（journald）", ""]
    pat = re.compile(r"(Started|Stopped|watchdog|Watchdog|timeout|Failed|failed|ERROR|錯誤|失敗|⚠|✗)")
    for f in sorted(glob.glob(p("10_logs", d, "*.log"))):
        hits = []
        with open(f, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                if pat.search(ln):
                    hits.append(ln.rstrip())
        if hits:
            lines.append(f"### {os.path.basename(f)}（{len(hits)} 行，列最後 15 行）")
            lines += ["```text", *[h[:240] for h in hits[-15:]], "```", ""]
    # 參數變更
    ch = sorted(os.listdir(p("60_config", "history", d))) if os.path.isdir(p("60_config", "history", d)) else []
    lines += ["## 參數 / 版本變更", ""] + ([f"- {c}" for c in ch] or ["- （無）"]) + [""]
    return "\n".join(lines) + "\n"


def job_knowledge():
    n = 0
    for day in recent_days(CFG["SYNC_DAYS"]):
        if os.path.isdir(p("10_logs", ymd(day))) or os.path.isdir(p("40_defects", ymd(day))):
            n += write_if_changed(p("70_knowledge", f"{ymd(day)}_daily.md"), summarize_day(day))
    return f"{n} 份更新"


# ───────────────────────── 保留期 / 水位 ─────────────────────────
FREED = [0]


def size_of(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    return sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(path) for f in fs
               if os.path.exists(os.path.join(r, f)))


def remove(path, why):
    sz = size_of(path)
    log(f"{'(dry-run) ' if DRY else ''}刪除 {path}（{sz / 1e6:.1f}MB，{why}）")
    if not DRY:
        shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.remove(path)
    FREED[0] += sz


def dated_dirs(cat):
    out = []
    if os.path.isdir(p(cat)):
        for f in os.listdir(p(cat)):
            if DATE_RE.match(f):
                try:
                    day = dt.datetime.strptime(f, "%Y%m%d").date()
                except ValueError:
                    continue
                if day < TODAY:
                    out.append((day, p(cat, f)))
    return sorted(out)


def used_pct():
    du = shutil.disk_usage(HOME)
    return du.used * 100 / du.total


def job_prune():
    rules = [("10_logs", "KEEP_LOG_DAYS"), ("40_defects", "KEEP_DEFECT_DAYS"), ("50_raw", "KEEP_RAW_DAYS"),
             ("30_tests/results", "KEEP_TEST_DAYS"), ("60_config/history", "KEEP_CONFIG_DAYS")]
    for cat, key in rules:
        for day, path in dated_dirs(cat):
            if (TODAY - day).days > int(CFG[key]):
                remove(path, f"超過 {CFG[key]} 天")
    for f in glob.glob(p("70_knowledge", "*_daily.md")):
        m = re.match(r"(\d{8})_daily\.md$", os.path.basename(f))
        if m and (TODAY - dt.datetime.strptime(m.group(1), "%Y%m%d").date()).days > int(CFG["KEEP_KNOWLEDGE_DAYS"]):
            remove(f, f"超過 {CFG['KEEP_KNOWLEDGE_DAYS']} 天")
    for f in glob.glob(p("50_raw", "align", "*")):     # 相機工具快照：同原始圖保留天數
        if time.time() - os.path.getmtime(f) > int(CFG["KEEP_RAW_DAYS"]) * 86400:
            remove(f, f"相機快照超過 {CFG['KEEP_RAW_DAYS']} 天")
    # 水位：先刪最舊的原始圖、再刪最舊的檢測結果（今天的不刪；log/文件/設定/摘要很小，不參與）
    hi, lo = float(CFG["MAX_USED_PCT"]), float(CFG["TARGET_USED_PCT"])
    if os.path.isdir(HOME) and used_pct() > hi:
        log(f"⚠ 磁碟 {used_pct():.1f}% > {hi:.0f}% → 從最舊的原始圖/檢測結果刪到 {lo:.0f}%")
        for _, path in dated_dirs("50_raw") + dated_dirs("40_defects"):
            if not DRY and used_pct() <= lo:
                break
            remove(path, "磁碟水位保護")
    return f"釋放 {FREED[0] / 1e9:.2f}GB；磁碟 {used_pct():.1f}%" if os.path.isdir(HOME) else "（尚未 init）"


STATUS = {}
JOBS = {"init": job_init, "logs": job_logs, "defects": job_defects, "raw": job_raw, "docs": job_docs,
        "tests": job_tests, "config": job_config, "software": job_software, "knowledge": job_knowledge,
        "prune": job_prune}


def main():
    want = [a for a in sys.argv[1:] if not a.startswith("--")] or list(JOBS)
    bad = [w for w in want if w not in JOBS]
    if bad:
        print(f"未知工作：{bad}；可用：{' '.join(JOBS)}")
        return 2
    if not os.path.isdir(HOME):
        print(f"✗ {HOME} 不存在 → 先跑 tools/archive/install_archive.sh（建資料夾 + 權限 + timer）")
        return 1
    if "init" not in want:
        want.insert(0, "init")
    lock = open(p(".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("上一輪還在跑（多半是原始圖在拉），這輪略過")
        return 0
    status_path = p("STATUS.json")
    try:
        with open(status_path, encoding="utf-8") as f:
            STATUS.update(json.load(f))
    except (OSError, ValueError):
        pass
    status = STATUS
    status.update({"machine_id": CFG["MACHINE_ID"], "cfaoi_home": HOME})
    jobs = status.setdefault("jobs", {})
    rc = 0
    for name in want:
        t0 = time.time()
        try:
            msg, ok = JOBS[name](), True
        except Exception as e:  # noqa: BLE001 — 一項失敗不擋其他項
            msg, ok = f"✗ {e}", False
            rc = 1
        ok = ok and "⚠" not in str(msg)
        jobs[name] = {"ok": ok, "msg": str(msg), "at": dt.datetime.now().isoformat(timespec="seconds"),
                      "sec": round(time.time() - t0, 1)}
        log(f"{name:9} {'OK ' if ok else 'NG '} {msg}（{time.time() - t0:.1f}s）")
    status["last_run"] = dt.datetime.now().isoformat(timespec="seconds")
    if not DRY:
        write_if_changed(status_path, json.dumps(status, ensure_ascii=False, indent=1))
    return rc


if __name__ == "__main__":
    sys.exit(main())
