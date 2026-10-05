#!/usr/bin/env python3
"""
fault_inject.py — 實驗室故障注入（驗證一鍵健檢與機況助手能不能幫線上人員判斷）。**只在實驗室用**。

每個情境：注入 → 等穩定 → 跑 cfaoi_triage.py（存報告）→ **一定還原**（finally）→ 再跑一次確認恢復。
結果寫進 <out>/<情境>.json（症狀、注入方式、健檢報告路徑與結論），給 eval 與案例紀錄用。

  fault_inject.py list
  fault_inject.py run cam_missing [--out DIR]
  fault_inject.py run all

情境（相機以 CCD 編號指定，交換機埠用相機 MAC 即時查，不依賴埠位）
  cam_missing     CCD05 所插的交換機埠 shutdown           → 模擬：CCD05 線斷/沒電
  packet_loss     CCD03 所插的埠最大框長 9416→1536          → 模擬：CCD03 掉封包（交換機 jumbo 沒開）
  param_drift     CCD02 曝光 70→10 µs                       → 模擬：有人調機沒還原（影像偏暗）
  uplink_down     交換機 → Grab 100G 上行 shutdown          → 模擬：全部相機不見（上行線斷）
  switch_reset    CCD05 所插埠所屬 port-group 取消 speed 1000 → 模擬：換交換機/重置後設定沒還原
  ip_down         Spark cfaoi-ip-production 停止            → 模擬：RDMA「機器/程式」問題（線是好的）
  （RDMA「線」問題需 sudo → 另見 rdma_cable_test.sh）
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, 'tools', 'cam_align'))
from switch_console import Switch, find_port  # noqa: E402

SPARK = 'auo001@192.168.3.1'


def baseline():
    for p in ('/srv/cfaoi/60_config/current/grab_triage_baseline.json',
              os.path.expanduser('~/cfaoi_logs/triage/grab_triage_baseline.json')):
        if os.path.exists(p):
            with open(p, encoding='utf-8') as f:
                return json.load(f)
    sys.exit('先在系統正常時跑 cfaoi_triage.py --save-baseline')


def cam_port(ccd):
    import cam_align as ca
    ca.do_discover()                    # 相機閒置不送封包，交換機 MAC 會過期 → 先掃描讓它回應
    mac = baseline()['macs'][ccd]
    with Switch() as sw:
        p = find_port(mac, sw.macs())
    if not p:
        raise RuntimeError(f'{ccd}（{mac}）目前不在交換機 MAC 表')
    return p


def triage(tag, out):
    t0 = time.time()
    p = subprocess.run([sys.executable, os.path.join(HERE, 'cfaoi_triage.py'), '--json', '--out', out],
                       capture_output=True, text=True, timeout=900)
    try:
        j = json.loads(p.stdout)
    except ValueError:
        j = {'error': p.stdout[-500:] + p.stderr[-500:]}
    os.replace(j['report'], os.path.join(out, f'{tag}.md')) if j.get('report') else None
    jr = j.get('report', '').replace('.md', '.json')
    if jr and os.path.exists(jr):
        os.replace(jr, os.path.join(out, f'{tag}.triage.json'))
    return dict(exit=p.returncode, sec=round(time.time() - t0, 1), report=os.path.join(out, f'{tag}.md'),
                problems=[f'{i["level"]} [{i["area"]}] {i["title"]}' for i in j.get('items', []) if i['level'] in ('FAIL', 'WARN')])


def set_expo(ccd, val):
    import cam_align as ca
    ca.do_discover()
    c = next(x for x in ca.CAMS.values() if x.info.get('user_id') == ccd)
    for _ in range(60):
        if c.ready():
            break
        time.sleep(0.2)
    return c.set_feature('expo', val)


def ssh(cmd):
    return subprocess.run(['ssh', '-o', 'BatchMode=yes', SPARK, cmd], capture_output=True, text=True, timeout=60)


# 每個情境：(症狀描述（線上人員看到的）, 注入, 還原, 等待秒數)
def scenarios():
    st = {}

    def sw_do(fn):
        with Switch() as sw:
            return fn(sw)

    st['cam_missing'] = dict(
        symptom='Control 主畫面 CCD05 一直沒有影像，其他相機正常；產線停在等待取像。',
        inject=lambda ctx: (ctx.__setitem__('port', cam_port('CCD05')), sw_do(lambda s: s.shutdown(ctx['port'], True))),
        restore=lambda ctx: sw_do(lambda s: s.shutdown(ctx['port'], False)), settle=6)
    # 5945 不支援入方向限速（qos lr inbound → The operation is not supported，2026-10-05 實測）→ 改用最大框長：
    # 相機送 jumbo 封包，該埠最大框長調成 1536 → 大封包被交換機丟（= 換別牌交換機沒開 jumbo 的典型狀況）
    st['packet_loss'] = dict(
        symptom='CCD03 的影像有時候缺一截、有黑色橫條，結果圖常判 NG，其他相機正常。',
        inject=lambda ctx: (ctx.__setitem__('port', cam_port('CCD03')),
                            sw_do(lambda s: s._sys(f'interface {ctx["port"]}', 'jumboframe enable 1536', 'quit'))),
        restore=lambda ctx: sw_do(lambda s: s._sys(f'interface {ctx["port"]}', 'jumboframe enable 9416', 'quit')), settle=3)
    st['param_drift'] = dict(
        symptom='CCD02 的影像比其他台暗很多，缺陷數也變得很奇怪；昨天有人來調過機。',
        inject=lambda ctx: set_expo('CCD02', 10.0), restore=lambda ctx: set_expo('CCD02', 70.0), settle=1)
    st['uplink_down'] = dict(
        symptom='開機後 Control 顯示所有相機都沒有影像，Grab 燈號是綠的。',
        inject=lambda ctx: (ctx.__setitem__('port', baseline()['uplink']), sw_do(lambda s: s.shutdown(ctx['port'], True))),
        restore=lambda ctx: sw_do(lambda s: s.shutdown(ctx['port'], False)), settle=8)
    st['switch_reset'] = dict(
        symptom='交換機壞掉換了一台新的（或被重置過），接回去後有兩台相機（CCD05、CCD06）一直連不上，線都換過了還是一樣。',
        inject=lambda ctx: (ctx.__setitem__('port', cam_port('CCD05')), sw_do(lambda s: s.speed(ctx['port'], None))),
        restore=lambda ctx: sw_do(lambda s: s.speed(ctx['port'], '1000')), settle=8)
    st['ip_down'] = dict(
        symptom='上位機送料後一直沒有結果回來，Control 的 IP 燈是紅的，Grab 燈是綠的。是不是 Grab 跟 Spark 中間的線壞了？',
        inject=lambda ctx: ssh('systemctl --no-ask-password stop cfaoi-ip-production'),
        restore=lambda ctx: ssh('systemctl --no-ask-password start cfaoi-ip-production'), settle=3)
    return st


def run_one(name, sc, out):
    print(f'═══ {name} ═══ 症狀：{sc["symptom"]}', flush=True)
    ctx, res = {}, {'scenario': name, 'symptom': sc['symptom']}
    try:
        sc['inject'](ctx)
        res['injected'] = {k: v for k, v in ctx.items()}
        time.sleep(sc['settle'])
        res['fault'] = triage(name, out)
        print('  健檢：' + ('；'.join(res['fault']['problems']) or '（沒有異常！）'), flush=True)
    except Exception as e:  # noqa: BLE001
        res['error'] = f'{e.__class__.__name__}: {e}'
        print('  ✗', res['error'], flush=True)
    finally:
        try:
            sc['restore'](ctx)
            res['restored'] = True
        except Exception as e:  # noqa: BLE001
            res['restored'] = False
            res['restore_error'] = str(e)
            print(f'  ⚠⚠ 還原失敗：{e} —— 需手動處理！', flush=True)
    time.sleep(sc['settle'] + 6)
    res['after'] = triage(name + '.after', out)
    print('  還原後：' + ('；'.join(res['after']['problems']) or '正常'), flush=True)
    with open(os.path.join(out, f'{name}.json'), 'w', encoding='utf-8') as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=['list', 'run'])
    ap.add_argument('names', nargs='*')
    ap.add_argument('--out', default=os.path.expanduser('~/cfaoi_logs/fault_exp'))
    a = ap.parse_args()
    st = scenarios()
    if a.action == 'list':
        for k, v in st.items():
            print(f'{k:14} {v["symptom"]}')
        return 0
    os.makedirs(a.out, exist_ok=True)
    names = list(st) if a.names in ([], ['all']) else a.names
    for n in names:
        run_one(n, st[n], a.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
