#!/usr/bin/env python3
"""逐步定位：序列橋開場序列中哪一個暫存器寫入會讓 iPORT 的 GVCP 卡死。

在剛上電的乾淨裝置上跑。每寫一個暫存器 → 立即 READREG 健檢 →
一旦裝置停止回應，最後那筆寫入就是兇手。

用法: python3 bisect_serial.py 192.168.4.54
"""
import socket
import struct
import sys
import time

sys.path.insert(0, '/home/damac/Addis/iport')
from gvcp_setip import Gvcp, REG_CCP, STATUS  # noqa: E402

STEPS = [
    ('CCP=2 取控制權',            REG_CCP,     2),
    ('Bulk0Mode=UART',           0x20017800,  0),
    ('Baud=9600',                0x20017814,  0),
    ('Parity=None',              0x20017818,  0),
    ('StopBits=One',             0x2001781C,  0),
    ('Loopback=0',               0x20017830,  0),
    ('Watermark=1',              0x20017810,  1),
    ('RxTimeout=66667',          0x20017840,  66667),
    ('SoftReset=1',              0x20017848,  1),
    ('SoftReset=0',              0x20017848,  0),
    ('MCDA=本機',                 0x0B10,      None),   # 特殊：填本機 IP
    ('MCP=偵聽埠',                0x0B00,      None),   # 特殊：填埠號
    ('MCTT=200',                 0x0B14,      200),
    ('MCRC=2',                   0x0B18,      2),
    ('EV_Bulk0=1',               0x00016000,  1),
    ('EV_Bulk0=0（消毒式關閉）',   0x00016000,  0),
    ('MCP=0（消毒式歸零）',        0x0B00,      0),
    ('CCP=0 釋放',                REG_CCP,     0),
]


def main():
    ip = sys.argv[1] if len(sys.argv) > 1 else '192.168.4.54'
    srcip, iface = '192.168.4.2', 'enp1s0f1np1'
    g = Gvcp(iface, srcip)
    ev = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ev.bind((srcip, 0))
    port = ev.getsockname()[1]

    def health():
        """READREG Width 三連發，容忍單次逾時。"""
        ok = 0
        for _ in range(3):
            if g.read_reg(ip, 0x12500) is not None:
                ok += 1
        return ok

    h = health()
    print(f'起始健檢: {h}/3 {"（裝置乾淨可測）" if h == 3 else "（裝置已卡死，請先斷電重開再跑）"}')
    if h < 3:
        return 1

    for name, reg, val in STEPS:
        if val is None:
            val = (struct.unpack('>I', socket.inet_aton(srcip))[0]
                   if reg == 0x0B10 else port)
        r = g.write_reg(ip, [(reg, val)])
        wr = STATUS.get(r[0], hex(r[0])) if r else '無ACK'
        h = health()
        flag = '' if h == 3 else ('  ← ← ← 兇手！GVCP 從這步開始不回' if h == 0
                                  else f'  （不穩：{h}/3）')
        print(f'  {name:24s} 寫入={wr:12s} 健檢={h}/3{flag}', flush=True)
        if h == 0:
            return 1
        time.sleep(0.3)
    print('全序列走完，裝置仍健康 —— 單獨寫入皆無害，問題在時序/併發')
    return 0


if __name__ == '__main__':
    sys.exit(main())
