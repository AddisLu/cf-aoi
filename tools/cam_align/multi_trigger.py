#!/usr/bin/env python3
"""量測多台 iPORT 一次性觸發的實際離散度（start spread）。

對應正式流程：Panel 進片 -> Control PC -> 一次 Grab_Start -> 全部相機同時取像。

量法：每台用獨立的接收埠，記錄「第一張影像的 leader 封包」抵達主機的時間，
台與台之間的差值就是實際離散度（含觸發送出間隔 + 網路延遲差異）。

觸發方式：
  --method burst       不等 ACK，連續送 WRITEREG AcquisitionStart（預設）
  --method sequential  每台送完等 ACK 再送下一台（當作對照組，會慢很多）

用法:
    python3 multi_trigger.py --frames 27 --height 5000
    python3 multi_trigger.py --method sequential   # 對照組
"""
import argparse
import os
import selectors
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gvcp_setip import Gvcp, REG_CCP, STATUS  # noqa: E402

REG_SCP0, REG_SCPS0, REG_SCDA0 = 0x0D00, 0x0D04, 0x0D18
REG_WIDTH, REG_HEIGHT = 0x12500, 0x12510
REG_ACQ_MODE = 0x13100
REG_ACQ_START, REG_ACQ_STOP = 0x13110, 0x13120
REG_ACQ_FRAME_COUNT = 0x20013438

ACQ_MODE_MULTIFRAME = 2
DO_NOT_FRAGMENT = 0x40000000
GVSP_LEADER, GVSP_TRAILER, GVSP_PAYLOAD = 1, 2, 3
WRITEREG_CMD = 0x0082
GVCP_PORT = 3956
HEARTBEAT_PERIOD = 1.0


class Camera:
    """一台 iPORT：自己的 control channel、接收埠、統計。"""

    def __init__(self, iface, srcip, devip):
        self.devip = devip
        self.g = Gvcp(iface, srcip)          # control channel（heartbeat 必須用這個）
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32 * 1024 * 1024)
        self.rx.bind((srcip, 0))
        self.rx.setblocking(False)
        self.port = self.rx.getsockname()[1]
        self.blocks = {}
        self.frames = 0
        self.lost = 0
        self.t_first = None                  # 第一個 leader 抵達時間
        self.t_last = None
        self.orig = None
        self.req_id = 0

    def take_control(self):
        r = self.g.write_reg(self.devip, [(REG_CCP, 2)])
        return r is not None and r[0] == 0

    def snapshot(self):
        self.orig = self.g.read_reg(self.devip, REG_SCP0, REG_SCPS0, REG_SCDA0,
                                    REG_ACQ_MODE, REG_HEIGHT)
        return self.orig

    def configure(self, srcip, height, frames, packet_size):
        o_scp = self.orig[0]
        for reg, val in ((REG_HEIGHT, height),
                         (REG_ACQ_MODE, ACQ_MODE_MULTIFRAME),
                         (REG_ACQ_FRAME_COUNT, frames),
                         (REG_SCPS0, DO_NOT_FRAGMENT | packet_size),
                         (REG_SCDA0, struct.unpack('>I', socket.inet_aton(srcip))[0]),
                         (REG_SCP0, (o_scp & ~0xFFFF) | self.port)):
            r = self.g.write_reg(self.devip, [(reg, val)])
            if r is None or r[0] != 0:
                return False, reg
        return True, None

    def fire_noack(self):
        """送出 AcquisitionStart 但不等 ACK —— 多台連發時這是關鍵。"""
        self.req_id = self.req_id % 0xfffe + 1
        pkt = (struct.pack('>BBHHH', 0x42, 0x00, WRITEREG_CMD, 8, self.req_id)
               + struct.pack('>II', REG_ACQ_START, 1))
        self.g.s.sendto(pkt, (self.devip, GVCP_PORT))

    def fire_ack(self):
        return self.g.write_reg(self.devip, [(REG_ACQ_START, 1)])

    def heartbeat(self):
        self.g.read_reg(self.devip, REG_CCP)

    def restore(self):
        o_scp, o_scps, o_scda, o_mode, o_height = self.orig
        self.g.write_reg(self.devip, [(REG_ACQ_STOP, 1)])
        for reg, val in ((REG_ACQ_MODE, o_mode), (REG_HEIGHT, o_height),
                         (REG_SCPS0, o_scps), (REG_SCP0, o_scp), (REG_SCDA0, o_scda),
                         (REG_CCP, 0)):
            self.g.write_reg(self.devip, [(reg, val)])
        self.rx.close()

    def handle(self, pkt, now):
        st = struct.unpack('>H', pkt[:2])[0]
        if len(pkt) < 20 or (st != 0 and (st & 0xC000) != 0x4000):
            return
        fmt_byte = pkt[4]
        fmt = fmt_byte & 0x0F
        if fmt_byte & 0x80:
            block_id = struct.unpack('>Q', pkt[8:16])[0]
            pid = struct.unpack('>I', pkt[16:20])[0]
        else:
            block_id = struct.unpack('>H', pkt[2:4])[0]
            pid = int.from_bytes(pkt[5:8], 'big')

        if fmt == GVSP_LEADER:
            if self.t_first is None:
                self.t_first = now
            self.blocks[block_id] = [0, 0]
        elif fmt == GVSP_PAYLOAD:
            b = self.blocks.get(block_id)
            if b is not None:
                b[0] += 1
                b[1] = max(b[1], pid)
        elif fmt == GVSP_TRAILER:
            b = self.blocks.pop(block_id, None)
            if b is not None:
                self.frames += 1
                self.lost += b[1] - b[0]
                self.t_last = now


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--iface', default='enp0s31f6')
    ap.add_argument('--srcip', default='192.168.5.2')
    ap.add_argument('--devices', nargs='*', default=None,
                    help='裝置 IP 清單；不給則自動探索')
    ap.add_argument('--frames', type=int, default=27)
    ap.add_argument('--height', type=int, default=5000)
    ap.add_argument('--packet-size', type=int, default=9000)
    ap.add_argument('--method', choices=['burst', 'sequential'], default='burst')
    ap.add_argument('--idle-timeout', type=float, default=8.0)
    a = ap.parse_args()

    if a.devices:
        ips = a.devices
    else:
        print('探索裝置中...')
        found = Gvcp(a.iface, a.srcip).discover()
        ips = sorted(found)
        for ip in ips:
            print(f'  {ip}  {found[ip]["model"]}  fw={found[ip]["version"]}')
    if not ips:
        print('找不到裝置')
        return 1
    print(f'\n共 {len(ips)} 台，觸發方式 = {a.method}\n')

    cams = [Camera(a.iface, a.srcip, ip) for ip in ips]
    try:
        print('=== 取得控制權並設定 ===')
        for c in cams:
            if not c.take_control():
                print(f'  {c.devip} 取得 control channel 失敗')
                return 1
            c.snapshot()
            ok, reg = c.configure(a.srcip, a.height, a.frames, a.packet_size)
            if not ok:
                print(f'  {c.devip} 設定失敗於 0x{reg:X}')
                return 1
            print(f'  {c.devip}  接收埠 {c.port}  Height={a.height}  '
                  f'FrameCount={a.frames}')

        sel = selectors.DefaultSelector()
        for c in cams:
            sel.register(c.rx, selectors.EVENT_READ, c)

        print(f'\n=== 觸發 ===')
        t_fire0 = time.perf_counter()
        if a.method == 'burst':
            for c in cams:
                c.fire_noack()
        else:
            for c in cams:
                c.fire_ack()
        t_fire1 = time.perf_counter()
        print(f'  觸發指令送出耗時 {(t_fire1-t_fire0)*1e6:.1f} us')

        last_hb = time.perf_counter()
        deadline = time.perf_counter() + a.idle_timeout
        while any(c.frames < a.frames for c in cams):
            now = time.perf_counter()
            if now - last_hb > HEARTBEAT_PERIOD:
                for c in cams:
                    c.heartbeat()
                last_hb = time.perf_counter()
            events = sel.select(timeout=0.2)
            if not events:
                if time.perf_counter() > deadline:
                    print('  逾時，提前結束')
                    break
                continue
            deadline = time.perf_counter() + a.idle_timeout
            for key, _ in events:
                c = key.data
                try:
                    while True:
                        pkt = c.rx.recv(a.packet_size + 64)
                        c.handle(pkt, time.perf_counter())
                except BlockingIOError:
                    pass

        print(f'\n=== 各台結果 ===')
        print(f'  {"裝置":<16} {"張數":>6} {"遺失":>6} {"首幀延遲":>12} {"取像耗時":>10}')
        firsts = [c.t_first for c in cams if c.t_first is not None]
        for c in cams:
            d = (c.t_first - t_fire0) * 1000 if c.t_first else float('nan')
            dur = (c.t_last - c.t_first) if (c.t_first and c.t_last) else float('nan')
            print(f'  {c.devip:<16} {c.frames:>6} {c.lost:>6} '
                  f'{d:>10.2f} ms {dur:>9.2f}s')

        print(f'\n=== 同步性 ===')
        if len(firsts) >= 2:
            spread = (max(firsts) - min(firsts)) * 1e6
            print(f'  首幀抵達離散度  {spread:.1f} us  '
                  f'({spread*13.7/1000:.1f} 行 @13.7kHz)')
        else:
            print(f'  只有 {len(firsts)} 台有資料，需要 2 台以上才能算離散度')
        print(f'  觸發送出耗時    {(t_fire1-t_fire0)*1e6:.1f} us')
        ok = all(c.frames == a.frames and c.lost == 0 for c in cams)
        print(f'  張數與丟包      {"全部正確" if ok else "有異常，見上表"}')
        return 0 if ok else 1
    finally:
        print('\n還原設定...')
        for c in cams:
            try:
                c.restore()
            except Exception as e:
                print(f'  {c.devip} 還原失敗: {e}')


if __name__ == '__main__':
    sys.exit(main())
