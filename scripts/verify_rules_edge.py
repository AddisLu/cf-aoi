#!/usr/bin/env python3
"""
verify_rules_edge.py — #32 邊界略過 端到端驗證（offline-tcp）

驗 recipe_saving.bypass_edge_x/y 經 LOAD_RECIPE → IP defect_rules 過濾鏈生效：
  Stage A: 無 bypass → 偵測 N（含近邊界缺陷）
  Stage B: bypass_edge_x=50 → 近左邊界缺陷被丟 → N-2
  Stage C: 同圖兩跑缺陷數一致（bit-exact 佐證）

用法：
  ./ip/build/cfaoi_ip --mode offline-tcp --control-port 8200 --output /tmp/rules_out &
  python3 scripts/verify_rules_edge.py [--host 127.0.0.1] [--port 8200]

（#16 Rule 改判各分支由 ip/build/rules_verify 單元測涵蓋；本腳本驗 #32 邊界 + 接線/決定性。）
"""
import argparse, json, socket, sys
import numpy as np

W, H = 8192, 5000
PITCH_X, PITCH_Y = 26, 19
_seq = 0

def tcp_connect(host, port, timeout=10):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout); s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.connect((host, port)); return s

def recv_line(s, timeout=60):
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
    # 3 個中間缺陷（保留）+ 2 個近左邊界但「在 kernel death-margin 外（x>~53）會被偵測」的缺陷。
    # bypass_edge_x=100 → x<=100 的被偵測缺陷遭濾除（death-margin≈pitch_x*2+fast≈53，故 x=70/85 偵測得到）。
    defects = [(2000,2500,220,5,5),(3000,3000,215,4,4),(4000,2000,40,5,5),
               (70,2500,220,5,5),(85,3000,220,4,4)]   # 後兩個 53<x<=100
    for dx,dy,val,dw,dh in defects: img[dy:dy+dh, dx:dx+dw] = val
    return img

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

def load(s, panel, bypass_x=0):
    rs = {"bypass_edge_x": bypass_x} if bypass_x else {}
    return send_cmd(s, "LOAD_RECIPE", {"recipe": "RULES", "recipe_xml": recipe_xml(),
                                       "panel_id": panel, "recipe_saving": rs})

def count(s, img, panel):
    r = send_with_payload(s, "SEND_IMAGE_FOR_REVIEW",
            {"panel_id": panel, "cam_id": 0, "width": W, "height": H,
             "frame_seq": 1, "last": True, "debug": False}, img.tobytes())
    if r.get("status") != "OK": return None
    return sum(len(z.get("DefectInfoList", [])) for z in r.get("result", {}).get("RoiInfoList", []))

results = []
def check(name, cond, detail):
    results.append(cond); print(f"[{'PASS' if cond else 'FAIL'}] {name}\n  {detail}")

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8200); a = ap.parse_args()
    print("="*60 + f"\n  #32 邊界略過 e2e — verify_rules_edge.py  {a.host}:{a.port}\n" + "="*60)
    try: s = tcp_connect(a.host, a.port)
    except Exception as e:
        print(f"[ERROR] 無法連線: {e}\n請先啟動 cfaoi_ip --mode offline-tcp --control-port 8200"); sys.exit(1)
    img = make_image()

    load(s, "A"); nA = count(s, img, "A")
    check("Stage A: 無 bypass → 5 缺陷基準", nA == 5, f"DefectCnt={nA}（3 中間 + 2 近左邊界 x=70/85 偵測得到）")

    load(s, "B", bypass_x=100); nB = count(s, img, "B")
    check("Stage B: bypass_edge_x=100 → 近邊界 2 個丟 → 3", nA == 5 and nB == 3,
          f"nA={nA} nB={nB}（{'PASS：接線生效, 邊界缺陷濾除' if (nA==5 and nB==3) else 'FAIL'}）")

    nB2 = count(s, img, "B")
    check("Stage C: 同圖兩跑一致（bit-exact 佐證）", nB2 == nB, f"nB={nB} nB2={nB2}")

    s.close()
    p = sum(1 for x in results if x); f = len(results) - p
    print("="*60 + f"\n結果: PASS {p} / FAIL {f}\n" + "="*60)
    sys.exit(0 if f == 0 else 1)

if __name__ == "__main__": main()
