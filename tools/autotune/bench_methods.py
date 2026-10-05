#!/usr/bin/env python3
"""
bench_methods — 演算法方案評比（離線、不需外網、不需真值）：
  前處理 × 偵測 的每個組合：①在乾淨整條上用平台法校準門檻（雜訊壓住）②在植入人工缺陷的影像上量
  檢出率（大小 × 對比 × 暗/亮）、誤判、GPU 時間 → 軟體依「同樣不誤判下誰抓得最多」自動選方案。

前處理（Python 先做原型、寫成新 TIF 交 IP 檢；有效的再搬進 IP host 端）：
  none      不處理
  ffc       逐欄平場（FFC）：欄平均 ÷ 同相位鄰欄（±k·pitch，內插）中位數 = 感測器逐欄增益（PRNU）；
            低頻（鏡頭暗角/光源）= 欄平均做 pitch 寬移動平均。兩者都由調參收集的影像算（不用另拍白板）
  rownorm   逐列正規化（線掃光源閃爍）：每張影像每列平均 ÷ 同相位鄰列（±k·pitch）中位數，**執行期每張算**
  ffc_row   兩者合併
  median3   3×3 中值去噪
偵測（IP，recipe_writer.METHODS）：div（mode 0）/ sub（mode 1，T550 生產參數）/ divvote（mode 2 + 多尺度）

在 Spark 跑：python3 bench_methods.py --strip ~/cfaoi_reference/T550_G/IP04 --ip ip/build_at/cfaoi_ip --out /tmp/bench
"""
import argparse
import glob
import json
import os
import re
import shutil
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autotune_engine as E      # noqa: E402
import autotune_strip as S       # noqa: E402
import recipe_writer as W        # noqa: E402
import validate_ip as V          # noqa: E402

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

SIZES = [1, 2, 3, 5, 9]
DARK_F = [0.85, 0.75, 0.65, 0.55, 0.45]
BRIGHT_F = [1.15, 1.25, 1.35, 1.50, 1.70]


# ───────────── 前處理 ─────────────
def periodic_median(v, p, k=3):
    """v[i] 的同相位鄰居（i ± j·p，j=1..k，線性內插）中位數。"""
    n = len(v)
    idx = np.arange(n, dtype=np.float64)
    samples = []
    for j in range(1, k + 1):
        for s in (j * p, -j * p):
            q = idx + s
            ok = (q >= 0) & (q <= n - 1)
            val = np.interp(np.clip(q, 0, n - 1), idx, v)
            val[~ok] = np.nan
            samples.append(val)
    return np.nanmedian(np.vstack(samples), axis=0)


def ffc_gain(paths, chips, h, px):
    """由乾淨整條算逐欄增益：PRNU（同相位比）× 低頻暗角（pitch 寬移動平均，正規化到 1）。"""
    acc, n = None, 0
    for i, p in enumerate(paths):
        rects = S.rects_for_slice(chips, i, h)
        if not rects:
            continue
        im = E.load(p).astype(np.float64)
        for x0, y0, x1, y1 in rects:
            if x0 != 0 or y1 - y0 < 500:
                continue
            cm = im[y0:y1].mean(axis=0)
            acc = cm if acc is None else acc + cm
            n += 1
    if acc is None:
        return None
    col = acc / n
    prnu = col / periodic_median(col, px)
    k = int(round(px)) * 4 + 1
    low = np.convolve(col, np.ones(k) / k, mode='same')
    low[:k] = low[k]; low[-k:] = low[-k - 1]
    low = low / np.median(low)
    return (prnu * low).astype(np.float32)


def rownorm(im, py):
    rm = im.astype(np.float64).mean(axis=1)
    g = rm / periodic_median(rm, py)
    return np.clip(im.astype(np.float32) / g[:, None].astype(np.float32), 0, 255)


def apply_pre(name, im, gain, py):
    f = im.astype(np.float32)
    if name in ('ffc', 'ffc_row') and gain is not None:
        f = f / gain[None, :]
    if name in ('rownorm', 'ffc_row'):
        f = rownorm(f, py)
    if name == 'median3':
        return cv2.medianBlur(im, 3)
    return np.clip(np.round(f), 0, 255).astype(np.uint8)


def write_set(src_paths, out_dir, pre, gain, py, imgs=None):
    os.makedirs(out_dir, exist_ok=True)
    for f in glob.glob(os.path.join(out_dir, '*.tif')):
        os.unlink(f)
    for i, p in enumerate(src_paths):
        im = imgs[i] if imgs is not None else E.load(p)
        if pre == 'none' and imgs is None:
            os.symlink(p, os.path.join(out_dir, os.path.basename(p)))
            continue
        cv2.imwrite(os.path.join(out_dir, os.path.basename(p)), apply_pre(pre, im, gain, py))
    return out_dir


# ───────────── 人工缺陷 ─────────────
def inject(paths, slices, chips, h, seed=7):
    """在指定 slice 的晶片內等距植入缺陷（大小 × 對比 × 暗/亮 輪流），回傳 (影像 dict, 真值 list)。"""
    rng = np.random.default_rng(seed)
    combos = [(pol, s, f) for pol, fs in (('dark', DARK_F), ('bright', BRIGHT_F)) for s in SIZES for f in fs]
    imgs, truth, ci = {}, [], 0
    for sl in slices:
        im = E.load(paths[sl]).copy()
        rects = S.rects_for_slice(chips, sl, h)
        if not rects:
            continue
        x0, y0, x1, y1 = max(rects, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))
        xs = np.linspace(x0 + 300, x1 - 300, 10)
        ys = np.linspace(y0 + 300, y1 - 300, 8)
        for yy in ys:
            for xx in xs:
                pol, s, f = combos[ci % len(combos)]
                ci += 1
                cx, cy = int(xx + rng.integers(-80, 80)), int(yy + rng.integers(-80, 80))
                r = s / 2.0
                yy0, xx0 = cy - int(np.ceil(r)), cx - int(np.ceil(r))
                Y, X = np.mgrid[yy0:yy0 + s + 1, xx0:xx0 + s + 1]
                m = ((X - cx + 0.5) ** 2 + (Y - cy + 0.5) ** 2) <= max(r, 0.71) ** 2
                patch = im[yy0:yy0 + s + 1, xx0:xx0 + s + 1].astype(np.float32)
                patch[m] = np.clip(patch[m] * f, 0, 255)
                im[yy0:yy0 + s + 1, xx0:xx0 + s + 1] = patch.astype(np.uint8)
                truth.append({'slice': sl, 'x': cx, 'y': cy, 'pol': pol, 'size': s, 'f': f})
        imgs[sl] = im
    return imgs, truth


def match(defs, truth, base_defs):
    """檢出 ↔ 真值（距離 ≤ max(4, size)）；乾淨影像本來就有的點（base_defs）不算誤判。"""
    hit = [False] * len(truth)
    fp = 0
    for d in defs:
        best = None
        for i, t in enumerate(truth):
            if t['slice'] == d['slice'] and abs(t['x'] - d['x']) <= max(4, t['size']) and abs(t['y'] - d['y']) <= max(4, t['size']):
                best = i
                break
        if best is not None:
            hit[best] = True
        elif not any(b['slice'] == d['slice'] and abs(b['x'] - d['x']) <= 4 and abs(b['y'] - d['y']) <= 4 for b in base_defs):
            fp += 1
    return hit, fp


# ───────────── 校準 ─────────────
def run(ip, chips, ioi, pi, th, strip, out, mode, opt=None, extra=('--edge-fill', '1')):
    V.IP_EXTRA[:] = list(extra)
    xml = W.make_recipe(chips, ioi, pi, th, (1, 1), mode, opt)
    res, sec, log = V.run_ip(ip, xml, strip, out)
    ms = 0.0
    for f in glob.glob(res + '/**/*ResultInfo.json', recursive=True):
        for roi in json.load(open(f)).get('RoiInfoList', []):
            ms += roi.get('process_time_ms', 0.0)
    return V.collect(res), ms


def calibrate(ip, chips, ioi, pi, strip, out, mode, floor, plateau_n=5):
    """平台法：暗/亮分開由敏感往鬆掃，第一個檢出數 ≤ plateau_n 的門檻。"""
    if mode == 'sub':
        dark_seq = [-v for v in range(4, 80, 2)]
        bright_seq = list(range(4, 80, 2))
        off_d, off_b = -255, 255
    else:
        dark_seq = [round(v, 3) for v in np.arange(min(0.95, floor['floor_dark'] + 0.05), 0.35, -0.01)]
        bright_seq = [round(v, 3) for v in np.arange(max(1.05, floor['floor_bright'] - 0.05), 2.5, 0.01)]
        off_d, off_b = 0.01, 9.0
    res = {}
    for pol, seq in (('dark', dark_seq), ('bright', bright_seq)):
        curve = []
        for t in seq:
            th = {'dark': t, 'bright': off_b} if pol == 'dark' else {'dark': off_d, 'bright': t}
            d, _ = run(ip, chips, ioi, pi, th, strip, out, mode)
            curve.append((t, len(d)))
            if len(d) <= plateau_n:
                break
        res[pol] = (curve[-1][0], curve)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--strip', required=True)
    ap.add_argument('--ip', required=True)
    ap.add_argument('--out', default='/tmp/bench')
    ap.add_argument('--pre', default='none,ffc,rownorm,ffc_row,median3')
    ap.add_argument('--modes', default='div,divvote,sub')
    ap.add_argument('--inj-slices', default='')
    ap.add_argument('--safety', type=float, default=0.03)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    paths = sorted(glob.glob(os.path.join(a.strip, '*_Origin*.tif')))
    cache = os.path.join(a.out, 'regions.json')
    if os.path.exists(cache):
        r = json.load(open(cache))
    else:
        r = S.analyze_strip(paths, progress=lambda s: None)
        r.pop('block_map', None)
        json.dump(r, open(cache, 'w'))
    chips, ioi, h = r['chips'], r['dummy'], r['slice_h']
    px, py = r['pitch']
    pi = (int(round(px)), int(round(py)))
    full = [i for i in range(len(paths)) if any(c['y0'] <= i * h and (i + 1) * h <= c['y1'] for c in chips)]
    inj_sl = [int(v) for v in a.inj_slices.split(',')] if a.inj_slices else full[2::max(1, len(full) // 6)][:6]
    print(f'pitch {px:.2f}×{py:.2f}；晶片 {[(c["y0"], c["y1"]) for c in chips]}；植入 slice {inj_sl}', flush=True)

    gain = ffc_gain(paths, chips, h, px)
    if gain is not None:
        np.save(os.path.join(a.out, 'ffc_gain.npy'), gain)
        print(f'FFC 逐欄增益：PRNU+暗角 範圍 {gain.min():.3f}–{gain.max():.3f}，標準差 {gain.std():.4f}', flush=True)
    inj_imgs, truth = inject(paths, inj_sl, chips, h)
    inj_paths = [paths[s] for s in inj_sl]
    json.dump(truth, open(os.path.join(a.out, 'truth.json'), 'w'))

    rows = []
    for pre in a.pre.split(','):
        clean_dir = write_set(paths, os.path.join(a.out, 'set', pre, 'clean'), pre, gain, py)
        inj_dir = write_set(inj_paths, os.path.join(a.out, 'set', pre, 'inj'), pre, gain, py,
                            imgs=[inj_imgs[s] for s in inj_sl])
        floor = None
        for mode in a.modes.split(','):
            t0 = time.time()
            work = os.path.join(a.out, 'run', f'{pre}_{mode}')
            if mode != 'sub' and floor is None:
                ims, masks = [], []
                for i in full[::3]:
                    im = E.load(os.path.join(clean_dir, os.path.basename(paths[i])))
                    ims.append(im)
                    masks.append(S.roi_mask(im.shape, S.rects_for_slice(chips, i, h)))
                floor = E.estimate_thresholds(ims, pi[0], pi[1], 'div', masks, margin=0.0)
            cal = calibrate(a.ip, chips, ioi, pi, clean_dir, work, mode, floor)
            td, tb = cal['dark'][0], cal['bright'][0]
            s = a.safety
            th = ({'dark': int(round(td * (1 + s))) - 1, 'bright': int(round(tb * (1 + s))) + 1} if mode == 'sub'
                  else {'dark': round(td * (1 - s), 3), 'bright': round(tb * (1 + s), 3)})
            clean_defs, ms_clean = run(a.ip, chips, ioi, pi, th, clean_dir, work, mode)
            base = [d for d in clean_defs if d['slice'] in inj_sl]
            inj_defs, _ = run(a.ip, chips, ioi, pi, th, inj_dir, work, mode)
            hit, fp = match(inj_defs, truth, base)
            det = {}
            for pol, fs in (('dark', DARK_F), ('bright', BRIGHT_F)):
                for sz in SIZES:
                    for f in fs:
                        idx = [i for i, t in enumerate(truth) if t['pol'] == pol and t['size'] == sz and t['f'] == f]
                        det[f'{pol}|{sz}|{f}'] = round(sum(hit[i] for i in idx) / len(idx), 2) if idx else None
            rate = sum(hit) / len(hit)
            row = {'pre': pre, 'mode': mode, 'th': th, 'clean_defects': len(clean_defs), 'inj_fp': fp,
                   'detect_rate': round(rate, 3), 'detail': det, 'gpu_ms_per_slice': round(ms_clean / len(paths), 2),
                   'curve_dark': cal['dark'][1], 'curve_bright': cal['bright'][1], 'sec': round(time.time() - t0)}
            rows.append(row)
            json.dump(rows, open(os.path.join(a.out, 'bench.json'), 'w'), ensure_ascii=False, indent=1)
            print(f'{pre:8s} {mode:8s} 門檻 {th["dark"]}/{th["bright"]}  乾淨檢出 {len(clean_defs):3d}  '
                  f'植入檢出率 {rate:6.1%}  植入誤判 {fp:3d}  GPU {row["gpu_ms_per_slice"]:.1f} ms/張（{row["sec"]}s）',
                  flush=True)
        shutil.rmtree(os.path.join(a.out, 'set', pre), ignore_errors=True)


if __name__ == '__main__':
    main()
