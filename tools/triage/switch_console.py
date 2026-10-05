"""switch_console.py — 經 USB console 讀 HPE Comware 交換機（5945；SN2201 到貨後另寫對應指令）。

健檢只用 display 指令（唯讀）。shutdown / 限速只給故障注入實驗用（fault_inject.py），一定成對還原。
console：/dev/ttyUSB0，9600 8N1，aux 線不需登入（2026-10-05 實測）。序列埠同時只能一個程式用。
"""
import re
import time

try:
    import serial                      # pyserial（Grab 已裝）
except ImportError:                    # pragma: no cover
    serial = None

PORT_RE = re.compile(r'^((?:WGE|HGE|GE|XGE|FGE)\d+/\d+/\d+)\s+(UP|DOWN|ADM|Stby)\s+(\S+)\s+(\S+)\s+\S+\s+\S+\s*(.*)$')
MAC_RE = re.compile(r'^\s*([0-9a-f]{4})-([0-9a-f]{4})-([0-9a-f]{4})\s+\d+\s+\S+\s+(\S+)')


class SwitchError(Exception):
    pass


def norm_mac(m):
    h = re.sub(r'[^0-9a-f]', '', m.lower())
    return ':'.join(h[i:i + 2] for i in range(0, 12, 2)) if len(h) == 12 else m.lower()


class Switch:
    def __init__(self, dev='/dev/ttyUSB0', baud=9600):
        if serial is None:
            raise SwitchError('沒有 pyserial')
        try:
            self.s = serial.Serial(dev, baud, timeout=1)
        except Exception as e:  # noqa: BLE001
            raise SwitchError(f'開不了交換機 console {dev}：{e}（線沒插？被其他程式占用？）')
        self.prompt = ''
        out = self.cmd('', 2.0)
        m = re.search(r'[<\[]([^>\]\s]+)[>\]]\s*$', out.strip())
        if not m:
            self.cmd('', 2.0)
            out = self.cmd('', 2.0)
            m = re.search(r'[<\[]([^>\]\s]+)[>\]]\s*$', out.strip())
        if not m:
            self.close()
            raise SwitchError('交換機 console 沒有回應提示字元（交換機沒開機？鮑率不對？）')
        self.name = m.group(1)
        self.cmd('screen-length disable', 1.5)

    def close(self):
        try:
            self.s.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def cmd(self, c, wait=3.0):
        self.s.reset_input_buffer()
        self.s.write((c + '\r').encode())
        end = time.time() + wait
        buf = b''
        while time.time() < end:
            b = self.s.read(4096)
            if b:
                buf += b
                end = time.time() + 0.7            # 還有資料就再等一下
        return buf.decode('utf-8', 'replace').replace('\r', '')

    # ── 唯讀查詢 ──
    def ports(self):
        """{port: {link, speed, duplex, desc}}"""
        out = {}
        for line in self.cmd('display interface brief', 5).splitlines():
            m = PORT_RE.match(line.strip())
            if m:
                out[m.group(1)] = {'link': m.group(2), 'speed': m.group(3), 'duplex': m.group(4),
                                   'desc': m.group(5).strip()}
        return out

    def macs(self, reads=3):
        """{mac: port}。console 有雜訊（實測 MAC 讀錯 1 個字、埠欄讀成「Learned」或 HGE3/0/25）→
        讀 3 次、埠名必須真的存在於 display interface brief、同一 MAC 的埠取多數決。
        **不做模糊比對**：同批相機 MAC 是連號（…e3:15、e3:16、e3:19），差 1 個字的多半就是另一台相機
        （2026-10-05 實驗踩到：CCD05 被錯配成 CCD01）。"""
        from collections import Counter
        valid = set(self.ports())
        votes = {}
        for _ in range(reads):
            for line in self.cmd('display mac-address', 4).splitlines():
                m = MAC_RE.match(line)
                if m and (not valid or m.group(4) in valid):
                    votes.setdefault(norm_mac(''.join(m.group(1, 2, 3))), Counter())[m.group(4)] += 1
        return {mac: c.most_common(1)[0][0] for mac, c in votes.items()}


    def counters(self, port):
        """單埠計數：入/出封包、input errors（含 giants）、CRC、output errors、最大框長、速率。
        9600 鮑率 console 偶有亂碼字元（實測「i~puu errors」）→ 比對只靠數字與少數穩定字。"""
        txt = self.cmd(f'display interface {port}', 4)
        c = {}
        pats = [('in_pkts', r'Input \(total\):\s*(\d+)\s+packets'),
                ('out_pkts', r'Output \(total\):\s*(\d+)\s+packets'),
                ('in_err', r'Input:\s*(\d+)\s+\S+\s+errors'),
                ('giants', r'(\d+)\s+giants'),
                ('crc', r'(\d+)\s+CRC'),
                ('out_err', r'Output:\s*(\d+)\s+output'),
                ('frame_len', r'Maximum frame length:\s*(\d+)'),
                ('speed_mbps', r'(\d+)Mbps-speed mode')]
        for key, pat in pats:
            m = re.search(pat, txt)
            if m:
                c[key] = int(m.group(1))
        c['state'] = 'UP' if re.search(r'Current state:\s*UP', txt) else ('DOWN' if 'DOWN' in txt else '?')
        m = re.search(r'Current system time:\s*(\d{4})-', txt.replace('\ufffd', 'e'))
        c['sys_year'] = int(m.group(1)) if m else None
        return c

    def running_config(self):
        txt = self.cmd('display current-configuration', 12)
        return '\n'.join(l for l in txt.splitlines() if l.strip() and not l.startswith('<'))

    def config_lines(self):
        """設定轉成「帶段落」的行集合：縮排行前面加上所屬段落（例 'interface Twenty-FiveGigE1/0/35 | shutdown'）。"""
        out, head = set(), ''
        for l in self.running_config().splitlines():
            if l.startswith('display ') or l.strip() in ('#', 'return'):
                continue
            if not l.startswith(' '):
                head = l.strip()
                out.add(head)
            else:
                out.add(f'{head} | {l.strip()}')
        return out

    def config_consensus(self, reads=2):
        """9600 鮑率 console 實測每幾百字元就有 1 個字元錯（同一份設定兩次讀出 28 行不同）→ 多讀幾次：
        回傳 (至少 2 次都出現的行, 任一次出現的行, 讀取間是否有雜訊)。"""
        sets = [self.config_lines() for _ in range(reads)]
        from collections import Counter
        cnt = Counter(l for s in sets for l in s)
        need = 2 if reads >= 2 else 1
        stable = {l for l, n in cnt.items() if n >= need}
        union = set(cnt)
        return stable, union, any(s != sets[0] for s in sets[1:])

    # ── 故障注入實驗用（fault_inject.py）；一定成對使用 ──
    def _sys(self, *lines):
        self.cmd('system-view', 1.5)
        out = ''
        for l in lines:
            out += self.cmd(l, 2.0)
        self.cmd('return', 1.5)
        return out

    def shutdown(self, port, down=True):
        return self._sys(f'interface {port}', 'shutdown' if down else 'undo shutdown', 'quit')

    def rate_limit(self, port, kbps=None):
        """kbps=None → 解除。入方向限速（相機送進交換機的串流被丟包 → 模擬掉封包）。"""
        if kbps:
            return self._sys(f'interface {port}', f'qos lr inbound cir {int(kbps)}', 'quit')
        return self._sys(f'interface {port}', 'undo qos lr inbound', 'quit')

    def speed(self, port, value):
        """value='1000' / None（undo speed → 自動協商）。speed 有 port-group 連動，會跳 [Y/N] → 自動回 Y。"""
        self.cmd('system-view', 1.5)
        self.cmd(f'interface {port}', 1.5)
        out = self.cmd(f'speed {value}' if value else 'undo speed', 3)
        if '[Y/N]' in out:
            out += self.cmd('Y', 4)
        self.cmd('quit', 1.0)
        self.cmd('return', 1.5)
        return out


def find_port(mac, table):
    """MAC → 埠（完全相同才算；見 macs() 說明為何不做模糊比對）。"""
    return table.get(norm_mac(mac))
