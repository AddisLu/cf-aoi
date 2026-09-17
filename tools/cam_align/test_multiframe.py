#!/usr/bin/env python3
"""驗證 MultiFrame 取像：一次 Grab_Start -> 取滿 N 張 -> 裝置自己停。

對應正式流程：Panel 進片訊號 -> Control PC -> Grab_Start -> free run 取 N 張後停止。

與 grab_gvsp.py 的差別：
  - AcquisitionMode 設為 MultiFrame、AcquisitionFrameCount 設為 N，
    由裝置自行停止，軟體不需要送 AcquisitionStop
  - 只保留第一張影像，其餘僅統計（27 x 40.8 MB = 1.1 GB，全存會吃光記憶體）

用法:
    python3 test_multiframe.py [--frames 27] [--height 5000] [--out panel]
"""
import argparse
import os
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

ACQ_MODE_CONTINUOUS, ACQ_MODE_MULTIFRAME = 0, 2
DO_NOT_FRAGMENT = 0x40000000
GVSP_LEADER, GVSP_TRAILER, GVSP_PAYLOAD = 1, 2, 3


# GVCP heartbeat 必須從「持有 control channel 的那個 socket」發出 ——
# 裝置是以來源 IP + 來源埠 認定控制端。用另一條 thread 開新 socket 去讀
# 暫存器不算數，3 秒後控制權就會被收回、串流中斷（症狀：固定在 5.1 秒停止）。
# 因此改由主迴圈用同一個 Gvcp 實例定期發送。
HEARTBEAT_PERIOD = 1.0


def st(r):
    return f'0x{r[0]:04x} {STATUS.get(r[0], "?")}' if r else 'no ACK'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--iface', default='enp0s31f6')
    ap.add_argument('--srcip', default='192.168.5.2')
    ap.add_argument('--devip', default='192.168.5.10')
    ap.add_argument('--frames', type=int, default=27)
    ap.add_argument('--height', type=int, default=5000)
    ap.add_argument('--packet-size', type=int, default=9000)
    ap.add_argument('--out', default='panel')
    ap.add_argument('--idle-timeout', type=float, default=5.0,
                    help='連續多久沒收到封包就視為結束')
    ap.add_argument('--mode', type=int, default=ACQ_MODE_MULTIFRAME,
                    help='AcquisitionMode: 0=Continuous 2=MultiFrame '
                         '3=ContinuousRecording 7=MultiFrameRecording')
    ap.add_argument('--stop-in-software', action='store_true',
                    help='收滿 N 張後由軟體送 AcquisitionStop（Continuous 模式用）')
    a = ap.parse_args()

    g = Gvcp(a.iface, a.srcip)
    D = a.devip

    width = g.read_reg(D, REG_WIDTH)
    if width is None:
        print('裝置沒回應')
        return 1
    width = width[0]

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32 * 1024 * 1024)
    rx.bind((a.srcip, 0))
    port = rx.getsockname()[1]
    rx.settimeout(a.idle_timeout)

    orig = g.read_reg(D, REG_SCP0, REG_SCPS0, REG_SCDA0, REG_ACQ_MODE, REG_HEIGHT)
    o_scp, o_scps, o_scda, o_mode, o_height = orig

    r = g.write_reg(D, [(REG_CCP, 2)])
    if r is None or r[0] != 0:
        print(f'取得 control channel 失敗: {st(r)}')
        return 1

    try:
        print('=== 設定 ===')
        for reg, val, nm in (
                (REG_HEIGHT, a.height, 'Height'),
                (REG_ACQ_MODE, a.mode, f'AcquisitionMode={a.mode}'),
                (REG_ACQ_FRAME_COUNT, a.frames, 'AcquisitionFrameCount'),
                (REG_SCPS0, DO_NOT_FRAGMENT | a.packet_size, 'GevSCPSPacketSize'),
                (REG_SCDA0, struct.unpack('>I', socket.inet_aton(a.srcip))[0], 'SCDA'),
                (REG_SCP0, (o_scp & ~0xFFFF) | port, 'SCP')):
            r = g.write_reg(D, [(reg, val)])
            if r is None or r[0] != 0:
                print(f'  寫 {nm} 失敗: {st(r)}')
                return 1
        got = g.read_reg(D, REG_HEIGHT, REG_ACQ_MODE, REG_ACQ_FRAME_COUNT)
        print(f'  Height={got[0]}  AcquisitionMode={got[1]}  '
              f'FrameCount={got[2]}')
        frame_bytes = width * got[0]
        print(f'  每張 {width}x{got[0]} = {frame_bytes/1e6:.1f} MB，'
              f'{a.frames} 張共 {frame_bytes*a.frames/1e9:.2f} GB')

        print(f'\n=== Grab_Start（單一次 AcquisitionStart）===')
        blocks, done = {}, []
        first_frame = None
        t_last = 0.0
        n_pkt = 0
        r = g.write_reg(D, [(REG_ACQ_START, 1)])
        if r is None or r[0] != 0:
            print(f'  AcquisitionStart 失敗: {st(r)}')
            return 1
        t0 = time.time()
        last_hb = t0

        while True:
            # 用持有 control channel 的同一個 socket 送 heartbeat
            if time.time() - last_hb > HEARTBEAT_PERIOD:
                g.read_reg(D, REG_CCP)
                last_hb = time.time()
            try:
                pkt = rx.recv(a.packet_size + 64)
            except socket.timeout:
                break
            n_pkt += 1
            st = struct.unpack('>H', pkt[:2])[0]
            if len(pkt) < 20 or (st != 0 and (st & 0xC000) != 0x4000):
                continue
            fmt_byte = pkt[4]
            fmt = fmt_byte & 0x0F
            if fmt_byte & 0x80:
                block_id = struct.unpack('>Q', pkt[8:16])[0]
                pid = struct.unpack('>I', pkt[16:20])[0]
                body = pkt[20:]
            else:
                block_id = struct.unpack('>H', pkt[2:4])[0]
                pid = int.from_bytes(pkt[5:8], 'big')
                body = pkt[8:]

            if fmt == GVSP_LEADER:
                sx, sy = struct.unpack('>II', body[16:24])
                blocks[block_id] = {'n': 0, 'max': 0, 'w': sx, 'h': sy,
                                    'data': [] if not done else None}
            elif fmt == GVSP_PAYLOAD:
                b = blocks.get(block_id)
                if b is not None:
                    b['n'] += 1
                    b['max'] = max(b['max'], pid)
                    if b['data'] is not None:
                        b['data'].append(body)
            elif fmt == GVSP_TRAILER:
                b = blocks.pop(block_id, None)
                if b is None:
                    continue
                lost = b['max'] - b['n']
                done.append((block_id, b['n'], lost, b['w'], b['h']))
                t_last = time.time()
                if first_frame is None and b['data']:
                    first_frame = (b''.join(b['data']), b['w'], b['h'])
                print(f'  第 {len(done):2d} 張  block_id={block_id}  '
                      f'{b["w"]}x{b["h"]}  封包={b["n"]}  遺失={lost}  '
                      f't={time.time()-t0:.2f}s')
                if len(done) >= a.frames:
                    if a.stop_in_software:
                        g.write_reg(D, [(REG_ACQ_STOP, 1)])
                        print('  → 軟體送出 AcquisitionStop')
                    break
        elapsed = t_last - t0 if done else time.time() - t0

        print(f'\n=== 結果 ===')
        total_lost = sum(d[2] for d in done)
        bytes_rx = sum(d[3] * d[4] for d in done)
        print(f'  收到張數      {len(done)} / 預期 {a.frames}  '
              f'{"OK" if len(done) == a.frames else "不符"}')
        print(f'  封包遺失      {total_lost}')
        print(f'  取像耗時      {elapsed:.2f}s（不含結束後的等待）')
        if elapsed > 0:
            print(f'  平均          {bytes_rx/elapsed/1e6:.1f} MB/s '
                  f'({sum(d[4] for d in done)/elapsed/1000:.2f} kHz line rate)')
        if blocks:
            print(f'  未完成的 block {sorted(blocks)}（收到 leader 但沒等到 trailer）')

        # 裝置是否自行停止：再等一小段確認沒有多餘影像
        print(f'\n=== 確認裝置自行停止（不送 AcquisitionStop）===')
        rx.settimeout(2.0)
        extra = 0
        try:
            while True:
                rx.recv(a.packet_size + 64)
                extra += 1
        except socket.timeout:
            pass
        print(f'  結束後 2 秒內多收到 {extra} 個封包  '
              f'{"→ 已自行停止" if extra == 0 else "→ 仍在送，需檢查"}')

        if first_frame:
            from PIL import Image
            data, w, h = first_frame
            img = Image.frombytes('L', (w, h), data[:w * h])
            img.save(f'{a.out}_first.png')
            pw = min(w, 1600)
            img.resize((pw, max(1, int(h * pw / w))), Image.NEAREST).save(
                f'{a.out}_first_preview.png')
            hist = img.histogram()
            tot = sum(hist)
            nz = [v for v, c in enumerate(hist) if c]
            print(f'\n  第一張已存 {a.out}_first.png  {w}x{h}  '
                  f'平均={sum(v*c for v, c in enumerate(hist))/tot:.1f} '
                  f'範圍={min(nz)}~{max(nz)}')
        return 0 if len(done) == a.frames and total_lost == 0 else 1
    finally:
        g.write_reg(D, [(REG_ACQ_STOP, 1)])
        g.write_reg(D, [(REG_ACQ_MODE, o_mode)])
        g.write_reg(D, [(REG_HEIGHT, o_height)])
        g.write_reg(D, [(REG_SCPS0, o_scps)])
        g.write_reg(D, [(REG_SCP0, o_scp)])
        g.write_reg(D, [(REG_SCDA0, o_scda)])
        g.write_reg(D, [(REG_CCP, 0)])
        print('已還原暫存器並釋放 control channel')
        rx.close()


if __name__ == '__main__':
    sys.exit(main())
