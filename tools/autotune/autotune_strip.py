#!/usr/bin/env python3
"""
autotune_strip — 整條 CCD（一片玻璃的所有 slice）→ panel 座標的檢測區 / IOI。

為什麼要整條：短邊進片，晶片間隙、前後緣只出現在某幾張 slice（實測 T550 IP04：間隙跨 #14/#15、
前緣在 #0），單張看不出整顆晶片；配方的 ROI 也必須是 **panel 座標**（Y = slice × 張高 + 列），
IP 逐張處理時依 sliceIndex 平移（I8）。

做法：
  1. 每張帶上下相鄰張各 CTX 列一起算「週期能量」（|高通| − 與 ±pitch 鄰居的殘差，X、Y 兩方向取大），
     去掉 CTX 後縮成 B×B 區塊 → 依序接成整條的區塊圖（145000 列 → ~4500 列）
  2. 區塊圖二值化 → 連通元件 → 每個元件的外框 = 一顆晶片或一條 dummy 帶
  3. 每條邊回到全解析度，用 energy_profile + refine_segments 細修到 ≤ 1 pitch
  4. 細的（< 60 pitch）且不貼 CCD 左右邊的 → dummy 帶 → DetectIoi（給 AI）；其餘 → DetectRoi
"""
import os

import numpy as np

import autotune_engine as E

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

CTX = 256          # 每張上下各帶相鄰張幾列（> 2 × pitch + 高通半徑）
EDGE_SHRINK = 1    # 晶片內側邊往內縮幾個 pitch（縮掉的那圈在 IOI 內，交 AI）
DARK_COL = 12      # 晶片內亮度低於此值的欄不檢（過暗不可檢）
B = 32             # 區塊大小（px）


class Strip:
    """一條 CCD 的所有 slice（依序號）；可取任意 panel 列範圍（跨張自動拼）。"""

    def __init__(self, paths):
        self.paths = list(paths)
        self._cache = {}
        im0 = self.slice(0)
        self.h, self.w = im0.shape
        self.H = self.h * len(self.paths)

    def slice(self, i):
        if i not in self._cache:
            if len(self._cache) > 4:
                self._cache.pop(next(iter(self._cache)))
            self._cache[i] = E.load(self.paths[i])
        return self._cache[i]

    def rows(self, y0, y1):
        """panel 列 [y0, y1)（夾在 0..H）。"""
        y0, y1 = max(0, y0), min(self.H, y1)
        parts = []
        for i in range(y0 // self.h, (y1 - 1) // self.h + 1):
            a, b = max(y0, i * self.h) - i * self.h, min(y1, (i + 1) * self.h) - i * self.h
            parts.append(self.slice(i)[a:b])
        return np.vstack(parts), y0


def periodic_energy(img, px, py):
    """每點「有週期」的紋理能量：|hp| 減掉 X 或 Y 方向對 ±pitch 鄰居的殘差（取較大者）。
    週期 pattern → 兩方向都對得上 → 高；平坦玻璃、單條線（只在一個方向週期）、pad → 低。"""
    f = img.astype(np.float32)
    bl = cv2.GaussianBlur(f, (0, 0), max(px, py))
    # 除以局部亮度 → 相對能量：鏡頭暗角（實測 T550 CCD 中央是邊緣的 2.5 倍）不會讓邊緣的晶片掉到門檻下
    h = (f - bl) / (bl + 8.0) * 100.0
    def res(p, axis):
        r = None
        for d in sorted({int(np.floor(p)), int(np.ceil(p))}):
            for s in (1, -1):
                x = np.abs(h - np.roll(h, s * d, axis=axis))
                r = x if r is None else np.minimum(r, x)
        return r
    return np.clip(np.abs(h) - np.maximum(res(px, 1), res(py, 0)), 0, None)


def block_map(st, px, py, progress=None, bright=None):
    """整條的區塊週期能量圖（每 B×B 一格）。bright：給 list 則順便收每列區塊的亮度中位數（找玻璃範圍用）。"""
    out = []
    for i in range(len(st.paths)):
        y0 = i * st.h
        img, top = st.rows(y0 - CTX, y0 + st.h + CTX)
        e = periodic_energy(img, px, py)[y0 - top:y0 - top + st.h]
        hb, wb = st.h // B, st.w // B
        if bright is not None:
            core = img[y0 - top:y0 - top + st.h]
            bright.append(core[:hb * B, :wb * B].reshape(hb, B, wb, B).mean(axis=(1, 3)))   # 區塊亮度
        out.append(e[:hb * B, :wb * B].reshape(hb, B, wb, B).mean(axis=(1, 3)))
        if progress:
            progress(f'  slice {i:02d} 區塊圖完成')
    return np.vstack(out)


def components(bm, px, py, min_blocks=6):
    """區塊圖 → (晶片外框, dummy 外框)（區塊座標）。
    兩層門檻（實測 T550：晶片 ≈ 12–16、dummy 帶 ≈ 2–4.6、平坦玻璃 ≈ 0.4–0.9）：
      高 = max(p90 × 0.3, 1.0) → 晶片；低 = 背景中位數 × 4 → 晶片以外的弱週期區 = dummy 帶。"""
    thr = max(float(np.percentile(bm, 90)) * 0.3, 0.5)
    k = max(1, int(round(2 * max(px, py) / B)))           # ~2 pitch：補 pattern 內的小洞、去雜點
    ker = np.ones((k, k), np.uint8)
    def clean(m):
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, ker)
        return cv2.morphologyEx(m, cv2.MORPH_OPEN, ker)
    hi = clean((bm > thr).astype(np.uint8))
    bg = float(np.median(bm[bm <= thr])) if (bm <= thr).any() else 0.0
    thr_lo = min(thr, max(4 * bg, 0.5))
    lo = clean(((bm > thr_lo) & (cv2.dilate(hi, ker) == 0)).astype(np.uint8))
    def boxes_of(m):
        n, _, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=4)
        out = []
        for j in range(1, n):
            x, y, w, h, a = stats[j]
            if w >= min_blocks and h >= 2:
                out.append({'bx': int(x), 'by': int(y), 'bw': int(w), 'bh': int(h),
                            'fill': round(float(a) / (w * h), 3)})
        return out
    return boxes_of(hi), boxes_of(lo), {'hi': round(thr, 3), 'lo': round(thr_lo, 3), 'bg': round(bg, 3)}, hi


def _cross(prof, b, inside_dir, search):
    """b 附近 ±search 找半高點（inside_dir：+1 = pattern 在 b 之後）。"""
    n = len(prof)
    a0, a1 = max(0, b - 3 * search), max(0, b - search)
    c0, c1 = min(n, b + search), min(n, b + 3 * search)
    before, after = prof[a0:a1], prof[c0:c1]
    if len(before) == 0 or len(after) == 0:
        return b
    ins, outs = (after, before) if inside_dir > 0 else (before, after)
    mid = (np.median(ins) + np.median(outs)) / 2
    lo, hi = max(0, b - search), min(n, b + search)
    w = prof[lo:hi] >= mid
    idx = np.nonzero(w[1:] != w[:-1])[0]
    if len(idx) == 0:
        return b
    return int(lo + idx[np.argmin(np.abs(lo + idx - b))] + 1)


def refine_box(st, box, px, py, nb_rows, nb_cols, n_col_samples=4, sample_h=1500):
    """區塊外框 → 全解析度 panel 座標（每條邊找半高點）。貼區塊圖邊的邊直接 = 整條 / CCD 邊。"""
    search = int(4 * max(px, py))
    x0, x1 = box['bx'] * B, (box['bx'] + box['bw']) * B
    y0, y1 = box['by'] * B, (box['by'] + box['bh']) * B
    at_top, at_bot = box['by'] == 0, box['by'] + box['bh'] >= nb_rows
    at_left, at_right = box['bx'] == 0, box['bx'] + box['bw'] >= nb_cols
    xi0, xi1 = x0 + search, x1 - search                              # 只看框內部的欄
    if xi1 - xi0 < 16:                                               # 框很窄（例：貼 CCD 邊的細條）→ 用整個框寬
        xi0, xi1 = max(0, x0), min(st.w, max(x1, x0 + 16))
    def yedge(y, d):
        img, top = st.rows(y - 4 * search, y + 4 * search)
        prof = E.energy_profile(img[:, xi0:xi1], px, py, 0)
        return top + _cross(prof, y - top, d, search)
    ny0 = 0 if at_top else yedge(y0, +1)
    ny1 = st.H if at_bot else yedge(y1, -1)
    nx0, nx1 = (0 if at_left else x0), (st.w if at_right else x1)
    if not (at_left and at_right):                                   # 左右邊：框內取幾段列帶平均欄剖面
        hh = min(sample_h, max(1, ny1 - ny0))
        ys = np.linspace(ny0, max(ny0, ny1 - hh), n_col_samples).astype(int)
        prof = None
        for yy in ys:
            img, _ = st.rows(int(yy), int(yy) + hh)
            p = E.energy_profile(img, px, py, 1)
            prof = p if prof is None else prof + p
        if not at_left:
            nx0 = _cross(prof, x0, +1, search)
        if not at_right:
            nx1 = _cross(prof, x1, -1, search)
    return {'x0': int(nx0), 'x1': int(nx1), 'y0': int(ny0), 'y1': int(ny1)}


def chip_pitch(st, chips, n=3):
    """只在晶片內量 pitch：取最大晶片完整涵蓋的幾張 slice，區塊必須 ≥95% 落在晶片內。
    （實測 T550 IP09：CCD 左側 2400 px 是外圍 pad/標記，整張量 → 29.3 × 20.89，晶片內才是 25.8 × 18.5）"""
    if not chips:
        return None
    c = max(chips, key=lambda r: (r['x1'] - r['x0']) * (r['y1'] - r['y0']))
    full = [i for i in range(len(st.paths)) if c['y0'] <= i * st.h and (i + 1) * st.h <= c['y1']]
    if not full:
        return None
    xs, ys = [], []
    for i in [full[len(full) * k // (n + 1)] for k in range(1, n + 1)]:
        m = np.zeros((st.h, st.w), np.uint8)
        m[:, c['x0']:c['x1']] = 1
        p = E.estimate_pitch(st.slice(i), mask=m)
        if p:
            xs.append(p['x'])
            ys.append(p['y'])
    return (float(np.median(xs)), float(np.median(ys))) if xs else None


def analyze_strip(paths, px=None, py=None, dummy_max_pitches=60, progress=print):
    """一條 CCD → {'pitch', 'chips': [panel 座標框], 'dummy': [...], 'bypass_frac', ...}。
    pitch 沒給時兩段式：整張先量 → 找晶片 → 只在晶片內重量；差 > 2% 就用新 pitch 重做區域。"""
    st = Strip(sorted(paths))
    if px is not None and py is not None:
        return _regions(st, px, py, dummy_max_pitches, progress)
    p = E.estimate_pitch(st.slice(len(st.paths) // 2))
    if not p:
        return _regions(st, 26.0, 18.0, dummy_max_pitches, progress) | {'error': 'pitch 量不出來（沒有 pattern？）'}
    r = _regions(st, p['x'], p['y'], dummy_max_pitches, progress)
    q = chip_pitch(st, r['chips'])
    if q and (abs(q[0] - p['x']) / q[0] > 0.02 or abs(q[1] - p['y']) / q[1] > 0.02):
        progress(f'晶片內重量 pitch：{p["x"]:.2f}×{p["y"]:.2f} → {q[0]:.2f}×{q[1]:.2f}，重做區域')
        r = _regions(st, q[0], q[1], dummy_max_pitches, progress)
        r['pitch_first'] = [round(p['x'], 2), round(p['y'], 2)]
    return r


def _regions(st, px, py, dummy_max_pitches, progress):
    progress(f'pitch {px:.2f} × {py:.2f}，{len(st.paths)} 張 → panel {st.w} × {st.H}')
    bl = []
    bm = block_map(st, px, py, bright=bl)
    bmb = np.vstack(bl)                                              # 區塊亮度圖
    rowb = np.median(bmb, axis=1)
    on = np.nonzero(rowb > 0.15 * np.median(rowb))[0]                # 玻璃範圍（亮度）：前後緣外是全黑
    gy = (max(0, int(on[0] - 1) * B), min(st.H, int(on[-1] + 2) * B)) if len(on) else (0, st.H)  # 多留 1 格含玻璃邊
    colb = np.median(bmb[gy[0] // B:max(gy[0] // B + 1, gy[1] // B)], axis=0)
    onx = np.nonzero(colb > 0.15 * np.median(colb))[0]               # 玻璃左右（角落 CCD 會看到長邊/倒角）
    gx = (max(0, int(onx[0] - 1) * B), min(st.w, int(onx[-1] + 2) * B)) if len(onx) else (0, st.w)
    hi_boxes, lo_boxes, thr, m = components(bm, px, py)
    # 晶片 = 框內幾乎全是強週期（填滿率 ≥ 0.8）且能量 ≥ 最強區域的一半。
    # 實測 T550 IP01（角落）：晶片 fill 0.99、能量中位數 16–17；外圍條紋/標記 fill 0.02–0.7、能量 ≤ 6
    #   → 舊版把前緣外圍（1632–7424）當晶片 → 第 0 張 2,843 顆假點、整片門檻被拖到 0.40 / 2.41。
    for b in hi_boxes:
        b['energy'] = float(np.median(bm[b['by']:b['by'] + b['bh'], b['bx']:b['bx'] + b['bw']]))
    big = [b for b in hi_boxes if b['bh'] * B >= dummy_max_pitches * py]
    emax = max([b['energy'] for b in big], default=0.0)
    chips = []
    for b in hi_boxes:
        if b['fill'] < 0.8 or b['energy'] < 0.5 * emax:
            continue
        r = refine_box(st, b, px, py, bm.shape[0], bm.shape[1])
        hgt, wid = r['y1'] - r['y0'], r['x1'] - r['x0']
        if hgt < dummy_max_pitches * py and r['y0'] > 0 and r['y1'] < st.H:
            continue                                                 # 細條（dummy 帶）→ IOI 幾何規則涵蓋
        if wid < dummy_max_pitches * px and r['x0'] > 0 and r['x1'] < st.w:
            continue
        r['fill'], r['energy'] = b['fill'], round(b['energy'], 2)
        chips.append(r)
    chips.sort(key=lambda r: (r['y0'], r['x0']))
    # 晶片內側的邊（不是影像邊）往內縮 1 pitch：最外一排格子跟內部不同，比 ±2 pitch 時會拉偏比值
    # （實測 T550 IP09/IP01 左緣晶片：限制暗門檻的點全在 x0+53 = 死區外第一欄 → 暗門檻 0.55 vs 其他 0.63–0.68）
    sx, sy = int(round(px)) * EDGE_SHRINK, int(round(py)) * EDGE_SHRINK
    for c in chips:
        if c['x0'] > 0: c['x0'] += sx
        if c['x1'] < st.w: c['x1'] -= sx
        if c['y0'] > 0: c['y0'] += sy
        if c['y1'] < st.H: c['y1'] -= sy
    # 過暗不可檢：晶片內亮度 < DARK_COL 的欄切掉（近黑處 DIV 比值被雜訊主導；實測 T550 IP08 左右緣亮度 ≈ 7、
    # 中央 42 → 2 顆假點）。切掉的寬度記在 dark_trim，報告提醒調光源。
    for c in chips:
        cb = np.median(bmb[c['y0'] // B:max(c['y0'] // B + 1, c['y1'] // B)], axis=0)
        ok = np.nonzero(cb >= DARK_COL)[0]
        if len(ok) == 0:
            c['dark_trim'] = [c['x0'], c['x1']]
            continue
        nx0, nx1 = max(c['x0'], int(ok[0]) * B), min(c['x1'], int(ok[-1] + 1) * B)
        if nx0 > c['x0'] or nx1 < c['x1']:
            c['dark_trim'] = [nx0 - c['x0'], c['x1'] - nx1]
            c['x0'], c['x1'] = nx0, nx1
    chips = [c for c in chips if c['x1'] - c['x0'] > 4 * B]
    ioi = ioi_rects(chips, gx, gy, 2 * int(round(py)) + 2, 2 * int(round(px)) + 2)
    area = st.w * st.H
    pat = float(m.sum()) * B * B
    roi = sum((r['x1'] - r['x0']) * (r['y1'] - r['y0']) for r in chips)
    bb = bmb[gy[0] // B:gy[1] // B, gx[0] // B:gx[1] // B]
    return {'glass_brightness': round(float(np.median(bb)), 1) if bb.size else 0.0,
            'pitch': [px, py], 'slice_h': st.h, 'width': st.w, 'height': st.H, 'n_slices': len(st.paths),
            'chips': chips, 'dummy': ioi, 'block_thr': thr, 'glass': list(gy), 'glass_x': list(gx),
            'pattern_frac': round(pat / area, 4), 'roi_frac': round(roi / area, 4), 'block_map': bm}


def ioi_rects(chips, gx, gy, mgy, mgx):
    """玻璃內、晶片以外 = IOI（給 AI）：前緣、後緣、晶片列之間、晶片左右外圍、同列晶片之間。
    每塊都往晶片內多包 kernel 死區（mgy / mgx = 2 pitch + 2）——晶片真正的邊不補邊，那一圈交給 AI。
    幾何規則不靠弱週期門檻：dummy 帶、pad、標記、玻璃邊（崩邊）都在裡面。"""
    out = []
    def add(x0, y0, x1, y1):
        x0, y0, x1, y1 = max(x0, gx[0]), max(y0, gy[0]), min(x1, gx[1]), min(y1, gy[1])
        if x1 - x0 > 2 * mgx and y1 - y0 > 2 * mgy:
            out.append({'x0': int(x0), 'y0': int(y0), 'x1': int(x1), 'y1': int(y1)})
    if not chips:
        add(gx[0], gy[0], gx[1], gy[1])
        return out
    rows = []                                                        # 依 Y 重疊分列
    for c in sorted(chips, key=lambda r: r['y0']):
        if rows and c['y0'] < rows[-1]['y1']:
            rows[-1]['cs'].append(c)
            rows[-1]['y1'] = max(rows[-1]['y1'], c['y1'])
            rows[-1]['y0'] = min(rows[-1]['y0'], c['y0'])
        else:
            rows.append({'y0': c['y0'], 'y1': c['y1'], 'cs': [c]})
    if rows[0]['y0'] - gy[0] > mgy:
        add(gx[0], gy[0], gx[1], rows[0]['y0'] + mgy)                # 前緣外圍
    for a, b in zip(rows, rows[1:]):
        add(gx[0], a['y1'] - mgy, gx[1], b['y0'] + mgy)              # 晶片列之間
    if gy[1] - rows[-1]['y1'] > mgy:
        add(gx[0], rows[-1]['y1'] - mgy, gx[1], gy[1])               # 後緣外圍
    for r in rows:
        cs = sorted(r['cs'], key=lambda c: c['x0'])
        if cs[0]['x0'] - gx[0] > mgx:
            add(gx[0], r['y0'], cs[0]['x0'] + mgx, r['y1'])          # 左側外圍
        for a, b in zip(cs, cs[1:]):
            add(a['x1'] - mgx, r['y0'], b['x0'] + mgx, r['y1'])      # 同列晶片之間
        if gx[1] - cs[-1]['x1'] > mgx:
            add(cs[-1]['x1'] - mgx, r['y0'], gx[1], r['y1'])         # 右側外圍
    return out


def _overlap(a, b):
    return a['x0'] < b['x1'] and b['x0'] < a['x1'] and a['y0'] < b['y1'] and b['y0'] < a['y1']


def merge_bands(rs, gap):
    """Y 方向重疊或相距 < gap 的框合併成一條帶（外框聯集）——同一個晶片間隙/外圍區只輸出一個 IOI。"""
    out = []
    for r in sorted(rs, key=lambda r: r['y0']):
        if out and r['y0'] <= out[-1]['y1'] + gap:
            o = out[-1]
            o.update(x0=min(o['x0'], r['x0']), x1=max(o['x1'], r['x1']), y1=max(o['y1'], r['y1']))
        else:
            out.append({k: r[k] for k in ('x0', 'x1', 'y0', 'y1')})
    return out


def rects_for_slice(rects, i, h):
    """panel 座標框 → 第 i 張的本地座標（給門檻估計用的遮罩）。"""
    out = []
    for r in rects:
        a, b = max(r['y0'], i * h), min(r['y1'], (i + 1) * h)
        if a < b:
            out.append((r['x0'], a - i * h, r['x1'], b - i * h))
    return out


def roi_mask(shape, rects):
    m = np.zeros(shape, np.uint8)
    for x0, y0, x1, y1 in rects:
        m[y0:y1, x0:x1] = 1
    return m


if __name__ == '__main__':  # pragma: no cover
    import argparse
    import glob
    ap = argparse.ArgumentParser()
    ap.add_argument('dir')
    a = ap.parse_args()
    r = analyze_strip(sorted(glob.glob(os.path.join(a.dir, '*_Origin*.tif'))))
    for k in ('pitch', 'chips', 'dummy', 'pattern_frac', 'roi_frac', 'block_thr'):
        print(k, r[k])
