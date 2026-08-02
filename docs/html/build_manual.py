#!/usr/bin/env python3
"""
build_manual.py — CF-AOI 說明書產生器（索引版）

把「檔案清單 + 函式索引 + 手冊回指 + 範例 _diag Log」注入
cf-aoi-training.html 的 <script id="srcdata"> 區塊：
  ① 內文任何 file:line 引用 → 點了開檔案卡（路徑/行號/outline/在 VS Code 開啟）
  ② 索引頁：全函式/類別自動索引（regex 掃描，重跑即更新）
  ③ Log 分析器：內建真實範例資料可離線示範

⚠️ 2026-08-02 起**不再內嵌源碼全文**（原 window.SRC_FILES，1.74 MB）。原因有二：
  1. 快照當天就會過時。實例：B1 修好當天，教材裡的 cam_pylon.cpp 仍是修前版本，
     還帶著「本函式無 try/catch → 全行程死亡」的註解——讀教材的人會相信它。
  2. 它是單行 1.74 MB，每次重生 = 一顆新 git blob。教材已 commit 26 次，
     .git 因此膨脹到 52 MB。掛 pre-commit 自動重生只會讓這個成本每天複利。
  四台機器都 clone 同一個 repo，源碼本來就在手邊 → 內嵌副本純屬冗餘。
  看碼改為 vscode:// 連結 + 複製路徑，永遠指向現況。

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
    ("docs/html",             {".py"}),              # build_manual.py（.html 只收 incident-viewer，見下）
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

# ── 手冊回指：檔頭前 10 行的 `[手冊 chX]` 註解 → 章節 id 清單 ──────────────
# 原本在瀏覽器端讀 SRC_FILES 檔頭算；源碼全文拿掉後改在這裡預先算好（資料量極小）。
MANUAL_REF = re.compile(r"\[手冊 ((?:ch|g|p|r)\d+)\]")

def manual_refs(files):
    out = {}
    for path, txt in files.items():
        ids = []
        for ln in txt.split("\n", 10)[:10]:
            m = MANUAL_REF.search(ln)
            if m and m.group(1) not in ids:
                ids.append(m.group(1))
        if ids:
            out[path] = ids
    return out

def main_worktree():
    """主 worktree 的絕對路徑（`git worktree list` 第一筆）。取不到就退回 ROOT。"""
    try:
        out = subprocess.run(["git", "-C", ROOT, "worktree", "list", "--porcelain"],
                             capture_output=True, text=True).stdout
        for ln in out.splitlines():
            if ln.startswith("worktree "):
                return ln[len("worktree "):].strip()
    except Exception:
        pass
    return ROOT

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
    # docs/html 只額外收 incident-viewer（其餘 .html 是獨立報告頁/舊版文件，不進 bundle）
    ivp = os.path.join(ROOT, "docs", "html", "incident-viewer.html")
    if os.path.isfile(ivp):
        files["docs/html/incident-viewer.html"] = open(ivp, encoding="utf-8", errors="replace").read()
    fn_index = index_functions(files)
    refs = manual_refs(files)
    # 只留「路徑 → 行數」，不留內容（見檔頭說明）。行數供檔案卡顯示與 outline 收斂。
    paths = {p: t.count("\n") + 1 for p, t in files.items()}
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
        "lines": sum(paths.values()),
        "fns": len(fn_index),
        # 索引版：教材不含源碼全文，看碼一律開真實檔案（見檔頭）
        "index_only": True,
        # repo 在本機的絕對路徑（vscode:// 連結用；頁內 ⚙ 可覆寫並存 localStorage）。
        # ⚠️ 必須是**主 worktree**：在 .claude/worktrees/* 底下跑產生器時 ROOT 會是臨時
        #    worktree 路徑，寫進教材會讓 VS Code 連結指向一個之後會消失的目錄。
        "repo_hint": main_worktree(),
    }
    def js(obj):
        # </script> 防護：JSON 內的 "</" 逸出成 "<\/"
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    block = ("<script id=\"srcdata\">\n"
             f"window.SRC_META={js(meta)};\n"
             f"window.SRC_PATHS={js(paths)};\n"
             f"window.SRC_REFS={js(refs)};\n"
             f"window.FN_INDEX={js(fn_index)};\n"
             f"window.DEMO_LOG={js(demo)};\n"
             "</script>")
    html = open(HTML, encoding="utf-8").read()
    pat = re.compile(r"<script id=\"srcdata\">.*?</script>", re.S)
    if not pat.search(html):
        print("[error] 找不到 <script id=\"srcdata\"> 佔位區塊", file=sys.stderr); sys.exit(1)
    html = pat.sub(lambda _: block, html, count=1)
    open(HTML, "w", encoding="utf-8").write(html)
    kb = len(block) / 1024
    print(f"[ok] 注入索引 {meta['files']} 檔 / {meta['lines']} 行 / {meta['fns']} 個索引項 "
          f"/ 手冊回指 {len(refs)} 檔 / demo={'有' if demo else '無'} "
          f"→ {os.path.relpath(HTML, ROOT)}（commit {commit}，區塊 {kb:.0f} KB，不含源碼全文）")

if __name__ == "__main__":
    main()
