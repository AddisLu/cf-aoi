#!/usr/bin/env python3
"""
cfaoi_cleanup — 磁碟自動清理（商業化階段 3；cfaoi-cleanup.timer 每天執行 + 開機後執行一次）

產線無人值守：檢測結果、原始影像、log 會一直長，塞滿磁碟 = 停線（存不了圖、journald 寫不進）。
只清 CF-AOI 自己產生、且位置/檔名格式明確的東西；其他檔案一律不碰。

角色 ip（Spark，OUTPUT_DIR = 檢測輸出）：
  <OUTPUT_DIR>/<yyyyMMdd>/        檢測結果（ResultInfo、缺陷小圖、overlay）   依日期夾名 > RETAIN_DAYS
  <OUTPUT_DIR>/source/*           原始影像（SaveSourceImage，每張 ~41MB）      依修改時間 > SOURCE_RETAIN_DAYS
  <OUTPUT_DIR>/_diag/*            行車紀錄（jsonl / incident，很小）           依修改時間 > DIAG_RETAIN_DAYS
角色 grab（OUTPUT_DIR = ~/cfaoi_logs）：
  <OUTPUT_DIR>/grab_YYYYMMDD.log  桌面圖示前景執行時的 log                     依檔名日期 > RETAIN_DAYS

水位保護：磁碟用量 > MAX_USED_PCT → 從最舊的開始刪（先原始影像、再結果日期夾），直到 ≤ TARGET_USED_PCT；
**今天的資料永遠不刪**。刪完仍超標 → 印警告（Control 系統狀態頁的磁碟 % 會顯示）。

設定（/etc/default/cfaoi-cleanup，systemd EnvironmentFile；沒有就用預設）：
  ROLE / OUTPUT_DIR（同 cfaoi-agent）  RETAIN_DAYS=30  SOURCE_RETAIN_DAYS=7  DIAG_RETAIN_DAYS=180
  MAX_USED_PCT=85  TARGET_USED_PCT=80
用法：cfaoi_cleanup.py [--dry-run]
"""
import datetime as dt
import os
import re
import shutil
import sys
import time

ROLE = os.environ.get("ROLE", "ip")
OUTPUT_DIR = os.path.expanduser(os.environ.get("OUTPUT_DIR", "~/cfaoi_output"))
RETAIN_DAYS = int(os.environ.get("RETAIN_DAYS", "30"))
SOURCE_RETAIN_DAYS = int(os.environ.get("SOURCE_RETAIN_DAYS", "7"))
DIAG_RETAIN_DAYS = int(os.environ.get("DIAG_RETAIN_DAYS", "180"))
MAX_USED_PCT = float(os.environ.get("MAX_USED_PCT", "85"))
TARGET_USED_PCT = float(os.environ.get("TARGET_USED_PCT", "80"))
DRY = "--dry-run" in sys.argv

TODAY = dt.date.today()
DATE_DIR = re.compile(r"^(\d{8})$")
GRAB_LOG = re.compile(r"^grab_(\d{8})\.log$")
freed = 0
removed = 0


def name_date(name, pattern):
    m = pattern.match(name)
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def size_of(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def remove(path, why):
    global freed, removed
    sz = size_of(path)
    print(f"[cleanup] {'(dry-run) ' if DRY else ''}刪除 {path}（{sz / 1e6:.1f}MB，{why}）", flush=True)
    if not DRY:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except OSError as e:
                print(f"[cleanup] ⚠ 刪不掉 {path}：{e}", flush=True)
                return
    freed += sz
    removed += 1


def used_pct():
    du = shutil.disk_usage(OUTPUT_DIR)
    return du.used * 100 / du.total


def mtime_files(d):
    """d 底下的檔案（不遞迴），依修改時間由舊到新；今天修改的不列入。"""
    if not os.path.isdir(d):
        return []
    today0 = time.mktime(TODAY.timetuple())
    out = []
    for f in os.listdir(d):
        p = os.path.join(d, f)
        if os.path.isfile(p):
            mt = os.path.getmtime(p)
            if mt < today0:
                out.append((mt, p))
    return sorted(out)


def dated(d, pattern):
    """d 底下符合日期格式的項目，依日期由舊到新；今天的不列入。"""
    if not os.path.isdir(d):
        return []
    out = []
    for f in os.listdir(d):
        day = name_date(f, pattern)
        if day is not None and day < TODAY:
            out.append((day, os.path.join(d, f)))
    return sorted(out)


def main():
    if not os.path.isdir(OUTPUT_DIR):
        print(f"[cleanup] 輸出目錄不存在：{OUTPUT_DIR}（略過）")
        return 0
    before = used_pct()
    print(f"[cleanup] 角色={ROLE} 目錄={OUTPUT_DIR} 磁碟 {before:.1f}%  保留：結果 {RETAIN_DAYS} 天、"
          f"原始影像 {SOURCE_RETAIN_DAYS} 天、行車紀錄 {DIAG_RETAIN_DAYS} 天；水位 {MAX_USED_PCT:.0f}%→{TARGET_USED_PCT:.0f}%"
          f"{'（dry-run，不實際刪除）' if DRY else ''}", flush=True)
    now = time.time()

    # 1) 依保留天數
    if ROLE == "ip":
        for day, p in dated(OUTPUT_DIR, DATE_DIR):
            if (TODAY - day).days > RETAIN_DAYS:
                remove(p, f"結果超過 {RETAIN_DAYS} 天")
        for mt, p in mtime_files(os.path.join(OUTPUT_DIR, "source")):
            if now - mt > SOURCE_RETAIN_DAYS * 86400:
                remove(p, f"原始影像超過 {SOURCE_RETAIN_DAYS} 天")
        for mt, p in mtime_files(os.path.join(OUTPUT_DIR, "_diag")):
            if now - mt > DIAG_RETAIN_DAYS * 86400:
                remove(p, f"行車紀錄超過 {DIAG_RETAIN_DAYS} 天")
    else:
        for day, p in dated(OUTPUT_DIR, GRAB_LOG):
            if (TODAY - day).days > RETAIN_DAYS:
                remove(p, f"log 超過 {RETAIN_DAYS} 天")

    # 2) 水位保護（dry-run 時無法真的降水位 → 只模擬一輪列出候選）
    if used_pct() > MAX_USED_PCT:
        print(f"[cleanup] ⚠ 磁碟 {used_pct():.1f}% > {MAX_USED_PCT:.0f}% → 從最舊的開始刪到 {TARGET_USED_PCT:.0f}%", flush=True)
        if ROLE == "ip":
            candidates = [p for _, p in mtime_files(os.path.join(OUTPUT_DIR, "source"))] + \
                         [p for _, p in dated(OUTPUT_DIR, DATE_DIR)]
        else:
            candidates = [p for _, p in dated(OUTPUT_DIR, GRAB_LOG)]
        for p in candidates:
            if not DRY and used_pct() <= TARGET_USED_PCT:
                break
            if os.path.exists(p):
                remove(p, "磁碟水位保護")
        if not DRY and used_pct() > MAX_USED_PCT:
            print(f"[cleanup] ⚠⚠ 已刪到只剩今天的資料，磁碟仍 {used_pct():.1f}% —— 磁碟被其他東西佔用，需人工處理", flush=True)

    print(f"[cleanup] 完成：刪除 {removed} 項、釋放 {freed / 1e9:.2f}GB；磁碟 {before:.1f}% → {used_pct():.1f}%", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
