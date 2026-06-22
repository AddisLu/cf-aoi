#!/usr/bin/env python3
"""
verify_remote_image.py — 「從 IP 載入影像」HARD verify（offline-tcp）

不變式（docs/STATUS.md「從 IP 載入影像」列）：
  REVIEW_LOCAL_IMAGE(IP 機磁碟某圖) 的缺陷結果  ==  SEND_IMAGE_FOR_REVIEW(同一張圖上傳) 的缺陷結果
  （兩者走同一 process_image → bit-exact；REVIEW 讀全解析度 IMREAD_UNCHANGED，預覽縮圖 display-only 絕不進檢測）

驗：
  Stage A  REVIEW_LOCAL_IMAGE == SEND_IMAGE_FOR_REVIEW（逐缺陷欄位 bit-exact）
  Stage B  REVIEW_LOCAL_IMAGE 同圖兩跑 bit-exact（決定性）
  Stage C  REVIEW_LOCAL_IMAGE 不存在路徑 → ERR（不 crash）
  Stage D  GET_IMAGE_PREVIEW 回全解析度寬高 + 縮圖（display-only，不等於檢測來源）

用法：
  ./ip/build/cfaoi_ip --mode offline-tcp --control-port 8200 --output /tmp/ri_out &
  python3 scripts/verify_remote_image.py [--host 127.0.0.1] [--port 8200] [--imgdir /tmp/ri_img]

落地檔（PGM P5，未壓縮 raw → 與 SEND 的 payload 逐位元相同，無編碼器對稱性假設）。
"""
import argparse, json, os, socket, sys
import numpy as np

W, H = 8192, 5000
PITCH_X, PITCH_Y = 26, 19
_seq = 0

def tcp_connect(host, port, timeout=10):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout); s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.connect((host, port)); return s

def recv_line(s, timeout=90):
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

def send_with_payload(s, cmd, params, payload):
    global _seq; _seq += 1
    params["payload_bytes"] = len(payload)
    s.sendall((json.dumps({"cmd": cmd, "seq": _seq, "params": params}) + "\n").encode())
    s.sendall(payload); return json.loads(recv_line(s))

def make_image():
    img = np.full((H, W), 128, dtype=np.uint8)
    for r in range(0, H, PITCH_Y): img[r:r+2, :] = 140
    for c in range(0, W, PITCH_X): img[:, c:c+2] = 140
    defects = [(2000,2500,220,5,5),(3000,3000,215,4,4),(4000,2000,40,5,5),
               (1500,1200,30,6,6),(6000,4000,230,5,5)]
    for dx,dy,val,dw,dh in defects: img[dy:dy+dh, dx:dx+dw] = val
    return img

def write_pgm(path, img):
    """PGM P5（未壓縮 raw）→ cv::imread(IMREAD_UNCHANGED) 得到逐位元相同的 Mono8。"""
    with open(path, "wb") as f:
        f.write(b"P5\n%d %d\n255\n" % (img.shape[1], img.shape[0]))
        f.write(img.tobytes())

def recipe_xml():
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Recipe><M_AlignRoi><AlignEnable>false</AlignEnable></M_AlignRoi>
<DetectRoiList><DetectRoi>
<StartX>-1</StartX><StartY>-1</StartY><EndX>-1</EndX><EndY>-1</EndY>
<AlgorithmWay>8-Way-Star</AlgorithmWay><AlgorithmCompare>DIV</AlgorithmCompare>
<BrightThreshold>1.4</BrightThreshold><DarkThreshold>0.6</DarkThreshold>
<PitchX>{PITCH_X}</PitchX><PitchY>{PITCH_Y}</PitchY><SearchX>1</SearchX><SearchY>1</SearchY>
<BlobMinSize>2</BlobMinSize><BlobMaxSize>500</BlobMaxSize>
</DetectRoi></DetectRoiList><DetectIoiList/></Recipe>"""

# 逐缺陷的「檢測幾何欄位」簽章（排序後比對 → 與送圖順序無關）。
SIG_KEYS = ("GC_X","GC_Y","Size","Width","Height","Type",
            "X_Min","X_Max","Y_Min","Y_Max","GL_Mean")
def signature(resp):
    if resp.get("status") != "OK": return None
    sigs = []
    for z in resp.get("result", {}).get("RoiInfoList", []):
        for d in z.get("DefectInfoList", []):
            sigs.append(tuple(d.get(k) for k in SIG_KEYS))
    return sorted(sigs)

results = []
def check(name, cond, detail):
    results.append(cond); print(f"[{'PASS' if cond else 'FAIL'}] {name}\n  {detail}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1"); ap.add_argument("--port", type=int, default=8200)
    ap.add_argument("--imgdir", default="/tmp/ri_img")
    a = ap.parse_args()
    os.makedirs(a.imgdir, exist_ok=True)
    pgm = os.path.join(a.imgdir, "remote_review_test.pgm")
    img = make_image(); write_pgm(pgm, img)
    print(f"[setup] 落地 {pgm} ({W}x{H} Mono8, {os.path.getsize(pgm)} bytes)")

    s = tcp_connect(a.host, a.port)
    lr = send_cmd(s, "LOAD_RECIPE", {"recipe": "REMOTEIMG", "recipe_xml": recipe_xml(),
                                     "panel_id": "ri_load"})
    check("LOAD_RECIPE OK", lr.get("status") == "OK", f"resp={lr.get('status')}")

    # Stage A：REVIEW(磁碟) vs SEND(上傳同 raw) 逐欄位 bit-exact
    r_send   = send_with_payload(s, "SEND_IMAGE_FOR_REVIEW",
                 {"panel_id":"ri_send","cam_id":0,"width":W,"height":H,
                  "frame_seq":1,"last":True,"debug":False}, img.tobytes())
    r_review = send_cmd(s, "REVIEW_LOCAL_IMAGE", {"path": pgm, "panel_id":"ri_review"})
    sig_send, sig_rev = signature(r_send), signature(r_review)
    check("Stage A: SEND/REVIEW 皆 OK", sig_send is not None and sig_rev is not None,
          f"send={r_send.get('status')} review={r_review.get('status')}")
    check("Stage A: 缺陷數 > 0（有意義的測試）",
          bool(sig_send) and len(sig_send) > 0, f"n_send={len(sig_send or [])}")
    check("Stage A: REVIEW_LOCAL_IMAGE == SEND_IMAGE_FOR_REVIEW（逐缺陷欄位 bit-exact）",
          sig_send == sig_rev,
          f"n_send={len(sig_send or [])} n_review={len(sig_rev or [])} "
          f"identical={sig_send == sig_rev}")

    # Stage B：REVIEW 同圖兩跑 bit-exact（決定性）
    r_rev2 = send_cmd(s, "REVIEW_LOCAL_IMAGE", {"path": pgm, "panel_id":"ri_review2"})
    sig_rev2 = signature(r_rev2)
    check("Stage B: REVIEW 同圖兩跑 bit-exact（決定性）",
          sig_rev == sig_rev2, f"run1={len(sig_rev or [])} run2={len(sig_rev2 or [])}")

    # Stage C：不存在路徑 → ERR（不 crash）
    r_err = send_cmd(s, "REVIEW_LOCAL_IMAGE", {"path":"/tmp/__no_such_image__.pgm","panel_id":"ri_err"})
    check("Stage C: 不存在路徑 → ERR（不 crash）",
          r_err.get("status") == "ERR", f"resp={r_err.get('status')} err={r_err.get('error','')[:60]}")

    # Stage D：GET_IMAGE_PREVIEW 回全解析度寬高 + 縮圖 bytes（display-only）
    r_prev = send_cmd(s, "GET_IMAGE_PREVIEW", {"path": pgm, "max_width": 2048})
    fw = r_prev.get("full_width") or r_prev.get("image_width") or r_prev.get("width")
    fh = r_prev.get("full_height") or r_prev.get("image_height") or r_prev.get("height")
    has_png = bool(r_prev.get("png_base64") or r_prev.get("preview_png_base64"))
    check("Stage D: GET_IMAGE_PREVIEW 回全解析度寬高 + 縮圖",
          r_prev.get("status") == "OK" and fw == W and fh == H and has_png,
          f"status={r_prev.get('status')} full={fw}x{fh}(期望{W}x{H}) png={has_png}")

    s.close()
    ok = all(results)
    print(f"\n{'='*54}\n總結：{sum(results)}/{len(results)} PASS — {'ALL PASS ✓' if ok else 'HAS FAIL ✗'}")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
