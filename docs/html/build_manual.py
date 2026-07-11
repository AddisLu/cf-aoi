#!/usr/bin/env python3
"""
build_manual.py — CF-AOI 說明書產生器（最大自動化）

把「全部源碼 + 函式索引 + 範例 _diag Log」打包注入 cf-aoi-training.html 的
<script id="srcdata"> 區塊，讓說明書做到：
  ① 內文任何 file:line 引用 → 點了直接在頁內看程式碼（不用去翻）
  ② 索引頁：全函式/類別自動索引（regex 掃描，重跑即更新）
  ③ Log 分析器：內建真實範例資料可離線示範

源碼改了之後重跑本腳本即可同步（行號索引自動跟上）：
  python3 docs/html/build_manual.py
"""
import json, os, re, subprocess, sys, datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
HTML = os.path.join(ROOT, "docs", "html", "cf-aoi-training.html")
DEMO = os.path.join(ROOT, "docs", "html", "demo_diag")

# ── 打包範圍（相對 repo 根；遞迴收集副檔名白名單）────────────────────────────
BUNDLE = [
    ("ip/src",                {".h", ".hpp", ".cpp", ".cu", ".cuh"}),
    ("ip/config",             {".ini"}),
    ("ip",                    {".md", ".txt"}),      # CLAUDE.md + CMakeLists.txt
    ("grab/src",              {".h", ".cpp"}),
    ("grab",                  {".md", ".txt"}),
    ("shared",                {".h"}),
    ("control/src/Controllers", {".cs"}),
    ("control/src/Services",  {".cs"}),
    ("control/src/Models",    {".cs"}),
    ("control/src/ViewModels", {".cs"}),
    ("control/src/Views",     {".axaml", ".cs"}),
    ("control/src/Controls",  {".axaml", ".cs"}),
    ("control",               {".md"}),
    ("scripts",               {".py", ".sh"}),
    ("docs",                  {".md"}),              # 總綱/STATUS/說明/驗證報告/審計（遞迴含 verification/）
    ("docs/html",             {".py", ".html"}),     # build_manual.py + incident-viewer.html
    ("tools",                 {".md", ".cpp", ".h"}),
    ("control/src",           {".json"}),            # appsettings + config/*.example.json（bin/obj 已剪枝）
    ("grab",                  {".json"}),            # cam_config.example.json
]
SKIP_FILES = {"cf-aoi-training.html"}   # 別把說明書自己打包進自己
MAX_FILE_BYTES = 400_000   # 單檔保險絲（不預期觸發）

def collect():
    files = {}
    for base, exts in BUNDLE:
        absbase = os.path.join(ROOT, base)
        if not os.path.isdir(absbase):
            print(f"[warn] 跳過不存在的 {base}", file=sys.stderr); continue
        for dirpath, dirnames, filenames in os.walk(absbase):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "build", "bin", "obj")]
            for fn in sorted(filenames):
                if fn in SKIP_FILES or os.path.splitext(fn)[1].lower() not in exts:
                    continue
                p = os.path.join(dirpath, fn)
                rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
                try:
                    txt = open(p, encoding="utf-8", errors="replace").read()
                except OSError as e:
                    print(f"[warn] 讀不到 {rel}: {e}", file=sys.stderr); continue
                if len(txt) > MAX_FILE_BYTES:
                    txt = txt[:MAX_FILE_BYTES] + "\n/* …（超長截斷，完整檔請看 repo）… */\n"
                files[rel] = txt
    return files

# ── 函式/類別索引（輕量 regex；求「找得到」不求編譯器級精確）────────────────
CPP_FN   = re.compile(r"^[A-Za-z_][\w:<>,\s\*&~]*?\b([A-Za-z_]\w*)\s*\([^;{]*\)\s*(?:const\s*)?\{", re.M)
CPP_KIND = re.compile(r"^\s*(?:class|struct)\s+([A-Za-z_]\w*)", re.M)
CPP_GLOB = re.compile(r"__global__\s+\w+\s+([A-Za-z_]\w*)")
CS_FN    = re.compile(r"^\s*(?:public|private|protected|internal|static|async|override|partial|sealed|virtual)[\w\s<>,\[\]\?]*?\b([A-Za-z_]\w*)\s*\([^;)]*\)\s*(?:where[^{]*)?\{", re.M)
CS_KIND  = re.compile(r"^\s*(?:public|internal)?\s*(?:sealed\s+|static\s+|partial\s+)*(?:class|record|interface|enum)\s+([A-Za-z_]\w*)", re.M)
PY_FN    = re.compile(r"^(?:def|class)\s+([A-Za-z_]\w*)", re.M)
NOISE    = {"if", "for", "while", "switch", "return", "catch", "sizeof", "defined", "Main"}

def index_functions(files):
    idx = []
    def add(name, path, line, kind):
        if name in NOISE or len(name) < 3: return
        idx.append({"n": name, "p": path, "l": line, "k": kind})
    for path, txt in files.items():
        ext = os.path.splitext(path)[1]
        if ext in (".h", ".hpp", ".cpp", ".cu", ".cuh"):
            for m in CPP_GLOB.finditer(txt): add(m.group(1), path, txt.count("\n", 0, m.start()) + 1, "kernel")
            for m in CPP_FN.finditer(txt):   add(m.group(1), path, txt.count("\n", 0, m.start()) + 1, "fn")
            for m in CPP_KIND.finditer(txt): add(m.group(1), path, txt.count("\n", 0, m.start()) + 1, "type")
        elif ext == ".cs":
            for m in CS_FN.finditer(txt):    add(m.group(1), path, txt.count("\n", 0, m.start()) + 1, "fn")
            for m in CS_KIND.finditer(txt):  add(m.group(1), path, txt.count("\n", 0, m.start()) + 1, "type")
        elif ext == ".py":
            for m in PY_FN.finditer(txt):    add(m.group(1), path, txt.count("\n", 0, m.start()) + 1, "fn")
    # 去重（同名同檔同行）＋排序
    seen, out = set(), []
    for e in idx:
        key = (e["n"], e["p"], e["l"])
        if key in seen: continue
        seen.add(key); out.append(e)
    out.sort(key=lambda e: (e["n"].lower(), e["p"], e["l"]))
    return out

def demo_log():
    if not os.path.isdir(DEMO): return None
    d = {"jsonl": "", "incidents": {}}
    for fn in sorted(os.listdir(DEMO)):
        p = os.path.join(DEMO, fn)
        if fn.endswith(".jsonl"):
            d["jsonl"] = open(p, encoding="utf-8").read()
        elif fn.startswith("incident_") and fn.endswith(".json"):
            try: d["incidents"][fn] = json.load(open(p, encoding="utf-8"))
            except Exception as e: print(f"[warn] demo {fn}: {e}", file=sys.stderr)
    return d if d["jsonl"] else None

def main():
    files = collect()
    fn_index = index_functions(files)
    demo = demo_log()
    try:
        commit = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True).stdout.strip()
    except Exception:
        commit = "?"
    meta = {
        "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "commit": commit,
        "files": len(files),
        "lines": sum(t.count("\n") + 1 for t in files.values()),
        "fns": len(fn_index),
    }
    def js(obj):
        # </script> 防護：JSON 內的 "</" 逸出成 "<\/"
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    block = ("<script id=\"srcdata\">\n"
             f"window.SRC_META={js(meta)};\n"
             f"window.SRC_FILES={js(files)};\n"
             f"window.FN_INDEX={js(fn_index)};\n"
             f"window.DEMO_LOG={js(demo)};\n"
             "</script>")
    html = open(HTML, encoding="utf-8").read()
    pat = re.compile(r"<script id=\"srcdata\">.*?</script>", re.S)
    if not pat.search(html):
        print("[error] 找不到 <script id=\"srcdata\"> 佔位區塊", file=sys.stderr); sys.exit(1)
    html = pat.sub(lambda _: block, html, count=1)
    open(HTML, "w", encoding="utf-8").write(html)
    print(f"[ok] 注入 {meta['files']} 檔 / {meta['lines']} 行 / {meta['fns']} 個索引項 "
          f"/ demo={'有' if demo else '無'} → {os.path.relpath(HTML, ROOT)}（commit {commit}）")

if __name__ == "__main__":
    main()
