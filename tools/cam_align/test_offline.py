#!/usr/bin/env python3
"""離線迴歸測試（不需相機）— cam_align / genicam_client 的解析層。

覆蓋 2026-09-18 讓 raL8192 與 L803K 共用同一條路徑時修掉的三個坑：
  1. Basler zip 的非標準 extra field（zipfile 嚴格解析會擲 BadZipFile）
  2. GVSP 標準 ID 的 trailer 只有 16 bytes（舊碼要求 >=20 → 永遠等不到結束封包）
  3. GenICam 公式節點（pAddress 動態位址、Converter 單位換算、pIsAvailable、Sign）

跑法：python3 test_offline.py      預期「全數通過」、exit 0
不涵蓋：真相機互動（GVCP 收發、iPORT 序列埠）——需接相機用 `cam_align.py --test`。
"""
import io
import os
import struct
import sys
import zipfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cam_align                                          # noqa: E402
from genicam_client import (GevDevice, _unzip_first,       # noqa: E402
                            compile_formula, eval_formula)

FAIL = []


def check(ok, name, detail=''):
    print(f'  {"PASS" if ok else "FAIL"}  {name}' + (f'  [{detail}]' if detail else ''))
    if not ok:
        FAIL.append(name)


def ev(text, env, integer=False):
    return eval_formula(compile_formula(text, list(env), integer), env)


# ── 1. zip ──────────────────────────────────────────────────────────────────
def basler_style_zip(payload, name='cam.xml'):
    """標準 zip，但 local header 夾帶 Basler 的非標準 extra field（id 0x4347 'GCV0'）。"""
    comp = zlib.compressobj(6, zlib.DEFLATED, -15)
    body = comp.compress(payload) + comp.flush()
    extra = b'GCV0' + b'\x01\x00\x00\x00' * 4        # 不合 (id,size) 規範
    nm = name.encode()
    return (struct.pack('<IHHHHHIIIHH', 0x04034B50, 20, 0, 8, 0, 0,
                        zlib.crc32(payload), len(body), len(payload), len(nm), len(extra))
            + nm + extra + body)


print('== zip 解壓')
xml = b'<?xml version="1.0"?><RegisterDescription/>'
check(_unzip_first(basler_style_zip(xml)) == xml, 'Basler 非標準 extra field 仍解得開')
buf = io.BytesIO()
with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
    z.writestr('a.xml', xml)
check(_unzip_first(buf.getvalue()) == xml, '標準 zip（iPORT 路徑）行為不變')

# ── 2. GVSP 封包解析 ────────────────────────────────────────────────────────
print('== GVSP 封包解析')


def std_pkt(fmt, pid, body=b''):            # 標準 ID：8 bytes 表頭
    return struct.pack('>HHB', 0, 0, fmt) + pid.to_bytes(3, 'big') + body


def ext_pkt(fmt, pid, body=b''):            # 擴充 ID：20 bytes 表頭
    return (struct.pack('>HHB', 0, 0, 0x80 | fmt) + b'\x00' * 11
            + struct.pack('>I', pid) + body)


trailer = std_pkt(cam_align.GVSP_TRAILER, 5, b'\x00' * 8)
check(len(trailer) == 16, 'trailer 只有 16 bytes（就是舊碼丟掉的那包）', str(len(trailer)))
check(cam_align.parse_gvsp(trailer) == (cam_align.GVSP_TRAILER, 5, b'\x00' * 8),
      '標準 ID trailer 解得出來')
check(cam_align.parse_gvsp(std_pkt(cam_align.GVSP_PAYLOAD, 7, b'xy'))
      == (cam_align.GVSP_PAYLOAD, 7, b'xy'), '標準 ID payload')
check(cam_align.parse_gvsp(ext_pkt(cam_align.GVSP_PAYLOAD, 9, b'z'))
      == (cam_align.GVSP_PAYLOAD, 9, b'z'), '擴充 ID payload（iPORT 路徑不變）')
check(cam_align.parse_gvsp(ext_pkt(cam_align.GVSP_PAYLOAD, 9)[:12]) is None,
      '擴充 ID 但長度不足 → 丟棄')
bad = bytearray(std_pkt(cam_align.GVSP_PAYLOAD, 1, b'x'))
bad[0:2] = struct.pack('>H', 0x8005)
check(cam_align.parse_gvsp(bytes(bad)) is None, '錯誤狀態 → 丟棄')
warn = bytearray(std_pkt(cam_align.GVSP_PAYLOAD, 1, b'x'))
warn[0:2] = struct.pack('>H', 0x4008)
check(cam_align.parse_gvsp(bytes(warn)) is not None, '0x4xxx 警告狀態 → 資料仍有效')
check(cam_align.parse_gvsp(b'\x00' * 4) is None, '過短封包 → 丟棄')

# ── 3. 公式 ────────────────────────────────────────────────────────────────
print('== GenICam 公式')
check(ev('(P1=1)?(TO*P2/1000):(TO)', {'P1': 1, 'P2': 100, 'TO': 700}) == 70.0,
      '曝光換算 raw 700 × 100ns → 70µs')
check(ev('(P1=1)?(TO*P2/1000):(TO)', {'P1': 0, 'P2': 100, 'TO': 700}) == 700,
      '條件為假 → 走另一分支')
check(ev('(P1=0)?0x2004c:0x20a0c', {'P1': 0}, True) == 0x2004C, '十六進位常數')
check(ev('1+2*3', {}, True) == 7, '運算優先序')
check(ev('(1+2)*3', {}, True) == 9, '括號')
check(ev('7/2', {}, True) == 3, '整數節點：除法截斷')
check(ev('7/2', {}) == 3.5, '浮點節點：除法不截斷')
check(ev('(A<>B)&&(A<3)||(B=9)', {'A': 1, 'B': 2}, True) == 1, '比較與邏輯（= 是相等）')
check(ev('ABS(0-5)', {}, True) == 5, '函式 ABS')
check(ev('(1<<4)|3', {}, True) == 19, '位移與位元或')
for bad_expr, why in (('__import__("os")', '禁止任意名稱'),
                      ('P9+1', '未宣告的變數'),
                      ('1+', '語法不完整'),
                      ('FOO(1)', '不支援的函式')):
    try:
        compile_formula(bad_expr, ['P1'], True)
        check(False, f'拒絕 {why}')
    except Exception:
        check(True, f'拒絕 {why}')

# ── 4. 節點解析（假 XML + 假暫存器）────────────────────────────────────────
print('== 節點解析（pAddress / Converter / Sign / pIsAvailable）')
FAKE_XML = b'''<?xml version="1.0"?>
<RegisterDescription xmlns="http://www.genicam.org/GenApi/Version_1_0">
  <Integer Name="Sel"><pValue>SelReg</pValue></Integer>
  <IntReg Name="SelReg"><Address>0x100</Address><Length>4</Length><AccessMode>RW</AccessMode></IntReg>
  <IntSwissKnife Name="GainAddr"><pVariable Name="P1">Sel</pVariable>
    <Formula>(P1=0)?0x200:0x300</Formula></IntSwissKnife>
  <IntReg Name="GainReg"><pAddress>GainAddr</pAddress><Length>4</Length><AccessMode>RW</AccessMode></IntReg>
  <Integer Name="GainRaw"><pValue>GainReg</pValue><Min>0</Min><Max>1000</Max></Integer>
  <IntReg Name="ExpReg"><Address>0x400</Address><Length>4</Length><AccessMode>RW</AccessMode></IntReg>
  <Integer Name="ExpRaw"><pValue>ExpReg</pValue><Min>20</Min><Max>1000</Max></Integer>
  <Converter Name="ExpConv"><FormulaTo>FROM*10</FormulaTo><FormulaFrom>TO/10</FormulaFrom>
    <pValue>ExpRaw</pValue></Converter>
  <Float Name="ExposureTimeAbs"><pValue>ExpConv</pValue><Unit>us</Unit></Float>
  <IntReg Name="BlackReg"><Address>0x500</Address><Length>4</Length><AccessMode>RW</AccessMode>
    <Sign>Signed</Sign></IntReg>
  <Integer Name="BlackLevelRaw"><pValue>BlackReg</pValue></Integer>
  <IntSwissKnife Name="Never"><Formula>0</Formula></IntSwissKnife>
  <Integer Name="Hidden"><pIsAvailable>Never</pIsAvailable><pValue>ExpReg</pValue></Integer>
</RegisterDescription>'''


class FakeDev(GevDevice):
    """只換掉 GVCP 收發：暫存器放在 dict 裡。"""

    def __init__(self, regs):
        self.regs, self.ip, self.g = dict(regs), '0.0.0.0', None
        self._feat_cache, self._formula_cache = {}, {}

    def fetch_xml(self):
        return FAKE_XML

    def read_u32(self, addr):
        return self.regs.get(addr)

    def write_u32(self, addr, val):
        self.regs[addr] = val & 0xFFFFFFFF
        return True

    def readmem(self, addr, n):
        return self.regs.get(addr, 0).to_bytes(n, 'big')


d = FakeDev({0x100: 0, 0x200: 111, 0x300: 222, 0x400: 700, 0x500: (1 << 32) - 2048})
d.load()
gain = d.resolve('GainRaw')
check(gain is not None and gain.addr_nodes == ['GainAddr'], 'pAddress 記成動態位址')
check(d.get(gain) == 111, 'selector=0 → 讀 0x200')
d.regs[0x100] = 1
check(d.get(gain) == 222, 'selector=1 → 同一參數改讀 0x300（換 selector 不必重解析）')
check(d.set(gain, 123)[0] and d.regs[0x300] == 123, '寫入寫到動態位址')
expo = d.resolve('ExposureTimeAbs')
check(expo is not None and d.get(expo) == 70.0, 'Converter：raw 700 → 70.0')
check(expo.unit == 'us', '單位取自 XML')
check(d.limits(expo) == (2.0, 100.0, None), '範圍經 Converter 換算', str(d.limits(expo)))
ok, back = d.set(expo, 12.0)
check(ok and d.regs[0x400] == 120 and back == 12.0, '寫入：對外值 → FormulaTo → 暫存器')
black = d.resolve('BlackLevelRaw')
check(d.get(black) == -2048, 'Sign=Signed → 讀成負值')
check(d.resolve('Hidden') is None, 'pIsAvailable=0 → 視同無此參數')

# ── 5. 裝置設定（provision.py：命名 CCDnn + persistent IP）────────────────────
print('\n[provision] CCD 命名 + IP（假 GVCP 裝置）')
import provision                                            # noqa: E402
from gvcp_setip import REG_CCP, REG_NET_IF_CONFIG, REG_PERSISTENT_IP   # noqa: E402


class FakeGvcp:
    """一台假相機：暫存器 dict；FORCEIP 改位址；CCP 被別人持有時寫入回 ACCESS_DENIED。"""
    def __init__(self, ip, busy=False, ignore_name=False):
        self.ip, self.busy, self.ignore_name, self.regs = ip, busy, ignore_name, {REG_CCP: 0}

    def force_ip(self, mac, ip, mask, gw):
        if self.busy:
            return (0x8006, b'')
        self.ip = ip
        return (0, b'')

    def read_reg(self, dest, *addrs):
        if dest != self.ip:
            return None
        return [self.regs.get(a, 0) for a in addrs]

    def write_reg(self, dest, pairs):
        if dest != self.ip:
            return None
        for a, v in pairs:
            if self.busy:
                return (0x8006, b'')
            if self.ignore_name and 0xE8 <= a < 0xF8:
                continue                                  # 模擬「回 SUCCESS 但沒寫進去」的韌體
            self.regs[a] = v
        return (0, b'')


name_pairs = provision.pack_name('CCD38')
check(provision.unpack_name([v for _, v in name_pairs]) == 'CCD38' and name_pairs[0][1] == 0x43434433,
      '名稱打包：大端序、第一個字元在最高位元組，可還原')
check(provision.parse_ccd('CCD05') == 5, 'CCD05 → 編號 5')
for bad in ('CCD5', 'CCD00', 'cam05', ''):
    try:
        provision.parse_ccd(bad)
        check(False, f'格式錯擋下：{bad!r}')
    except ValueError:
        check(True, f'格式錯擋下：{bad!r}')
check(provision.camera_kind('raL8192-12gm', 'Basler') == 'basler' and
      provision.camera_kind('iPORT CL-GigE', 'Pleora Technologies Inc.') == 'iport',
      '型號判斷：raL8192 / iPORT')
check(provision.default_ip('basler', 5) == '192.168.5.5' and provision.default_ip('iport', 38) == '192.168.4.38',
      'IP 規則：raL8192 → 192.168.5.nn、iPORT → 192.168.4.nn')
devs = [dict(mac='aa:00', ip='192.168.5.3', user_id='CCD03', model='raL8192-12gm', serial='1'),
        dict(mac='bb:00', ip='192.168.4.1', user_id='', model='iPORT', serial='2')]
check('已被' in (provision.check_conflicts(devs, 'bb:00', 'CCD03', '192.168.4.38') or ''), '撞名擋下')
check('已被' in (provision.check_conflicts(devs, 'bb:00', 'CCD38', '192.168.5.3') or ''), '撞 IP 擋下')
check(provision.check_conflicts(devs, 'bb:00', 'CCD38', '192.168.4.38') is None, '不撞號放行')
g = FakeGvcp('192.168.4.1')
ok, log = provision.provision(g, devs[1], 'CCD38', '192.168.4.38', sleep=lambda s: None)
check(ok and g.ip == '192.168.4.38' and g.regs[REG_PERSISTENT_IP] == provision.ip_u32('192.168.4.38')
      and g.regs[REG_NET_IF_CONFIG] == 0x5 and g.regs[REG_CCP] == 0,
      '跨網段新相機：ForceIP → 寫名稱/IP → 讀回一致 → 釋放控制權', '; '.join(log[-1:]))
ok, log = provision.provision(FakeGvcp('192.168.4.1', busy=True), devs[1], 'CCD38', '192.168.4.38',
                              sleep=lambda s: None)
check(not ok and '控制' in log[-1], '被其他程式占用 → 拒絕（不搶）')
ok, log = provision.provision(FakeGvcp('192.168.4.1', ignore_name=True), devs[1], 'CCD38', '192.168.4.38',
                              sleep=lambda s: None)
check(not ok and '不符' in log[-1], '韌體回成功但沒寫進去 → 讀回比對抓到')

print(f'\n{len(FAIL)} 項失敗' if FAIL else '\n全數通過')
sys.exit(1 if FAIL else 0)
