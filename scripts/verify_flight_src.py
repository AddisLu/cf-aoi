#!/usr/bin/env python3
"""
verify_flight_src.py — 行車紀錄 incident `src` 欄位（出錯源碼 檔名:行號）端到端驗證

STATUS「行車紀錄」列：`src` 欄位（FR_RECORD_INCIDENT 巨集帶入 __FILE__:__LINE__）原為 L1
（已寫碼，待 Linux/Spark 重編 + 觸發 incident 確認 src 內容正確再升 L3）。

本腳本經 offline-tcp 非破壞性觸發三種 incident（bad_json / frame_validation / recipe_load），
讀 <output>/_diag 確認每筆都帶 repo 相對 `src`（ip/src/...:行號），且該行號**正好命中**
源碼中該 kind 的 `FR_RECORD_INCIDENT` 呼叫點——呼叫點由 `scan_call_sites()` 即時掃描 ip/src 取得，
不再寫死行號（2026-08-02 改；原寫死值早已因改碼位移而失效，見 docs/code_review_20260802.md S6）。
（cuda_fatal / uncaught_exception 屬破壞性，recorder 機制本身已於 2026-06-15 RTX2080 驗；此處驗新增的 src 欄位。）

用法：
  ./ip/build/cfaoi_ip --mode offline-tcp --control-port 8200 --output <OUT> &
  python3 scripts/verify_flight_src.py --out <OUT> [--host 127.0.0.1] [--port 8200]
"""
import argparse, glob, json, os, re, socket, sys
_seq = 0

def tcp_connect(host, port, timeout=10):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout); s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.connect((host, port)); return s

def recv_line(s, timeout=30):
    buf = b""; s.settimeout(timeout)
    while True:
        c = s.recv(1)
        if not c: raise ConnectionError("closed")
        if c == b"\n": return buf.decode()
        buf += c

def send_cmd(s, cmd, params):
    global _seq; _seq += 1
    s.sendall((json.dumps({"cmd": cmd, "seq": _seq, "params": params or {}}) + "\n").encode())
    return json.loads(recv_line(s))

def send_raw(s, raw):
    s.sendall(raw)
    return json.loads(recv_line(s))

# 預期 src 呼叫點：**從源碼即時掃描推導**（2026-08-02 改）。
# 原本寫死行號（bad_json 412 / frame_validation 539,569 / recipe_load 426,593），但每次改碼
# （含只加註解）都會位移 → 複查時實測已與源碼不符（S6）。改為掃 ip/src 找 FR_RECORD_INCIDENT("kind",
# 呼叫點，比對 src 是否**正好命中**其中之一（不再需要 ±3 容差）。
# 驗的不變式不變且更強：incident.src 必須指向該 kind 的真實呼叫點，而非任意行號。
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FR_CALL = re.compile(r'FR_RECORD_INCIDENT\s*\(\s*"([A-Za-z0-9_]+)"')

def scan_call_sites():
    """回傳 {kind: {(repo相對檔名, 行號), ...}}；掃不到源碼時回 {}（呼叫端會退回寬鬆檢查）。"""
    sites = {}
    for root, dirs, files in os.walk(os.path.join(REPO, "ip", "src")):
        dirs[:] = [d for d in dirs if d not in ("build", ".git")]
        for fn in files:
            if not fn.endswith((".cpp", ".h", ".cu", ".cuh")):
                continue
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, REPO).replace(os.sep, "/")
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, 1):
                        m = FR_CALL.search(line)
                        if m:
                            sites.setdefault(m.group(1), set()).add((rel, lineno))
            except OSError:
                pass
    return sites

# 本腳本觸發的三種 kind（觸發手法見 main()）
EXPECT_KINDS = ["bad_json", "frame_validation", "recipe_load"]

results = []
def check(name, cond, detail):
    results.append(cond); print(f"[{'PASS' if cond else 'FAIL'}] {name}\n  {detail}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1"); ap.add_argument("--port", type=int, default=8200)
    ap.add_argument("--out", required=True, help="IP --output 目錄（_diag 在其下）")
    a = ap.parse_args()
    diag = os.path.join(a.out, "_diag")

    s = tcp_connect(a.host, a.port)
    # 1) bad_json：送非 JSON 整行
    r1 = send_raw(s, b"this is definitely not json {{{\n")
    check("觸發 bad_json → ERR（連線存活）", r1.get("status") == "ERR", f"resp={r1.get('status')}")
    # 2) frame_validation：非法尺寸（width=0；驗證在 read_exact 之前 → 不送 payload，socket 不錯位）
    r2 = send_cmd(s, "SEND_IMAGE_FOR_REVIEW",
                  {"panel_id":"fv","width":0,"height":0,"payload_bytes":0,"frame_seq":1})
    check("觸發 frame_validation → ERR", r2.get("status") == "ERR", f"resp={r2.get('status')}")
    # 3) recipe_load：壞 XML（解析丟例外）
    r3 = send_cmd(s, "LOAD_RECIPE", {"recipe":"BAD","recipe_xml":"<Recipe><not closed","panel_id":"rl"})
    check("觸發 recipe_load → ERR", r3.get("status") == "ERR", f"resp={r3.get('status')}")
    s.close()

    # 讀 _diag：jsonl（每事件一行 compact）+ incident_*.json（完整現場）
    jsonl = sorted(glob.glob(os.path.join(diag, "*.jsonl")))
    inc   = sorted(glob.glob(os.path.join(diag, "incident_*.json")))
    check("_diag 落地（jsonl + incident_*.json）",
          len(jsonl) >= 1 and len(inc) >= 1, f"jsonl={len(jsonl)} incident={len(inc)} dir={diag}")

    events = []
    for jl in jsonl:
        with open(jl, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try: events.append(json.loads(line))
                    except Exception: pass
    # incident json 也解析（驗 src 在完整現場亦有）
    inc_objs = []
    for p in inc:
        try:
            with open(p, encoding="utf-8") as f: inc_objs.append(json.load(f))
        except Exception: pass

    # 只對 incident 事件要求 src（type=session 的開機紀錄無 src 屬正常）
    incidents = [e for e in events if e.get("type") == "incident"]
    check("每筆 incident event 都有非空 src 欄位",
          len(incidents) > 0 and all(e.get("src") for e in incidents),
          "; ".join(f"{e.get('kind')}={e.get('src')}" for e in incidents) or "no incidents")

    pat = re.compile(r"^(ip/src/[^:]+):(\d+)$")
    sites = scan_call_sites()          # 從源碼推導真實呼叫點（見檔案上方說明）
    if not sites:
        print("[warn] 掃不到 ip/src 源碼（非 repo 內執行？）→ 退回寬鬆檢查：只驗 src 格式")
    for kind in EXPECT_KINDS:
        ev = next((e for e in incidents if e.get("kind") == kind), None)
        if ev is None:
            check(f"{kind}: 有事件且 src 正確", False, "找不到該 kind 事件")
            continue
        src = ev.get("src", "")
        m = pat.match(src)
        expected = sites.get(kind, set())
        if not sites:                   # 無源碼可比 → 只確認格式合法
            ok, detail = bool(m), f"src={src}（未比對呼叫點：無源碼）"
        else:
            ok = bool(m) and (m.group(1), int(m.group(2))) in expected
            detail = f"src={src}（該 kind 源碼呼叫點：{sorted(f'{f}:{l}' for f, l in expected) or '無'}）"
        check(f"{kind}: src 命中真實 FR_RECORD_INCIDENT 呼叫點", ok, detail)

    # incident_*.json 完整現場也帶 src
    check("incident_*.json 完整現場亦含 src",
          len(inc_objs) > 0 and all(o.get("src") for o in inc_objs),
          "; ".join(f"{o.get('kind')}={o.get('src')}" for o in inc_objs) or "no incident files")

    ok = all(results)
    print(f"\n{'='*54}\n行車紀錄 src：{sum(results)}/{len(results)} PASS — {'ALL PASS ✓' if ok else 'HAS FAIL ✗'}")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
