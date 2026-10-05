#!/usr/bin/env python3
"""
validate_ip — 自動調參 → 真的寫成 RecipeInfo.xml → 真的用 IP（cfaoi_ip offline-file）檢整條 CCD →
統計「真缺陷抓到幾顆 / 誤判幾顆 / bypass 多少」，並掃描門檻安全邊際，找「不誤判又最敏感」的點。

在 Spark 跑（影像與 GPU 都在那）：
  python3 validate_ip.py --strip ~/cfaoi_reference/T550_IP04 --ip ~/Addis/cf-aoi/ip/build_at/cfaoi_ip \
      --truth 27:5894:725 --out /tmp/at_val
truth 格式：slice:x:y（本地座標，±3 px 視為同一顆）；其餘一律算誤判。
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autotune_engine as E      # noqa: E402
import autotune_strip as S       # noqa: E402
import recipe_writer as W        # noqa: E402


def run_ip(ip, recipe_xml, strip, out):
    os.makedirs(out, exist_ok=True)
    rp = os.path.join(out, 'RecipeInfo.xml')
    open(rp, 'w').write(recipe_xml)
    res = os.path.join(out, 'result')
    subprocess.run(['rm', '-rf', res])
    t = time.time()
    p = subprocess.run([ip, '--mode', 'offline-file', '--input', strip, '--recipe', rp, '--output', res,
                        '--no-overlay', '--max-patches', '0', '--ip-name', 'IPAT'],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f'IP 失敗 rc={p.returncode}\n{p.stdout[-2000:]}\n{p.stderr[-2000:]}')
    return res, time.time() - t, p.stdout


def collect(res):
    out = []
    for f in sorted(glob.glob(res + '/**/*ResultInfo.json', recursive=True)):
        m = re.search(r'Origin(\d+)', f)
        s = int(m.group(1)) if m else -1
        d = json.load(open(f))
        for roi in d.get('RoiInfoList', []):
            for df in roi.get('DefectInfoList', []):
                out.append({'slice': s, 'x': df['GC_X'], 'y': df['GC_Y'], 'type': df.get('Type'),
                            'size': df.get('Size'), 'gl': df.get('GL_Mean')})
    return out


def score(defs, truth, h, chips):
    found, fps = set(), []
    for d in defs:
        hit = [i for i, (s, x, y) in enumerate(truth) if d['slice'] == s and abs(d['x'] - x) <= 3 and abs(d['y'] - y) <= 3]
        if hit:
            found.add(hit[0])
            continue
        Y = d['slice'] * h + d['y']
        d['panel_y'] = Y
        d['edge_dist'] = int(min(min(abs(Y - c['y0']), abs(Y - c['y1']), abs(d['x'] - c['x0']) if c['x0'] > 0 else 1e9,
                                     abs(d['x'] - c['x1']) if c['x1'] < 8160 else 1e9) for c in chips)) if chips else -1
        fps.append(d)
    return len(found), fps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--strip', required=True)
    ap.add_argument('--ip', required=True)
    ap.add_argument('--truth', nargs='*', default=[])
    ap.add_argument('--out', default='/tmp/at_val')
    ap.add_argument('--margins', default='0,0.05,0.10,0.15,0.20,0.30')
    ap.add_argument('--search', default='1,1')
    a = ap.parse_args()
    truth = [tuple(int(v) for v in t.split(':')) for t in a.truth]
    search = tuple(int(v) for v in a.search.split(','))
    paths = sorted(glob.glob(os.path.join(a.strip, '*_Origin*.tif')))
    os.makedirs(a.out, exist_ok=True)

    t0 = time.time()
    r = S.analyze_strip(paths, progress=lambda s: None)
    px, py = r['pitch']
    pi = (int(round(px)), int(round(py)))
    h = r['slice_h']
    print(f'[區域] pitch {px:.2f}×{py:.2f} → {pi}；晶片 {len(r["chips"])}：{[(c["y0"], c["y1"]) for c in r["chips"]]}；'
          f'IOI {len(r["dummy"])}：{[(d["y0"], d["y1"]) for d in r["dummy"]]}（{time.time() - t0:.0f}s）')

    # 門檻底線：每張只取落在晶片內的像素
    t0 = time.time()
    imgs, masks = [], []
    for i, p in enumerate(paths):
        rects = S.rects_for_slice(r['chips'], i, h)
        if not rects:
            continue
        im = E.load(p)
        imgs.append(im)
        masks.append(S.roi_mask(im.shape, rects))
    base = E.estimate_thresholds(imgs, pi[0], pi[1], 'div', masks, margin=0.0)
    del imgs, masks
    print(f'[門檻] 雜訊底線 暗 {base["floor_dark"]} / 亮 {base["floor_bright"]}（{base["n_images"]} 張，{time.time() - t0:.0f}s）')

    rows = []
    cases = [('auto', m) for m in (float(v) for v in a.margins.split(','))] + [('manual', None)]
    for kind, m in cases:
        th = ({'dark': round(base['floor_dark'] * (1 - m), 3), 'bright': round(base['floor_bright'] * (1 + m), 3)}
              if kind == 'auto' else {'dark': 0.60, 'bright': 1.40})
        xml = W.make_recipe(r['chips'], r['dummy'], pi, th, search)
        tag = f'{kind}_{m:.2f}' if m is not None else kind
        res, sec, _ = run_ip(a.ip, xml, a.strip, os.path.join(a.out, tag))
        defs = collect(res)
        tp, fps = score(defs, truth, h, r['chips'])
        rows.append({'case': tag, 'dark': th['dark'], 'bright': th['bright'], 'tp': tp, 'n_truth': len(truth),
                     'fp': len(fps), 'fp_list': fps[:20], 'ip_sec': round(sec, 1)})
        print(f'[IP] {tag:12s} 暗 {th["dark"]:.3f} 亮 {th["bright"]:.3f} → 真缺陷 {tp}/{len(truth)}、誤判 {len(fps)}'
              f'（{sec:.0f}s）' + (f'  例：{[(f["slice"], f["x"], f["y"], f["type"], f["size"], f["edge_dist"]) for f in fps[:5]]}' if fps else ''))

    area = r['width'] * r['height']
    roi = sum((c['x1'] - c['x0']) * (c['y1'] - c['y0']) for c in r['chips'])
    summary = {'pitch': r['pitch'], 'pitch_int': pi, 'search': search, 'chips': r['chips'], 'ioi': r['dummy'],
               'floor': base, 'roi_frac': round(roi / area, 4), 'pattern_frac': r['pattern_frac'],
               'block_thr': r['block_thr'], 'runs': rows}
    json.dump(summary, open(os.path.join(a.out, 'summary.json'), 'w'), ensure_ascii=False, indent=1)
    print(f'[bypass] 檢測區占整條 {roi / area:.2%}（區塊判定的 pattern 區 {r["pattern_frac"]:.2%}）')


if __name__ == '__main__':
    main()
