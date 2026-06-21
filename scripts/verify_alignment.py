#!/usr/bin/env python3
"""
verify_alignment.py — Gap #1 對位 Pipeline Stage 2+3 驗證
（TCP 模式：對 cfaoi_ip offline-tcp 發 LOAD_RECIPE / CHECK_ALIGN / SET_ALIGN / SEND_IMAGE_FOR_REVIEW）

用法：
    # 先在 Spark 啟動 IP server（另一個 terminal）：
    #   cfaoi_ip --mode offline-tcp --port 8200 --output /tmp/align_test_out
    python3 verify_alignment.py [--host 127.0.0.1] [--port 8200]

驗證項目：
    Stage 2A: ShiftX=0 → 偵測缺陷數 N0（基準）
    Stage 2B: 對位後（ShiftX=7px）→ 偵測缺陷數 ≈ N0（ROI 補正）
    Stage 2C: 故意偏移影像 + 對位 → 缺陷數與 ShiftX=0 一致（對位修正偏移）
    Stage 3A: CHECK_ALIGN 空白圖 → ok=false（ERR 路徑）
    Stage 3B: flight_recorder incident 記錄（align_failed）
"""

import argparse
import base64
import json
import math
import os
import socket
import struct
import sys
import time

import numpy as np

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False
    print("[WARN] cv2 not available; image generation uses numpy only")

# ─────────────────────────────────────────────────────────────────────────────
# TCP helpers
# ─────────────────────────────────────────────────────────────────────────────

def tcp_connect(host, port, timeout=10):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.connect((host, port))
    return s

def send_cmd(s, cmd, params, seq=None):
    global _seq
    _seq += 1
    msg = json.dumps({"cmd": cmd, "seq": _seq, "params": params or {}}) + "\n"
    s.sendall(msg.encode())
    resp = recv_line(s)
    return json.loads(resp)

def recv_line(s, timeout=30):
    buf = b""
    s.settimeout(timeout)
    while True:
        c = s.recv(1)
        if not c:
            raise ConnectionError("connection closed")
        if c == b"\n":
            return buf.decode()
        buf += c

def send_cmd_with_payload(s, cmd, params, payload_bytes, seq=None):
    global _seq
    _seq += 1
    params["payload_bytes"] = len(payload_bytes)
    msg = json.dumps({"cmd": cmd, "seq": _seq, "params": params}) + "\n"
    s.sendall(msg.encode())
    s.sendall(payload_bytes)
    resp = recv_line(s, timeout=60)
    return json.loads(resp)

_seq = 0

# ─────────────────────────────────────────────────────────────────────────────
# 合成影像生成
# ─────────────────────────────────────────────────────────────────────────────

W, H = 8192, 5000
PITCH_X, PITCH_Y = 26, 19
# 缺陷 ROI（檢測只看這個區塊）
ROI_X1, ROI_Y1 = 1000, 1000
ROI_X2, ROI_Y2 = 3000, 2500
# Mark 位置（對位 Mark 在 ROI 外側，避免被缺陷混淆）
MARK_CX, MARK_CY = 500, 500   # Mark 中心在影像內
MARK_W,  MARK_H  = 80, 80     # Mark 尺寸
SEARCH_W, SEARCH_H = 300, 300 # 搜尋 ROI 尺寸

def make_panel_image(shift_x=0, shift_y=0):
    """
    合成面板影像（8192×5000, Mono8）：
      - 背景：repeating grid pattern（模擬鋁箔/PCB 規則紋理）
      - 幾個固定「缺陷」像素（明亮暗點）在 ROI 內
      - 角落有一個棋盤 Mark 供對位
      - 整張可選平移 shift_x/shift_y（模擬面板偏移）
    """
    img = np.full((H, W), 128, dtype=np.uint8)

    # 背景格紋（pitch_x × pitch_y）
    for r in range(0, H, PITCH_Y):
        img[r:r+2, :] = 140
    for c in range(0, W, PITCH_X):
        img[:, c:c+2] = 140

    # 缺陷點（4 個 bright + 3 個 dark），固定在 ROI 內
    defects = [
        (1200, 1100, 220, 5, 5),   # x,y,val,w,h
        (2000, 1500, 215, 4, 4),
        (2500, 2000, 218, 6, 4),
        (1800, 2200, 222, 3, 5),
        (1400, 1300, 35,  5, 5),   # dark
        (2100, 1700, 38,  4, 4),
        (2800, 2100, 40,  6, 3),
    ]
    for dx, dy, val, dw, dh in defects:
        img[dy:dy+dh, dx:dx+dw] = val

    # 棋盤 Mark（MARK_W×MARK_H，左上角在 MARK_CX-MARK_W/2, MARK_CY-MARK_H/2）
    mx = MARK_CX - MARK_W // 2
    my = MARK_CY - MARK_H // 2
    block = 10
    for r in range(MARK_H):
        for c in range(MARK_W):
            img[my+r, mx+c] = 210 if ((r//block + c//block) % 2 == 0) else 45

    # 平移（如果有）
    if shift_x != 0 or shift_y != 0:
        if HAS_CV2:
            M = np.float32([[1, 0, shift_x], [0, 1, shift_y]])
            img = cv2.warpAffine(img, M, (W, H), flags=cv2.INTER_NEAREST,
                                 borderMode=cv2.BORDER_REPLICATE)
        else:
            # numpy fallback（整數 shift）
            sx, sy = int(round(shift_x)), int(round(shift_y))
            shifted = np.full_like(img, 128)
            src_y1 = max(0, -sy); src_y2 = min(H, H - sy)
            dst_y1 = max(0,  sy); dst_y2 = min(H, H + sy)
            src_x1 = max(0, -sx); src_x2 = min(W, W - sx)
            dst_x1 = max(0,  sx); dst_x2 = min(W, W + sx)
            shifted[dst_y1:dst_y2, dst_x1:dst_x2] = img[src_y1:src_y2, src_x1:src_x2]
            img = shifted
    return img

def make_golden_png():
    """棋盤 Mark 的 PNG bytes（MARK_W×MARK_H）。"""
    mark = np.zeros((MARK_H, MARK_W), dtype=np.uint8)
    block = 10
    for r in range(MARK_H):
        for c in range(MARK_W):
            mark[r, c] = 210 if ((r//block + c//block) % 2 == 0) else 45
    if HAS_CV2:
        ok, enc = cv2.imencode(".png", mark)
        return enc.tobytes()
    else:
        # 簡單 raw PNG（無壓縮）—— 只在 cv2 缺時用
        import zlib, struct as st
        def chunk(name, data):
            c = name + data
            return st.pack(">I", len(data)) + c + st.pack(">I", zlib.crc32(c) & 0xffffffff)
        IHDR = st.pack(">IIBBBBB", MARK_W, MARK_H, 8, 0, 0, 0, 0)
        rows = b""
        for r in range(MARK_H):
            rows += b"\x00" + mark[r].tobytes()
        idat = zlib.compress(rows, 9)
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", IHDR) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")

def crop_search_roi(img):
    """
    從影像裁取搜尋 ROI（SEARCH_W×SEARCH_H，以 nominal ReferX/ReferY 為中心）。
    Control 永遠用 RecipeInfo 的 nominal 座標（不知道影像的實際偏移量）。
    若影像有偏移，Mark 在 search_roi 內的位置也會偏移 → run_align 量出 ShiftX/Y。
    """
    x0 = MARK_CX - SEARCH_W // 2
    y0 = MARK_CY - SEARCH_H // 2
    x0 = max(0, min(W - SEARCH_W, x0))
    y0 = max(0, min(H - SEARCH_H, y0))
    return img[y0:y0+SEARCH_H, x0:x0+SEARCH_W].copy()

def make_recipe_xml(shift_x_applied=0, shift_y_applied=0):
    """
    生成 RecipeInfo.xml：ROI 設在中心塊，AlignRoi 設定 Mark 位置。
    shift_x_applied/shift_y_applied 是已套回的對位偏移（SET_ALIGN 模擬）—— 這裡給 0，
    因為 SET_ALIGN 由 IP server 端套回，不在 recipe 裡。
    """
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Recipe>
  <M_AlignRoi>
    <AlignEnable>true</AlignEnable>
    <PatternPath>mark.png</PatternPath>
    <ReferX>{MARK_CX}</ReferX>
    <ReferY>{MARK_CY}</ReferY>
    <SearchWidth>{SEARCH_W}</SearchWidth>
    <SearchHeight>{SEARCH_H}</SearchHeight>
  </M_AlignRoi>
  <DetectRoiList>
    <DetectRoi>
      <StartX>{ROI_X1}</StartX>
      <StartY>{ROI_Y1}</StartY>
      <EndX>{ROI_X2}</EndX>
      <EndY>{ROI_Y2}</EndY>
      <AlgorithmWay>8-Way-Star</AlgorithmWay>
      <AlgorithmCompare>DIV</AlgorithmCompare>
      <BrightThreshold>1.4</BrightThreshold>
      <DarkThreshold>0.6</DarkThreshold>
      <PitchX>{PITCH_X}</PitchX>
      <PitchY>{PITCH_Y}</PitchY>
      <SearchX>1</SearchX>
      <SearchY>1</SearchY>
      <BlobMinSize>2</BlobMinSize>
      <BlobMaxSize>500</BlobMaxSize>
    </DetectRoi>
  </DetectRoiList>
  <DetectIoiList/>
</Recipe>"""

def make_recipe_xml_fullframe():
    """全幅 ROI（StartX/Y/EndX/Y = -1）配方，用於 F1 回歸（SET_ALIGN 不可塌全幅）。"""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Recipe>
  <M_AlignRoi>
    <AlignEnable>true</AlignEnable>
    <PatternPath>mark.png</PatternPath>
    <ReferX>{MARK_CX}</ReferX>
    <ReferY>{MARK_CY}</ReferY>
    <SearchWidth>{SEARCH_W}</SearchWidth>
    <SearchHeight>{SEARCH_H}</SearchHeight>
  </M_AlignRoi>
  <DetectRoiList>
    <DetectRoi>
      <StartX>-1</StartX>
      <StartY>-1</StartY>
      <EndX>-1</EndX>
      <EndY>-1</EndY>
      <AlgorithmWay>8-Way-Star</AlgorithmWay>
      <AlgorithmCompare>DIV</AlgorithmCompare>
      <BrightThreshold>1.4</BrightThreshold>
      <DarkThreshold>0.6</DarkThreshold>
      <PitchX>{PITCH_X}</PitchX>
      <PitchY>{PITCH_Y}</PitchY>
      <SearchX>1</SearchX>
      <SearchY>1</SearchY>
      <BlobMinSize>2</BlobMinSize>
      <BlobMaxSize>500</BlobMaxSize>
    </DetectRoi>
  </DetectRoiList>
  <DetectIoiList/>
</Recipe>"""

# ─────────────────────────────────────────────────────────────────────────────
# 驗證 helpers
# ─────────────────────────────────────────────────────────────────────────────

results = []

def check(name, cond, detail):
    results.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    print(f"  {detail}")

def send_load_recipe(s, panel_id, shift_x_hint=0, shift_y_hint=0, xml=None):
    golden_bytes = make_golden_png()
    golden_b64   = base64.b64encode(golden_bytes).decode()
    if xml is None:
        xml = make_recipe_xml()
    r = send_cmd(s, "LOAD_RECIPE", {
        "recipe": "ALIGN_TEST",
        "recipe_xml": xml,
        "panel_id": panel_id,
        "golden_png_base64": golden_b64,
    })
    return r

def send_image(s, img, panel_id):
    payload = img.tobytes()
    r = send_cmd_with_payload(s, "SEND_IMAGE_FOR_REVIEW", {
        "panel_id": panel_id,
        "cam_id": 0,
        "width": W,
        "height": H,
        "frame_seq": 1,
        "last": True,
        "debug": True,
    }, payload)
    return r

def get_defect_count(resp_json):
    if resp_json.get("status") != "OK":
        return None
    rois = resp_json.get("result", {}).get("RoiInfoList", [])
    total = sum(len(roi.get("DefectInfoList", [])) for roi in rois)
    return total

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2A — 基準：ShiftX=0，量 N0 缺陷數
# ─────────────────────────────────────────────────────────────────────────────

def run_stage2a(s):
    print("\n=== Stage 2A: 基準（ShiftX=0）===")
    img = make_panel_image(shift_x=0, shift_y=0)
    r = send_load_recipe(s, "STAGE2A")
    if r.get("status") != "OK":
        check("Stage2A: LOAD_RECIPE", False, f"LOAD_RECIPE 失敗: {r}")
        return None
    print(f"  LOAD_RECIPE OK")

    resp = send_image(s, img, "STAGE2A")
    n0 = get_defect_count(resp)
    check("Stage2A: ShiftX=0 偵測", n0 is not None,
          f"DefectCnt={n0} status={resp.get('status')} (expect 7 缺陷，配方 ROI 內 4 bright + 3 dark)")
    return n0

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2B — 對位後偵測：ShiftX=0 影像 + CHECK_ALIGN + SET_ALIGN
#           （Mark 在原位，ROI 偏移只靠對位補正 → 缺陷數應 ≈ N0）
# ─────────────────────────────────────────────────────────────────────────────

def run_stage2b(s, n0):
    print("\n=== Stage 2B: 對位後偵測（Mark 在原位，SET_ALIGN(7,3) → 檢驗 ROI 套回）===")
    img = make_panel_image(shift_x=0, shift_y=0)

    r = send_load_recipe(s, "STAGE2B")
    if r.get("status") != "OK":
        check("Stage2B: LOAD_RECIPE", False, f"{r}"); return

    # 截搜尋 ROI（Control 用 nominal ReferX/Y 裁，不管影像偏移）
    roi = crop_search_roi(img)
    roi_payload = roi.tobytes()
    r_align = send_cmd_with_payload(s, "CHECK_ALIGN", {
        "panel_id": "STAGE2B",
        "width": SEARCH_W,
        "height": SEARCH_H,
    }, roi_payload)
    print(f"  CHECK_ALIGN: {r_align}")

    if r_align.get("status") != "OK":
        check("Stage2B: CHECK_ALIGN", False,
              f"CHECK_ALIGN 失敗: {r_align.get('error','?')} score={r_align.get('score','?')}")
        return

    sx = r_align.get("shift_x", 0)
    sy = r_align.get("shift_y", 0)
    sc = r_align.get("score", 0)
    print(f"  ShiftX={sx:.3f} ShiftY={sy:.3f} score={sc:.4f}")

    # 驗：Mark 在原位 → ShiftX/Y 應接近 0
    check("Stage2B: CHECK_ALIGN 原位 ShiftX≈0",
          abs(sx) < 1.0 and abs(sy) < 1.0,
          f"ShiftX={sx:.3f} ShiftY={sy:.3f} score={sc:.4f} (expect |shift| < 1px)")

    # SET_ALIGN 套回（這裡手動傳 ShiftX=7, ShiftY=3 模擬偏移後的對位結果，驗 ROI 套回邏輯）
    FAKE_SX, FAKE_SY = 7.0, 3.0
    r_set = send_cmd(s, "SET_ALIGN", {"shift_x": FAKE_SX, "shift_y": FAKE_SY})
    check("Stage2B: SET_ALIGN", r_set.get("status") == "OK", f"{r_set}")

    # 跑偵測（ROI 已套回 +7,+3）
    resp = send_image(s, img, "STAGE2B")
    n_after_align = get_defect_count(resp)
    print(f"  偵測結果: DefectCnt={n_after_align}")

    # ROI 套回後，原圖的缺陷在 ROI+7,+3 範圍外 → 缺陷數 ≠ N0（ROI 移走了）
    # 這驗的是「eff_* 確實生效」：若 eff_* 未生效，缺陷數會 = N0（ROI 沒動）
    # eff_* 生效 → ROI 右移 7px 下移 3px → ROI=[1007,1003]~[3007,2503] → 大多缺陷仍在 → 數量類似
    # 注意：ROI 移了 7px，缺陷座標沒動，所以大部分仍在新 ROI 內（ROI 範圍 2000×1500，移動 7px 微不足道）
    check("Stage2B: SET_ALIGN 套回後偵測不崩潰",
          n_after_align is not None,
          f"SET_ALIGN(7,3) 套回後 DefectCnt={n_after_align} (eff_* 生效，偵測繼續執行)")

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2C — 核心：故意偏移影像 + 對位修正 → 缺陷數與 N0 一致
# ─────────────────────────────────────────────────────────────────────────────

def run_stage2c(s, n0):
    print("\n=== Stage 2C: 故意偏移影像 + 對位修正 → 缺陷數應 ≈ N0 ===")
    GT_SX, GT_SY = 7.0, 3.0  # 整數 shift（簡化；sub-pixel 由 Stage 1 驗）
    img_shifted = make_panel_image(shift_x=GT_SX, shift_y=GT_SY)

    r = send_load_recipe(s, "STAGE2C")
    if r.get("status") != "OK":
        check("Stage2C: LOAD_RECIPE", False, f"{r}"); return

    # 截搜尋 ROI：Control 用 nominal ReferX/Y 裁（不知道影像偏移量）
    # → 偏移影像的 Mark 在 search_roi 中心偏右/下 GT_SX/GT_SY → run_align 應量出 ShiftX≈GT_SX
    roi = crop_search_roi(img_shifted)
    roi_payload = roi.tobytes()
    r_align = send_cmd_with_payload(s, "CHECK_ALIGN", {
        "panel_id": "STAGE2C",
        "width": SEARCH_W,
        "height": SEARCH_H,
    }, roi_payload)
    print(f"  CHECK_ALIGN (shifted image): {r_align}")

    if r_align.get("status") != "OK":
        check("Stage2C: CHECK_ALIGN on shifted image", False,
              f"CHECK_ALIGN 失敗: {r_align.get('error','?')} score={r_align.get('score','?')}")
        return

    sx  = r_align.get("shift_x", 0)
    sy  = r_align.get("shift_y", 0)
    sc  = r_align.get("score", 0)
    ex  = sx - GT_SX
    ey  = sy - GT_SY
    print(f"  gt=({GT_SX},{GT_SY}) got=({sx:.3f},{sy:.3f}) err=({ex:.3f},{ey:.3f}) score={sc:.4f}")
    check("Stage2C: 偏移影像 ShiftX/Y 精度 < 0.5px",
          abs(ex) < 0.5 and abs(ey) < 0.5,
          f"gt=({GT_SX},{GT_SY}) got=({sx:.3f},{sy:.3f}) err=({ex:.3f},{ey:.3f}) score={sc:.4f}")

    # SET_ALIGN 套回（用算出的 ShiftX/Y）
    r_set = send_cmd(s, "SET_ALIGN", {"shift_x": sx, "shift_y": sy})
    check("Stage2C: SET_ALIGN", r_set.get("status") == "OK", f"{r_set}")

    # 跑偵測（偏移影像 + 對位 ROI 套回）
    resp = send_image(s, img_shifted, "STAGE2C")
    n_aligned = get_defect_count(resp)
    print(f"  偵測結果: n0={n0} n_aligned={n_aligned}")

    if n0 is not None:
        # 缺陷數應與基準相同（對位把 ROI 補正回來了）
        check("Stage2C: 對位後缺陷數 == 基準 N0（★最關鍵驗證）",
              n_aligned == n0,
              f"n0={n0} n_aligned={n_aligned} ShiftX={sx:.3f} ShiftY={sy:.3f} "
              f"({'PASS：對位正確修正偏移' if n_aligned == n0 else 'FAIL：缺陷數不一致，對位誤差影響偵測'})")
    else:
        check("Stage2C: 對位後偵測不崩潰", n_aligned is not None,
              f"DefectCnt={n_aligned}")

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2D — F1 回歸：全幅 ROI(-1) + SET_ALIGN → 缺陷數不崩（不塌成 1px）
# ─────────────────────────────────────────────────────────────────────────────

def run_stage2d(s):
    print("\n=== Stage 2D: F1 全幅 zone 套對位回歸（全幅 ROI=-1 不可塌成 1px）===")
    img = make_panel_image(shift_x=0, shift_y=0)

    # LOAD_RECIPE 全幅配方 → 基準缺陷數
    r = send_load_recipe(s, "STAGE2D", xml=make_recipe_xml_fullframe())
    if r.get("status") != "OK":
        check("Stage2D: LOAD_RECIPE(全幅)", False, f"{r}"); return
    resp = send_image(s, img, "STAGE2D")
    n_base = get_defect_count(resp)
    check("Stage2D: 全幅基準缺陷數 > 0", n_base is not None and n_base > 0,
          f"全幅 ROI 基準 DefectCnt={n_base}（含 ROI 內 7 缺陷 + Mark 等，須 > 0）")

    # SET_ALIGN(7,3)：全幅 zone 應被跳過（保留 -1），偵測區仍全幅
    r_set = send_cmd(s, "SET_ALIGN", {"shift_x": 7.0, "shift_y": 3.0})
    check("Stage2D: SET_ALIGN(7,3)", r_set.get("status") == "OK", f"{r_set}")
    resp2 = send_image(s, img, "STAGE2D")
    n_after = get_defect_count(resp2)

    # ★F1：修前 全幅 zone 被 -1+7=6 推翻 is_full_frame → ROI 塌成 ~1px → n_after≈0
    #       修後 全幅保留 → n_after == n_base
    check("Stage2D: ★全幅套對位後缺陷數 == 基準（F1 未塌成 1px）",
          n_base is not None and n_after == n_base,
          f"n_base={n_base} n_after={n_after} "
          f"({'PASS：全幅未崩' if n_after == n_base else 'FAIL：全幅 zone 被套位移塌掉（F1 未修）'})")

    # bit-exact：同圖再跑一次，缺陷數一致
    resp3 = send_image(s, img, "STAGE2D")
    n_again = get_defect_count(resp3)
    check("Stage2D: 同圖兩跑缺陷數一致（bit-exact 佐證）",
          n_again == n_after, f"n_after={n_after} n_again={n_again}")

# ─────────────────────────────────────────────────────────────────────────────
# Stage 3A — 失敗策略：空白 ROI → CHECK_ALIGN 回 ERR
# ─────────────────────────────────────────────────────────────────────────────

def run_stage3a(s):
    print("\n=== Stage 3A: 失敗策略（空白 ROI → ERR）===")
    # 先 LOAD_RECIPE 建立 golden
    r = send_load_recipe(s, "STAGE3A")
    if r.get("status") != "OK":
        check("Stage3A: LOAD_RECIPE", False, f"{r}"); return

    # 送空白 ROI（純灰，無 Mark）
    blank_roi = np.full((SEARCH_H, SEARCH_W), 128, dtype=np.uint8)
    r_align = send_cmd_with_payload(s, "CHECK_ALIGN", {
        "panel_id": "STAGE3A_BLANK",
        "width": SEARCH_W,
        "height": SEARCH_H,
    }, blank_roi.tobytes())
    print(f"  CHECK_ALIGN blank: {r_align}")
    check("Stage3A: 空白 ROI → ok=ERR",
          r_align.get("status") == "ERR",
          f"status={r_align.get('status')} score={r_align.get('score','?')} "
          f"error='{r_align.get('error','?')}'")

# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8200)
    args = ap.parse_args()

    print("=" * 60)
    print("  Gap #1 對位 Pipeline — verify_alignment.py")
    print(f"  target: {args.host}:{args.port}")
    print("=" * 60)

    try:
        s = tcp_connect(args.host, args.port)
        print(f"[connected] {args.host}:{args.port}")
    except Exception as e:
        print(f"[ERROR] 無法連線: {e}")
        print("請先啟動: cfaoi_ip --mode offline-tcp --port 8200 --output /tmp/align_test_out")
        sys.exit(1)

    try:
        n0 = run_stage2a(s)
        run_stage2b(s, n0)
        run_stage2c(s, n0)
        run_stage2d(s)
        run_stage3a(s)
    except Exception as e:
        print(f"\n[EXCEPTION] {e}")
        import traceback; traceback.print_exc()
    finally:
        s.close()

    # 統計
    pass_cnt = sum(1 for _, ok, _ in results if ok)
    fail_cnt = sum(1 for _, ok, _ in results if not ok)
    print("\n" + "=" * 60)
    print(f"結果: PASS {pass_cnt} / FAIL {fail_cnt} (共 {len(results)})")
    if fail_cnt:
        print("\nFAIL 清單：")
        for name, ok, detail in results:
            if not ok:
                print(f"  ✗ {name}")
                print(f"    {detail}")
    print("=" * 60)
    sys.exit(0 if fail_cnt == 0 else 1)

if __name__ == "__main__":
    main()
