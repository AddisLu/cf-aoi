#!/usr/bin/env python3
"""
verify_flight_v2.py — 行車紀錄器 2026-07-12 盲區收口功能 端到端驗證（commit a441005+）

驗證五項新能力（見 ip/CLAUDE.md 不變式 16「2026-07-12 修訂」）：
  A. record_recipe   LOAD_RECIPE 成功 → jsonl 出現 type="recipe" 行（含每 zone algo_mode/參數）
  B. incident 節流   連續 5 筆 bad_json → 只寫 1 個完整 incident 檔 + incident_suppressed 摘要行
  C. defect_flood    Pitch 錯配的網格圖（>10000 缺陷觸頂）→ 自動 defect_flood incident
                     （訊號取 GPU 過濾前計數——配方開 BlobMinSize 也不會遮蔽）
  D. tick_stats      連送 ≥200 張小圖 → jsonl 出現 type="stats" 行（fps/gpu_ms p50/p95/queue）
  E. ZoneSnap 擴欄   incident 的 current_frame.zones[] 帶 algo_mode/multiscale/blob 等新欄位

用法（與 verify_flight_src.py 相同模式）：
  ./ip/build/cfaoi_ip --mode offline-tcp --control-port 8200 --output /tmp/frv2_out &
  python3 scripts/verify_flight_v2.py --out /tmp/frv2_out [--host 127.0.0.1] [--port 8200]

另兩項手動 regression（腳本外，各一條命令）：
  bench no-op：./cfaoi_ip --mode bench --input <img> → 確認 <cwd>/output/_diag 無新檔
  決定性：    ./cfaoi_ip --mode offline-file --input <img> --verify-deterministic → ALL bit-exact
"""
import argparse, glob, json, os, socket, sys, time

# 純 Python 影像產生（不依賴 numpy——驗證機系統 python 可能沒裝）
def uniform_bytes(w, h, val):
    return bytes([val]) * (w * h)

def grid_bytes(w, h, bg, dot, step):
    """亮點網格：每 step px 一顆 3×3 dot 塊，其餘 bg（單像素撐不起 DIV 比例差 → 用 3×3 塊）。"""
    dot_row = bytearray([bg]) * w
    for x in range(0, w, step):
        for dx in range(3):
            if x + dx < w: dot_row[x + dx] = dot
    dot_row = bytes(dot_row)
    plain_row = bytes([bg]) * w
    rows = []
    for y in range(h):
        rows.append(dot_row if (y % step) < 3 else plain_row)
    return b"".join(rows)

_seq = 0

def tcp_connect(host, port, timeout=15):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout); s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.connect((host, port)); return s

def recv_line(s, timeout=120):
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

def send_with_payload(s, cmd, params, payload: bytes):
    global _seq; _seq += 1
    s.sendall((json.dumps({"cmd": cmd, "seq": _seq, "params": params or {}}) + "\n").encode())
    s.sendall(payload)
    return json.loads(recv_line(s))

def recipe_xml(pitch_x, pitch_y, blob_min=0):
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Recipe><M_AlignRoi><AlignEnable>false</AlignEnable></M_AlignRoi>
<DetectRoiList><DetectRoi>
<StartX>-1</StartX><StartY>-1</StartY><EndX>-1</EndX><EndY>-1</EndY>
<AlgorithmWay>8-Way-Star</AlgorithmWay><AlgorithmCompare>DIV</AlgorithmCompare>
<BrightThreshold>1.4</BrightThreshold><DarkThreshold>0.6</DarkThreshold>
<PitchX>{pitch_x}</PitchX><PitchY>{pitch_y}</PitchY><SearchX>1</SearchX><SearchY>1</SearchY>
<BlobMinSize>{blob_min}</BlobMinSize><BlobMaxSize>0</BlobMaxSize>
</DetectRoi></DetectRoiList><DetectIoiList/></Recipe>"""

def load_flood_image(path_arg):
    """載入 flood 測試影像 → (bytes, w, h)。TIF 用 Pillow；PGM 純 Python。找不到回 (None,0,0)。"""
    candidates = [path_arg] if path_arg else []
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "..", "test_images", "IP01_Origin000002.tif"))
    for p in candidates:
        if not p or not os.path.isfile(p): continue
        if p.lower().endswith(".pgm"):
            with open(p, "rb") as f: data = f.read()
            # 極簡 P5 解析（magic\n w h\n maxval\n raw）
            parts = data.split(b"\n", 3)
            if parts[0].strip() != b"P5": continue
            w, h = map(int, parts[1].split())
            return parts[3][: w * h], w, h
        try:
            from PIL import Image
            im = Image.open(p).convert("L")
            return im.tobytes(), im.width, im.height
        except ImportError:
            print(f"[SKIP] 需要 Pillow 解 {p}（pip3 install pillow）")
            return None, 0, 0
    return None, 0, 0

def read_events(diag_dir):
    events = []
    for jl in sorted(glob.glob(os.path.join(diag_dir, "*.jsonl"))):
        with open(jl, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try: events.append(json.loads(line))
                    except Exception: pass
    return events

results = []
def check(name, cond, detail=""):
    results.append(cond); print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"\n  {detail}" if detail else ""))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1"); ap.add_argument("--port", type=int, default=8200)
    ap.add_argument("--out", required=True, help="IP --output 目錄（_diag 在其下）")
    ap.add_argument("--stats-frames", type=int, default=205, help="D 項送幀數（≥200 才會出 stats 行）")
    ap.add_argument("--flood-image", default=None,
                    help="C 項爆量測試影像（TIF 需 Pillow / PGM 免依賴）；預設找 test_images/IP01_Origin000002.tif")
    a = ap.parse_args()
    diag = os.path.join(a.out, "_diag")
    pre_inc_files = set(glob.glob(os.path.join(diag, "incident_*.json")))
    pre_events = len(read_events(diag))

    s = tcp_connect(a.host, a.port)

    # ── A. record_recipe：LOAD_RECIPE 成功留痕 ──────────────────────────────
    r = send_cmd(s, "LOAD_RECIPE", {"recipe": "FRV2_A", "recipe_xml": recipe_xml(26, 19), "panel_id": "frv2"})
    check("A0 LOAD_RECIPE(合法 DIV) → OK", r.get("status") == "OK", f"resp={r}")
    ev = read_events(diag)[pre_events:]
    rec = [e for e in ev if e.get("type") == "recipe"]
    ok = len(rec) >= 1 and rec[-1].get("zones") and "algo_mode" in rec[-1]["zones"][0]
    check("A1 jsonl 有 type=recipe 行且 zones[0] 帶 algo_mode",
          ok, f"recipe行={len(rec)} 內容={json.dumps(rec[-1], ensure_ascii=False)[:200] if rec else '無'}")
    if rec:
        check("A2 recipe 行 label 含配方名 + algo_mode=0(DIV)",
              "FRV2_A" in rec[-1].get("label", "") and rec[-1]["zones"][0].get("algo_mode") == 0,
              f"label={rec[-1].get('label')} algo_mode={rec[-1]['zones'][0].get('algo_mode')}")

    # ── B. incident 節流：連續 5 筆 bad_json ────────────────────────────────
    for i in range(5):
        global _seq
        s.sendall(b"not json at all {{{\n")
        resp = json.loads(recv_line(s))
        assert resp.get("status") == "ERR", f"bad_json #{i} 未回 ERR"
    time.sleep(0.5)
    new_inc_files = set(glob.glob(os.path.join(diag, "incident_*.json"))) - pre_inc_files
    bad_files = []
    for p in new_inc_files:
        try:
            with open(p, encoding="utf-8") as f:
                if json.load(f).get("kind") == "bad_json": bad_files.append(p)
        except Exception: pass
    ev = read_events(diag)[pre_events:]
    n_bad_inc = sum(1 for e in ev if e.get("type") == "incident" and e.get("kind") == "bad_json")
    n_supp    = sum(1 for e in ev if e.get("type") == "incident_suppressed" and e.get("kind") == "bad_json")
    check("B1 5 筆 bad_json → 完整 incident 檔恰 1 個（30s 節流窗）",
          len(bad_files) == 1, f"bad_json incident 檔={len(bad_files)}")
    check("B2 jsonl：incident 行=1 + incident_suppressed 摘要 ≥1",
          n_bad_inc == 1 and n_supp >= 1, f"incident行={n_bad_inc} suppressed行={n_supp}")

    # ── C. defect_flood：真實面板圖 + 錯 pitch 30/30 → 觸頂 ─────────────────
    # 這正是 documented 生產爆量情境（不變式 14：pitch 26→30 → 561→10000）。
    # 合成圖注意：與 pitch 同週期的點陣會被演算法視為正常網格、平滑/噪點被 best-match
    # 搜尋抵消 → 觸不了頂（2026-07-12 實測）；故 flood 測試必須用真實紋理影像。
    # 配方帶 BlobMinSize=5（會把小顆全洗掉）→ 同時驗「訊號取過濾前計數、不被 Blob 遮蔽」。
    flood_img, fw, fh = load_flood_image(a.flood_image)
    if flood_img is None:
        print("[SKIP] C/E：找不到 flood 測試影像（--flood-image 或 test_images/IP01_Origin000002.tif）")
    else:
        r = send_cmd(s, "LOAD_RECIPE", {"recipe": "FRV2_C", "recipe_xml": recipe_xml(30, 30, blob_min=5), "panel_id": "flood"})
        check("C0 LOAD_RECIPE(錯 pitch + BlobMinSize=5) → OK", r.get("status") == "OK", f"resp={r}")
        r = send_with_payload(s, "SEND_IMAGE_FOR_REVIEW",
                              {"panel_id": "frv2_flood", "cam_id": 0, "width": fw, "height": fh,
                               "payload_bytes": fw * fh, "frame_seq": 1, "last": True},
                              flood_img)
        check("C1 爆量影像處理完成（回 OK）", r.get("status") == "OK", f"resp status={r.get('status')}")
        ev = read_events(diag)[pre_events:]
        floods = [e for e in ev if e.get("type") == "incident" and e.get("kind") == "defect_flood"]
        check("C2 defect_flood incident 落地（過濾前訊號）",
              len(floods) >= 1, f"defect_flood 行={len(floods)} detail={floods[-1].get('detail','')[:120] if floods else '無'}")

        # ── E. ZoneSnap 擴欄：flood incident 的 current_frame.zones 帶新欄位 ──
        zones_ok = False; sample = ""
        for p in sorted(glob.glob(os.path.join(diag, "incident_*.json"))):
            if p in pre_inc_files: continue
            try:
                with open(p, encoding="utf-8") as f: o = json.load(f)
            except Exception: continue
            if o.get("kind") != "defect_flood": continue
            zs = (o.get("current_frame") or {}).get("zones") or []
            if zs and all(k in zs[0] for k in ("algo_mode", "multiscale", "blob", "pitch_times", "choose_amount")):
                zones_ok = True; sample = json.dumps(zs[0], ensure_ascii=False)[:180]
        check("E1 incident current_frame.zones 帶 algo_mode/multiscale/blob/pitch_times/choose_amount",
              zones_ok, sample or "flood incident 無 zones 或缺欄位")

    # ── D. tick_stats：連送 ≥200 張小圖 ────────────────────────────────────
    r = send_cmd(s, "LOAD_RECIPE", {"recipe": "FRV2_D", "recipe_xml": recipe_xml(26, 19), "panel_id": "stats"})
    assert r.get("status") == "OK"
    w, h = 256, 256
    quiet = uniform_bytes(w, h, 128)   # 均勻圖 → 0 缺陷、處理快
    t0 = time.time()
    okc = 0
    for i in range(a.stats_frames):
        r = send_with_payload(s, "SEND_IMAGE_FOR_REVIEW",
                              {"panel_id": f"st{i:04d}", "cam_id": 0, "width": w, "height": h,
                               "payload_bytes": w * h, "frame_seq": i + 2, "last": True},
                              quiet)
        if r.get("status") == "OK": okc += 1
    check(f"D0 {a.stats_frames} 張小圖全部處理 OK（{time.time()-t0:.1f}s）",
          okc == a.stats_frames, f"ok={okc}/{a.stats_frames}")
    ev = read_events(diag)[pre_events:]
    stats = [e for e in ev if e.get("type") == "stats"]
    ok = (len(stats) >= 1 and stats[-1].get("frames") == 200
          and stats[-1].get("fps", 0) > 0 and "gpu_ms_p50" in stats[-1] and "gpu_ms_p95" in stats[-1])
    check("D1 jsonl 有 type=stats 行（frames=200, fps>0, p50/p95）",
          ok, json.dumps(stats[-1], ensure_ascii=False)[:200] if stats else "無 stats 行")

    s.close()
    ok = all(results)
    print(f"\n{'='*60}\n行車紀錄 v2：{sum(results)}/{len(results)} PASS — {'ALL PASS ✓' if ok else 'HAS FAIL ✗'}")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
