#!/usr/bin/env python3
"""
bench_speed — 「DIV 只檢暗 + DIV 投票只檢亮」雙方案的產能評估：DIV 投票的輕量變體
（PitchTime 2→1、多尺度開/關）各自校準亮門檻（平台法）→ 植入缺陷的亮檢出率 + GPU ms/張。
產能：37 CCD × 30 張 = 1,110 張/片、30 s 節拍、1 台 Spark（docs/verification/verification_report_arm_20260615.md）。
在 Spark：python3 bench_speed.py --strip ~/cfaoi_reference/T550_G/IP04 --ip ip/build_at/cfaoi_ip --out /tmp/bench_speed
"""
import argparse
import glob
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autotune_strip as S       # noqa: E402
import bench_methods as BM       # noqa: E402

SLICES_PER_PANEL = 37 * 30
TACT_S = 30.0

VARIANTS = [
    ('divvote 2p+ms（評比設定）', 'divvote', {'pitch_time': 2, 'choose': 13, 'multiscale': 1}),
    ('divvote 2p', 'divvote', {'pitch_time': 2, 'choose': 13, 'multiscale': 0}),
    ('divvote 1p+ms', 'divvote', {'pitch_time': 1, 'choose': 7, 'multiscale': 1}),
    ('divvote 1p', 'divvote', {'pitch_time': 1, 'choose': 7, 'multiscale': 0}),
    ('divvote 1p choose6', 'divvote', {'pitch_time': 1, 'choose': 6, 'multiscale': 0}),
    ('div（對照）', 'div', {}),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--strip', required=True)
    ap.add_argument('--ip', required=True)
    ap.add_argument('--out', default='/tmp/bench_speed')
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
    pi = (int(round(r['pitch'][0])), int(round(r['pitch'][1])))
    full = [i for i in range(len(paths)) if any(c['y0'] <= i * h and (i + 1) * h <= c['y1'] for c in chips)]
    inj_sl = full[2::max(1, len(full) // 6)][:6]
    inj_imgs, truth = BM.inject(paths, inj_sl, chips, h)
    inj_dir = BM.write_set([paths[s] for s in inj_sl], os.path.join(a.out, 'inj'), 'none', None, r['pitch'][1],
                           imgs=[inj_imgs[s] for s in inj_sl])
    bt = [i for i, t in enumerate(truth) if t['pol'] == 'bright']
    rows = []
    for name, mode, opt in VARIANTS:
        work = os.path.join(a.out, 'run')
        curve = []
        for b in np.arange(1.10, 2.0, 0.01):
            th = {'dark': 0.01, 'bright': round(float(b), 3)}
            d, ms = BM.run(a.ip, chips, ioi, pi, th, a.strip, work, mode, opt)
            curve.append((round(float(b), 3), len(d)))
            if len(d) <= 5:
                break
        tb = round(curve[-1][0] * 1.03, 3)
        th = {'dark': 0.01, 'bright': tb}
        _, ms = BM.run(a.ip, chips, ioi, pi, th, a.strip, work, mode, opt)
        ms_slice = ms / len(paths)
        d, _ = BM.run(a.ip, chips, ioi, pi, th, inj_dir, work, mode, opt)
        hit, fp = BM.match(d, truth, [])
        by = {f: round(np.mean([hit[i] for i in bt if truth[i]['f'] == f]), 2) for f in BM.BRIGHT_F}
        rate = float(np.mean([hit[i] for i in bt]))
        panel_s = (ms_slice + 7.7) * SLICES_PER_PANEL / 1000 if mode != 'div' else ms_slice * SLICES_PER_PANEL / 1000
        row = {'name': name, 'bright_th': tb, 'bright_rate': round(rate, 3), 'by_contrast': by,
               'ms_slice': round(ms_slice, 1), 'panel_s_with_div': round(panel_s, 1),
               'tact_use': round(panel_s / TACT_S, 2)}
        rows.append(row)
        json.dump(rows, open(os.path.join(a.out, 'speed.json'), 'w'), ensure_ascii=False, indent=1)
        print(f'{name:22s} 亮門檻 {tb:.3f}  亮檢出 {rate:6.1%}  25/35/50/70%：'
              f'{by[1.25]:.0%}/{by[1.35]:.0%}/{by[1.5]:.0%}/{by[1.7]:.0%}  GPU {ms_slice:5.1f} ms/張  '
              f'＋DIV 每片 {panel_s:5.1f} s（節拍 {panel_s / TACT_S:.0%}）', flush=True)


if __name__ == '__main__':
    main()
