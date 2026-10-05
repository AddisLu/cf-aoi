#!/usr/bin/env python3
"""離線測試一鍵健檢的判斷邏輯（不需交換機/相機/Spark）：解析與決策用假資料驗。

涵蓋：Switch 類別方法齊全（防止模組層函式插進類別中間把類別切斷 —— 2026-10-05 實際踩到）、
交換機輸出解析（含 console 亂碼）、MAC 容錯比對、設定共識比對不誤報、Xid 硬體/程式分類、
RDMA 決策表（線 / 設定 / MTU / IP 程式 / 正常）、報告輸出結論排序。
跑法：python3 tools/triage/test_triage.py   預期「全數通過」、exit 0
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cfaoi_triage as T  # noqa: E402
import switch_console as S  # noqa: E402

FAIL = []


def check(name, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + str(detail)) if detail and not cond else ''}")
    if not cond:
        FAIL.append(name)


print('1. 交換機模組')
need = {'cmd', 'ports', 'macs', 'counters', 'running_config', 'config_lines', 'config_consensus', 'shutdown', 'rate_limit', 'speed'}
check('Switch 類別方法齊全', need <= set(dir(S.Switch)), need - set(dir(S.Switch)))
check('find_port 在模組層', callable(getattr(S, 'find_port', None)))


class FakeSw(S.Switch):
    def __init__(self, outputs):
        self.outputs = outputs           # cmd → list of outputs（依序）
        self.n = {}

    def cmd(self, c, wait=3.0):
        i = self.n.get(c, 0)
        self.n[c] = i + 1
        o = self.outputs.get(c, [''])
        return o[min(i, len(o) - 1)]


brief = """Interface            Link Speed     Duplex Type PVID Description
HGE1/0/25            UP   100G(a)   F(a)   A    1    uplink
WGE1/0/35            UP   1G        F(a)   A    1
WGE1/0/33            UP   1G        F(a)   A    1
WGE1/0/35            ADM  1G        A      A    1
WGE1/0/37            DOWN 1G        A      A    1
"""
sw = FakeSw({'display interface brief': [brief]})
p = sw.ports()
check('埠狀態解析（UP/ADM/DOWN）', p.get('WGE1/0/35', {}).get('link') == 'ADM' and p['HGE1/0/25']['speed'] == '100G(a)', p)
cnt = """ Input (total):  1592847 packets, 14280776156 bytes
 Input:  26 i~puu errors, 0 runts, 26 giants, 0 throttles
\t 3 CRC, 0 frame, - overruns, 0 aborts
 Output (total): 25406 packets, 2905307 bytes
 Output: 0 output errors, - underruns, 0 buffer failures
Maximum frame length: 9416
1000Mbps-speed mode, full-duplex mode
Current state: UP
Curr�nt system time:2001-01-07 10:24:32"""
c = FakeSw({'display interface WGE1/0/35': [cnt]}).counters('WGE1/0/35')
check('計數解析容忍亂碼（i~puu errors）', c.get('in_err') == 26 and c.get('crc') == 3 and c.get('speed_mbps') == 1000, c)
check('交換機年份（亂碼也解析）', c.get('sys_year') == 2001, c)
tbl = {'00:30:53:54:e3:19': 'WGE1/0/43', '00:30:53:54:e3:15': 'WGE1/0/33'}
check('MAC 完全相同才對到', S.find_port('00:30:53:54:e3:19', tbl) == 'WGE1/0/43' and S.find_port('00-30-53-54-E3-19', tbl) == 'WGE1/0/43')
check('連號相機 MAC 差 1 字不可錯配（CCD05 e3:16 ≠ CCD01 e3:19）', S.find_port('00:30:53:54:e3:16', tbl) is None)
macs_txt = ['0030-5354-e316   1        Learned        WGE1/0/35   AGING\n', '0030-5754-e316   1        Learned        WGE1/0/35   AGING\n',
            '0030-5354-e316   1        Learned        WGE1/0/35   AGING\n']
macs_txt[1] = '0030-5354-e316   1        Learned        HGE3/0/25   AGING\n'   # 埠欄亂碼（實測）
m3 = FakeSw({'display mac-address': macs_txt, 'display interface brief': [brief]}).macs()
check('MAC 表讀 3 次取聯集（一次讀錯仍找得到）', S.find_port('00:30:53:54:e3:16', m3) == 'WGE1/0/35', m3)

cfg = "#\ninterface Twenty-FiveGigE1/0/35\n port link-mode bridge\n speed 1000\n stp edged-port\n#\n sysname SW\n"
garbled = cfg.replace('stp edged-port', 'stp edgcd-port')
sw = FakeSw({'display current-configuration': [cfg, garbled, cfg]})
stable, union, noisy = sw.config_consensus(3)
check('共識：亂碼行不進 stable、原行仍在', 'interface Twenty-FiveGigE1/0/35 | stp edged-port' in stable
      and any('edgcd' in x for x in union) and noisy)
check('設定行帶段落', any(x.startswith('interface Twenty-FiveGigE1/0/35 | speed 1000') for x in stable), stable)

print('2. Xid 分類')
lines = ['2026-09-29T23:51:31+08:00 k: NVRM: Xid (PCI:000f:01:00): 13, Graphics SM Warp Exception',
         '2026-09-30T06:38:55+08:00 k: NVRM: Xid (PCI:000f:01:00): 31, pid=1, name=ccl_bench, channel 0x1e',
         '2026-10-01T01:00:00+08:00 k: NVRM: Xid (PCI:000f:01:00): 79, GPU has fallen off the bus']
x = T.parse_xid(lines)
check('解析 Xid 碼與程式名', [(c, p) for c, p, _ in x] == [(13, ''), (31, 'ccl_bench'), (79, '')], x)
check('79 = 硬體類、13/31 = 程式類', 79 in T.XID_HW and 13 not in T.XID_HW and 31 in T.XID_APP)

print('3. RDMA 決策表')


def rdma_case(carrier, ping, jumbo, spark, iph=True):
    rep = T.Report()
    T.nic_with_ip = lambda prefix: 'enp1s0f0np0'
    T.sysfs = lambda nic, name, default=None: {'carrier': '1' if carrier else '0', 'speed': '100000', 'mtu': '9000'}.get(name, default)
    T.run = lambda cmd, timeout=20, shell=False: ((0 if ping else 1, '', '') if cmd[0] == 'ping' and '8972' not in cmd
                                                   else (0 if jumbo else 1, '', '') if cmd[0] == 'ping'
                                                   else (0, 'link rocep1s0f0/1 state ACTIVE physical_state LINK_UP netdev enp1s0f0np0', ''))
    T.ssh_spark = lambda script, timeout=25: (0, '\n'.join(f'{k}={v}' for k, v in spark.items()), '') if spark else (255, '', 'x')
    T.tcp_cmd = lambda addr, cmd, params=None, timeout=5.0: {'status': 'OK'} if iph else None
    T.check_rdma(rep, None, {}, None)
    return rep.items[0]


ok_sp = dict(carrier='1', mtu='9000', rdma='state ACTIVE', svc='active', svc_off='inactive', listen='4')
r = rdma_case(False, False, False, None)
check('無 link → 判「線或 Spark 沒開機」', r['level'] == 'FAIL' and '線' in r['title'], r['title'])
r = rdma_case(True, False, False, None)
check('有 link 不通 → 判「設定問題，不是線」', '不是線' in r['title'], r['title'])
r = rdma_case(True, True, False, ok_sp)
check('大封包不通 → MTU', 'MTU' in r['title'], r['title'])
r = rdma_case(True, True, True, dict(ok_sp, svc='inactive'))
check('IP 服務停 → 判「機器/程式，不是線」', '不是線' in r['title'] and 'IP 程式' in r['title'], r['title'])
r = rdma_case(True, True, True, ok_sp)
check('全部正常 → OK', r['level'] == 'OK', r)

print('4. 報告輸出')
rep = T.Report()
rep.add('相機', 'OK', '相機 6 台都在')
rep.add('RDMA', 'WARN', '某注意事項', cause='c', action='a')
rep.add('相機', 'FAIL', 'CCD05 不見了', cause='線', action='重插', who='設備工程')
md = T.render(rep, 'TEST')
check('結論列出異常與注意（異常在前）', md.index('CCD05 不見了') < md.index('某注意事項') and '❌ 1 項異常' in md)
check('worst = FAIL', rep.worst() == 0)

print('全數通過' if not FAIL else f'失敗 {len(FAIL)} 項：{FAIL}')
sys.exit(1 if FAIL else 0)
