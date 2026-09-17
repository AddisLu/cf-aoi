#!/usr/bin/env python3
"""Camera Link 鏈路健康度即時表 — 邊動硬體邊看完整度變化。

每秒印一行：完整度 %（實收 payload / 應收）、幀數、掉行估計。
100% = CL 鏈路健康；隨機 80~90% = 訊號劣化；<5% = 資料對斷路。
（iPORT 測試圖樣可 100% 過 → 若本表低於 100%，問題必在 CL 線/接頭/相機輸出級）

用法:
    python3 cl_health.py 192.168.4.53          # Ctrl+C 結束
    python3 cl_health.py 192.168.4.54 --height 500
"""
import argparse
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gvcp_setip import Gvcp, REG_CCP  # noqa: E402

REG_SCP0, REG_SCPS0, REG_SCDA0 = 0x0D00, 0x0D04, 0x0D18
REG_WIDTH, REG_HEIGHT = 0x12500, 0x12510
REG_ACQ_START, REG_ACQ_STOP = 0x13110, 0x13120
DF = 0x40000000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('ip')
    ap.add_argument('--iface', default='enp1s0f1np1')
    ap.add_argument('--srcip', default='192.168.4.2')
    ap.add_argument('--height', type=int, default=1000)
    ap.add_argument('--packet', type=int, default=9000)
    a = ap.parse_args()

    g = Gvcp(a.iface, a.srcip)
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32 * 1024 * 1024)
    rx.bind((a.srcip, 0))
    port = rx.getsockname()[1]
    rx.settimeout(0.5)

    r = g.write_reg(a.ip, [(REG_CCP, 2)])
    if r is None or r[0] != 0:
        print(f'取得控制權失敗（eBUS Player 開著？）: {r}')
        return 1
    o = g.read_reg(a.ip, REG_SCP0, REG_SCPS0, REG_SCDA0, REG_HEIGHT)
    w = g.read_reg(a.ip, REG_WIDTH)[0]
    expect_pkts = (w * a.height + (a.packet - 36) - 1) // (a.packet - 36)
    print(f'{a.ip}  {w}x{a.height}  每幀應收 ~{expect_pkts} 包   Ctrl+C 結束')
    print(f'{"時間":>6} {"幀":>4} {"平均完整度":>8} {"最差":>6} {"最好":>6}   健康判讀')

    try:
        g.write_reg(a.ip, [(REG_HEIGHT, a.height)])
        g.write_reg(a.ip, [(REG_SCPS0, DF | a.packet)])
        g.write_reg(a.ip, [(REG_SCDA0, struct.unpack('>I', socket.inet_aton(a.srcip))[0])])
        g.write_reg(a.ip, [(REG_SCP0, (o[0] & ~0xFFFF) | port)])
        g.write_reg(a.ip, [(REG_ACQ_START, 1)])

        t0 = time.time()
        last_hb = last_report = t0
        cur = None
        frames = []
        while True:
            now = time.time()
            if now - last_hb > 1.0:
                g.read_reg(a.ip, REG_CCP)
                last_hb = now
            if now - last_report >= 1.0:
                if frames:
                    pct = [100.0 * f / expect_pkts for f in frames]
                    avg = sum(pct) / len(pct)
                    verdict = ('✔ 健康' if avg > 99.5 else
                               '△ 訊號劣化（隨機掉行）' if avg > 30 else
                               '✘ 幾乎斷路（資料對不通）')
                    print(f'{now-t0:6.0f} {len(frames):4d} {avg:7.1f}% '
                          f'{min(pct):5.1f}% {max(pct):5.1f}%   {verdict}')
                else:
                    print(f'{now-t0:6.0f}    0       —      —      —   ✘ 完全無資料')
                frames = []
                last_report = now
            try:
                pkt = rx.recv(a.packet + 64)
            except socket.timeout:
                continue
            st = struct.unpack('>H', pkt[:2])[0]
            if st != 0 and (st & 0xC000) != 0x4000:
                continue
            f = pkt[4] & 0x0F
            if f == 1:
                cur = 0
            elif f == 3 and cur is not None:
                cur += 1
            elif f == 2 and cur is not None:
                frames.append(cur)
                cur = None
    except KeyboardInterrupt:
        print('\n結束')
    finally:
        g.write_reg(a.ip, [(REG_ACQ_STOP, 1)])
        g.write_reg(a.ip, [(REG_HEIGHT, o[3])])
        g.write_reg(a.ip, [(REG_SCPS0, o[1])])
        g.write_reg(a.ip, [(REG_SCP0, o[0])])
        g.write_reg(a.ip, [(REG_SCDA0, o[2])])
        g.write_reg(a.ip, [(REG_CCP, 0)])
    return 0


if __name__ == '__main__':
    sys.exit(main())
