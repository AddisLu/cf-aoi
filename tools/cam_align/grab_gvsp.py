#!/usr/bin/env python3
"""直接用 GVSP 從 iPORT CL-GigE 取像 —— 不需要 eBUS SDK，也不需要 root。

為什麼不用 aravis：iPORT 韌體 1.03.03.109 不接受 GVCP WRITEMEM_CMD，
而 aravis 所有暫存器寫入都走 WRITEMEM，因此 arv-tool 的寫入會被靜默忽略、
create_stream 直接失敗。改用 WRITEREG_CMD 就一切正常（本檔的做法）。

流程：
  1. WRITEREG CCP 取得 control channel（heartbeat 由主迴圈用同一 socket 送）
  2. 設定 stream channel：SCDA=主機 IP、SCP=主機埠、SCPS=封包大小
  3. WRITEREG AcquisitionStart
  4. 收 GVSP 封包（leader / payload / trailer）重組成影像
  5. AcquisitionStop、還原暫存器、釋放 control channel

用法:
    python3 grab_gvsp.py [--frames N] [--height H] [--packet-size S]
                         [--out PREFIX] [--timeout SEC]
"""
import argparse
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gvcp_setip import Gvcp, REG_CCP, STATUS  # noqa: E402

# GigE Vision bootstrap
REG_SCP0 = 0x0D00
REG_SCPS0 = 0x0D04
REG_SCDA0 = 0x0D18
REG_HEARTBEAT = 0x0938

# iPORT GenICam 暫存器（由裝置自己的 XML 解出，見 README）
REG_WIDTH = 0x12500
REG_HEIGHT = 0x12510
REG_PIXELFORMAT = 0x12540
REG_ACQ_START = 0x13110
REG_ACQ_STOP = 0x13120

DO_NOT_FRAGMENT = 0x40000000

GVSP_LEADER, GVSP_TRAILER, GVSP_PAYLOAD = 1, 2, 3

PIXEL_FORMATS = {0x01080001: ('Mono8', 1)}


# GVCP heartbeat 必須從持有 control channel 的那個 socket 發出 —— 裝置以
# 來源 IP + 來源埠 認定控制端。另開 thread 用新 socket 讀暫存器不算數，
# 3 秒後控制權被收回、串流中斷（症狀：固定在 5.1 秒停止）。
HEARTBEAT_PERIOD = 1.0


def parse_leader(body):
    payload_type = struct.unpack('>H', body[2:4])[0]
    pixel_format = struct.unpack('>I', body[12:16])[0]
    size_x, size_y = struct.unpack('>II', body[16:24])
    return payload_type, pixel_format, size_x, size_y


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--iface', default='enp0s31f6')
    ap.add_argument('--srcip', default='192.168.5.2')
    ap.add_argument('--devip', default='192.168.5.10')
    ap.add_argument('--frames', type=int, default=3)
    ap.add_argument('--height', type=int, default=None)
    ap.add_argument('--packet-size', type=int, default=9000)
    ap.add_argument('--out', default='grab')
    ap.add_argument('--timeout', type=float, default=10.0)
    a = ap.parse_args()

    g = Gvcp(a.iface, a.srcip)
    D = a.devip

    print('=== 裝置狀態 ===')
    v = g.read_reg(D, REG_WIDTH, REG_HEIGHT, REG_PIXELFORMAT)
    if v is None:
        print('讀取失敗，裝置沒回應')
        return 1
    width, height, pixfmt = v
    name, bpp = PIXEL_FORMATS.get(pixfmt, (f'0x{pixfmt:08x}', 1))
    print(f'  Width={width}  Height={height}  PixelFormat={name}')
    if not width:
        print('  Width=0，Camera Link 沒有有效訊號')
        return 1

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32 * 1024 * 1024)
    rx.bind((a.srcip, 0))
    port = rx.getsockname()[1]
    rx.settimeout(a.timeout)
    print(f'  接收埠 {a.srcip}:{port}  '
          f'SO_RCVBUF={rx.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)//2//1024//1024} MB')

    orig = g.read_reg(D, REG_SCP0, REG_SCPS0, REG_SCDA0)
    o_scp, o_scps, o_scda = orig

    r = g.write_reg(D, [(REG_CCP, 2)])
    if r is None or r[0] != 0:
        print(f'取得 control channel 失敗: {r}')
        return 1
    print('  control channel 已取得')

    try:
        print('\n=== 設定 ===')
        if a.height and a.height != height:
            g.write_reg(D, [(REG_HEIGHT, a.height)])
            height = g.read_reg(D, REG_HEIGHT)[0]
            print(f'  Height -> {height}')

        for reg, val, nm in (
                (REG_SCPS0, DO_NOT_FRAGMENT | a.packet_size, 'SCPS'),
                (REG_SCDA0, struct.unpack('>I', socket.inet_aton(a.srcip))[0], 'SCDA'),
                (REG_SCP0, (o_scp & ~0xFFFF) | port, 'SCP')):
            r = g.write_reg(D, [(reg, val)])
            if r is None or r[0] != 0:
                print(f'  寫 {nm} 失敗: {r}')
                return 1
        print(f'  封包大小 {a.packet_size}，串流目的地 {a.srcip}:{port}')

        frame_bytes = width * height * bpp
        print(f'  每幀 {width}x{height} = {frame_bytes} bytes ({frame_bytes/1e6:.1f} MB)')

        print(f'\n=== 取像（{a.frames} 幀）===')
        r = g.write_reg(D, [(REG_ACQ_START, 1)])
        if r is None or r[0] != 0:
            print(f'  AcquisitionStart 失敗: {r}')
            return 1

        blocks, saved = {}, []
        t0 = time.time()
        last_hb = t0
        n_pkt = 0
        try:
            while len(saved) < a.frames:
                if time.time() - last_hb > HEARTBEAT_PERIOD:
                    g.read_reg(D, REG_CCP)      # 同一 socket，才算有效 heartbeat
                    last_hb = time.time()
                try:
                    pkt = rx.recv(a.packet_size + 64)
                except socket.timeout:
                    print(f'  逾時 {a.timeout}s，收到 {n_pkt} 個封包')
                    break
                n_pkt += 1
                if len(pkt) < 8:
                    continue
                status = struct.unpack('>H', pkt[:2])[0]
                if status != 0 and (status & 0xC000) != 0x4000:
                    continue          # 0x4xxx 警告類（資料有效）放行
                fmt_byte = pkt[4]
                fmt = fmt_byte & 0x0F
                if fmt_byte & 0x80:      # extended ID：64-bit block_id + 32-bit packet_id
                    if len(pkt) < 20:
                        continue
                    block_id = struct.unpack('>Q', pkt[8:16])[0]
                    pid = struct.unpack('>I', pkt[16:20])[0]
                    body = pkt[20:]
                else:                    # 標準 ID：16-bit block_id + 24-bit packet_id
                    block_id = struct.unpack('>H', pkt[2:4])[0]
                    pid = int.from_bytes(pkt[5:8], 'big')
                    body = pkt[8:]

                if fmt == GVSP_LEADER:
                    ptype, pf, sx, sy = parse_leader(body)
                    if ptype != 1 or not (0 < sx <= 65536) or not (0 < sy <= 65536):
                        print(f'  略過異常 leader: type={ptype} {sx}x{sy}')
                        continue
                    blocks[block_id] = {'parts': {}, 'w': sx, 'h': sy, 'pf': pf}
                elif fmt == GVSP_PAYLOAD:
                    b = blocks.get(block_id)
                    if b is not None:
                        b['parts'][pid] = body
                elif fmt == GVSP_TRAILER:
                    b = blocks.pop(block_id, None)
                    if b is None:
                        continue
                    ids = sorted(b['parts'])
                    data = b''.join(b['parts'][i] for i in ids)
                    expect = b['w'] * b['h'] * bpp
                    missing = ids[-1] - len(ids) if ids else 0
                    print(f'  幀 block_id={block_id}  {b["w"]}x{b["h"]}  '
                          f'{len(data)}/{expect} bytes  封包={len(ids)}  遺失={missing}  '
                          f't={time.time()-t0:.2f}s')
                    saved.append((data, b['w'], b['h'], expect))
            elapsed = time.time() - t0
        finally:
            g.write_reg(D, [(REG_ACQ_STOP, 1)])

        total_mb = sum(len(d) for d, *_ in saved) / 1e6
        print(f'\n收到 {n_pkt} 個封包，{len(saved)} 幀，{total_mb:.1f} MB'
              f'，耗時 {elapsed:.2f}s' +
              (f'，平均 {total_mb/elapsed:.1f} MB/s ({total_mb*8/elapsed/1000:.2f} Gb/s)'
               if elapsed > 0 and saved else ''))

        if not saved:
            print('\n沒收到影像。Camera Link 端可能沒有訊號輸入。')
            return 1

        print('\n=== 存檔與亮度統計 ===')
        from PIL import Image
        for i, (data, w, h, expect) in enumerate(saved):
            with open(f'{a.out}_{i:02d}.raw', 'wb') as f:
                f.write(data)
            if len(data) < expect:
                data = data + b'\x00' * (expect - len(data))
            img = Image.frombytes('L', (w, h), data[:w * h])
            png = f'{a.out}_{i:02d}.png'
            img.save(png)
            pw = min(w, 1600)
            img.resize((pw, max(1, int(h * pw / w))), Image.NEAREST).save(
                f'{a.out}_{i:02d}_preview.png')
            hist = img.histogram()
            total = sum(hist)
            mean = sum(v * c for v, c in enumerate(hist)) / total
            nz = [v for v, c in enumerate(hist) if c]
            print(f'  {png}  {w}x{h}')
            print(f'      平均={mean:.1f}  最小={min(nz)}  最大={max(nz)}  '
                  f'暗區(<10)={sum(hist[:10])/total*100:.1f}%')
        print(f'\n預覽圖：{a.out}_00_preview.png')
        return 0
    finally:
        g.write_reg(D, [(REG_SCPS0, o_scps)])
        g.write_reg(D, [(REG_SCP0, o_scp)])
        g.write_reg(D, [(REG_SCDA0, o_scda)])
        g.write_reg(D, [(REG_CCP, 0)])
        print('已還原 stream channel 暫存器並釋放 control channel')
        rx.close()


if __name__ == '__main__':
    sys.exit(main())
