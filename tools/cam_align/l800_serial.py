#!/usr/bin/env python3
"""透過 iPORT Bulk0（Camera Link 序列埠橋）與 Basler L800k 相機對話。

路徑：主機 --GVCP--> iPORT Bulk0 UART --RS-644--> L803K

iPORT 端（位址由裝置 GenICam XML 的 AddrCalc 解出，SEL=Bulk0）：
  TX：WRITEMEM 到資料窗 0x40058000
  RX：GVCP message channel 事件（EVENTDATA, event_id=0x9001）
      需設定 bootstrap MCP/MCDA 指向本機並開啟 0x00016000 事件通知

相機端：Basler L800k binary read/write command protocol（手冊 §4.3）
  frame = BFS(0x01) FTF DataLen Addr(LE,2B) [Data...] BCC BFE(0x03)
  FTF：讀=0x0C 寫=0x04（皆含 BCC、16-bit 位址）；ACK=0x06 NAK=0x15
  讀回應 frame 的 FTF=0x14（opcode 00010 + BCC）

用法（單機測試）:
  python3 l800_serial.py <裝置IP> [--iface I] [--srcip S] [--loopback-only]
  python3 l800_serial.py 192.168.4.54 --set-gain-db 6.0
"""
import argparse
import math
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gvcp_setip import Gvcp, REG_CCP  # noqa: E402

# ---- iPORT Bulk0 / 事件 / message channel ----
B0_MODE, B0_BAUD, B0_PARITY, B0_STOP = 0x20017800, 0x20017814, 0x20017818, 0x2001781C
B0_LOOPBACK, B0_SOFT_RESET = 0x20017830, 0x20017848
B0_WATERMARK, B0_RXTIMEOUT = 0x20017810, 0x20017840   # bytes / ticks(66.67MHz)
B0_TX_WINDOW = 0x40058000
EV_ENABLE_B0 = 0x00016000            # EventNotification(Bulk0UpstreamData)
EVENT_ID_B0 = 0x9001                 # 36865
MCP, MCDA, MCTT, MCRC = 0x0B00, 0x0B10, 0x0B14, 0x0B18
EVENT_CMD, EVENT_ACK = 0x00C0, 0x00C1
EVENTDATA_CMD, EVENTDATA_ACK = 0x00C2, 0x00C3
WRITEMEM_CMD, WRITEMEM_ACK = 0x0086, 0x0087

# ---- L800k CSR（手冊 §4.2.2）----
CSR_GAIN = 0x0E00        # +0 status, +1 abs f32, +5 min, +9 max, +0xD raw u16LE, +0xF/+0x11 raw min/max
CSR_EXPO_MODE = 0x1400   # +1 mode（0=free-run programmable）
CSR_EXPO = 0x1500        # +1 abs f32 µs, +5 min, +9 max
CSR_LINE_PERIOD = 0x1600  # +1 abs f32 µs, +5 min, +9 max
CSR_TEST_IMAGE = 0x1800  # +1 mode
CSR_CMD_STATUS = 0x0C30  # binary 協定錯誤旗標

ACK, NAK = 0x06, 0x15


class SerialBridge:
    """iPORT Bulk0 UART + message channel 事件接收。"""

    def __init__(self, g: Gvcp, ip: str, srcip: str):
        self.g, self.ip, self.srcip = g, ip, srcip
        self.ev = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ev.bind((srcip, 0))
        self.ev.settimeout(0.05)
        self.port = self.ev.getsockname()[1]
        self.buf = bytearray()
        self.opened = False
        self.debug = False

    def _w(self, reg, val):
        r = self.g.write_reg(self.ip, [(reg, val)])
        if r is None or r[0] != 0:
            raise RuntimeError(f'寫 0x{reg:X}={val} 失敗 ({r})')

    def open(self, baud_enum=0):
        """設定 UART 9600 8N1 + 事件導向本機。需已持有 CCP。"""
        self._w(B0_MODE, 0)                      # UART
        self._w(B0_BAUD, baud_enum)              # 0 = 9600
        self._w(B0_PARITY, 0)                    # None
        self._w(B0_STOP, 0)                      # One
        self._w(B0_LOOPBACK, 0)
        self._w(B0_WATERMARK, 1)                 # 每個 byte 立即以事件送出
        self._w(B0_RXTIMEOUT, 66667)             # ~1ms 逾時（66.67MHz ticks）
        self._w(B0_SOFT_RESET, 1)                # 清 FIFO（準位訊號，
        self._w(B0_SOFT_RESET, 0)                #  必須放開，否則 UART 卡在 reset）
        self._w(MCDA, struct.unpack('>I', socket.inet_aton(self.srcip))[0])
        self._w(MCP, self.port)
        self._w(MCTT, 200)
        self._w(MCRC, 2)
        self._w(EV_ENABLE_B0, 1)
        self.opened = True
        self.flush()

    def close(self):
        try:
            self._w(EV_ENABLE_B0, 0)
            self._w(MCP, 0)
        except Exception:
            pass
        self.opened = False

    def loopback(self, on):
        self._w(B0_LOOPBACK, 1 if on else 0)

    def rearm(self):
        """重掛 message channel。韌體在 CCP 釋放時會把 MCP 歸零，
        因此每次重新取得控制權後、序列收發前都要呼叫。冪等。"""
        self._w(MCDA, struct.unpack('>I', socket.inet_aton(self.srcip))[0])
        self._w(MCP, self.port)
        self._w(MCTT, 200)
        self._w(MCRC, 2)
        self._w(EV_ENABLE_B0, 1)

    def tx(self, data: bytes):
        # GVCP WRITEMEM 長度必須 4 對齊；L800 的 frame parser 以 BFS=0x01
        # 尋找開頭，frame 之間的 0x00 填充會被忽略（實測驗證於迴環與相機）。
        pad = (-len(data)) % 4
        data = data + b'\x00' * pad
        r = self.g.xact(WRITEMEM_CMD, struct.pack('>I', B0_TX_WINDOW) + data,
                        self.ip, broadcast=False, expect_ack=WRITEMEM_ACK)
        if r is None or r[0] != 0:
            raise RuntimeError(f'TX 失敗 ({r})')
        return pad

    def _pump(self, timeout):
        """收 message channel 封包，序列 bytes 進 self.buf。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                pkt, addr = self.ev.recvfrom(2048)
            except socket.timeout:
                continue
            if len(pkt) < 8 or pkt[0] != 0x42:
                continue
            flags = pkt[1]
            cmd, length, req_id = struct.unpack('>HHH', pkt[2:8])
            payload = pkt[8:8 + length]
            if flags & 0x01:                     # ack required
                ackcmd = EVENT_ACK if cmd == EVENT_CMD else EVENTDATA_ACK
                self.ev.sendto(struct.pack('>HHHH', 0, ackcmd, 0, req_id), addr)
            if self.debug:
                print(f'    [ev] cmd=0x{cmd:04X} len={length} '
                      f'payload={payload[:24].hex()}')
            if cmd != EVENTDATA_CMD or len(payload) < 4:
                continue
            event_id = struct.unpack('>H', payload[2:4])[0]
            if event_id != EVENT_ID_B0 or len(payload) < 20:
                continue
            # 實測 payload 佈局：reserved(2) id(2) ffff(2) 0000(2)
            #                   timestamp(8) datalen(4,BE) data[datalen] pad
            dlen = struct.unpack('>I', payload[16:20])[0]
            self.buf += payload[20:20 + dlen]
        return len(self.buf)

    def rx(self, n, timeout=1.0):
        deadline = time.time() + timeout
        while len(self.buf) < n and time.time() < deadline:
            self._pump(0.08)
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def flush(self):
        self._pump(0.15)
        self.buf.clear()


class L800:
    """Basler L800k binary read/write 協定。"""

    def __init__(self, bridge: SerialBridge):
        self.s = bridge
        self.float_le = True                     # 由 probe() 校正

    @staticmethod
    def _bcc(body):
        x = 0
        for b in body:
            x ^= b
        return x

    def _cmd_read(self, addr, n):
        body = bytes([0x0C, n, addr & 0xFF, addr >> 8])
        return bytes([0x01]) + body + bytes([self._bcc(body), 0x03])

    def _cmd_write(self, addr, data):
        body = bytes([0x04, len(data), addr & 0xFF, addr >> 8]) + data
        return bytes([0x01]) + body + bytes([self._bcc(body), 0x03])

    def read(self, addr, n, tries=3):
        for t in range(tries):
            self.s.flush()
            self.s.tx(self._cmd_read(addr, n))
            # 預期：ACK + [01 14 n data.. bcc 03]（順序可能互換，掃描處理）
            raw = self.s.rx(1 + 5 + n, timeout=0.8)
            data = self._parse_response(raw, n)
            if data is not None:
                return data
        raise RuntimeError(f'讀 0x{addr:04X} 無有效回應（收到 {raw.hex() if raw else "空"}）')

    def _parse_response(self, raw, n):
        i = 0
        while i < len(raw):
            b = raw[i]
            if b == ACK or b == NAK:
                i += 1
                continue
            if b == 0x01 and i + 2 < len(raw):
                ftf, dlen = raw[i + 1], raw[i + 2]
                if (ftf & 0xF8) == 0x10 and dlen == n:      # read response opcode 00010
                    need_bcc = ftf & 0x04
                    end = i + 3 + n + (1 if need_bcc else 0)
                    if end < len(raw) and raw[end] == 0x03:
                        data = raw[i + 3:i + 3 + n]
                        if need_bcc:
                            if self._bcc(raw[i + 1:i + 3 + n]) != raw[i + 3 + n]:
                                return None
                        return data
            i += 1
        return None

    def write(self, addr, data, tries=3):
        for t in range(tries):
            self.s.flush()
            self.s.tx(self._cmd_write(addr, bytes(data)))
            raw = self.s.rx(1, timeout=0.8)
            if raw and raw[0] == ACK:
                return
            if raw and raw[0] == NAK:
                continue
        raise RuntimeError(f'寫 0x{addr:04X} 未收到 ACK')

    # ---- 數值欄位 ----
    def _f32(self, b):
        return struct.unpack('<f' if self.float_le else '>f', b)[0]

    def _f32b(self, v):
        return struct.pack('<f' if self.float_le else '>f', v)

    def probe(self):
        """讀 gain raw + abs，用手冊公式 dB=20log(raw/256) 校正浮點端序。"""
        st = self.read(CSR_GAIN, 1)[0]
        raw = struct.unpack('<H', self.read(CSR_GAIN + 0x0D, 2))[0]
        ab = self.read(CSR_GAIN + 0x01, 4)
        want = 20 * math.log10(max(raw, 1) / 256)
        le, be = struct.unpack('<f', ab)[0], struct.unpack('>f', ab)[0]
        self.float_le = abs(le - want) <= abs(be - want)
        return dict(status=st, gain_raw=raw, gain_db_expect=round(want, 2),
                    gain_db_le=round(le, 2), gain_db_be=round(be, 2),
                    float_le=self.float_le)

    def gain_db(self):
        return self._f32(self.read(CSR_GAIN + 0x01, 4))

    def gain_limits(self):
        return (self._f32(self.read(CSR_GAIN + 0x05, 4)),
                self._f32(self.read(CSR_GAIN + 0x09, 4)))

    def set_gain_db(self, v):
        self.write(CSR_GAIN + 0x01, self._f32b(float(v)))
        return self.gain_db()

    def expo_us(self):
        return self._f32(self.read(CSR_EXPO + 0x01, 4))

    def expo_limits(self):
        return (self._f32(self.read(CSR_EXPO + 0x05, 4)),
                self._f32(self.read(CSR_EXPO + 0x09, 4)))

    def set_expo_us(self, v):
        self.write(CSR_EXPO + 0x01, self._f32b(float(v)))
        return self.expo_us()

    def line_period_us(self):
        return self._f32(self.read(CSR_LINE_PERIOD + 0x01, 4))

    def line_period_limits(self):
        return (self._f32(self.read(CSR_LINE_PERIOD + 0x05, 4)),
                self._f32(self.read(CSR_LINE_PERIOD + 0x09, 4)))

    def set_line_period_us(self, v):
        self.write(CSR_LINE_PERIOD + 0x01, self._f32b(float(v)))
        return self.line_period_us()

    def expo_mode(self):
        return self.read(CSR_EXPO_MODE + 1, 1)[0]

    def cmd_status(self):
        return self.read(CSR_CMD_STATUS, 1)[0]


def attach(g, ip, srcip, retries=2):
    """開橋 + 迴環自檢 + 建 L800。回 (bridge, l800)；失敗丟例外。
    需呼叫端已持有 CCP。eBUS 剛釋放後第一次可能失敗，故預設重試。"""
    last = None
    for _ in range(retries):
        br = SerialBridge(g, ip, srcip)
        try:
            br.open()
            br.loopback(True)
            probe = b'\xa5\x5a\x01\x02'
            br.tx(probe)
            if br.rx(len(probe), timeout=1.2) != probe:
                raise RuntimeError('迴環自檢失敗')
            br.loopback(False)
            br.flush()
            cam = L800(br)
            cam.probe()                       # 校正浮點端序
            return br, cam
        except Exception as e:
            last = e
            try:
                br.close()
            except Exception:
                pass
            time.sleep(0.2)
    raise RuntimeError(f'序列橋建立失敗: {last}')


def set_line_rate_hz(cam: 'L800', hz: float):
    """安全設定行率：提高行率（縮短週期）前先把曝光收進新範圍。

    注意不能用「目前的」Absolute Min 夾目標週期 —— 週期下限是動態的
    （受曝光牽制），要先收曝光再寫週期，讓相機自己做最終 clamp。"""
    period = min(1e6 / max(float(hz), 1.0), 100000.0)
    margin = 2.0                              # 實測 expo max ≈ period - 1.53
    if cam.expo_us() > period - margin:
        cam.set_expo_us(max(period - margin, 10.0))
    back = cam.set_line_period_us(period)
    return 1e6 / back


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('ip')
    ap.add_argument('--iface', default='enp1s0f1np1')
    ap.add_argument('--srcip', default='192.168.4.2')
    ap.add_argument('--loopback-only', action='store_true')
    ap.add_argument('--set-gain-db', type=float)
    ap.add_argument('--set-expo-us', type=float)
    ap.add_argument('--set-line-period-us', type=float)
    ap.add_argument('--debug', action='store_true')
    a = ap.parse_args()

    g = Gvcp(a.iface, a.srcip)
    r = g.write_reg(a.ip, [(REG_CCP, 2)])
    if r is None or r[0] != 0:
        print(f'取得控制權失敗: {r}')
        return 1
    br = SerialBridge(g, a.ip, a.srcip)
    br.debug = a.debug
    try:
        br.open()
        print('=== 1. 迴環自我測試（不經相機）===')
        br.loopback(True)
        probe = bytes.fromhex('a55a0011223344')
        back = b''
        for attempt in range(2):                 # 首次會談偶發空收，重試一次
            pad = br.tx(probe)
            expect = probe + b'\x00' * pad
            back = br.rx(len(expect), timeout=1.5)
            if back == expect:
                break
            br.flush()
        print(f'  送出 {expect.hex()}（含 {pad}B 填充） 收到 {back.hex()}')
        if back != expect:
            print('  迴環失敗 —— TX/RX 路徑有問題，中止')
            return 1
        print('  迴環 OK（TX 資料窗與事件 RX 都通）')
        br.loopback(False)
        br.flush()
        if a.loopback_only:
            return 0

        print('\n=== 2. 讀 L803K 現值 ===')
        cam = L800(br)
        info = cam.probe()
        print(f'  Gain CSR status=0x{info["status"]:02X}  raw={info["gain_raw"]}'
              f'  dB(公式)={info["gain_db_expect"]}'
              f'  abs LE={info["gain_db_le"]} BE={info["gain_db_be"]}'
              f'  → float {"LE" if info["float_le"] else "BE"}')
        gmin, gmax = cam.gain_limits()
        print(f'  Gain      = {cam.gain_db():.2f} dB   （範圍 {gmin:.2f} ~ {gmax:.2f}）')
        emin, emax = cam.expo_limits()
        print(f'  曝光      = {cam.expo_us():.2f} µs  （範圍 {emin:.2f} ~ {emax:.2f}）')
        pmin, pmax = cam.line_period_limits()
        lp = cam.line_period_us()
        print(f'  行週期    = {lp:.2f} µs = {1e6/lp:.0f} Hz （範圍 {pmin:.2f} ~ {pmax:.2f}）')
        print(f'  曝光模式  = 0x{cam.expo_mode():02X}（0x00=free-run programmable）')

        print('\n=== 3. 寫入驗證（改一點再還原）===')
        g0 = cam.gain_db()
        t = g0 + (0.5 if g0 + 0.5 <= gmax else -0.5)
        back = cam.set_gain_db(t)
        print(f'  Gain {g0:.2f} -> 寫 {t:.2f} -> 讀回 {back:.2f}')
        cam.set_gain_db(g0)
        print(f'  還原 -> {cam.gain_db():.2f} dB')

        for label, val, setter, getter in (
                ('Gain(dB)', a.set_gain_db, cam.set_gain_db, cam.gain_db),
                ('曝光(µs)', a.set_expo_us, cam.set_expo_us, cam.expo_us),
                ('行週期(µs)', a.set_line_period_us, cam.set_line_period_us,
                 cam.line_period_us)):
            if val is not None:
                back = setter(val)
                print(f'\n  使用者指定 {label} = {val} -> 讀回 {back:.2f}')
        return 0
    finally:
        try:
            br.close()
        except Exception:
            pass
        g.write_reg(a.ip, [(REG_CCP, 0)])
        print('\n已釋放（事件通知關閉、control channel 釋放）')


if __name__ == '__main__':
    sys.exit(main())
