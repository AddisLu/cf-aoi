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
                        '--no-overlay', '--max-patches', '0', '--ip-name', 'IPAT'] + IP_EXTRA,
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f'IP 失敗 rc={p.returncode}\n{p.stdout[-2000:]}\n{p.stderr[-2000:]}')
    return res, time.time() - t, p.stdout


IP_EXTRA = []      # --ip-args 傳給 cfaoi_ip 的額外參數（例：--edge-fill 1）


def collect(res):
    out = []
    for f in sorted(glob.glob(res + '/**/*ResultInfo.json', recursive=True)):
        m = re.search(r'Origin(\d+)', f)
        s = int(m.group(1)) if m else -1
        d = json.load(open(f))
        for roi in d.get('RoiInfoList', []):
            for df in roi.get('DefectInfoList', []):
                out.append({'slice': s, 'x': df['GlobalPosX'], 'y': df['GlobalPosY'], 'type': df.get('Type'),
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


def subset_dir(paths, keep, out):
    """只含部分 slice 的目錄（symlink；檔名保留 → sliceIndex 不變，panel 座標照樣對）。"""
    os.makedirs(out, exist_ok=True)
    for f in os.listdir(out):
        os.unlink(os.path.join(out, f))
    for p in paths:
        m = re.search(r'Origin(\d+)', p)
        if m and keep(int(m.group(1))):
            os.symlink(p, os.path.join(out, os.path.basename(p)))
    return out


DM_RE = re.compile(r'\[DeathMargin\] zone \d+ ROI=\((\d+),(\d+)\)\+(\d+)x(\d+) death_margin=\(l:(\d+), r:(\d+), t:(\d+), b:(\d+)\)')


def inspected_area(log):
    """IP log 的 DeathMargin → (zone 面積總和, 扣掉四邊死區後實際檢到的面積)。"""
    roi = eff = 0
    for x, y, w, h, ml, mr, mt, mb in (tuple(map(int, m)) for m in DM_RE.findall(log)):
        roi += w * h
        eff += max(0, w - ml - mr) * max(0, h - mt - mb)
    return roi, eff


def fp_count(a, r, pi, search, th, strip, truth, h, tag, want_log=False):
    xml = W.make_recipe(r['chips'], r['dummy'], pi, th, search)
    res, _, log = run_ip(a.ip, xml, strip, os.path.join(a.out, tag))
    tp, fps = score(collect(res), truth, h, r['chips'])
    return (tp, fps, log) if want_log else (tp, fps)


def bisect(f, lo, hi, n=9):
    """f(x) = 誤判數，x 往 hi 走誤判越少（單調）。回傳 0 誤判的最靠 lo 的 x。"""
    if f(hi) > 0:
        return None
    for _ in range(n):
        mid = (lo + hi) / 2
        if f(mid) == 0:
            hi = mid
        else:
            lo = mid
    return hi


def calibrate_on(a, r, pi, search, base, truth, strip, h, tag):
    """暗：亮門檻關掉（9.0）找最大的 0 誤判 DTH；亮：暗門檻關掉（0.01）找最小的 0 誤判 BTH。"""
    fd = lambda d: len(fp_count(a, r, pi, search, {'dark': round(d, 4), 'bright': 9.0}, strip, truth, h, tag + '_d')[1])
    fb = lambda b: len(fp_count(a, r, pi, search, {'dark': 0.01, 'bright': round(b, 4)}, strip, truth, h, tag + '_b')[1])
    # 暗：x = 1 - DTH（越大越鬆）；亮：x = BTH
    d = bisect(lambda x: fd(1 - x), 1 - base['floor_dark'], 0.7)
    b = bisect(fb, base['floor_bright'], 3.0)
    return (None if d is None else round(1 - d, 4)), (None if b is None else round(b, 4))


def calibrate(a, r, pi, search, base, truth, paths, h):
    t0 = time.time()
    folds = {'全部': lambda i: True, '偶數張': lambda i: i % 2 == 0, '奇數張': lambda i: i % 2 == 1}
    dirs = {k: subset_dir(paths, f, os.path.join(a.out, 'sub_' + str(j))) for j, (k, f) in enumerate(folds.items())}
    cal = {}
    for k in folds:
        cal[k] = calibrate_on(a, r, pi, search, base, truth, dirs[k], h, 'cal_' + k)
        print(f'[校準] {k}：0 誤判的最緊門檻 暗 {cal[k][0]} / 亮 {cal[k][1]}（{time.time() - t0:.0f}s）')
    s = a.safety
    final = lambda c: {'dark': round(c[0] * (1 - s), 3), 'bright': round(c[1] * (1 + s), 3)}
    report = {'pitch_int': pi, 'search': search, 'floor': base, 'chips': r['chips'], 'ioi': r['dummy'],
              'calibrated': cal, 'safety': s, 'checks': []}
    for train, test in (('偶數張', '奇數張'), ('奇數張', '偶數張'), ('全部', '全部')):
        th = final(cal[train])
        tp, fps = fp_count(a, r, pi, search, th, dirs[test], truth, h, f'x_{train}_{test}')
        n_t = sum(1 for (sl, _, _) in truth if folds[test](sl))
        report['checks'].append({'train': train, 'test': test, 'th': th, 'tp': tp, 'n_truth': n_t, 'fp': len(fps),
                                 'fp_list': fps[:10]})
        print(f'[驗證] {train}校準 → {test}實檢：暗 {th["dark"]} 亮 {th["bright"]} → 真缺陷 {tp}/{n_t}、誤判 {len(fps)}'
              + (f'  例：{[(f["slice"], f["x"], f["y"], f["type"], f["size"]) for f in fps[:5]]}' if fps else ''))
    # bypass：整條面積裡，晶片外（間隙/外圍）+ 每張 slice 四邊死區（逐張處理時 ±2 pitch 比不到）
    _, _, log = fp_count(a, r, pi, search, final(cal['全部']), dirs['全部'], truth, h, 'area', want_log=True)
    roi_a, eff_a = inspected_area(log)
    total = r['width'] * r['height']
    glass = r['width'] * (r['height'] - min([d['y0'] for d in r['dummy']] + [c['y0'] for c in r['chips']]))
    report['area'] = {'strip': total, 'glass': glass, 'roi': roi_a, 'inspected': eff_a,
                      'roi_of_glass': round(roi_a / glass, 4), 'inspected_of_glass': round(eff_a / glass, 4),
                      'inspected_of_roi': round(eff_a / roi_a, 4)}
    print(f'[bypass] 玻璃內：檢測區 {roi_a / glass:.2%}；扣每張四邊死區後實際檢到 {eff_a / glass:.2%}'
          f'（死區吃掉檢測區的 {1 - eff_a / roi_a:.2%}）')
    json.dump(report, open(os.path.join(a.out, 'calibration.json'), 'w'), ensure_ascii=False, indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--strip', required=True)
    ap.add_argument('--ip', required=True)
    ap.add_argument('--truth', nargs='*', default=[])
    ap.add_argument('--out', default='/tmp/at_val')
    ap.add_argument('--margins', default='0,0.05,0.10,0.15,0.20,0.30')
    ap.add_argument('--search', default='1,1')
    ap.add_argument('--pitch', default='', help='覆寫整數 pitch，如 26,19（預設 = 自動量測四捨五入）')
    ap.add_argument('--manual', default='0.60,1.40', help='對照組人工門檻 暗,亮')
    ap.add_argument('--calibrate', action='store_true', help='IP 實檢二分搜尋：暗/亮各自找 0 誤判的最緊門檻')
    ap.add_argument('--safety', type=float, default=0.03, help='校準後再留的安全邊際')
    ap.add_argument('--ip-args', default='', help='傳給 cfaoi_ip 的額外參數，如 "--edge-fill 1"')
    a = ap.parse_args()
    truth = [tuple(int(v) for v in t.split(':')) for t in a.truth]
    IP_EXTRA[:] = a.ip_args.split()
    search = tuple(int(v) for v in a.search.split(','))
    paths = sorted(glob.glob(os.path.join(a.strip, '*_Origin*.tif')))
    os.makedirs(a.out, exist_ok=True)

    t0 = time.time()
    r = S.analyze_strip(paths, progress=lambda s: None)
    px, py = r['pitch']
    pi = tuple(int(v) for v in a.pitch.split(',')) if a.pitch else (int(round(px)), int(round(py)))
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

    if a.calibrate:
        calibrate(a, r, pi, search, base, truth, paths, h)
        return

    rows = []
    cases = [('auto', m) for m in (float(v) for v in a.margins.split(',') if v)] + [('manual', None)]
    md, mb = (float(v) for v in a.manual.split(','))
    for kind, m in cases:
        th = ({'dark': round(base['floor_dark'] * (1 - m), 3), 'bright': round(base['floor_bright'] * (1 + m), 3)}
              if kind == 'auto' else {'dark': md, 'bright': mb})
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
