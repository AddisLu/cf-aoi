#!/usr/bin/env python3
"""
autotune_engine — 自動調參核心（純影像分析，不碰設備）。

輸入：存圖模式收集的影像，依舊機台 IP04 命名：<集合>/IPnn/IPnn_Origin000001.tif（nn = CCD 編號）
      + 使用者唯一要設的「幾 up」（一片玻璃幾顆晶片）。
輸出：每台 CCD 的建議參數（pitch、門檻、檢測區/bypass、上下邊界）+ 信心 + 給介面用的預覽。

演算法（每一步都有實測依據，見 docs/design/autotune_spec.md）：
  pitch   每台取數個 pattern 區塊，去低頻後 X/Y 剖面做 FFT，**諧波疊加（HPS）**挑基本週期
          ——直接取最大峰會抓到子畫素倍頻（實測 IP04 Y 方向抓到 6.0 而非 18.3）
  門檻    pattern 區每點與 ±pitch 四鄰平均比較（DIV = 比值；SUB = 灰階差），取乾淨影像的
          雜訊底線（1e-6 分位），再留安全邊際（預設 15%）。實測 IP04：底線 0.70/1.25 → 0.60/1.44，
          與人工調定的 0.60/1.40 一致；已知暗缺陷比值 0.41 仍抓得到
  區域    局部紋理能量（高通後的區域標準差，門檻 = 90 百分位 × 30%）→ pattern 遮罩 → Y 投影分段（晶片列）、
          X 投影分段（該 CCD 看到的晶片欄）；玻璃上下邊界 = 平均亮度剖面的大躍變
          整片：各 CCD 的 X 投影依序接起來分段 = 晶片欄數；「欄 × 列」必須 = 幾 up，否則標出不一致
  放寬    個別 CCD：門檻邊際乘上 (1 + 放寬%)（暗門檻往下、亮門檻往上 / SUB 絕對值變大）
"""
import glob
import json
import os
import re

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None


# ───────────────────────── 讀圖 ─────────────────────────
NAME_RE = re.compile(r'IP(\d{2})_Origin(\d{6})\.(tif|tiff|png|bmp)$', re.I)


def list_images(root):
    """{ccd 編號: [檔案…]}（IPnn 資料夾或平放都可）。"""
    out = {}
    for f in sorted(glob.glob(os.path.join(root, '**', 'IP??_Origin*.*'), recursive=True)):
        m = NAME_RE.search(os.path.basename(f))
        if m:
            out.setdefault(int(m.group(1)), []).append(f)
    return out


def load(path):
    im = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if im is None:
        raise IOError(f'讀不到影像：{path}')
    return im if im.ndim == 2 else cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)


# ───────────────────────── pitch ─────────────────────────
def pitch_hps(profile, lo=8.0, hi=200.0, harmonics=3, pad=8):
    """1D 剖面 → 基本週期（px，浮點）與信心（峰值 / 中位頻譜）。"""
    p = np.asarray(profile, np.float64)
    p = (p - p.mean()) * np.hanning(len(p))
    n = len(p)
    N = pad * n
    F = np.abs(np.fft.rfft(p, N))
    kmin, kmax = max(1, int(N / hi)), min(len(F) - 1, int(N / lo))
    if kmax <= kmin + 2:
        return None, 0.0
    hps = np.ones(kmax - kmin)
    for h in range(1, harmonics + 1):
        idx = np.arange(kmin, kmax) * h
        v = np.zeros(kmax - kmin)
        ok = idx < len(F)
        v[ok] = F[idx[ok]]
        hps *= v + 1e-12
    k = kmin + int(np.argmax(hps))
    lo_k, hi_k = max(kmin, k - 3), min(kmax, k + 4)
    k2 = lo_k + int(np.argmax(F[lo_k:hi_k]))
    if 1 <= k2 < len(F) - 1:                                  # 拋物線內插
        a, b, c = np.log(F[k2 - 1] + 1e-12), np.log(F[k2] + 1e-12), np.log(F[k2 + 1] + 1e-12)
        d = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else 0.0
    else:
        d = 0.0
    conf = float(F[k2] / (np.median(F[kmin:kmax]) + 1e-12))
    return float(N / (k2 + d)), conf


def estimate_pitch(img, mask=None, block=1024, nblocks=4):
    """在 pattern 區取數個區塊估 pitch；回 {'x','y','conf','samples'}（中位數）。"""
    h, w = img.shape
    rng = np.random.default_rng(0)
    xs, ys, cs = [], [], []
    tries = 0
    while len(xs) < nblocks and tries < 40:
        tries += 1
        y0 = int(rng.integers(0, max(1, h - block)))
        x0 = int(rng.integers(0, max(1, w - block)))
        if mask is not None and mask[y0:y0 + block, x0:x0 + block].mean() < 0.95:
            continue
        roi = img[y0:y0 + block, x0:x0 + block].astype(np.float32)
        roi = roi - cv2.GaussianBlur(roi, (0, 0), 25)
        px, cx = pitch_hps(roi.mean(axis=0))
        py, cy = pitch_hps(roi.mean(axis=1))
        if px and py:
            xs.append(px)
            ys.append(py)
            cs.append(min(cx, cy))
    if not xs:
        return None
    return {'x': float(np.median(xs)), 'y': float(np.median(ys)), 'conf': float(np.median(cs)),
            'samples': [[round(a, 2), round(b, 2)] for a, b in zip(xs, ys)]}


# ───────────────────────── 門檻 ─────────────────────────
def neighbor_stat(img, px, py, mode='div'):
    """每點與 ±pitch 四鄰平均比較：div → 比值；sub → 灰階差。邊緣 2×pitch 去掉。"""
    f = img.astype(np.float32) + (1.0 if mode == 'div' else 0.0)
    nb = (np.roll(f, px, 1) + np.roll(f, -px, 1) + np.roll(f, py, 0) + np.roll(f, -py, 0)) / 4
    r = f / nb if mode == 'div' else f - nb
    m = 2 * max(px, py)
    return r[m:-m, m:-m]


def estimate_thresholds(imgs, px, py, mode='div', masks=None, q=1e-6, margin=0.15):
    """乾淨影像的雜訊底線（每張取 q / 1-q 分位，取跨張中位數 → 少數真缺陷不影響）＋安全邊際。
    div：暗 = 底線 × (1-margin)、亮 = 底線 × (1+margin)；sub：灰階差 × (1+margin)。"""
    lo, hi = [], []
    for i, im in enumerate(imgs):
        r = neighbor_stat(im, px, py, mode)
        if masks is not None and masks[i] is not None:
            m = 2 * max(px, py)
            r = r[masks[i][m:-m, m:-m] > 0]
        r = r.ravel()
        if r.size < 1000:
            continue
        lo.append(float(np.quantile(r, q)))
        hi.append(float(np.quantile(r, 1 - q)))
    if not lo:
        return None
    flo, fhi = float(np.median(lo)), float(np.median(hi))
    if mode == 'div':
        dark, bright = flo * (1 - margin), fhi * (1 + margin)
    else:
        dark, bright = flo * (1 + margin), fhi * (1 + margin)          # sub：負值更負、正值更大
    return {'mode': mode, 'floor_dark': round(flo, 4), 'floor_bright': round(fhi, 4),
            'dark': round(dark, 3), 'bright': round(bright, 3), 'margin': margin, 'n_images': len(lo)}


def loosen(th, pct):
    """個別 CCD 放寬：邊際乘 (1+pct)。pct=0.2 → 邊際 15% 變 18%…；回新 dict。"""
    m = th['margin'] * (1 + pct) + pct * 0.5 * (1 if th['mode'] == 'div' else 0)
    if th['mode'] == 'div':
        d, b = th['floor_dark'] * (1 - m), th['floor_bright'] * (1 + m)
    else:
        d, b = th['floor_dark'] * (1 + m), th['floor_bright'] * (1 + m)
    return {**th, 'dark': round(d, 3), 'bright': round(b, 3), 'margin': round(m, 4), 'loosened': pct}


# ───────────────────────── 區域：pattern / bypass / 上下邊界 ─────────────────────────
def pattern_mask(img, px=26, py=18, win=None, rel=0.3, abs_min=3.0):
    """局部紋理能量（高通後區域標準差）→ pattern 遮罩（1 = 有 pattern 要檢測，0 = bypass）。
    門檻 = max(90 百分位能量 × rel, abs_min)：pattern 能量遠高於平坦玻璃的雜訊；
    不用 Otsu——整張都是 pattern 時它會把暗角處切掉（實測 IP04 誤切 35%）。"""
    win = win or int(4 * max(px, py))
    f = img.astype(np.float32)
    hp = f - cv2.GaussianBlur(f, (0, 0), max(px, py))
    e = cv2.sqrt(cv2.boxFilter(hp * hp, -1, (win, win)))
    thr = max(float(np.percentile(e, 90)) * rel, abs_min)
    m = (e > thr).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((win, win), np.uint8))
    return m


def segments(profile, min_len, thr=0.5):
    """0/1 投影比例 → [(start, end)] 連續段（長度 ≥ min_len）。"""
    on = profile >= thr
    out, s = [], None
    for i, v in enumerate(on):
        if v and s is None:
            s = i
        if not v and s is not None:
            if i - s >= min_len:
                out.append((s, i))
            s = None
    if s is not None and len(on) - s >= min_len:
        out.append((s, len(on)))
    return out


def energy_profile(img, px, py, axis, sel=None):
    """沿某方向的紋理能量剖面（axis=0：每列一值；axis=1：每欄一值），只平均 sel 選到的另一方向範圍，
    再以一個 pitch 寬做方框平滑——週期起伏被抹平、邊界仍是對稱的斜坡，半高點 = 真邊界。"""
    f = img.astype(np.float32)
    h = f - cv2.GaussianBlur(f, (0, 0), max(px, py))
    # 只算「有週期」的能量：|hp| 減掉與 ±1 pitch 鄰居（沿剖面方向）的最小差。單條線、pad 邊、
    # 亮帶交界在 ±pitch 處對不上 → 歸零，不會把邊界拉走（實測 dummy 區下緣：65 px → 見 spec）。
    p = py if axis == 0 else px
    shifts = sorted({int(np.floor(p)), int(np.ceil(p))})
    res = None
    for d in shifts:
        for sgn in (1, -1):
            r = np.abs(h - np.roll(h, sgn * d, axis=axis))
            res = r if res is None else np.minimum(res, r)
    hp = np.clip(np.abs(h) - res, 0, None)
    if sel is not None:
        hp = hp[:, sel] if axis == 0 else hp[sel, :]
    prof = hp.mean(axis=1 - axis)
    k = int(round(py if axis == 0 else px)) | 1
    return np.convolve(prof, np.ones(k) / k, mode='same')


def refine_segments(prof, segs, search):
    """粗分段（大窗口，邊界會往 bypass 多吃半個窗口）→ 在 ±search 內找能量剖面的半高點。
    實測 T550 IP04 第 14/15 張晶片間隙：粗分段誤差 ~50 px → 細修後 ≤ 1 個 pitch。"""
    n = len(prof)
    out = []
    for s, e in segs:
        def cross(b, inside_dir):
            lo, hi = max(0, b - search), min(n, b + search)
            ins = prof[max(0, b - 3 * search):max(1, b - search)] if inside_dir < 0 else prof[min(n - 1, b + search):min(n, b + 3 * search)]
            outs = prof[min(n - 1, b + search):min(n, b + 3 * search)] if inside_dir < 0 else prof[max(0, b - 3 * search):max(1, b - search)]
            if len(ins) == 0 or len(outs) == 0:
                return b                                           # 貼影像邊：沒有外側可比，維持原值
            mid = (np.median(ins) + np.median(outs)) / 2
            w = prof[lo:hi] >= mid
            idx = np.nonzero(w[1:] != w[:-1])[0]
            if len(idx) == 0:
                return b
            return int(lo + idx[np.argmin(np.abs(lo + idx - b))] + 1)
        s2 = cross(s, +1) if s > 0 else s
        e2 = cross(e, -1) if e < n else e
        out.append((s2, e2) if e2 - s2 > search else (s, e))
    return out


def glass_edges(img, smooth=31, min_jump=12.0):
    """玻璃上下邊界：列平均亮度剖面的第一/最後一個大躍變（同 IP edge_check 的想法）。"""
    prof = img.mean(axis=1).astype(np.float32)
    k = smooth | 1
    p = np.convolve(prof, np.ones(k) / k, mode='same')
    gap = k
    d = np.zeros_like(p)
    d[gap:-gap] = p[2 * gap:] - p[:-2 * gap]
    top = next((i for i in range(len(d)) if abs(d[i]) >= min_jump), None)
    bot = next((i for i in range(len(d) - 1, -1, -1) if abs(d[i]) >= min_jump), None)
    return top, bot


def split_dummy(segs, n, pitch, max_pitches=60):
    """晶片 vs 晶片間的 dummy 週期帶：不貼影像邊、長度 < max_pitches 個 pitch 的段 = dummy 帶（實測 T550 dummy 帶 ≈ 30 pitch，留 2 倍）
    （最小的 32″ 晶片短邊也有數千 px；貼邊的段可能是只照到一部分的晶片，一律當晶片）。
    dummy 帶不做週期比對（bypass），改列為 IOI 興趣區裁圖給 AI 檢（Addis 2026-10-05）。"""
    chip, dummy = [], []
    for s, e in segs:
        (dummy if s > 0 and e < n and e - s < max_pitches * pitch else chip).append((s, e))
    return chip, dummy


def analyze_regions(img, px, py, min_chip_frac=0.08):
    mask = pattern_mask(img, int(round(px)), int(round(py)))
    h, w = mask.shape
    rows = segments(mask.mean(axis=1), int(h * min_chip_frac))
    cols = segments(mask.mean(axis=0), int(w * 0.02))
    search = int(4 * max(px, py))                       # = pattern_mask 窗口
    if rows and cols:                                   # 細修：只看有 pattern 的那幾欄 / 那幾列
        csel = np.zeros(w, bool); [csel.__setitem__(slice(a, b), True) for a, b in cols]
        rsel = np.zeros(h, bool); [rsel.__setitem__(slice(a, b), True) for a, b in rows]
        rows = refine_segments(energy_profile(img, px, py, 0, csel), rows, search)
        cols = refine_segments(energy_profile(img, px, py, 1, rsel), cols, search)
    top, bot = glass_edges(img)
    rows, drows = split_dummy(rows, h, py)
    cols, dcols = split_dummy(cols, w, px)
    rect = lambda r, c: {'StartX': c[0], 'EndX': c[1] - 1, 'StartY': r[0], 'EndY': r[1] - 1}
    zones = [rect(r, c) for r in rows for c in cols]                       # DetectRoi：週期比對
    ioi = [rect(r, c) for r in rows + drows for c in cols + dcols           # DetectIoi：dummy 帶給 AI
           if r in drows or c in dcols]
    return {'mask': mask, 'rows': rows, 'cols': cols, 'dummy_rows': drows, 'dummy_cols': dcols,
            'glass_top': top, 'glass_bottom': bot, 'zones': zones, 'ioi': ioi,
            'pattern_frac': round(float(mask.mean()), 3)}


def check_up(per_ccd_regions, n_up, min_col_frac=0.02):
    """晶片列數 = 各 CCD 列數的眾數；晶片欄數 = 把各 CCD 的 X 方向 pattern 投影依 CCD 順序接成整片
    寬的剖面後分段（晶片間隙比一條 CCD 窄，不能用「整條 CCD 有沒有 pattern」判斷）。列 × 欄 應 = 幾 up。"""
    rows = [len(r['rows']) for r in per_ccd_regions.values() if r['rows']]
    nrow = int(np.bincount(rows).argmax()) if rows else 0
    prof = np.concatenate([r['mask'].mean(axis=0) for _, r in sorted(per_ccd_regions.items())]) if per_ccd_regions else np.zeros(1)
    cols = segments(prof, int(len(prof) * min_col_frac), thr=0.2)
    ncol = len(cols)
    ok = nrow * ncol == n_up if n_up else None
    return {'rows': nrow, 'cols': ncol, 'detected': nrow * ncol, 'n_up': n_up, 'ok': ok}


# ───────────────────────── 整體流程 ─────────────────────────
def run(root, n_up, mode='div', margin=0.15, max_per_ccd=6, loosen_map=None, progress=print):
    files = list_images(root)
    if not files:
        raise SystemExit(f'{root} 底下沒有 IPnn_Origin*.tif')
    res, regions = {}, {}
    for ccd, fl in sorted(files.items()):
        imgs = [load(f) for f in fl[:max_per_ccd]]
        p = estimate_pitch(imgs[0])
        if not p:
            res[ccd] = {'error': 'pitch 估不出來（沒有 pattern？）'}
            continue
        px, py = int(round(p['x'])), int(round(p['y']))
        reg = analyze_regions(imgs[0], p['x'], p['y'])
        regions[ccd] = reg
        masks = [analyze_regions(im, p['x'], p['y'])['mask'] if i else reg['mask'] for i, im in enumerate(imgs)]
        th = estimate_thresholds(imgs, px, py, mode, masks, margin=margin)
        if th and loosen_map and loosen_map.get(ccd):
            th = loosen(th, loosen_map[ccd])
        res[ccd] = {'pitch': p, 'pitch_int': [px, py], 'thresholds': th,
                    'zones': reg['zones'], 'ioi': reg['ioi'], 'glass': [reg['glass_top'], reg['glass_bottom']],
                    'pattern_frac': reg['pattern_frac'], 'n_images': len(fl)}
        progress(f'CCD{ccd:02d}: pitch {p["x"]:.2f}×{p["y"]:.2f}（{px}×{py}，信心 {p["conf"]:.0f}）'
                 f' 門檻 {th["dark"] if th else "?"}/{th["bright"] if th else "?"} 區塊 {len(reg["zones"])}')
    up = check_up(regions, n_up)
    return {'n_up': n_up, 'mode': mode, 'margin': margin, 'up_check': up, 'ccd': res}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='自動調參（分析存圖模式收集的影像）')
    ap.add_argument('root')
    ap.add_argument('--up', type=int, required=True, help='一片玻璃幾顆晶片')
    ap.add_argument('--mode', choices=['div', 'sub'], default='div')
    ap.add_argument('--margin', type=float, default=0.15)
    ap.add_argument('--out', default='')
    a = ap.parse_args()
    r = run(a.root, a.up, a.mode, a.margin)
    print(json.dumps(r['up_check'], ensure_ascii=False))
    if a.out:
        json.dump(r, open(a.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
