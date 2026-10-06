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


DARK_ABS = 15        # 玻璃亮度中位數低於此值 = 偏暗，不調參（T550 正常 CCD 55–75、IP06 9.1、IP08 24.6）
DARK_REL = 0.5       # 低於全片中位數 × 此值 = 偏暗（相對其他 CCD）


def count(ip, r, pi, search, th, strip, out, extra):
    V.IP_EXTRA[:] = extra
    xml = W.make_recipe(r['chips'], r['dummy'], pi, th, search)
    res, _, log = V.run_ip(ip, xml, strip, out)
    return V.collect(res), log


def per_chip(defs, chips, h):
    n = [0] * len(chips)
    for d in defs:
        Y = d['slice'] * h + d['y']
        for i, c in enumerate(chips):
            if c['y0'] <= Y < c['y1'] and c['x0'] <= d['x'] < c['x1']:
                n[i] += 1
                break
    return n


def budgets(chips, p):
    """雜訊配額：整台 CCD 共 p 顆，依面積分給各段（舊版每段 p 顆 → 分段後一台最多 10×p 顆雜訊）。"""
    a = [(c['x1'] - c['x0']) * (c['y1'] - c['y0']) for c in chips]
    tot = float(sum(a)) or 1.0
    return [max(1, int(round(p * x / tot))) for x in a]


FLAT_MAX = 8        # 檢出數 ≤ 此值且連續 3 個門檻不變 = 平台（剩下的是真缺陷，不再往鬆壓）


def sweep(ip, r, pi, search, strip, out, extra, polarity, start, stop, step, p):
    """每段的檢出數曲線：[(門檻, [n...])]。所有段都「≤ 配額」或「≤ FLAT_MAX 且已連續 3 點不變」就停。
    暗：亮門檻關（9.0）、亮：暗門檻關（0.01）。"""
    curve, h = [], r['slice_h']
    bud = budgets(r['chips'], p)
    t = start
    while (t >= stop) if polarity == 'dark' else (t <= stop):
        th = {'dark': round(t, 4), 'bright': 9.0} if polarity == 'dark' else {'dark': 0.01, 'bright': round(t, 4)}
        d, _ = count(ip, r, pi, search, th, strip, out, extra)
        curve.append((round(t, 4), per_chip(d, r['chips'], h)))
        if all(plateau(curve, i, bud[i], final=False)[1] for i in range(len(r['chips']))):
            break
        t = t - step if polarity == 'dark' else t + step
    return curve


def plateau(curve, i, p, final=True):
    """第 i 段的門檻（由敏感往鬆）：第一個「檢出 ≤ p」或「檢出 ≤ FLAT_MAX 且和後兩個門檻一樣」的點。
    舊版只看 ≤ p：真缺陷也佔名額 → 配額小的段門檻被推鬆到連真缺陷都消失（實測 8 台真缺陷 9/10 → 4/10）。
    回傳 (門檻, 是否壓住)；final=False 時只回報「現在能不能停」。"""
    n = [c[1][i] for c in curve]
    for k, v in enumerate(n):
        if v <= p:
            return curve[k][0], True
        if v <= FLAT_MAX and k + 2 < len(n) and n[k + 1] == v and n[k + 2] == v:
            return curve[k][0], True
    return curve[-1][0], False


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
    res['glass_brightness'] = r.get('glass_brightness')
    if r.get('pitch_first'):
        res['pitch_first'] = r['pitch_first']
    if (r.get('glass_brightness') or 0) < DARK_ABS:   # 絕對偏暗才不調參；相對偏暗（比其他 CCD 暗一半）照調、告警
        # 偏暗（光源/相機/曝光異常）：不調參——門檻會被放到很鬆把問題蓋掉（實測 T550 IP06 → 0.47/1.59）
        res['warning'] = f'影像偏暗（玻璃亮度中位數 {r.get("glass_brightness")} < {DARK_ABS}）→ 先查光源/相機/曝光，不自動調參'
        return res
    if not r['chips'] or base is None:
        res['error'] = '找不到晶片（沒有 pattern？）'
        return res
    work = os.path.join(out, 'run')
    # 兩段式（--cascade-*）時第二段投票門檻可以比 DIV 雜訊底線鬆很多（實測 IP04 暗 0.834、亮 1.174）→ 從更鬆的地方開始掃
    casc = any(a.startswith('--cascade') for a in extra)
    d0 = 0.95 if casc else round(base['floor_dark'], 2)
    b0 = 1.05 if casc else round(base['floor_bright'], 2)
    cd = sweep(ip, r, pi, search, ccd_dir, work, extra, 'dark', d0, 0.40, 0.01, p_dark)
    cb = sweep(ip, r, pi, search, ccd_dir, work, extra, 'bright', b0, 2.5, 0.01, p_bright)
    # 每顆晶片各自的門檻：一塊區域出問題（外圍被當晶片、髒污、真缺陷群）不會把整片拖鬆
    ths, plats, flags = [], [], []
    bd, bb = budgets(r['chips'], p_dark), budgets(r['chips'], p_bright)
    for i in range(len(r['chips'])):
        (td, okd), (tb, okb) = plateau(cd, i, bd[i]), plateau(cb, i, bb[i])
        plats.append([td, tb])
        ths.append({'dark': round(td * (1 - safety), 3), 'bright': round(tb * (1 + safety), 3)})
        flags.append([] if okd and okb else ['雜訊壓不住'])
    md, mb = float(np.median([t[0] for t in plats])), float(np.median([t[1] for t in plats]))
    for i, (td, tb) in enumerate(plats):
        if td < md - 0.08 or tb > mb + 0.12:
            flags[i].append(f'門檻明顯比其他晶片鬆（{td}/{tb} vs 中位數 {md:.2f}/{mb:.2f}）→ 檢查是否含外圍/髒污')
    th = ths
    defs, log = count(ip, r, pi, search, th, ccd_dir, work, extra)
    roi_a, eff_a = V.inspected_area(log)
    glass = r['width'] * (r['height'] - min([d['y0'] for d in r['dummy']] + [c['y0'] for c in r['chips']]))
    open(os.path.join(out, 'RecipeInfo.xml'), 'w').write(W.make_recipe(r['chips'], r['dummy'], pi, th, search))
    for c, f in zip(r['chips'], flags):
        c['flags'] = f
    crops(paths, defs, os.path.join(out, 'candidates'), pi[0])
    res.update({'floor': base, 'curve_dark': cd, 'curve_bright': cb, 'plateau': plats, 'th': th, 'chips': r['chips'],
                'candidates': defs, 'inspected_of_glass': round(eff_a / glass, 4) if glass else None,
                'roi_of_glass': round(roi_a / glass, 4) if glass else None, 'sec': round(time.time() - t0)})
    json.dump(res, open(os.path.join(out, 'result.json'), 'w'), ensure_ascii=False, indent=1)
    return res


def score_truth(res, truth):
    """候選 vs 真值（±4 px）：抓到的真缺陷、漏掉的真缺陷、又報出的已知誤判、新出現（未標）的候選。"""
    tr = [t for t in truth if t['ccd'] == res['ccd']]
    near = lambda a, b: a['slice'] == b['slice'] and abs(a['x'] - b['x']) <= 4 and abs(a['y'] - b['y']) <= 4
    c = res.get('candidates') or []
    hit = [t for t in tr if t['label'] and any(near(t, d) for d in c)]
    miss = [t for t in tr if t['label'] and not any(near(t, d) for d in c)]
    fp = [d for d in c if any(near(t, d) for t in tr if not t['label'])]
    new = [d for d in c if not any(near(t, d) for t in tr)]
    return {'tp': len(hit), 'n_true': sum(t['label'] for t in tr), 'miss': miss, 'known_fp': fp, 'new': new}


def fmt_th(th):
    return '、'.join(f'{t["dark"]:.3f}/{t["bright"]:.3f}' for t in (th if isinstance(th, list) else [th]))


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
    ap.add_argument('--truth', default='', help='真值檔（tools/autotune/truth/*.json）→ 每台 TP/漏/已知誤判/新候選')
    a = ap.parse_args()
    dirs = sorted(d for d in glob.glob(os.path.join(a.glass, 'IP*')) if os.path.isdir(d))
    if a.ccds:
        dirs = [d for d in dirs if os.path.basename(d) in a.ccds.split(',')]
    jobs = [(d, a.ip, a.out, a.ip_args.split(), a.plateau_dark, a.plateau_bright, a.safety, (1, 1)) for d in dirs]
    with ProcessPoolExecutor(a.jobs) as ex:
        results = list(ex.map(one_ccd, jobs))
    bs = [r['glass_brightness'] for r in results if r.get('glass_brightness')]
    med = float(np.median(bs)) if bs else 0
    for r in results:
        if r.get('glass_brightness') and r['glass_brightness'] < DARK_REL * med and 'warning' not in r:
            r['warning'] = f'比其他 CCD 暗（{r["glass_brightness"]} < 全片中位數 {med:.0f} × {DARK_REL}）→ 查光源/相機'
    print(f'{"CCD":5s} {"pitch":>13s} {"晶片":>4s} {"IOI":>3s} {"底線 暗/亮":>13s} {"門檻 暗/亮":>13s} {"候選":>4s} {"實檢":>7s}')
    for r in results:
        if 'error' in r or ('warning' in r and 'th' not in r):
            print(f'{r["ccd"]:5s} 亮度 {r.get("glass_brightness")}  {r.get("error") or r.get("warning")}')
            continue
        print(f'{r["ccd"]:5s} {r["pitch"][0]:6.2f}×{r["pitch"][1]:5.2f} {len(r["chips"]):4d} {len(r["ioi"]):3d} '
              f'{r["floor"]["floor_dark"]:.3f}/{r["floor"]["floor_bright"]:.3f} '
              f'{fmt_th(r["th"])} '
              f'{len(r["candidates"]):4d} {r["inspected_of_glass"]:7.2%}  亮度 {r.get("glass_brightness")}  ({r["sec"]}s)'
              + (f'  ⚠ {r["warning"]}' if r.get('warning') else ''))
    if a.truth:
        truth = json.load(open(a.truth))['defects']
        tot = {'tp': 0, 'n_true': 0, 'fp': 0, 'new': 0}
        print('真值對照（±4 px）：')
        for r in results:
            if 'candidates' not in r:
                continue
            sc = score_truth(r, truth)
            r['truth'] = sc
            tot['tp'] += sc['tp']; tot['n_true'] += sc['n_true']; tot['fp'] += len(sc['known_fp']); tot['new'] += len(sc['new'])
            print(f'  {r["ccd"]}: 真缺陷 {sc["tp"]}/{sc["n_true"]}  已知誤判又報 {len(sc["known_fp"])}  新候選 {len(sc["new"])}'
                  + (f'  漏 {[(t["slice"], t["x"], t["y"]) for t in sc["miss"]]}' if sc['miss'] else '')
                  + (f'  新 {[(d["slice"], d["x"], d["y"], d["type"][5:], d["size"]) for d in sc["new"]][:6]}' if sc['new'] else ''))
        print(f'  合計：真缺陷 {tot["tp"]}/{tot["n_true"]}、已知誤判又報 {tot["fp"]}、新候選 {tot["new"]}')
    json.dump(results, open(os.path.join(a.out, 'glass_summary.json'), 'w'), ensure_ascii=False, indent=1)


def resheet(out_root, glass_dir=None):
    """由既有 result.json 重做候選小圖（不重跑 IP）。"""
    for f in sorted(glob.glob(os.path.join(out_root, 'IP*', 'result.json'))):
        r = json.load(open(f))
        if r.get('candidates') is None:
            continue
        g = r.get('glass_dir') or glass_dir
        paths = sorted(glob.glob(os.path.join(g, r['ccd'], '*_Origin*.tif'))) if g else None
        if paths:
            crops(paths, r['candidates'], os.path.join(os.path.dirname(f), 'candidates'), r['pitch_int'][0])


if __name__ == '__main__':
    if len(sys.argv) >= 3 and sys.argv[1] == '--resheet':
        resheet(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    else:
        main()
