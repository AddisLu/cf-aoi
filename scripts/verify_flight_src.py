#!/usr/bin/env python3
"""
verify_flight_src.py — 行車紀錄 incident `src` 欄位（出錯源碼 檔名:行號）端到端驗證

STATUS「行車紀錄」列：`src` 欄位（FR_RECORD_INCIDENT 巨集帶入 __FILE__:__LINE__）原為 L1
（已寫碼，待 Linux/Spark 重編 + 觸發 incident 確認 src 內容正確再升 L3）。

本腳本經 offline-tcp 非破壞性觸發三種 incident，讀 <output>/_diag 確認每筆都帶
repo 相對 `src`（ip/src/...:行號），且行號落在預期呼叫點：
  bad_json         → ip/src/control_server.cpp:407
  frame_validation → ip/src/control_server.cpp:534
  recipe_load      → ip/src/main.cpp:357
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

# 預期 src 呼叫點（檔名須對；行號比對已知 FR_RECORD_INCIDENT 呼叫點集合，±3 行容編輯漂移）。
# recipe_load 有兩處呼叫點：main.cpp:357（解析丟例外）/ :466（找不到 DetectRoi）；本腳本的壞 XML 命中後者。
EXPECT = {
    "bad_json":         ("ip/src/control_server.cpp", {407}),
    "frame_validation": ("ip/src/control_server.cpp", {534, 564}),
    "recipe_load":      ("ip/src/main.cpp",            {357, 466}),
}

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
    for kind, (efile, elines) in EXPECT.items():
        ev = next((e for e in incidents if e.get("kind") == kind), None)
        if ev is None:
            check(f"{kind}: 有事件且 src 正確", False, "找不到該 kind 事件")
            continue
        src = ev.get("src", "")
        m = pat.match(src)
        ok = bool(m) and m.group(1) == efile and any(abs(int(m.group(2)) - L) <= 3 for L in elines)
        check(f"{kind}: src={src}（期望 {efile}:{sorted(elines)}）", ok, f"src={src}")

    # incident_*.json 完整現場也帶 src
    check("incident_*.json 完整現場亦含 src",
          len(inc_objs) > 0 and all(o.get("src") for o in inc_objs),
          "; ".join(f"{o.get('kind')}={o.get('src')}" for o in inc_objs) or "no incident files")

    ok = all(results)
    print(f"\n{'='*54}\n行車紀錄 src：{sum(results)}/{len(results)} PASS — {'ALL PASS ✓' if ok else 'HAS FAIL ✗'}")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
