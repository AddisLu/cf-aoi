#!/usr/bin/env python3
"""
bench_speed — 「DIV 只檢暗 + DIV 投票只檢亮」雙方案的產能評估：DIV 投票的輕量變體
（PitchTime 2→1、多尺度開/關）各自校準亮門檻（平台法）→ 植入缺陷的亮檢出率 + GPU ms/張。
產能：37 CCD × 63 張 = 2,331 張/片（87.5 mm/s、8 µm/列）、掃描 28.6 s / 30 s 節拍、1 台 Spark。
兩段式（--cascade-bright t1）：DIV 亮門檻 t1 粗篩 → 候選小塊跑投票確認（配方 BTH = 投票門檻）。
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

SLICES_PER_PANEL = 37 * 63     # 87.5 mm/s、8 µm/列、玻璃 2500 mm → 每台 63 張（Addis 2026-10-06 確認 8 µm）
TACT_S = 28.6     # 掃描時間（邊拍邊算要跟上的是這個）

EF = ['--edge-fill', '1']
VARIANTS = {   # 極性 → [(名稱, 偵測, 配方選項, IP 額外參數)]
    'bright': [
        ('div（對照）', 'div', {}, EF),
        ('兩段式 t1=1.33', 'div', {}, EF + ['--cascade-bright', '1.33']),
        ('兩段式 t1=1.31', 'div', {}, EF + ['--cascade-bright', '1.31']),
    ],
    'dark': [
        ('div（對照）', 'div', {}, EF),
        ('divvote 1p', 'divvote', {'pitch_time': 1, 'choose': 7, 'multiscale': 0}, EF),
        ('兩段式暗 t1=0.70', 'div', {}, EF + ['--cascade-dark', '0.70']),
        ('兩段式暗 t1=0.72', 'div', {}, EF + ['--cascade-dark', '0.72']),
        ('兩段式暗 t1=0.74', 'div', {}, EF + ['--cascade-dark', '0.74']),
    ],
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--strip', required=True)
    ap.add_argument('--ip', required=True)
    ap.add_argument('--out', default='/tmp/bench_speed')
    ap.add_argument('--pol', default='bright', choices=['bright', 'dark'])
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
    bt = [i for i, t in enumerate(truth) if t['pol'] == a.pol]
    FS = BM.BRIGHT_F if a.pol == 'bright' else BM.DARK_F
    rows = []
    for name, mode, opt, extra in VARIANTS[a.pol]:
        work = os.path.join(a.out, 'run')
        curve = []
        seq = np.arange(1.10, 2.0, 0.01) if a.pol == 'bright' else np.arange(0.95, 0.35, -0.01)
        for b in seq:
            th = {'dark': 0.01, 'bright': round(float(b), 3)} if a.pol == 'bright' else {'dark': round(float(b), 3), 'bright': 9.0}
            d, ms = BM.run(a.ip, chips, ioi, pi, th, a.strip, work, mode, opt, extra)
            curve.append((round(float(b), 3), len(d)))
            if len(d) <= 5:
                break
        tb = round(curve[-1][0] * (1.03 if a.pol == 'bright' else 0.97), 3)
        th = {'dark': 0.01, 'bright': tb} if a.pol == 'bright' else {'dark': tb, 'bright': 9.0}
        _, ms = BM.run(a.ip, chips, ioi, pi, th, a.strip, work, mode, opt, extra)
        ms_slice = ms / len(paths)
        d, _ = BM.run(a.ip, chips, ioi, pi, th, inj_dir, work, mode, opt, extra)
        clean, _ = BM.run(a.ip, chips, ioi, pi, th, a.strip, work, mode, opt, extra)
        hit, fp = BM.match(d, truth, [x for x in clean if x['slice'] in inj_sl])
        by = {f: round(np.mean([hit[i] for i in bt if truth[i]['f'] == f]), 2) for f in FS}
        bs = {z: round(np.mean([hit[i] for i in bt if truth[i]['size'] == z and truth[i]['f'] in FS[1:]]), 2) for z in BM.SIZES}
        rate = float(np.mean([hit[i] for i in bt]))
        panel_s = (ms_slice + 7.4) * SLICES_PER_PANEL / 1000 if mode != 'div' else ms_slice * SLICES_PER_PANEL / 1000
        row = {'name': name, 'pol': a.pol, 'th': tb, 'rate': round(rate, 3), 'by_contrast': by, 'by_size': bs, 'fp_inj': fp,
               'ms_slice': round(ms_slice, 1), 'panel_s_with_div': round(panel_s, 1),
               'tact_use': round(panel_s / TACT_S, 2)}
        rows.append(row)
        json.dump(rows, open(os.path.join(a.out, 'speed.json'), 'w'), ensure_ascii=False, indent=1)
        cs = '/'.join(f'{by[f]:.0%}' for f in FS)
        ss = '/'.join(f'{bs[z]:.0%}' for z in BM.SIZES)
        print(f'{name:18s} {a.pol} 門檻 {tb:.3f}  檢出 {rate:6.1%}  對比 {cs}  大小1/2/3/5/9 {ss}  植入誤判 {fp}  '
              f'GPU {ms_slice:5.1f} ms/張  每片 {panel_s:5.1f} s（掃描 {panel_s / TACT_S:.0%}）', flush=True)


if __name__ == '__main__':
    main()
