#!/usr/bin/env python3
"""
write_cases.py — 把一輪故障實驗（fault_inject / *_sudo_test / eval_loop 的輸出）寫成案例庫 + 原始資料入庫。

  write_cases.py --exp ~/cfaoi_logs/fault_exp2 --grades grades.json --round 20261005_round2

grades.json：{ "<情境>": {"cat": "camera|switch|rdma|host|gpu|ip", "title": "...", "cause": "...",
                          "actions": ["..."], "learn": "...", "A": [分, "評語"], "B": [分, "評語"]} }
輸出：docs/troubleshooting/experiments/<round>/（原始資料 + grades.json）、docs/troubleshooting/cases/<日期>_<cat>_<情境>.md
"""
import argparse
import glob
import json
import os
import re
import shutil

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def conclusion(md):
    try:
        t = open(md, encoding='utf-8').read()
    except OSError:
        return '（無）'
    m = re.search(r'## 結論.*?(?=\n## 各項明細)', t, re.S)
    return m.group(0).strip() if m else t[:800]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--exp', required=True)
    ap.add_argument('--grades', required=True)
    ap.add_argument('--round', required=True, help='例 20261005_round2（前 8 碼當案例日期）')
    ap.add_argument('--source', default='實驗室故障注入（tools/triage/fault_inject.py、host_sudo_test.sh）')
    a = ap.parse_args()
    exp = os.path.expanduser(a.exp)
    raw = os.path.join(REPO, 'docs', 'troubleshooting', 'experiments', a.round)
    os.makedirs(raw, exist_ok=True)
    for f in glob.glob(os.path.join(exp, '*')):
        if os.path.isfile(f):
            shutil.copy2(f, raw)
    grades = json.load(open(a.grades, encoding='utf-8'))
    shutil.copy2(a.grades, os.path.join(raw, 'grades.json'))
    rel = os.path.relpath(raw, REPO)
    date = a.round[:8]
    out = []
    for name, g in grades.items():
        sc = json.load(open(os.path.join(exp, f'{name}.json'), encoding='utf-8'))
        try:
            lp = json.load(open(os.path.join(exp, f'{name}.loop.json'), encoding='utf-8'))
        except OSError:
            lp = {}
        fn = os.path.join(REPO, 'docs', 'troubleshooting', 'cases', f'{date}_{g["cat"]}_{name}.md')
        after = '；'.join(sc.get('after', {}).get('problems', [])) or '正常'
        body = [f'# {g["title"]}', '',
                f'- 日期：{date[:4]}-{date[4:6]}-{date[6:]}　來源：{a.source}　機台：user-IMB-M47 + spark-c16f + HPE 5945（借用機）',
                f'- 分類：{g["cat"]}　注入：{json.dumps(sc.get("injected", {}), ensure_ascii=False)}　已還原：{"是" if sc.get("restored") else "否"}（還原後健檢：{after}）',
                '', '## 線上人員看到的（原話）', sc['symptom'], '',
                '## 證據：一鍵健檢結論', conclusion(os.path.join(exp, f'{name}.md')), '',
                f'（完整報告：`{rel}/{name}.md`）', '', '## 根因', g['cause'], '', '## 處置']
        body += [f'{i + 1}. {x}' for i, x in enumerate(g['actions'])]
        body += ['', '## 學到的 / 預防', g['learn'], '', '## 機況助手表現（0–2 分）',
                 f'- 只給症狀（A）：**{g["A"][0]}** — {g["A"][1]}',
                 f'- 症狀 + 健檢報告（B）：**{g["B"][0]}** — {g["B"][1]}', '']
        for c in ('A', 'B'):
            body += [f'<details><summary>助手 {c} 回答原文</summary>', '', lp.get(c, {}).get('answer', '（無）'), '', '</details>', '']
        open(fn, 'w', encoding='utf-8').write('\n'.join(body))
        out.append((os.path.relpath(fn, REPO), g['title'], g['A'][0], g['B'][0]))
    for f, t, sa, sb in out:
        print(f'{f}  A={sa} B={sb}  {t}')
    print(f'A 合計 {sum(x[2] for x in out)}/{2 * len(out)}、B 合計 {sum(x[3] for x in out)}/{2 * len(out)}')


if __name__ == '__main__':
    main()
