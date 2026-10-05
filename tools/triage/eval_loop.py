#!/usr/bin/env python3
"""
eval_loop.py — 用故障注入實驗的結果考機況助手（LoopEngineering + 本地大模型 + 知識庫）。

對每個情境問兩種條件（同一個症狀描述）：
  A 只給線上人員看到的症狀（知識庫檢索開著）—— 模擬「線上人員直接問」
  B 症狀 + 一鍵健檢報告（助手讀得到機台資料夾的最新健檢）—— 模擬「先按健檢再問」
另有不需注入的知識題（交換機更換設定、GPU 是否壞、Xid）。

回答原文存 <exp>/<情境>.loop.json，給人工評分與寫成案例（docs/troubleshooting/cases/）。

  eval_loop.py [--exp ~/cfaoi_logs/fault_exp] [--only A|B] [情境...]
需要：機況助手已開（大模型 ready）；Loop 經 Spark 節點代理轉送口 http://192.168.3.1:4711。
"""
import argparse
import glob
import json
import os
import socket
import sys
import time
import urllib.request

AGENT = ('192.168.3.1', 8300)
SYSTEM_HINT = ('你是 CF-AOI 機台的機況助手。回答對象是不懂硬體的線上人員：'
               '先用一句話說結論（哪裡壞、是哪一顆 CCD/哪條線/哪台機器），再列可能原因與「現場照做」的步驟（誰可以做），'
               '最後說怎麼確認修好。相機一律用 CCD 編號稱呼（線材上有 CCD 標示，交換機埠位會變）。不確定就說不確定，不要編造。')
KNOWLEDGE_QS = {
    'q_switch_replace': ('交換機壞掉，廠商換了一台新的 HPE 5945 過來，接回去之後要怎麼設定才能讓相機正常？', None),
    'q_gpu_ok': ('今天缺陷數忽然變很多，是不是 Spark 的 GPU 壞了？要怎麼確認？', 'gpu_deep'),
    'q_xid': ('我看到 Spark 的系統 log 裡最近有一百多筆「NVRM: Xid」錯誤，GPU 是不是壞了？需要送修嗎？', 'gpu_deep'),
}


def loop_url():
    for _ in range(12):                         # Loop 忙（收錄中）時狀態可能暫時讀不到 → 重試 1 分鐘
        with socket.create_connection(AGENT, timeout=10) as s:
            s.sendall(b'{"cmd":"STATUS","seq":1}\n')
            buf = b''
            while not buf.endswith(b'\n'):
                buf += s.recv(65536)
        d = json.loads(buf)['data']['loop']
        if d.get('model', {}).get('status') == 'ready':
            break
        time.sleep(5)
    else:
        sys.exit(f'大模型未就緒（{d.get("model")}）→ 先開機況助手')
    url = d['url']
    base, _, tok = url.partition('/?token=')
    return base, tok


def ask(base, tok, question, max_tokens=2500):
    body = json.dumps({'messages': [{'role': 'system', 'content': SYSTEM_HINT}, {'role': 'user', 'content': question}],
                       'knowledge': True, 'max_tokens': max_tokens}).encode()
    req = urllib.request.Request(base + '/api/chat', data=body, method='POST',
                                 headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {tok}'})
    t0 = time.time()
    content, reasoning, sources = [], [], []
    with urllib.request.urlopen(req, timeout=900) as r:
        for raw in r:
            line = raw.decode('utf-8', 'replace').strip()
            if not line.startswith('data:'):
                continue
            data = line[5:].strip()
            if data == '[DONE]':
                break
            try:
                j = json.loads(data)
            except ValueError:
                continue
            if 'loop_knowledge' in j:
                sources = [s.get('label') or s.get('path') or str(s) for s in j['loop_knowledge'].get('sources', [])]
                continue
            for ch in j.get('choices', []):
                dlt = ch.get('delta') or {}
                if dlt.get('content'):
                    content.append(dlt['content'])
                if dlt.get('reasoning') or dlt.get('reasoning_content'):
                    reasoning.append(dlt.get('reasoning') or dlt.get('reasoning_content'))
    return {'answer': ''.join(content).strip(), 'reasoning_chars': len(''.join(reasoning)),
            'sources': sources[:10], 'sec': round(time.time() - t0, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('names', nargs='*')
    ap.add_argument('--exp', default=os.path.expanduser('~/cfaoi_logs/fault_exp'))
    ap.add_argument('--only', choices=['A', 'B'])
    ap.add_argument('--tag', default='', help='輸出檔名後綴（例 _v2 = 補強後重考）')
    a = ap.parse_args()
    base, tok = loop_url()
    items = []
    for f in sorted(glob.glob(os.path.join(a.exp, '*.json'))):
        if f.endswith(('.triage.json', '.loop.json')) or '.after' in f:
            continue
        sc = json.load(open(f, encoding='utf-8'))
        if 'scenario' not in sc:
            continue
        rep = sc.get('fault', {}).get('report')
        items.append((sc['scenario'], sc['symptom'], rep if rep and os.path.exists(rep) else None))
    for k, (q, rep_name) in KNOWLEDGE_QS.items():
        rep = os.path.join(a.exp, f'{rep_name}.md') if rep_name else None
        items.append((k, q, rep if rep and os.path.exists(rep) else None))
    for name, symptom, rep in items:
        if a.names and name not in a.names:
            continue
        out = {'scenario': name, 'symptom': symptom}
        for cond in ('A', 'B'):
            if a.only and cond != a.only:
                continue
            if cond == 'B' and not rep:
                continue
            q = symptom if cond == 'A' else (symptom + '\n\n以下是剛剛按「一鍵健檢」得到的報告：\n\n' + open(rep, encoding='utf-8').read())
            print(f'── {name} [{cond}] …', flush=True)
            try:
                out[cond] = ask(base, tok, q)
            except Exception as e:  # noqa: BLE001
                out[cond] = {'error': str(e)}
            print('   ' + (out[cond].get('answer') or out[cond].get('error', ''))[:300].replace('\n', ' '), flush=True)
        with open(os.path.join(a.exp, f'{name}{a.tag}.loop.json'), 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == '__main__':
    sys.exit(main())
