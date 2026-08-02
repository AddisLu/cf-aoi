#!/usr/bin/env python3
"""
gen_dashboard.py — 生成 docs/DASHBOARD.md：把「現在做到哪、下一步做什麼」壓成一頁。

為什麼要這支腳本
    STATUS.md 有 14.8 萬字、code_review 有 100+ 條、三份模組 CLAUDE.md 各有自己的
    不變式清單。回答「6 相機到貨前還剩什麼沒修」需要自己橫跨三份文件拼裝——資訊
    都在，但每次都要重拼。這支腳本把那次拼裝自動化。

為什麼是生成的
    手寫的現況表會過時，而且是最惡劣的那種過時（看起來很權威）。這裡所有數字都
    從 repo 現況算出來，重跑就對。**不要手改 DASHBOARD.md。**

用法
    python3 scripts/gen_dashboard.py          # 寫入 docs/DASHBOARD.md
    python3 scripts/gen_dashboard.py --stdout # 只印出來不寫檔
"""
import datetime, os, re, subprocess, sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REVIEW = os.path.join(ROOT, "docs", "code_review_20260802.md")
OUT = os.path.join(ROOT, "docs", "DASHBOARD.md")

# 模組原始碼範圍（用於規模統計）
MODULES = [
    ("grab", "grab/src", (".cpp", ".h")),
    ("ip", "ip/src", (".cpp", ".h", ".cu", ".cuh", ".hpp")),
    ("control", "control/src", (".cs",)),
]
# 人要讀的文件（用於維護成本統計）
DOCS = [
    "docs/CLAUDE.md", "docs/STATUS.md", "docs/code_review_20260802.md",
    "docs/6cam_setup_runbook.md", "docs/ip_程式完整說明.md",
    "docs/grab_程式完整說明.md", "docs/control_程式完整說明.md",
    "ip/CLAUDE.md", "grab/CLAUDE.md", "control/CLAUDE.md",
]


def git(*args, default=""):
    try:
        r = subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True)
        return r.stdout.strip() or default
    except Exception:
        return default


def parse_review():
    """解析 code_review 的 P0–P4 表格。回傳 [(級別, 標題, [條目])]。

    兩種列格式都要吃：
      P0–P2  `| **B4** | grab | 問題… | 位置 | 情境 |`
      P3     `| B13 | 一句話 | 位置 |`
    已修標記：第一格出現「已修」（例：`| ~~**B1**~~ **已修 L2** | …`）。
    """
    if not os.path.isfile(REVIEW):
        return []
    t = open(REVIEW, encoding="utf-8").read()
    heads = [(m.group(1), m.group(2).strip(), m.start())
             for m in re.finditer(r"^## (P\d) — ([^\n]*)", t, re.M)]
    out = []
    for i, (lvl, title, st) in enumerate(heads):
        en = heads[i + 1][2] if i + 1 < len(heads) else len(t)
        items = []
        for ln in t[st:en].split("\n"):
            if not ln.startswith("|"):
                continue
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if len(cells) < 2:
                continue
            raw = cells[0]
            # 只去粗體/刪除線標記，**不要**去掉單顆 `*`——`Awc_*_Way_*_Div` 這種
            # 內容裡的星號是資料的一部分，砍掉會變成另一個字串。
            ident = raw.replace("~~", "").replace("**", "").replace("`", "").strip()
            ident = ident.split()[0] if ident.split() else ""   # 去掉「已修 L2」等後綴
            m = re.match(r"^([A-Z]+\d+)", ident)
            if not m:
                continue                      # 表頭與分隔列
            fixed = "已修" in raw
            # 「端」欄只有寬表有；窄表(P3)第 2 格就是描述
            side = cells[1] if len(cells) >= 4 and len(cells[1]) <= 8 else ""
            desc = cells[2] if side else cells[1]
            items.append({
                "id": m.group(1), "alias": ident, "fixed": fixed,
                "side": side, "desc": desc,
            })
        if items:
            out.append((lvl, title, items))
    return out


def one_line(s, n=76):
    """把 markdown 描述壓成一行短句（去 code fence/粗體/換行，過長截斷）。"""
    s = re.sub(r"<br\s*/?>", " ", s)
    s = s.replace("**", "").replace("`", "").replace("\n", " ")   # 保留單顆 * （見上）
    s = re.sub(r"\s+", " ", s).strip()
    # 取到第一個句號/分號為止，讀起來像一句話
    for sep in ("。", "；", "──"):
        if sep in s[:n + 20]:
            s = s.split(sep)[0]
            break
    return (s[:n] + "…") if len(s) > n else s


def count_src():
    rows = []
    for name, rel, exts in MODULES:
        base = os.path.join(ROOT, rel)
        files = lines = 0
        for dp, dn, fn in os.walk(base):
            dn[:] = [d for d in dn if d not in ("build", "bin", "obj", "__pycache__")]
            for f in fn:
                if f.endswith(exts):
                    files += 1
                    try:
                        lines += sum(1 for _ in open(os.path.join(dp, f),
                                                     encoding="utf-8", errors="replace"))
                    except OSError:
                        pass
        rows.append((name, rel, files, lines))
    return rows


def count_docs():
    rows = []
    for rel in DOCS:
        p = os.path.join(ROOT, rel)
        if not os.path.isfile(p):
            continue
        t = open(p, encoding="utf-8", errors="replace").read()
        rows.append((rel, t.count("\n") + 1, len(t)))
    return rows


def main():
    review = parse_review()
    src, docs = count_src(), count_docs()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    commit = git("rev-parse", "--short", "HEAD", default="?")
    branch = git("rev-parse", "--abbrev-ref", "HEAD", default="?")

    L = []
    add = L.append
    add("# CF-AOI 現況儀表板")
    add("")
    add(f"> 生成於 **{now}**（commit `{commit}`，branch `{branch}`）。")
    add("> ⚠️ **這個檔是 `scripts/gen_dashboard.py` 生成的，不要手改**——重跑腳本即更新。")
    add("> 它的存在理由：回答「現在做到哪、下一步做什麼」不該需要翻 14 萬字的 STATUS.md。")
    add("")

    # ── 缺陷收斂 ──
    if review:
        cnt = {lvl: sum(1 for i in its if not i["fixed"]) for lvl, _, its in review}
        act = sum(v for k, v in cnt.items() if k in ("P0", "P1", "P2"))
        add("## 一句話")
        add("")
        add(f"該修的（P0–P2）還有 **{act} 條**，其中 "
            f"**P0（到貨日標準動線直接會撞）{cnt.get('P0', 0)} 條**、"
            f"**P1（靜默漏檢／假 PASS）{cnt.get('P1', 0)} 條**。"
            f"另有 P3 低危 {cnt.get('P3', 0)} 條、P4 刻意未改 {cnt.get('P4', 0)} 條（不列待辦）。")
        add("")
        add("## 缺陷收斂進度")
        add("")
        add("| 級別 | 未修 | 已修 | 總數 | 這一級是什麼 |")
        add("|---|---:|---:|---:|---|")
        for lvl, title, its in review:
            f = sum(1 for i in its if i["fixed"])
            add(f"| **{lvl}** | {len(its) - f} | {f} | {len(its)} | {title.replace('**','')[:34]} |")
        add("")
        add("<small>來源：`docs/code_review_20260802.md`。P4 是「刻意未改」的字串／文件不符項，"
            "不列入待辦。</small>")
        add("")

        # ── P0/P1 未修明細（真正每天要看的）──
        for lvl in ("P0", "P1"):
            hit = [(t_, its) for l_, t_, its in review if l_ == lvl]
            if not hit:
                continue
            title, its = hit[0]
            todo = [i for i in its if not i["fixed"]]
            done = [i for i in its if i["fixed"]]
            add(f"## {lvl} 未修（{len(todo)} 條）— {title.replace('**','')}")
            add("")
            if todo:
                add("| # | 端 | 一句話 |")
                add("|---|---|---|")
                for i in todo:
                    add(f"| `{i['alias']}` | {i['side'] or '—'} | {one_line(i['desc'])} |")
            else:
                add("（全部修完）")
            if done:
                add("")
                add(f"<small>已修：{'、'.join('`' + i['alias'] + '`' for i in done)}</small>")
            add("")

    # ── 最近做了什麼 ──
    log = git("log", "-8", "--format=%h|%ad|%s", "--date=short")
    if log:
        add("## 最近做了什麼")
        add("")
        add("| commit | 日期 | 內容 |")
        add("|---|---|---|")
        for ln in log.split("\n"):
            parts = ln.split("|", 2)
            if len(parts) == 3:
                add(f"| `{parts[0]}` | {parts[1]} | {one_line(parts[2], 68)} |")
        add("")

    # ── 規模 ──
    add("## 規模：程式碼 vs 文件")
    add("")
    add("| 模組 | 路徑 | 檔 | 行 |")
    add("|---|---|---:|---:|")
    for name, rel, f, l in src:
        add(f"| {name} | `{rel}` | {f} | {l:,} |")
    tot_src = sum(l for *_, l in src)
    add(f"| **合計** | | | **{tot_src:,}** |")
    add("")
    tot_doc = sum(c for _, _, c in docs)
    add("| 文件 | 行 | 字 |")
    add("|---|---:|---:|")
    for rel, ln, ch in sorted(docs, key=lambda r: -r[2]):
        add(f"| `{rel}` | {ln:,} | {ch:,} |")
    add(f"| **合計** | | **{tot_doc:,}** |")
    add("")
    add(f"<small>約每行程式碼配 **{tot_doc / max(tot_src,1):.0f} 個中文字**的散文"
        f"（另有教材 HTML 約 93,000 字未計入）。維護成本目前由一個人付，"
        f"帶新人的收益還沒到帳——擴充文件前先想清楚這筆帳。</small>")
    add("")

    # ── 下一步 ──
    if review:
        p0 = [it for lvl, _, its in review if lvl == "P0" for it in its if not it["fixed"]]
        if p0:
            add("## 下一步（P0 未修，由上而下）")
            add("")
            for i in p0[:3]:
                add(f"1. **{i['alias']}**（{i['side'] or '—'}）{one_line(i['desc'], 90)}")
            add("")
            add("<small>完整清單與修法建議見 `docs/code_review_20260802.md`。</small>")
            add("")

    add("---")
    add("")
    add("**其他入口**：架構契約 `docs/CLAUDE.md`｜誠實帳本 `docs/STATUS.md`｜"
        "現場架設 `docs/6cam_setup_runbook.md`｜互動教材 `docs/html/cf-aoi-training.html`。")

    text = "\n".join(L) + "\n"
    if "--stdout" in sys.argv:
        print(text)
        return
    open(OUT, "w", encoding="utf-8").write(text)
    lv = {l: (sum(1 for i in its if not i["fixed"]), len(its)) for l, _, its in review}
    print(f"[ok] {os.path.relpath(OUT, ROOT)}（{len(text)} 字）  "
          + "  ".join(f"{k} {v[0]}/{v[1]} 未修" for k, v in lv.items()))


if __name__ == "__main__":
    main()
