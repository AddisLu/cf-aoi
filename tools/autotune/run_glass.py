#!/usr/bin/env python3
"""
run_glass — 整片玻璃（多台 CCD）自動調參：每台 CCD 區域 → IP 掃門檻 → 找「平台」→ 配方 + 缺陷候選小圖。

沒有真值（舊機台缺陷清單）時不能用「0 誤判」校準（真缺陷會被當誤判把門檻推鬆）。改用**平台法**：
  門檻從敏感往鬆掃，雜訊檢出數隨門檻指數下降；真缺陷少且不隨門檻變 → 檢出數第一次降到 ≤ plateau 的門檻
  = 雜訊剛好壓住的點，再留 safety。平台上剩下的點 = 缺陷候選，裁小圖給人確認（介面「③ 確認缺陷」）。

在 Spark 跑：python3 run_glass.py --glass ~/cfaoi_reference/T550_G --ip ip/build_at/cfaoi_ip --out /tmp/glass
"""
import argparse
import glob
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

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


def count(ip, r, pi, search, th, strip, out, extra):
    V.IP_EXTRA[:] = extra
    xml = W.make_recipe(r['chips'], r['dummy'], pi, th, search)
    res, _, log = V.run_ip(ip, xml, strip, out)
    return V.collect(res), log


def sweep(ip, r, pi, search, strip, out, extra, polarity, start, stop, step):
    """回傳 [(門檻, 檢出數)]；暗：亮門檻關（9.0）、亮：暗門檻關（0.01）。"""
    curve = []
    t = start
    while (t >= stop) if polarity == 'dark' else (t <= stop):
        th = {'dark': round(t, 4), 'bright': 9.0} if polarity == 'dark' else {'dark': 0.01, 'bright': round(t, 4)}
        d, _ = count(ip, r, pi, search, th, strip, out, extra)
        curve.append((round(t, 4), len(d)))
        if len(d) == 0:
            break
        t = t - step if polarity == 'dark' else t + step
    return curve


def plateau(curve, polarity, p):
    """第一個檢出數 ≤ p 的門檻（由敏感往鬆走）。"""
    for t, n in curve:
        if n <= p:
            return t
    return curve[-1][0] if curve else None


def crops(strip_paths, defs, out, pitch_x=26, half=20, zoom=6):
    """每個候選：[缺陷 40×40 放大 6 倍 | 隔一個 pitch 的正常格子]，方便人看「跟鄰居哪裡不一樣」。"""
    os.makedirs(out, exist_ok=True)
    tiles = []
    for i, d in enumerate(defs[:60]):
        im = E.load(strip_paths[d['slice']])
        x, y = int(d['x']), int(d['y'])
        def cut(cx):
            c = np.zeros((2 * half, 2 * half), np.uint8)
            a = im[max(0, y - half):y + half, max(0, cx - half):cx + half]
            c[:a.shape[0], :a.shape[1]] = a
            return cv2.cvtColor(cv2.resize(c, (2 * half * zoom, 2 * half * zoom), interpolation=cv2.INTER_NEAREST),
                                cv2.COLOR_GRAY2BGR)
        ref_x = x + pitch_x if x + pitch_x + half < im.shape[1] else x - pitch_x
        a, b = cut(x), cut(ref_x)
        col = (0, 0, 255) if d['type'] == 'PointDark' else (0, 200, 255)
        cv2.circle(a, (half * zoom, half * zoom), 4 * zoom, col, 1)
        cv2.putText(a, f"#{d['slice']} ({x},{y}) {d['type'][5:]} s{d['size']}", (4, 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        cv2.putText(b, 'ref +1 pitch', (4, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
        t = np.hstack([a, np.full((a.shape[0], 4, 3), 255, np.uint8), b])
        cv2.imwrite(os.path.join(out, f"cand_{i:02d}_s{d['slice']}_x{x}_y{y}.png"), t)
        tiles.append(t)
    if tiles:
        while len(tiles) % 3:
            tiles.append(np.zeros_like(tiles[0]))
        sheet = np.vstack([np.hstack(tiles[i:i + 3]) for i in range(0, len(tiles), 3)])
        cv2.imwrite(os.path.join(out, 'sheet.png'), sheet)


def one_ccd(args):
    ccd_dir, ip, out_root, extra, p_dark, p_bright, safety, search = args
    name = os.path.basename(ccd_dir.rstrip('/'))
    out = os.path.join(out_root, name)
    os.makedirs(out, exist_ok=True)
    t0 = time.time()
    paths = sorted(glob.glob(os.path.join(ccd_dir, '*_Origin*.tif')))
    r = S.analyze_strip(paths, progress=lambda s: None)
    px, py = r['pitch']
    pi = (int(round(px)), int(round(py)))
    h = r['slice_h']
    imgs, masks = [], []
    for i, p in enumerate(paths):
        rects = S.rects_for_slice(r['chips'], i, h)
        if rects:
            im = E.load(p)
            imgs.append(im)
            masks.append(S.roi_mask(im.shape, rects))
    base = E.estimate_thresholds(imgs, pi[0], pi[1], 'div', masks, margin=0.0) if imgs else None
    del imgs, masks
    res = {'glass_dir': os.path.dirname(ccd_dir.rstrip('/')), 'ccd': name, 'pitch': [round(px, 2), round(py, 2)], 'pitch_int': pi, 'chips': r['chips'], 'ioi': r['dummy'],
           'roi_frac': r['roi_frac'], 'pattern_frac': r['pattern_frac'], 'n_slices': len(paths)}
    if not r['chips'] or base is None:
        res['error'] = '找不到晶片（沒有 pattern？）'
        return res
    work = os.path.join(out, 'run')
    cd = sweep(ip, r, pi, search, ccd_dir, work, extra, 'dark', round(base['floor_dark'], 2), 0.40, 0.01)
    cb = sweep(ip, r, pi, search, ccd_dir, work, extra, 'bright', round(base['floor_bright'], 2), 2.5, 0.01)
    td, tb = plateau(cd, 'dark', p_dark), plateau(cb, 'bright', p_bright)
    th = {'dark': round(td * (1 - safety), 3), 'bright': round(tb * (1 + safety), 3)}
    defs, log = count(ip, r, pi, search, th, ccd_dir, work, extra)
    roi_a, eff_a = V.inspected_area(log)
    glass = r['width'] * (r['height'] - min([d['y0'] for d in r['dummy']] + [c['y0'] for c in r['chips']]))
    open(os.path.join(out, 'RecipeInfo.xml'), 'w').write(W.make_recipe(r['chips'], r['dummy'], pi, th, search))
    crops(paths, defs, os.path.join(out, 'candidates'), pi[0])
    res.update({'floor': base, 'curve_dark': cd, 'curve_bright': cb, 'plateau': [td, tb], 'th': th,
                'candidates': defs, 'inspected_of_glass': round(eff_a / glass, 4) if glass else None,
                'roi_of_glass': round(roi_a / glass, 4) if glass else None, 'sec': round(time.time() - t0)})
    json.dump(res, open(os.path.join(out, 'result.json'), 'w'), ensure_ascii=False, indent=1)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--glass', required=True, help='含 IPnn/ 子目錄的整片玻璃資料夾')
    ap.add_argument('--ip', required=True)
    ap.add_argument('--out', default='/tmp/glass')
    ap.add_argument('--ccds', default='', help='只跑這些，如 IP01,IP02')
    ap.add_argument('--ip-args', default='--edge-fill 1')
    ap.add_argument('--plateau-dark', type=int, default=5, help='檢出數 ≤ 此值視為壓住雜訊（每台整條）')
    ap.add_argument('--plateau-bright', type=int, default=5)
    ap.add_argument('--safety', type=float, default=0.03)
    ap.add_argument('--jobs', type=int, default=3)
    a = ap.parse_args()
    dirs = sorted(d for d in glob.glob(os.path.join(a.glass, 'IP*')) if os.path.isdir(d))
    if a.ccds:
        dirs = [d for d in dirs if os.path.basename(d) in a.ccds.split(',')]
    jobs = [(d, a.ip, a.out, a.ip_args.split(), a.plateau_dark, a.plateau_bright, a.safety, (1, 1)) for d in dirs]
    with ProcessPoolExecutor(a.jobs) as ex:
        results = list(ex.map(one_ccd, jobs))
    print(f'{"CCD":5s} {"pitch":>13s} {"晶片":>4s} {"IOI":>3s} {"底線 暗/亮":>13s} {"門檻 暗/亮":>13s} {"候選":>4s} {"實檢":>7s}')
    for r in results:
        if 'error' in r:
            print(f'{r["ccd"]:5s} {r["error"]}')
            continue
        print(f'{r["ccd"]:5s} {r["pitch"][0]:6.2f}×{r["pitch"][1]:5.2f} {len(r["chips"]):4d} {len(r["ioi"]):3d} '
              f'{r["floor"]["floor_dark"]:.3f}/{r["floor"]["floor_bright"]:.3f} {r["th"]["dark"]:.3f}/{r["th"]["bright"]:.3f} '
              f'{len(r["candidates"]):4d} {r["inspected_of_glass"]:7.2%}  ({r["sec"]}s)')
    json.dump(results, open(os.path.join(a.out, 'glass_summary.json'), 'w'), ensure_ascii=False, indent=1)


def resheet(out_root):
    """由既有 result.json 重做候選小圖（不重跑 IP）。"""
    for f in sorted(glob.glob(os.path.join(out_root, 'IP*', 'result.json'))):
        r = json.load(open(f))
        if r.get('candidates') is None:
            continue
        paths = sorted(glob.glob(os.path.join(r['glass_dir'], r['ccd'], '*_Origin*.tif'))) if r.get('glass_dir') else None
        if paths:
            crops(paths, r['candidates'], os.path.join(os.path.dirname(f), 'candidates'), r['pitch_int'][0])


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--resheet':
        resheet(sys.argv[2])
    else:
        main()
