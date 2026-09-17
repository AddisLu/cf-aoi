#!/usr/bin/env python3
"""通用 GigE Vision / GenICam 裝置層 — 不限 iPORT，任何 GEV 相機皆可。

做的事：
  1. 用 GVCP READMEM 從相機下載它自己的 GenICam XML（支援 zip）
  2. 解析 XML，把常用參數（Gain / 曝光 / 行率 / Height...）
     追到實體暫存器（IntReg / MaskedIntReg / FloatReg / Enumeration / Command）
  3. 提供 get / set（4-byte 對齊優先走 READREG/WRITEREG —— iPORT 的
     WRITEMEM 有寫入變 0 的韌體 bug，見 README）

支援 Converter / SwissKnife 公式節點與 pAddress 動態位址（Basler raL8192 的
ExposureTimeAbs、GainRaw 都靠這兩者），故兩種相機（原生 GigE / iPORT）共用同一條路徑。
"""
import os
import io
import math
import re
import struct
import time
import zipfile
import zlib
import xml.etree.ElementTree as ET

from gvcp_setip import Gvcp, REG_CCP

READMEM_CMD, READMEM_ACK = 0x0084, 0x0085
WRITEMEM_CMD, WRITEMEM_ACK = 0x0086, 0x0087
REG_XML_URL0 = 0x0200
CHUNK = 256
CACHE_DIR = os.path.expanduser('~/.cache/cam_align')


# ---------------------------------------------------------------------------
# GenICam 公式（SwissKnife / Converter 的 Formula）求值
# ---------------------------------------------------------------------------
# 為什麼需要：Basler raL8192 的 GainRaw 位址由 GainSelector 經 IntSwissKnife 算出
# （pAddress），ExposureTimeAbs(µs) 由 Converter 從 ExposureTimeRaw 換算。
# 不支援公式 → 這些參數整個消失（調機面板只剩曝光 raw 值、沒有增益）。
# 作法：以 GenICam 文法遞迴下降解析成**受限的 Python 運算式**後 compile 一次。
# 只有下列 token 進得來，變數名必須出現在 names 白名單 → 沒有任意程式碼的空間。

_TOKEN_RE = re.compile(
    r'\s*(0[xX][0-9a-fA-F]+|\d+\.\d+(?:[eE][-+]?\d+)?|\d+'
    r'|[A-Za-z_][A-Za-z_0-9]*|<<|>>|<>|<=|>=|&&|\|\||[-+*/%&|^~()?:,<>=])')

_FUNCS = {'ABS': 'abs', 'SGN': '_SGN', 'NEG': '_NEG', 'SQRT': '_SQRT',
          'TRUNC': '_TRUNC', 'FLOOR': '_FLOOR', 'CEIL': '_CEIL', 'ROUND': '_ROUND',
          'LN': '_LN', 'LG': '_LG', 'EXP': '_EXP', 'SIN': '_SIN', 'COS': '_COS',
          'TAN': '_TAN', 'ASIN': '_ASIN', 'ACOS': '_ACOS', 'ATAN': '_ATAN'}
_CONSTS = {'PI': 'math.pi', 'E': 'math.e'}


class _Parser:
    """GenICam 公式 → Python 運算式（字串）。"""

    def __init__(self, text, names):
        self.toks, pos = [], 0
        while pos < len(text):
            if text[pos].isspace():
                pos += 1
                continue
            m = _TOKEN_RE.match(text, pos)
            if not m:
                raise ValueError(f'公式無法解析（位置 {pos}）: {text!r}')
            self.toks.append(m.group(1))
            pos = m.end()
        self.names, self.i = names, 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self, *want):
        t = self.peek()
        if t in want:
            self.i += 1
            return t
        return None

    def parse(self):
        out = self.ternary()
        if self.i != len(self.toks):
            raise ValueError(f'公式有多餘 token: {self.toks[self.i:]}')
        return out

    def ternary(self):
        cond = self.binary(0)
        if self.take('?'):
            a = self.ternary()
            if not self.take(':'):
                raise ValueError('三元運算缺少 ":"')
            return f'(({a}) if ({cond}) else ({self.ternary()}))'
        return cond

    # 由低到高優先序；'=' 是 GenICam 的相等比較（不是指派）
    LEVELS = [('||',), ('&&',), ('|',), ('^',), ('&',), ('=', '<>'),
              ('<', '>', '<=', '>='), ('<<', '>>'), ('+', '-'), ('*', '/', '%')]
    PY = {'||': 'or', '&&': 'and', '=': '==', '<>': '!='}

    def binary(self, lvl):
        if lvl >= len(self.LEVELS):
            return self.unary()
        out = self.binary(lvl + 1)
        while True:
            op = self.take(*self.LEVELS[lvl])
            if not op:
                return out
            rhs = self.binary(lvl + 1)
            if op == '/':
                out = f'_DIV({out}, {rhs})'       # 整數節點要截斷，見 compile_formula
            else:
                out = f'({out} {self.PY.get(op, op)} {rhs})'

    def unary(self):
        op = self.take('-', '+', '~')
        if op:
            return f'({op}{self.unary()})'
        return self.primary()

    def primary(self):
        t = self.peek()
        if t is None:
            raise ValueError('公式結尾不完整')
        self.i += 1
        if t == '(':
            out = self.ternary()
            if not self.take(')'):
                raise ValueError('括號未閉合')
            return f'({out})'
        if t[0].isdigit():
            return t
        if not t[0].isalpha() and t[0] != '_':
            raise ValueError(f'非預期的 token {t!r}')
        if self.peek() == '(':                     # 函式呼叫
            fn = _FUNCS.get(t.upper())
            if not fn:
                raise ValueError(f'不支援的函式 {t}')
            self.i += 1
            args = [self.ternary()]
            while self.take(','):
                args.append(self.ternary())
            if not self.take(')'):
                raise ValueError('函式括號未閉合')
            return f'{fn}({", ".join(args)})'
        if t.upper() in _CONSTS:
            return _CONSTS[t.upper()]
        if t not in self.names:
            raise ValueError(f'公式用到未知變數 {t}')
        return f'V[{t!r}]'


def compile_formula(text, names, integer):
    """回傳 (code, integer)；求值用 eval_formula。integer=True → 除法截斷（GenICam 整數節點語意）。"""
    src = _Parser(text, set(names)).parse()
    return compile(src, '<genicam-formula>', 'eval'), integer


_EVAL_GLOBALS = {
    '__builtins__': {}, 'math': math, 'abs': abs,
    '_SGN': lambda x: (x > 0) - (x < 0), '_NEG': lambda x: -x,
    '_SQRT': math.sqrt, '_TRUNC': math.trunc, '_FLOOR': math.floor,
    '_CEIL': math.ceil, '_ROUND': lambda x: math.floor(x + 0.5),
    '_LN': math.log, '_LG': math.log10, '_EXP': math.exp,
    '_SIN': math.sin, '_COS': math.cos, '_TAN': math.tan,
    '_ASIN': math.asin, '_ACOS': math.acos, '_ATAN': math.atan,
}


def eval_formula(compiled, env):
    code, integer = compiled
    g = dict(_EVAL_GLOBALS)
    g['V'] = env
    g['_DIV'] = (lambda a, b: int(a / b)) if integer else (lambda a, b: a / b)
    v = eval(code, g)                      # noqa: S307 — 來源僅 _Parser 產生的受限運算式
    if isinstance(v, bool):
        v = int(v)
    return int(v) if integer else v


class Feature:
    """解析後的單一參數：終點暫存器 + 型別資訊。"""

    def __init__(self, name, kind, addr, length=4, access='RW',
                 lsb=None, msb=None, entries=None, cmdval=1,
                 vmin=None, vmax=None, inc=None, unit='', regkind=None,
                 addr_nodes=(), convs=(), pmin=None, pmax=None, pinc=None,
                 signed=False):
        self.name, self.kind, self.addr, self.length = name, kind, addr, length
        self.access, self.lsb, self.msb = access, lsb, msb
        self.entries = entries or {}          # enum: name -> value
        self.cmdval = cmdval
        self.vmin, self.vmax, self.inc, self.unit = vmin, vmax, inc, unit
        # 暫存器實際格式（float 走 IEEE754，其餘走整數）。kind 是「對外型別」，
        # 兩者在 Converter 之後會不同（例：暫存器 int raw → 對外 float µs）。
        self.regkind = regkind or kind
        self.addr_nodes = list(addr_nodes)     # pAddress：位址 = addr + Σ 這些節點的值（存取時求值）
        self.convs = list(convs)               # Converter 層（內→外）
        self.pmin, self.pmax, self.pinc = pmin, pmax, pinc   # 動態上下限節點名
        self.signed = signed                   # 二補數（XML 的 <Sign>Signed</Sign>）

    def as_dict(self):
        return dict(name=self.name, kind=self.kind, access=self.access,
                    min=self.vmin, max=self.vmax, inc=self.inc, unit=self.unit,
                    entries={k: v for k, v in self.entries.items()})


def _unzip_first(raw):
    """解出 zip 內第一個檔案。

    Basler raL8192 的 GenICam zip 夾帶非標準 extra field（id 0x4347 "GCV0"），
    Python zipfile 嚴格解析 extra field 會擲 BadZipFile: Corrupt extra field
    → 退回手動讀 local file header + raw deflate（extra field 只是跳過，不解析）。
    iPORT 的 zip 走標準路徑，行為不變。
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        return zf.read(zf.namelist()[0])
    except Exception:
        pass
    if raw[:4] != b'PK\x03\x04':
        raise RuntimeError('不是 zip（缺 local file header）')
    method, csize, nlen, elen = struct.unpack('<H', raw[8:10])[0], \
        struct.unpack('<I', raw[18:22])[0], \
        struct.unpack('<H', raw[26:28])[0], struct.unpack('<H', raw[28:30])[0]
    body = raw[30 + nlen + elen:]
    if csize:
        body = body[:csize]                      # csize=0 = 走 data descriptor，整段交給 zlib
    if method == 0:
        return body
    if method != 8:
        raise RuntimeError(f'不支援的 zip 壓縮法 {method}')
    return zlib.decompress(body, -15)            # -15 = raw deflate（無 zlib 檔頭）


class GevDevice:
    def __init__(self, iface, srcip, ip):
        self.ip = ip
        self.g = Gvcp(iface, srcip)
        self.byname = {}
        self.ns = ''
        self._feat_cache = {}
        self._formula_cache = {}

    # ---------- 低階 ----------
    def readmem(self, addr, n):
        out = b''
        while n > 0:
            take = min(CHUNK, n)
            req = (take + 3) // 4 * 4            # GVCP 要求長度為 4 的倍數
            r = self.g.xact(READMEM_CMD, struct.pack('>IHH', addr, 0, req),
                            self.ip, broadcast=False, expect_ack=READMEM_ACK)
            if r is None or r[0] != 0 or len(r[1]) < 4 + take:
                raise RuntimeError(f'READMEM 0x{addr:X} 失敗')
            out += r[1][4:4 + take]
            addr += take
            n -= take
        return out

    def writemem(self, addr, data):
        r = self.g.xact(WRITEMEM_CMD, struct.pack('>I', addr) + data,
                        self.ip, broadcast=False, expect_ack=WRITEMEM_ACK)
        return r is not None and r[0] == 0

    def read_u32(self, addr):
        v = self.g.read_reg(self.ip, addr)
        return v[0] if v else None

    def write_u32(self, addr, val):
        r = self.g.write_reg(self.ip, [(addr, val & 0xFFFFFFFF)])
        return r is not None and r[0] == 0

    # ---------- XML ----------
    def fetch_xml(self):
        url = self.readmem(REG_XML_URL0, 512).split(b'\x00')[0].decode('latin-1')
        os.makedirs(CACHE_DIR, exist_ok=True)
        cache = os.path.join(CACHE_DIR,
                             re.sub(r'[^A-Za-z0-9_.-]', '_', f'{self.ip}_{url}')[:120] + '.xml')
        if os.path.exists(cache):
            return open(cache, 'rb').read()
        m = re.match(r'[Ll]ocal:([^;]+);([0-9A-Fa-f]+);([0-9A-Fa-f]+)', url)
        if not m:
            raise RuntimeError(f'不支援的 XML URL: {url!r}')
        fname, addr, length = m.group(1), int(m.group(2), 16), int(m.group(3), 16)
        raw = self.readmem(addr, length)
        if fname.lower().endswith('.zip'):
            raw = _unzip_first(raw)
        i = raw.find(b'<?xml')
        raw = raw[i:] if i >= 0 else raw
        with open(cache, 'wb') as f:
            f.write(raw)
        return raw

    def load(self):
        root = ET.fromstring(self.fetch_xml())
        self.ns = root.tag[1:root.tag.index('}')] if root.tag.startswith('{') else ''
        self.byname = {}
        for e in root.iter():
            n = e.get('Name')
            if n:
                self.byname.setdefault(n, []).append(e)

    def _f(self, el, tag):
        x = el.find(f'{{{self.ns}}}{tag}') if self.ns else el.find(tag)
        return x.text.strip() if x is not None and x.text else None

    def _fs(self, el, tag):
        xs = el.findall(f'{{{self.ns}}}{tag}') if self.ns else el.findall(tag)
        return [x.text.strip() for x in xs if x.text]

    @staticmethod
    def _num(s):
        if s is None:
            return None
        s = s.strip()
        try:
            return int(s, 0)
        except ValueError:
            try:
                return float(s)
            except ValueError:
                return None

    # ---------- 解析 ----------
    def resolve(self, name, depth=0):
        """把 feature 追到實體暫存器；不支援的節點回 None。"""
        if name in self._feat_cache:
            return self._feat_cache[name]
        if depth > 8 or name not in self.byname:
            return None
        result = None
        for el in self.byname[name]:
            tag = el.tag.split('}')[-1]
            if tag in ('IntReg', 'MaskedIntReg', 'FloatReg'):
                addrs = [self._num(a) for a in self._fs(el, 'Address')]
                # pAddress = 動態位址（Basler 以 GainSelector 選暫存器就靠它）：
                # 位址 = Σ Address + Σ pAddress 節點當下的值，存取時才算。
                pnodes = [n for n in self._fs(el, 'pAddress') if n]
                addrs = [a for a in addrs if a is not None]
                if not addrs and not pnodes:
                    continue
                addr = sum(addrs)
                length = self._num(self._f(el, 'Length')) or 4
                access = self._f(el, 'AccessMode') or 'RW'
                signed = (self._f(el, 'Sign') or '').lower() == 'signed'
                if tag == 'FloatReg':
                    result = Feature(name, 'float', addr, length, access,
                                     addr_nodes=pnodes)
                elif tag == 'MaskedIntReg':
                    bit = self._num(self._f(el, 'Bit'))
                    lsb = self._num(self._f(el, 'LSB'))
                    msb = self._num(self._f(el, 'MSB'))
                    if bit is not None:
                        lsb = msb = bit
                    result = Feature(name, 'int', addr, length, access,
                                     lsb=lsb, msb=msb, addr_nodes=pnodes,
                                     signed=signed)
                else:
                    result = Feature(name, 'int', addr, length, access,
                                     addr_nodes=pnodes, signed=signed)
                break
            if tag in ('Integer', 'Float', 'Boolean', 'Enumeration', 'Command',
                       'IntConverter', 'Converter'):
                if not self._node_available(el):
                    continue          # pIsAvailable=0（例：raL8192 無行率節點）→ 不要給使用者看
                pv = self._f(el, 'pValue')
                sub = self.resolve(pv, depth + 1) if pv else None
                if sub is None:
                    continue
                f = Feature(name, sub.kind, sub.addr, sub.length, sub.access,
                            lsb=sub.lsb, msb=sub.msb, regkind=sub.regkind,
                            addr_nodes=sub.addr_nodes, convs=sub.convs,
                            signed=sub.signed)
                inner = (sub.vmin, sub.vmax, sub.pmin, sub.pmax)
                # 巢狀包裝（Float → Converter → Raw）時，範圍留在最內層 → 往外傳遞
                f.inner_limits = (inner if any(x is not None for x in inner)
                                  else getattr(sub, 'inner_limits', (None,) * 4))
                if tag in ('IntConverter', 'Converter'):
                    # Converter：對外值 = FormulaFrom(TO=內層值)，寫入時 FormulaTo(FROM=對外值)
                    # （Basler ExposureTimeAbs µs ↔ ExposureTimeRaw 就是這層）
                    integer = tag == 'IntConverter'
                    variables = {e.get('Name'): (e.text or '').strip() for e in
                                 (el.findall(f'{{{self.ns}}}pVariable') if self.ns
                                  else el.findall('pVariable'))}
                    names = list(variables) + ['TO', 'FROM']
                    try:
                        layer = dict(
                            integer=integer, variables=variables,
                            frm=compile_formula(self._f(el, 'FormulaFrom'), names, integer),
                            to=compile_formula(self._f(el, 'FormulaTo'), names, integer))
                    except Exception:
                        continue                  # 公式不支援 → 視同此節點解不出來
                    f.convs = sub.convs + [layer]
                    f.kind = 'int' if integer else 'float'
                f.vmin = self._num(self._f(el, 'Min'))
                f.vmax = self._num(self._f(el, 'Max'))
                f.inc = self._num(self._f(el, 'Inc'))
                f.pmin, f.pmax = self._f(el, 'pMin'), self._f(el, 'pMax')
                f.pinc = self._f(el, 'pInc')
                f.unit = self._f(el, 'Unit') or ''
                if tag == 'Enumeration':
                    f.kind = 'enum'
                    for ee in (el.findall(f'{{{self.ns}}}EnumEntry') if self.ns
                               else el.findall('EnumEntry')):
                        v = self._num(self._f(ee, 'Value'))
                        if v is not None:
                            f.entries[ee.get('Name')] = v
                elif tag == 'Command':
                    f.kind = 'command'
                    f.cmdval = self._num(self._f(el, 'CommandValue')) or 1
                elif tag == 'Boolean':
                    f.kind = 'bool'
                result = f
                break
        self._feat_cache[name] = result
        return result

    def _node_available(self, el):
        """pIsAvailable / pIsImplemented 求值；求不出來時當作可用（保守，不誤刪參數）。"""
        for tag in ('pIsImplemented', 'pIsAvailable'):
            n = self._f(el, tag)
            if not n:
                continue
            try:
                if not self.eval_node(n):
                    return False
            except Exception:
                pass
        return True

    def resolve_first(self, names):
        for n in names:
            f = self.resolve(n)
            if f is not None:
                return f
        return None

    # ---------- 讀寫 ----------
    def _extract_bits(self, raw, f):
        if f.lsb is None:
            return raw
        width = f.length * 8
        nbits = f.lsb - f.msb + 1
        return (raw >> (width - 1 - f.lsb)) & ((1 << nbits) - 1)

    def _insert_bits(self, raw, f, val):
        width = f.length * 8
        nbits = f.lsb - f.msb + 1
        mask = ((1 << nbits) - 1) << (width - 1 - f.lsb)
        return (raw & ~mask) | ((val << (width - 1 - f.lsb)) & mask)

    # ---------- 節點求值（pAddress / pVariable / pMin-pMax 用）----------
    def eval_node(self, name, depth=0):
        """算出任一節點當下的數值（SwissKnife 公式、常數、或讀暫存器）。"""
        if depth > 12:
            raise RuntimeError(f'節點遞迴過深: {name}')
        for el in self.byname.get(name, []):
            tag = el.tag.split('}')[-1]
            if tag in ('IntSwissKnife', 'SwissKnife'):
                integer = tag == 'IntSwissKnife'
                variables = {e.get('Name'): (e.text or '').strip() for e in
                             (el.findall(f'{{{self.ns}}}pVariable') if self.ns
                              else el.findall('pVariable'))}
                key = (name, id(el))
                if key not in self._formula_cache:
                    self._formula_cache[key] = compile_formula(
                        self._f(el, 'Formula'), list(variables), integer)
                env = {k: self.eval_node(v, depth + 1) for k, v in variables.items()}
                return eval_formula(self._formula_cache[key], env)
            if tag in ('Integer', 'Float') and self._f(el, 'Value') is not None \
                    and not self._f(el, 'pValue'):
                return self._num(self._f(el, 'Value'))       # 常數節點
        f = self.resolve(name, depth)
        if f is None:
            raise RuntimeError(f'無法求值節點 {name}')
        v = self.get(f, numeric=True)
        if v is None:
            raise RuntimeError(f'讀取節點失敗 {name}')
        return v

    def addr_of(self, f):
        """實際位址：靜態位址 + 每個 pAddress 節點當下的值（Basler 以 selector 選暫存器）。"""
        addr = f.addr or 0
        for n in f.addr_nodes:
            addr += int(self.eval_node(n))
        return addr

    def limits(self, f):
        """(min, max, inc)：靜態值優先，否則求值 pMin/pMax/pInc（隨其他參數變動）。"""
        out = []
        for static, node in ((f.vmin, f.pmin), (f.vmax, f.pmax), (f.inc, f.pinc)):
            v = static
            if v is None and node:
                try:
                    v = self.eval_node(node)
                except Exception:
                    v = None
            out.append(v)
        if f.convs and out[0] is None and out[1] is None:
            inner = getattr(f, 'inner_limits', None)
            if inner:
                conv = []
                for static, node in ((inner[0], inner[2]), (inner[1], inner[3])):
                    v = static
                    if v is None and node:
                        try:
                            v = self.eval_node(node)
                        except Exception:
                            v = None
                    if v is not None:
                        try:
                            v = self._apply_convs(f, v)
                        except Exception:
                            v = None
                    conv.append(v)
                lo, hi = conv
                if lo is not None and hi is not None and lo > hi:
                    lo, hi = hi, lo               # Slope=Decreasing 的換算會翻轉
                out[0], out[1] = lo, hi
        return tuple(out)

    def _apply_convs(self, f, v):
        for layer in f.convs:
            env = {k: self.eval_node(n) for k, n in layer['variables'].items()}
            env['TO'] = v
            v = eval_formula(layer['frm'], env)
        return v

    def describe(self, f):
        """as_dict + 動態上下限（供 UI 滑桿）。"""
        d = f.as_dict()
        lo, hi, inc = self.limits(f)
        d.update(min=lo, max=hi, inc=inc)
        return d

    # ---------- 讀寫 ----------
    def get(self, f, numeric=False):
        addr = self.addr_of(f)
        if f.regkind == 'float':
            raw = self.readmem(addr, f.length)
            if raw is None:
                return None
            v = struct.unpack('>f' if f.length == 4 else '>d', raw)[0]
        else:
            if f.length == 4:
                raw = self.read_u32(addr)
                if raw is None:
                    return None
            else:
                b = self.readmem(addr, f.length)
                if b is None:
                    return None
                raw = int.from_bytes(b, 'big')
            v = self._extract_bits(raw, f)
            if f.signed:                           # 二補數 → 負值（BlackLevelRaw 可為負）
                bits = (f.msb is not None and f.lsb - f.msb + 1) or f.length * 8
                if v >= 1 << (bits - 1):
                    v -= 1 << bits
        for layer in f.convs:                      # 內層→外層套用 FormulaFrom
            env = {k: self.eval_node(n) for k, n in layer['variables'].items()}
            env['TO'] = v
            v = eval_formula(layer['frm'], env)
        if f.kind == 'enum' and not numeric:
            for name, ev in f.entries.items():
                if ev == v:
                    return name
        return v

    def set(self, f, value):
        """寫入並回 (ok, readback)。enum 可給名稱或數值。"""
        addr = self.addr_of(f)
        if f.kind == 'command':
            return self.write_u32(addr, f.cmdval), None
        if f.kind == 'enum' and isinstance(value, str):
            if value not in f.entries:
                return False, None
            value = f.entries[value]
        for layer in reversed(f.convs):            # 外層→內層套用 FormulaTo
            env = {k: self.eval_node(n) for k, n in layer['variables'].items()}
            env['FROM'] = float(value)
            value = eval_formula(layer['to'], env)
        if f.regkind == 'float':
            data = struct.pack('>f' if f.length == 4 else '>d', float(value))
            if f.length == 4:
                ok = self.write_u32(addr, struct.unpack('>I', data)[0])
            else:
                ok = self.writemem(addr, data)
            return ok, self.get(f)
        value = int(round(float(value)))
        if f.lsb is not None:
            cur = (self.read_u32(addr) if f.length == 4
                   else int.from_bytes(self.readmem(addr, f.length), 'big'))
            value = self._insert_bits(cur, f, value)
        if f.length == 4:
            ok = self.write_u32(addr, value)
        else:
            ok = self.writemem(addr, value.to_bytes(f.length, 'big'))
        return ok, self.get(f)
        value = int(round(float(value)))
        if f.lsb is not None:
            cur = (self.read_u32(f.addr) if f.length == 4
                   else int.from_bytes(self.readmem(f.addr, f.length), 'big'))
            value = self._insert_bits(cur, f, value)
        if f.length == 4:
            ok = self.write_u32(f.addr, value)
        else:
            ok = self.writemem(f.addr, value.to_bytes(f.length, 'big'))
        return ok, self.get(f)


# 調機面板要顯示的參數（依序嘗試，取第一個解析成功的）
CURATED = [
    ('gain',   '增益 Gain',      ['Gain', 'GainAbs', 'GainRaw']),
    ('expo',   '曝光 Exposure',  ['ExposureTime', 'ExposureTimeAbs', 'ExposureTimeRaw']),
    ('lrate',  '行率 LineRate',  ['AcquisitionLineRate', 'AcquisitionLineRateAbs',
                                  'AcquisitionLineRateRaw']),
    ('black',  '黑位準 Black',   ['BlackLevel', 'BlackLevelRaw']),
    ('height', '每張行數',        ['Height']),
    ('testimg', '測試圖樣',       ['TestImageSelector', 'TestPattern']),
]

# 串流控制（各相機位址不同，一律由 XML 解出）
CONTROL = ['AcquisitionMode', 'AcquisitionStart', 'AcquisitionStop',
           'TLParamsLocked', 'PixelFormat', 'Width', 'Height',
           'AcquisitionFrameCount']
