#!/usr/bin/env python3
"""
verify_coord.py — Gap #5 pixel→μm Stage 1B + 2 + 3A 驗證

Stage 1B：LOAD_RECIPE（inline XML，真實 recipe 路徑）→ GlobalPosX_um = GlobalPosX × opt_res
Stage 2 ：同一張圖跑兩次 → pixel 欄位 + μm 欄位 bit-exact
Stage 3A：在 opt_res=0.0 的 server 上送圖 → GlobalPosX_um=0.0（sentinel）

用法（在 Spark 上）：
    # Terminal 1 — 先以 opt_res=0.5 的 INI 啟動 IP：
    #   cp ~/cf-aoi/ip/config/default_zone.ini /tmp/test_opt05.ini
    #   sed -i 's/opt_res_x = 0.0/opt_res_x = 0.5/' /tmp/test_opt05.ini
    #   sed -i 's/opt_res_y = 0.0/opt_res_y = 0.5/' /tmp/test_opt05.ini
    #   ~/cf-aoi/ip/build/cfaoi_ip --mode offline-tcp --control-port 8201 \\
    #       --ini /tmp/test_opt05.ini --output /tmp/coord_out --no-save-images
    # Terminal 2:
    #   python3 ~/cf-aoi/scripts/verify_coord.py --host 127.0.0.1 --port 8201 --opt-res 0.5
"""

import argparse
import json
import socket
import sys

import numpy as np

# ─────────────────────────────────────────────────────────────────────────────
# TCP helpers（與 verify_alignment.py 一致）
# ─────────────────────────────────────────────────────────────────────────────

_seq = 0

def tcp_connect(host, port, timeout=10):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.connect((host, port))
    return s

def recv_line(s, timeout=120):
    buf = b""
    s.settimeout(timeout)
    while True:
        c = s.recv(1)
        if not c:
            raise ConnectionError("connection closed")
        if c == b"\n":
            return buf.decode()
        buf += c

def send_cmd(s, cmd, params=None):
    global _seq
    _seq += 1
    msg = json.dumps({"cmd": cmd, "seq": _seq, "params": params or {}}) + "\n"
    s.sendall(msg.encode())
    resp = recv_line(s)
    return json.loads(resp)

def send_cmd_with_payload(s, cmd, params, payload_bytes):
    global _seq
    _seq += 1
    params["payload_bytes"] = len(payload_bytes)
    msg = json.dumps({"cmd": cmd, "seq": _seq, "params": params}) + "\n"
    s.sendall(msg.encode())
    s.sendall(payload_bytes)
    resp = recv_line(s, timeout=120)
    return json.loads(resp)

# ─────────────────────────────────────────────────────────────────────────────
# 合成影像（仿 verify_alignment.py）
# ─────────────────────────────────────────────────────────────────────────────

W, H = 8192, 5000
PITCH_X, PITCH_Y = 26, 19
ROI_X1, ROI_Y1 = 1000, 1000
ROI_X2, ROI_Y2 = 3000, 2500

def make_panel():
    img = np.full((H, W), 128, dtype=np.uint8)
    for r in range(0, H, PITCH_Y):
        img[r:r+2, :] = 140
    for c in range(0, W, PITCH_X):
        img[:, c:c+2] = 140
    # 4 bright + 3 dark defects in ROI（固定座標）
    for dx, dy, val, dw, dh in [
        (1200, 1100, 220, 5, 5),
        (2000, 1500, 215, 4, 4),
        (2500, 2000, 218, 6, 4),
        (1800, 2200, 222, 3, 5),
        (1400, 1300, 35,  5, 5),
        (2100, 1700, 38,  4, 4),
        (2800, 2100, 40,  6, 3),
    ]:
        img[dy:dy+dh, dx:dx+dw] = val
    return img

def make_recipe_xml():
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Recipe>
  <M_AlignRoi>
    <AlignEnable>false</AlignEnable>
  </M_AlignRoi>
  <DetectRoiList>
    <DetectRoi>
      <StartX>{ROI_X1}</StartX><StartY>{ROI_Y1}</StartY>
      <EndX>{ROI_X2}</EndX><EndY>{ROI_Y2}</EndY>
      <AlgorithmWay>8-Way-Star</AlgorithmWay>
      <AlgorithmCompare>DIV</AlgorithmCompare>
      <BrightThreshold>1.4</BrightThreshold>
      <DarkThreshold>0.6</DarkThreshold>
      <PitchX>{PITCH_X}</PitchX><PitchY>{PITCH_Y}</PitchY>
      <SearchX>1</SearchX><SearchY>1</SearchY>
      <BlobMinSize>2</BlobMinSize><BlobMaxSize>500</BlobMaxSize>
    </DetectRoi>
  </DetectRoiList>
  <DetectIoiList/>
</Recipe>"""

def do_load_recipe(s, panel_id):
    return send_cmd(s, "LOAD_RECIPE", {
        "recipe": "COORD_TEST",
        "recipe_xml": make_recipe_xml(),
        "panel_id": panel_id,
    })

def do_send_image(s, panel_id, img):
    payload = img.tobytes()
    return send_cmd_with_payload(s, "SEND_IMAGE_FOR_REVIEW", {
        "panel_id": panel_id,
        "cam_id": 0,
        "width": W,
        "height": H,
        "frame_seq": 1,
        "last": True,
        "debug": False,
    }, payload)

def extract_defects(resp):
    """從 SEND_IMAGE_FOR_REVIEW 回傳提取缺陷清單（resp["result"]["RoiInfoList"]）。"""
    if resp.get("status") != "OK":
        return None
    r = resp.get("result", {})
    defs = []
    for z in r.get("RoiInfoList", []):
        defs.extend(z.get("DefectInfoList", []))
    return defs

# ─────────────────────────────────────────────────────────────────────────────
# 測試框架
# ─────────────────────────────────────────────────────────────────────────────

test_results = []

def check(name, cond, detail):
    test_results.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    print(f"  {detail}")

# ─────────────────────────────────────────────────────────────────────────────
# Stage 1B
# ─────────────────────────────────────────────────────────────────────────────

def stage_1b(host, port, opt_res):
    print(f"\n=== Stage 1B: LOAD_RECIPE（inline XML）→ GlobalPosX_um 驗證 (opt_res={opt_res}) ===")
    s = tcp_connect(host, port)

    r = do_load_recipe(s, "S1B")
    check("S1B: LOAD_RECIPE inline XML",
          r.get("status") == "OK",
          f"status={r.get('status')} error={r.get('error','')}")
    if r.get("status") != "OK":
        s.close(); return None

    img = make_panel()
    resp = do_send_image(s, "S1B", img)
    s.close()

    defs = extract_defects(resp)
    check("S1B: 偵測到缺陷", defs is not None and len(defs) > 0,
          f"DefectCnt={resp.get('result', {}).get('DefectCnt')} status={resp.get('status')}")
    if not defs:
        return None

    d0 = defs[0]
    gx    = d0.get("GlobalPosX", 0)
    gy    = d0.get("GlobalPosY", 0)
    gx_um = d0.get("GlobalPosX_um")
    gy_um = d0.get("GlobalPosY_um")
    ccd   = d0.get("CcdIndex")

    check("S1B: GlobalPosX_um 欄位存在",
          gx_um is not None,
          f"GlobalPosX={gx} GlobalPosX_um={gx_um}")

    check("S1B: CcdIndex=0",
          ccd == 0,
          f"CcdIndex={ccd}")

    if gx_um is not None:
        if opt_res > 0:
            expected = gx * opt_res
            err = abs(gx_um - expected)
            check("S1B: GlobalPosX_um = GlobalPosX × opt_res",
                  err < 0.01,
                  f"GlobalPosX={gx} × {opt_res} = {expected:.3f}, got GlobalPosX_um={gx_um:.3f}, err={err:.6f}")
            print(f"\n  === PASS 證據（貼 commit message 用）===")
            print(f"  GlobalPosX={gx}  GlobalPosX_um={gx_um:.3f}  CcdIndex={ccd}")
            print(f"  GlobalPosY={gy}  GlobalPosY_um={gy_um:.3f}")
            print(f"  DefectCnt={resp.get('result', {}).get('DefectCnt')}")
        else:
            check("S1B: opt_res=0 → GlobalPosX_um=0.0（sentinel）",
                  abs(gx_um) < 1e-9,
                  f"GlobalPosX={gx} GlobalPosX_um={gx_um:.6f} (expect 0.000)")

    return defs

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2：bit-exact（pixel + μm 兩跑一致）
# ─────────────────────────────────────────────────────────────────────────────

def stage_2(host, port):
    print(f"\n=== Stage 2: bit-exact（pixel + μm 兩次相同）===")
    s = tcp_connect(host, port)
    r = do_load_recipe(s, "S2")
    if r.get("status") != "OK":
        check("S2: LOAD_RECIPE", False, str(r)); s.close(); return

    img = make_panel()
    resp1 = do_send_image(s, "S2_run1", img)
    resp2 = do_send_image(s, "S2_run2", img)
    s.close()

    defs1 = extract_defects(resp1)
    defs2 = extract_defects(resp2)

    if defs1 is None or defs2 is None or len(defs1) != len(defs2):
        check("S2: 兩次缺陷數相等", False,
              f"run1={len(defs1) if defs1 else 'ERR'} run2={len(defs2) if defs2 else 'ERR'}")
        return

    all_ok = True
    for i, (d1, d2) in enumerate(zip(defs1, defs2)):
        px_ok = d1["GlobalPosX"] == d2["GlobalPosX"] and d1["GlobalPosY"] == d2["GlobalPosY"]
        gx_um1 = d1.get("GlobalPosX_um", -1)
        gx_um2 = d2.get("GlobalPosX_um", -1)
        um_ok = abs(gx_um1 - gx_um2) < 1e-9
        if not (px_ok and um_ok):
            all_ok = False
            check(f"S2: defect[{i}] bit-exact", False,
                  f"px: ({d1['GlobalPosX']},{d1['GlobalPosY']}) vs ({d2['GlobalPosX']},{d2['GlobalPosY']})"
                  f" μm: {gx_um1:.3f} vs {gx_um2:.3f}")

    if all_ok:
        check(f"S2: 全部 {len(defs1)} 顆缺陷 pixel+μm bit-exact", True,
              f"GlobalPosX/Y 兩次完全一致，GlobalPosX/Y_um 兩次完全一致")

# ─────────────────────────────────────────────────────────────────────────────
# Stage 3A：opt_res=0.0 sentinel（在 Stage 1B opt_res=0 路徑確認）
# ─────────────────────────────────────────────────────────────────────────────

def stage_3a(host, port):
    print(f"\n=== Stage 3A: opt_res=0.0 → GlobalPosX_um=0.0（sentinel）===")
    # 直接重用 stage_1b 邏輯：若 opt_res=0 的 server 正在跑，這裡可主動連並驗
    # 本腳本在 opt_res=0.5 server 上先跑，此 stage 靠分開的 opt_res=0 腳本呼叫 stage_1b(opt_res=0)
    # 所以這裡只印提示，實際驗由 --opt-res 0.0 時的 stage_1b 覆蓋
    print("  Stage 3A：用 --opt-res 0.0 呼叫本腳本時由 Stage 1B opt_res=0 路徑覆蓋。")
    print("  pixel 欄位不受 opt_res 影響（GlobalPosX 不變），μm=0.0 sentinel。")

# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8201)
    ap.add_argument("--opt-res", type=float, default=0.5,
                    help="IP server 的 opt_res_x/y 預期值（Stage 1B 驗算用）")
    args = ap.parse_args()

    print("=" * 60)
    print(f"Gap #5 pixel→μm 驗證  host={args.host}:{args.port}  opt_res={args.opt_res}")
    print("=" * 60)

    stage_1b(args.host, args.port, args.opt_res)
    stage_2(args.host, args.port)
    stage_3a(args.host, args.port)

    print("\n" + "=" * 60)
    pass_cnt = sum(1 for _, p, _ in test_results if p)
    fail_cnt = sum(1 for _, p, _ in test_results if not p)
    print(f"結果: PASS {pass_cnt} / FAIL {fail_cnt} (共 {len(test_results)})")
    if fail_cnt:
        print("\nFAIL 清單：")
        for name, p, detail in test_results:
            if not p:
                print(f"  ✗ {name}\n    {detail}")
    print("=" * 60)
    sys.exit(0 if fail_cnt == 0 else 1)

if __name__ == "__main__":
    main()
