#!/usr/bin/env python3
"""探測 GigE Vision 裝置實際接受的 GVSP 封包大小上限。

ping -M do 測到的是裝置 ICMP 回應器的上限，不一定等於串流封包上限。
這支直接寫 GevSCPSPacketSize (0x0D04) 再讀回 —— 裝置若不接受某個大小，
會拒絕寫入或把值夾到它支援的上限，讀回值就是真正的答案。

全程會在結束時還原原始值。

用法: python3 gvsp_pktsize_probe.py <iface> <本機IP> <裝置IP>
"""
import socket
import struct
import sys

sys.path.insert(0, __file__.rsplit('/', 1)[0])
from gvcp_setip import Gvcp, REG_CCP, STATUS  # noqa: E402

REG_SCPS0 = 0x0D04      # Stream Channel 0 Packet Size
SCPS_SIZE_MASK = 0x0000FFFF

CANDIDATES = [576, 1500, 1512, 2048, 4096, 6000, 8192, 8960, 9000, 16384]


def main():
    iface, srcip, devip = sys.argv[1], sys.argv[2], sys.argv[3]
    g = Gvcp(iface, srcip)

    orig = g.read_reg(devip, REG_SCPS0)
    if orig is None:
        print('讀取 0x0D04 失敗（裝置可能不在線或不回應 READREG）')
        return 1
    orig = orig[0]
    print(f'原始 GevSCPSPacketSize = 0x{orig:08x}  (size={orig & SCPS_SIZE_MASK})')

    r = g.write_reg(devip, [(REG_CCP, 0x00000002)])
    if r is None or r[0] != 0:
        print(f'取得 control channel 失敗: {r}')
        return 1
    print('已取得 control channel\n')

    try:
        hdr = orig & ~SCPS_SIZE_MASK      # 保留 fire-test / do-not-fragment 等旗標
        print(f'{"要求":>8}  {"讀回":>8}   結果')
        best = 0
        for want in CANDIDATES:
            r = g.write_reg(devip, [(REG_SCPS0, hdr | want)])
            if r is None or r[0] != 0:
                code = f'0x{r[0]:04x} {STATUS.get(r[0], "?")}' if r else 'no ACK'
                print(f'{want:>8}  {"-":>8}   拒絕寫入 ({code})')
                continue
            got = g.read_reg(devip, REG_SCPS0)
            got = got[0] & SCPS_SIZE_MASK if got else -1
            ok = got == want
            if ok:
                best = max(best, got)
            print(f'{want:>8}  {got:>8}   {"接受" if ok else "被夾到 %d" % got}')

        print(f'\n裝置接受的最大 GVSP 封包: {best} bytes')
        if best > 1500:
            payload = best - 20 - 8 - 8      # IP + UDP + GVSP header
            eff = payload / (best + 14 + 4 + 8 + 12)   # + Eth hdr/FCS/preamble/IFG
            print(f'  -> 每包影像資料 {payload} bytes，線路效率 {eff*100:.1f}%'
                  f'，1GigE 可用頻寬約 {125 * eff:.0f} MB/s')
        else:
            payload = 1500 - 20 - 8 - 8
            eff = payload / (1500 + 38)
            print(f'  -> 只能用標準 frame：每包 {payload} bytes，效率 {eff*100:.1f}%'
                  f'，1GigE 可用頻寬約 {125 * eff:.0f} MB/s')
    finally:
        g.write_reg(devip, [(REG_SCPS0, orig)])
        back = g.read_reg(devip, REG_SCPS0)
        print(f'\n已還原 0x0D04 = 0x{back[0]:08x}' if back else '\n還原後讀回失敗')
        g.write_reg(devip, [(REG_CCP, 0x00000000)])
        print('已釋放 control channel')
    return 0


if __name__ == '__main__':
    sys.exit(main())
