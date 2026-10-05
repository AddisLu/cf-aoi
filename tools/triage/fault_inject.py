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
  cam_ip_changed  CCD04 ForceIP → 192.168.6.4（暫時）           → 模擬：相機 IP 被改到別網段
  dup_name        CCD06 改名成 CCD03                         → 模擬：換相機名稱寫錯（撞名）
  cam_busy        拿走 CCD02 控制權並持續心跳                → 模擬：pylon Viewer/相機工具開著沒關
  line_rate       CCD01 行速率 12000 → 11001 Hz              → 模擬：UserSet 鎖住舊行速率（歷史事故）
  grab_down       停止 cfaoi-grab                             → 模擬：Grab 程式當掉
  ip_offline_mode Spark 切到 cfaoi-ip-offline                 → 模擬：調參後沒切回生產
  ini_changed     Spark default_zone.ini DTH 0.60→0.30（--gpu）→ 模擬：有人改了 IP 預設參數
  （RDMA「線」、MTU、轉送需 sudo → 另見 rdma_cable_test.sh、host_sudo_test.sh）
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


def triage(tag, out, extra=()):
    t0 = time.time()
    p = subprocess.run([sys.executable, os.path.join(HERE, 'cfaoi_triage.py'), '--json', '--out', out, *extra],
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


def cam_dev(ccd=None, mac=None):
    import cam_align as ca
    ca.do_discover()
    for d in ca.INVENTORY.values():
        if (ccd and d.get('user_id') == ccd) or (mac and d['mac'].lower() == mac.lower()):
            return d
    raise RuntimeError(f'掃描不到 {ccd or mac}')


def gvcp(d):
    from gvcp_setip import Gvcp
    return Gvcp(d['iface'], d['srcip'])


def force_ip(ctx, ccd, ip):
    d = cam_dev(ccd, ctx.get('mac'))
    ctx['mac'] = d['mac']
    g = gvcp(d)
    try:
        return g.force_ip(d['mac'], ip, '255.255.255.0', '0.0.0.0')
    finally:
        g.s.close()


def rename(ctx, mac_or_ccd, new):
    import provision
    from gvcp_setip import REG_CCP
    d = cam_dev(ccd=None if ':' in mac_or_ccd else mac_or_ccd, mac=mac_or_ccd if ':' in mac_or_ccd else None)
    ctx['mac'] = d['mac']
    g = gvcp(d)
    try:
        g.write_reg(d['ip'], [(REG_CCP, 2)])
        for a, v in provision.pack_name(new):
            g.write_reg(d['ip'], [(a, v)])
    finally:
        g.write_reg(d['ip'], [(REG_CCP, 0)])
        g.s.close()


def hold_ccp(ctx, ccd):
    """模擬「pylon Viewer 開著沒關」：拿走相機控制權並持續心跳，直到 ctx['stop'] 被設。"""
    import threading
    from gvcp_setip import REG_CCP
    d = cam_dev(ccd)
    g = gvcp(d)
    if not g.write_reg(d['ip'], [(REG_CCP, 2)]):
        raise RuntimeError('拿不到控制權')
    ctx['stop'] = threading.Event()

    def beat():
        while not ctx['stop'].wait(1.0):
            g.read_reg(d['ip'], REG_CCP)
        g.write_reg(d['ip'], [(REG_CCP, 0)])
        g.s.close()
    ctx['thread'] = threading.Thread(target=beat, daemon=True)
    ctx['thread'].start()


def release_ccp(ctx):
    ctx['stop'].set()
    ctx['thread'].join(5)


def set_lrate(ctx, ccd, val):
    import cam_align as ca
    ca.do_discover()
    c = next(x for x in ca.CAMS.values() if x.info.get('user_id') == ccd)
    for _ in range(60):
        if c.ready():
            break
        time.sleep(0.2)
    if 'lrate0' not in ctx:
        ctx['lrate0'] = next(f['value'] for f in c.list_features() if f['key'] == 'lrate')
    return c.set_feature('lrate', val if val is not None else ctx['lrate0'])


def ssh(cmd):
    r = subprocess.run(['ssh', '-o', 'BatchMode=yes', SPARK, cmd], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f'ssh 失敗：{(r.stderr or r.stdout).strip()[-200:]}')
    return r


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
    # ── 第二輪（2026-10-05）──
    st['cam_ip_changed'] = dict(
        symptom='CCD04 沒有影像，可是交換機燈有亮、相機燈也有亮，線也重插過了。',
        inject=lambda ctx: force_ip(ctx, 'CCD04', '192.168.6.4'),
        restore=lambda ctx: force_ip(ctx, 'CCD04', '192.168.5.4'), settle=3)
    st['dup_name'] = dict(
        symptom='剛換了一台相機（CCD06 的位置），換完之後 CCD06 沒有影像，而且 CCD03 的影像好像怪怪的。',
        inject=lambda ctx: rename(ctx, 'CCD06', 'CCD03'),
        restore=lambda ctx: rename(ctx, ctx['mac'], 'CCD06'), settle=2)
    st['cam_busy'] = dict(
        symptom='按開始取像之後 CCD02 一直失敗，其他台都正常；早上工程師有來看過相機。',
        inject=lambda ctx: hold_ccp(ctx, 'CCD02'), restore=release_ccp, settle=2)
    st['line_rate'] = dict(
        symptom='CCD01 拍出來的影像好像被壓扁/拉長了，跟旁邊相機接不起來，缺陷位置也對不上。',
        inject=lambda ctx: set_lrate(ctx, 'CCD01', 11001.1),
        restore=lambda ctx: set_lrate(ctx, 'CCD01', None), settle=1)
    st['grab_down'] = dict(
        symptom='Control 上 Grab 的燈是紅的，按什麼都沒反應，相機看起來都有開。',
        inject=lambda ctx: subprocess.run(['systemctl', '--no-ask-password', 'stop', 'cfaoi-grab'], check=True),
        restore=lambda ctx: subprocess.run(['systemctl', '--no-ask-password', 'start', 'cfaoi-grab'], check=True), settle=3)
    st['ip_offline_mode'] = dict(
        symptom='昨天工程師在調參數，今天開線送料後一直沒有結果，Control 的 IP 燈是綠的。',
        inject=lambda ctx: ssh('systemctl --no-ask-password start cfaoi-ip-offline'),
        restore=lambda ctx: ssh('systemctl --no-ask-password start cfaoi-ip-production'), settle=4)
    st['ini_changed'] = dict(
        symptom='重開機之後，暗的缺陷好像都抓不到了，以前會抓到的點現在都沒有；最近有人動過 Spark。',
        inject=lambda ctx: ssh("cd ~/Addis/cf-aoi && sed -i 's/^DTH = 0.60/DTH = 0.30/' ip/config/default_zone.ini && grep '^DTH' ip/config/default_zone.ini"),
        restore=lambda ctx: ssh('cd ~/Addis/cf-aoi && git checkout -- ip/config/default_zone.ini && git status --porcelain ip/config'),
        settle=1, triage_args=('--gpu',))
    return st


def run_one(name, sc, out):
    print(f'═══ {name} ═══ 症狀：{sc["symptom"]}', flush=True)
    ctx, res = {}, {'scenario': name, 'symptom': sc['symptom']}
    try:
        sc['inject'](ctx)
        res['injected'] = {k: v for k, v in ctx.items() if isinstance(v, (str, int, float, bool, type(None)))}
        time.sleep(sc['settle'])
        res['fault'] = triage(name, out, sc.get('triage_args', ()))
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
