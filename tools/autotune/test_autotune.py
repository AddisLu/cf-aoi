#!/usr/bin/env python3
"""離線測試自動調參引擎（不需設備）。

1. 真實影像：舊機台 IP04 參考圖（整張 pattern，人工調定 pitch 26×19、門檻 0.60/1.40，第 27 張 (5894,725) 一顆暗缺陷）
   → pitch X≈25.8（Y 實測 ≈18.3，與 ini 的 19 不同，見規格）、DIV 門檻與人工值相近、已知缺陷仍低於暗門檻
2. 合成整片玻璃（真實 pattern 拼成 3 欄 × 2 列 = 6 up，含玻璃外、晶片間隙、上下邊界），切成 6 條 CCD
   → 每條找出 2 列晶片、上下邊界位置正確、跨 CCD 合成「欄 × 列 = 6」與使用者輸入一致；輸入 4 up → 判不一致
3. SUB 模式門檻（灰階差）方向正確；放寬：暗門檻往下、亮門檻往上
跑法：python3 tools/autotune/test_autotune.py   預期「全數通過」
（參考圖不在時第 1 部分略過）
"""
import glob
import os
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import autotune_engine as E  # noqa: E402
import cv2  # noqa: E402

FAIL = []


def check(name, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + str(detail)) if detail and not cond else ''}")
    if not cond:
        FAIL.append(name)


REF = next((d for d in glob.glob('/srv/cfaoi/50_raw/reference/*IP04*') + glob.glob(os.path.expanduser('~/cfaoi_reference/*IP04*'))), None)

print('1. 真實影像（IP04 參考圖）')
if REF:
    im1 = E.load(os.path.join(REF, 'IP04_Origin000001.tif'))
    im27 = E.load(os.path.join(REF, 'IP04_Origin000027.tif'))
    p = E.estimate_pitch(im1)
    check('pitch X ≈ 25.8（人工值 26）', abs(p['x'] - 25.84) < 0.3, p)
    check('pitch Y ≈ 18.3（基本週期，不是子畫素倍頻 6）', 17.5 < p['y'] < 19.2, p)
    th = E.estimate_thresholds([im1, im27], 26, 18, 'div')
    check('DIV 暗門檻 ≈ 人工 0.60（±0.05）', abs(th['dark'] - 0.60) < 0.05, th)
    check('DIV 亮門檻 ≈ 人工 1.40（±0.08）', abs(th['bright'] - 1.40) < 0.08, th)
    r = E.neighbor_stat(im27, 26, 18, 'div')
    m = 2 * 26
    check('已知暗缺陷 (5895,725) 的比值低於自動暗門檻', r[725 - m - 3:725 - m + 4, 5895 - m - 3:5895 - m + 4].min() < th['dark'], th)
    reg = E.analyze_regions(im1, p['x'], p['y'])
    check('整張 pattern → 1 個檢測區（沒有誤切 bypass）', len(reg['zones']) == 1 and reg['pattern_frac'] > 0.95, reg['pattern_frac'])
else:
    print('  （找不到參考圖，略過）')

print('2. 合成整片玻璃（6 up = 3 欄 × 2 列，切 6 條 CCD）')
rng = np.random.default_rng(3)
pat = E.load(os.path.join(REF, 'IP04_Origin000001.tif'))[:2400, :2400] if REF else \
    (np.add.outer(np.sin(np.arange(2400) / 18 * 2 * np.pi), np.sin(np.arange(2400) / 26 * 2 * np.pi)) * 30 + 90).astype(np.uint8)
H, W, NC = 5000, 1600, 6                                  # 每條 CCD 1600 寬（縮小以加速）
sheet = np.full((H, W * NC), 20, np.uint8)               # 玻璃外
top, bot = 400, 4600
sheet[top:bot] = 70                                       # 玻璃（無 pattern）
sheet[top:bot] = np.clip(sheet[top:bot] + rng.normal(0, 1.5, (bot - top, W * NC)), 0, 255).astype(np.uint8)
chips = []
gx, gy = 200, 250
cw = (W * NC - gx * 4) // 3
ch = (bot - top - gy * 3) // 2
for r in range(2):
    for c in range(3):
        x, y = gx + c * (cw + gx), top + gy + r * (ch + gy)
        t = np.tile(pat, (ch // 2400 + 1, cw // 2400 + 1))[:ch, :cw]
        sheet[y:y + ch, x:x + cw] = t
        chips.append((x, y))
tmp = tempfile.mkdtemp(prefix='autotune_')
for i in range(NC):
    d = os.path.join(tmp, f'IP{i + 1:02d}')
    os.makedirs(d)
    cv2.imwrite(os.path.join(d, f'IP{i + 1:02d}_Origin000001.tif'), sheet[:, i * W:(i + 1) * W])
res = E.run(tmp, 6, 'div', progress=lambda s: None)
up = res['up_check']
check('跨 CCD：晶片 2 列 × 3 欄 = 6 up（與輸入一致）', up['rows'] == 2 and up['cols'] == 3 and up['ok'], up)
c1 = res['ccd'][1]
check('CCD01 偵測到 2 個晶片區塊', len(c1['zones']) == 2, c1['zones'])
z = c1['zones'][0]
check('晶片上緣位置正確（±60 px）', abs(z['StartY'] - (top + gy)) < 60, z)
gt, gb = c1['glass']
check('玻璃上下邊界正確（±60 px）', gt is not None and abs(gt - top) < 60 and abs(gb - bot) < 60, c1['glass'])
gap_ccd = [k for k, v in res['ccd'].items() if not v.get('zones')]
check('全在晶片間隙/空白的 CCD 不給檢測區（bypass）', all(len(res['ccd'][k]['zones']) in (0, 2) for k in res['ccd']), gap_ccd)
res4 = E.run(tmp, 4, 'div', progress=lambda s: None)
check('輸入 4 up 但偵測 6 → 判不一致（提示使用者）', res4['up_check']['ok'] is False, res4['up_check'])

print('3. SUB 模式與放寬')
base = E.load(os.path.join(REF, 'IP04_Origin000001.tif')) if REF else sheet[:, :W]
ts = E.estimate_thresholds([base], 26, 18, 'sub')
check('SUB 暗門檻為負、亮門檻為正（灰階差）', ts['dark'] < 0 < ts['bright'], ts)
td = E.estimate_thresholds([base], 26, 18, 'div')
lo = E.loosen(td, 0.2)
check('放寬 20%：DIV 暗門檻變低、亮門檻變高', lo['dark'] < td['dark'] and lo['bright'] > td['bright'], (td, lo))
lo_s = E.loosen(ts, 0.2)
check('放寬 20%：SUB 暗門檻更負、亮門檻更大', lo_s['dark'] < ts['dark'] and lo_s['bright'] > ts['bright'], (ts, lo_s))

print('4. 真實晶片間隙（T550 IP04 第 14/15 張，間隙跨兩張；有 dummy 週期帶、pad、亮帶、暗線）')
GAP = next((d for pat in ('/srv/cfaoi/50_raw/reference/T550*/IP04', '~/cfaoi_reference/T550*/IP04', '~/cfaoi_reference/T550*_IP04')
            for d in sorted(glob.glob(os.path.expanduser(pat))) if os.path.exists(os.path.join(d, 'IP04_Origin000015.tif'))), None)
if GAP:
    st = np.vstack([E.load(os.path.join(GAP, 'IP04_Origin%06d.tif' % n)) for n in (14, 15)])
    rg = E.analyze_regions(st, 25.86, 18.5, min_chip_frac=0.03)
    # 人工 1:1 判讀：上晶片 pattern 止 3857、dummy 帶 4437–4996、下晶片起 5572（接圖後座標，0–10000）
    truth = [3857, 4437, 4996, 5572]
    got = [rg['rows'][0][1], rg['rows'][1][0], rg['rows'][1][1], rg['rows'][2][0]] if len(rg['rows']) == 3 else []
    check('間隙內分出 3 段 pattern（上晶片 / dummy 帶 / 下晶片）', len(rg['rows']) == 3, rg['rows'])
    check('四條邊界都在 1 個 pitch 內（≤ 20 px；粗分段原本差 ~50–65 px）',
          len(got) == 4 and all(abs(a - b) <= 20 for a, b in zip(got, truth)), list(zip(got, truth)))
else:
    print('  SKIP  找不到 T550 IP04 第 14/15 張')

import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)
print('全數通過' if not FAIL else f'失敗 {len(FAIL)} 項：{FAIL}')
sys.exit(1 if FAIL else 0)
