#!/usr/bin/env python3
"""用 GigE Vision test packet 機制實測 jumbo frame 能不能真的送達。

寫暫存器只證明裝置「接受」某個封包大小；這支設定 stream channel 目的地為本機，
然後用 GevSCPSPacketSize 的 Fire Test Packet 旗標讓裝置實際送一個該大小的封包，
確認它真的到得了主機。這正是 eBUS / pylon 自動協商封包大小的做法。

結束時會還原所有動過的暫存器。

用法: python3 gvsp_testpacket.py <iface> <本機IP> <裝置IP>
"""
import socket
import struct
import sys
import time

sys.path.insert(0, __file__.rsplit('/', 1)[0])
from gvcp_setip import Gvcp, REG_CCP, STATUS  # noqa: E402

REG_SCP0 = 0x0D00       # Stream Channel 0 Port
REG_SCPS0 = 0x0D04      # Stream Channel 0 Packet Size
REG_SCDA0 = 0x0D18      # Stream Channel 0 Destination Address

FIRE_TEST = 0x80000000
DO_NOT_FRAGMENT = 0x40000000
SIZE_MASK = 0x0000FFFF

SIZES = [1500, 2048, 4096, 6000, 8192, 8960, 9000]


def main():
    iface, srcip, devip = sys.argv[1], sys.argv[2], sys.argv[3]
    g = Gvcp(iface, srcip)

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind((srcip, 0))
    rx.settimeout(1.0)
    port = rx.getsockname()[1]
    print(f'本機接收埠 {srcip}:{port}\n')

    orig = g.read_reg(devip, REG_SCP0, REG_SCPS0, REG_SCDA0)
    if orig is None:
        print('讀取 stream channel 暫存器失敗')
        return 1
    o_scp, o_scps, o_scda = orig
    print(f'原始  SCP0=0x{o_scp:08x}  SCPS0=0x{o_scps:08x}  SCDA0=0x{o_scda:08x}')

    r = g.write_reg(devip, [(REG_CCP, 0x00000002)])
    if r is None or r[0] != 0:
        print(f'取得 control channel 失敗: {r}')
        return 1
    print('已取得 control channel\n')

    try:
        dst = struct.unpack('>I', socket.inet_aton(srcip))[0]
        for reg, val, name in ((REG_SCDA0, dst, 'SCDA0'),
                               (REG_SCP0, (o_scp & ~0xFFFF) | port, 'SCP0')):
            r = g.write_reg(devip, [(reg, val)])
            if r is None or r[0] != 0:
                print(f'寫 {name} 失敗: {r}')
                return 1

        print(f'{"大小":>6}  {"實收":>6}   結果')
        best = 0
        for size in SIZES:
            while True:                       # 清掉殘留封包
                try:
                    rx.recvfrom(65535)
                except socket.timeout:
                    break
            r = g.write_reg(devip, [(REG_SCPS0, FIRE_TEST | DO_NOT_FRAGMENT | size)])
            if r is None or r[0] != 0:
                code = f'0x{r[0]:04x} {STATUS.get(r[0], "?")}' if r else 'no ACK'
                print(f'{size:>6}  {"-":>6}   寫入被拒 ({code})')
                continue
            try:
                data, _ = rx.recvfrom(65535)
                got = len(data) + 28          # UDP payload + IP/UDP header = 該封包大小
                ok = abs(got - size) <= 8
                if ok:
                    best = max(best, size)
                print(f'{size:>6}  {got:>6}   {"到達" if ok else "大小不符"}')
            except socket.timeout:
                print(f'{size:>6}  {"-":>6}   未收到（此大小送不過去）')

        print()
        if best >= 8192:
            payload = best - 20 - 8 - 8
            eff = payload / (best + 38)
            print(f'jumbo frame 實測可用，最大 {best} bytes')
            print(f'  每包影像資料 {payload} bytes，線路效率 {eff*100:.1f}%'
                  f'，1GigE 可用頻寬約 {125 * eff:.0f} MB/s')
        elif best:
            print(f'實測最大只到 {best} bytes')
        else:
            print('沒有任何大小的 test packet 到達 —— 裝置可能不支援此機制，'
                  '需改用 eBUS Player 實際串流驗證')
    finally:
        g.write_reg(devip, [(REG_SCPS0, o_scps)])
        g.write_reg(devip, [(REG_SCP0, o_scp)])
        g.write_reg(devip, [(REG_SCDA0, o_scda)])
        back = g.read_reg(devip, REG_SCP0, REG_SCPS0, REG_SCDA0)
        if back:
            print(f'\n已還原  SCP0=0x{back[0]:08x}  SCPS0=0x{back[1]:08x}  '
                  f'SCDA0=0x{back[2]:08x}')
        g.write_reg(devip, [(REG_CCP, 0x00000000)])
        print('已釋放 control channel')
        rx.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
