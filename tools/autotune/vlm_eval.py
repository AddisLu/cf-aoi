#!/usr/bin/env python3
"""
vlm_eval — 本地視覺模型（vLLM OpenAI API，離線）能不能直接分辨面內區 / bypass / 缺陷？與引擎對照。

  T1 帶狀分區：T550 IP04 #14+#15（間隙跨兩張）縮 8 倍 → 請模型列出每條帶的 y 範圍與 active/bypass
     → 與人工 1:1 判讀的邊界比（上晶片止 3857、dummy 4437–4996、下晶片起 5572）
  T2 小圖分類（原解析度 256×256）：active / dummy / periphery / outside → 正確率
  T3 缺陷有無（原解析度 96×96）：#27、#28 真缺陷 vs 乾淨 → 正確率
  每題重複 n 次看一致性。
在 Spark：python3 vlm_eval.py --strip ~/cfaoi_reference/T550_IP04 --url http://127.0.0.1:8001
"""
import argparse
import base64
import json
import os
import re
import time
import urllib.request

import cv2
import numpy as np

H = 5000


def b64png(img):
    ok, buf = cv2.imencode('.png', img)
    return base64.b64encode(buf.tobytes()).decode()


def ask(url, model, img, prompt, think=False, max_tokens=4000):
    body = {'model': model, 'temperature': 0.2, 'max_tokens': max_tokens,
            'chat_template_kwargs': {'enable_thinking': think},
            'messages': [{'role': 'user', 'content': [
                {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + b64png(img)}},
                {'type': 'text', 'text': prompt}]}]}
    t = time.time()
    r = json.load(urllib.request.urlopen(urllib.request.Request(
        url + '/v1/chat/completions', json.dumps(body).encode(), {'Content-Type': 'application/json'}), timeout=900))
    return r['choices'][0]['message']['content'] or '', time.time() - t


def jparse(s):
    m = re.search(r'(\[.*\]|\{.*\})', s, re.S)
    try:
        return json.loads(m.group(1)) if m else None
    except Exception:
        return None


def load(strip, n):
    return cv2.imread(os.path.join(strip, 'IP04_Origin%06d.tif' % n), cv2.IMREAD_UNCHANGED)


def tile(strip, Y, x, size):
    """panel 座標 Y（中心）、x（中心）的原解析度小圖（可跨張）。"""
    y0 = Y - size // 2
    s0, s1 = y0 // H, (y0 + size - 1) // H
    im = np.vstack([load(strip, s) for s in range(s0, s1 + 1)])
    a = y0 - s0 * H
    return im[a:a + size, x - size // 2:x + size // 2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--strip', required=True)
    ap.add_argument('--url', default='http://127.0.0.1:8001')
    ap.add_argument('--model', default='nvidia/Qwen3.6-35B-A3B-NVFP4')
    ap.add_argument('--n', type=int, default=3)
    ap.add_argument('--out', default='/tmp/vlm_eval.json')
    a = ap.parse_args()
    out = {}

    # T1
    st = np.vstack([load(a.strip, 14), load(a.strip, 15)])
    small = cv2.resize(st, (1020, 1250), interpolation=cv2.INTER_AREA)
    truth = [3857 / 8, 4437 / 8, 4996 / 8, 5572 / 8]
    p1 = ('Grayscale image (1020 wide x 1250 tall) from a color-filter glass AOI line-scan camera. It contains display '
          'active area (fine, dense, regular repeating pixel grid) and non-active areas between panels (bright bands, thin '
          'lines, pads, and a narrower band with a sparser dummy pixel pattern). List every horizontal band from top to '
          'bottom with y0 and y1 in pixels of THIS image and type "active", "dummy" or "bypass". '
          'Answer ONLY JSON: [{"y0":int,"y1":int,"type":"active|dummy|bypass"}]')
    t1 = []
    for think in (False, True):
        for i in range(a.n):
            txt, sec = ask(a.url, a.model, small, p1, think, 8000 if think else 3000)
            bands = jparse(txt) or []
            act = [b for b in bands if isinstance(b, dict) and b.get('type') == 'active']
            err = None
            if len(act) >= 2:
                got = [act[0]['y1'], None, None, act[-1]['y0']]
                dm = [b for b in bands if isinstance(b, dict) and b.get('type') == 'dummy']
                if dm:
                    got[1], got[2] = dm[0]['y0'], dm[0]['y1']
                err = [None if g is None else round(abs(g - t) * 8) for g, t in zip(got, truth)]
            t1.append({'think': think, 'sec': round(sec), 'bands': bands, 'err_px_fullres': err})
            print(f'T1 think={think} #{i}: {len(bands)} 帶, 原解析度誤差(px) {err}  ({sec:.0f}s)', flush=True)
    out['T1'] = t1

    # T2：小圖分類（panel 座標中心）
    cases = [('active', 20000, 4000), ('active', 100000, 2000), ('active', 60000, 7000), ('active', 3500, 4000),
             ('dummy', 74700, 3000), ('dummy', 74700, 6000),
             ('periphery', 74000, 4000), ('periphery', 75300, 5000), ('periphery', 2150, 4000), ('periphery', 1800, 6000),
             ('outside', 600, 4000), ('outside', 1200, 2000)]
    p2 = ('Native-resolution 256x256 crop from a color-filter glass AOI image. Classify it as exactly one of: '
          '"active" (dense regular display pixel grid), "dummy" (sparser/weaker regular pattern band between panels), '
          '"periphery" (non-pattern border: flat bright band, lines, pads, marks), "outside" (no glass, black). '
          'Answer ONLY JSON: {"type": "..."}')
    t2 = []
    for lab, Y, x in cases:
        im = tile(a.strip, Y, x, 256)
        votes = []
        for i in range(a.n):
            txt, sec = ask(a.url, a.model, im, p2)
            j = jparse(txt) or {}
            votes.append(j.get('type') if isinstance(j, dict) else None)
        t2.append({'truth': lab, 'Y': Y, 'x': x, 'votes': votes})
        print(f'T2 {lab:9s} Y={Y:6d} → {votes}', flush=True)
    out['T2'] = t2
    out['T2_acc'] = round(sum(v == c['truth'] for c in t2 for v in c['votes']) / (len(t2) * a.n), 3)

    # T3：缺陷有無
    dcases = [(True, 27 * H + 725, 5894), (True, 28 * H + 2372, 1852),
              (False, 27 * H + 2000, 3000), (False, 10 * H + 2500, 4000), (False, 20 * H + 1000, 6000),
              (False, 28 * H + 2372, 1852 + 26 * 3)]
    p3 = ('Native-resolution 96x96 crop of a color-filter display pixel grid (regular repeating pattern). Is there a '
          'defect (a spot, particle, or pixel that breaks the repeating pattern)? Answer ONLY JSON: '
          '{"defect": true|false, "x": int, "y": int}')
    t3 = []
    for lab, Y, x in dcases:
        im = tile(a.strip, Y, x, 96)
        votes = []
        for i in range(a.n):
            txt, _ = ask(a.url, a.model, im, p3)
            j = jparse(txt) or {}
            votes.append(j.get('defect') if isinstance(j, dict) else None)
        t3.append({'truth': lab, 'Y': Y, 'x': x, 'votes': votes})
        print(f'T3 defect={lab} Y={Y} x={x} → {votes}', flush=True)
    out['T3'] = t3
    out['T3_acc'] = round(sum(v == c['truth'] for c in t3 for v in c['votes']) / (len(t3) * a.n), 3)
    json.dump(out, open(a.out, 'w'), ensure_ascii=False, indent=1)
    print(f'T2 小圖分類正確率 {out["T2_acc"]:.0%}；T3 缺陷有無正確率 {out["T3_acc"]:.0%}')


if __name__ == '__main__':
    main()
