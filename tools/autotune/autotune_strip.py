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
    h = f - cv2.GaussianBlur(f, (0, 0), max(px, py))
    def res(p, axis):
        r = None
        for d in sorted({int(np.floor(p)), int(np.ceil(p))}):
            for s in (1, -1):
                x = np.abs(h - np.roll(h, s * d, axis=axis))
                r = x if r is None else np.minimum(r, x)
        return r
    return np.clip(np.abs(h) - np.maximum(res(px, 1), res(py, 0)), 0, None)


def block_map(st, px, py, progress=None):
    """整條的區塊週期能量圖（每 B×B 一格）。"""
    out = []
    for i in range(len(st.paths)):
        y0 = i * st.h
        img, top = st.rows(y0 - CTX, y0 + st.h + CTX)
        e = periodic_energy(img, px, py)[y0 - top:y0 - top + st.h]
        hb, wb = st.h // B, st.w // B
        out.append(e[:hb * B, :wb * B].reshape(hb, B, wb, B).mean(axis=(1, 3)))
        if progress:
            progress(f'  slice {i:02d} 區塊圖完成')
    return np.vstack(out)


def components(bm, px, py, min_blocks=6):
    """區塊圖 → (晶片外框, dummy 外框)（區塊座標）。
    兩層門檻（實測 T550：晶片 ≈ 12–16、dummy 帶 ≈ 2–4.6、平坦玻璃 ≈ 0.4–0.9）：
      高 = max(p90 × 0.3, 1.0) → 晶片；低 = 背景中位數 × 4 → 晶片以外的弱週期區 = dummy 帶。"""
    thr = max(float(np.percentile(bm, 90)) * 0.3, 1.0)
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
    xi0, xi1 = x0 + search, max(x0 + search + 1, x1 - search)          # 只看框內部的欄
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


def analyze_strip(paths, px=None, py=None, dummy_max_pitches=60, progress=print):
    """一條 CCD → {'pitch', 'chips': [panel 座標框], 'dummy': [...], 'bypass_frac', ...}。"""
    st = Strip(sorted(paths))
    if px is None or py is None:
        mid = st.slice(len(st.paths) // 2)
        p = E.estimate_pitch(mid)
        px, py = p['x'], p['y']
    progress(f'pitch {px:.2f} × {py:.2f}，{len(st.paths)} 張 → panel {st.w} × {st.H}')
    bm = block_map(st, px, py)
    hi_boxes, lo_boxes, thr, m = components(bm, px, py)
    chips, dummy = [], []
    for b, weak in [(b, False) for b in hi_boxes] + [(b, True) for b in lo_boxes]:
        r = refine_box(st, b, px, py, bm.shape[0], bm.shape[1])
        hgt, wid = r['y1'] - r['y0'], r['x1'] - r['x0']
        thin_y = hgt < dummy_max_pitches * py and r['y0'] > 0 and r['y1'] < st.H
        thin_x = wid < dummy_max_pitches * px and r['x0'] > 0 and r['x1'] < st.w
        r['fill'] = b['fill']
        (dummy if (weak or thin_y or thin_x) else chips).append(r)
    chips.sort(key=lambda r: (r['y0'], r['x0']))
    dummy = merge_bands([d for d in dummy if not any(_overlap(d, c) for c in chips) and d['y1'] > d['y0']],
                        gap=int(16 * py))
    # IOI 撐滿到上下相鄰晶片的邊：間隙裡的 dummy/外圍整段給 AI，不依賴弱週期門檻剛好切在哪
    # （實測只用 2 張時弱門檻變高，dummy 帶只抓到 4429–4832，真實到 4996）
    for d in dummy:
        above = [c['y1'] for c in chips if c['y1'] <= d['y0'] and c['x0'] < d['x1'] and d['x0'] < c['x1']]
        below = [c['y0'] for c in chips if c['y0'] >= d['y1'] and c['x0'] < d['x1'] and d['x0'] < c['x1']]
        # 再往晶片內多包 kernel 死區（2 pitch + search）：晶片真正的邊不補邊（補了會誤判），那一圈交給 AI
        m = 2 * int(round(py)) + 2
        if above:
            d['y0'] = max(above) - m
        if below:
            d['y1'] = min(below) + m
    dummy = merge_bands(dummy, gap=0)
    area = st.w * st.H
    pat = float(m.sum()) * B * B
    roi = sum((r['x1'] - r['x0']) * (r['y1'] - r['y0']) for r in chips)
    return {'pitch': [px, py], 'slice_h': st.h, 'width': st.w, 'height': st.H, 'n_slices': len(st.paths),
            'chips': chips, 'dummy': dummy, 'block_thr': thr,
            'pattern_frac': round(pat / area, 4), 'roi_frac': round(roi / area, 4), 'block_map': bm}


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
