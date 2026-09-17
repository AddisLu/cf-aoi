#!/usr/bin/env python3
"""GVCP 探索診斷版 — 不過濾，收到什麼就 dump 什麼。

用來分辨「裝置沒回應」與「有回應但格式不符預期」。
同時監聽 ephemeral port 與 3956，並重試多輪。

用法: python3 gvcp_probe_raw.py <iface> <src_ip> [rounds]
"""
import socket
import struct
import sys
import time

GVCP_PORT = 3956
STATUS = {0x0000: 'SUCCESS', 0x8001: 'NOT_IMPLEMENTED', 0x8002: 'INVALID_PARAMETER',
          0x8003: 'INVALID_ADDRESS', 0x8004: 'WRITE_PROTECT', 0x8005: 'BAD_ALIGNMENT',
          0x8006: 'ACCESS_DENIED', 0x8007: 'BUSY', 0x800B: 'MSG_TIMEOUT',
          0x800E: 'INVALID_HEADER', 0x800F: 'WRONG_CONFIG'}
CMD = {0x0003: 'DISCOVERY_ACK', 0x0005: 'FORCEIP_ACK', 0x0081: 'READREG_ACK'}


def hexdump(b, limit=320):
    out = []
    for off in range(0, min(len(b), limit), 16):
        chunk = b[off:off + 16]
        hexs = ' '.join('%02x' % c for c in chunk)
        text = ''.join(chr(c) if 32 <= c < 127 else '.' for c in chunk)
        out.append(f'    {off:04x}  {hexs:<47}  {text}')
    if len(b) > limit:
        out.append(f'    ... 共 {len(b)} bytes')
    return '\n'.join(out)


def decode(data):
    if len(data) < 8:
        return '  (太短，非 GVCP ACK)'
    status, ack_cmd, length, ack_id = struct.unpack('>HHHH', data[:8])
    lines = [f'  status=0x{status:04x} ({STATUS.get(status, "?")})  '
             f'cmd=0x{ack_cmd:04x} ({CMD.get(ack_cmd, "?")})  '
             f'payload_len={length}  ack_id={ack_id}']
    p = data[8:]
    if ack_cmd == 0x0003 and len(p) >= 0xf8:
        def z(o, n):
            return p[o:o + n].split(b'\x00')[0].decode('latin-1').strip()
        lines += [
            f'  MAC          {":".join("%02x" % b for b in p[0x0a:0x10])}',
            f'  IP           {socket.inet_ntoa(p[0x24:0x28])}',
            f'  Subnet       {socket.inet_ntoa(p[0x34:0x38])}',
            f'  Gateway      {socket.inet_ntoa(p[0x44:0x48])}',
            f'  Manufacturer {z(0x48, 32)}',
            f'  Model        {z(0x68, 32)}',
            f'  Version      {z(0x88, 32)}',
            f'  Serial       {z(0xd8, 16)}',
            f'  UserID       {z(0xe8, 16)}',
        ]
    elif ack_cmd == 0x0003:
        lines.append(f'  DISCOVERY_ACK 但 payload 只有 {len(p)} bytes（預期 248）')
    return '\n'.join(lines)


def main():
    ifname = sys.argv[1] if len(sys.argv) > 1 else 'enp0s31f6'
    srcip = sys.argv[2] if len(sys.argv) > 2 else '192.168.5.2'
    rounds = int(sys.argv[3]) if len(sys.argv) > 3 else 3

    socks = []
    # a) ephemeral port（GVCP ACK 正常會回到這裡）
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind((srcip, 0))
    socks.append(('ephemeral:%d' % s.getsockname()[1], s))
    # b) 3956（少數實作回到固定埠）
    try:
        s2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s2.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s2.bind(('0.0.0.0', GVCP_PORT))
        socks.append(('0.0.0.0:3956', s2))
    except OSError as e:
        print(f'(無法綁 3956: {e})')

    for _, sk in socks:
        sk.settimeout(0.3)

    got = 0
    for rnd in range(1, rounds + 1):
        req_id = rnd
        pkt = struct.pack('>BBHHH', 0x42, 0x11, 0x0002, 0x0000, req_id)
        print(f'--- 第 {rnd} 輪 (req_id={req_id}) ---')
        for dest in ('255.255.255.255', srcip.rsplit('.', 1)[0] + '.255'):
            try:
                socks[0][1].sendto(pkt, (dest, GVCP_PORT))
                print(f'  送出 -> {dest}:{GVCP_PORT}')
            except OSError as e:
                print(f'  送往 {dest} 失敗: {e}')
        deadline = time.time() + 3.0
        while time.time() < deadline:
            for label, sk in socks:
                try:
                    data, addr = sk.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    continue
                got += 1
                print(f'  [{label}] 收到 {len(data)} bytes 來自 {addr[0]}:{addr[1]}')
                print(decode(data))
                print(hexdump(data))
    print()
    print(f'總共收到 {got} 個 UDP 封包')
    if got == 0:
        print('UDP 層完全沒回應。若網卡 RX 計數有增加，代表回的是 ARP 而非 GVCP ACK,')
        print('通常表示裝置 IP 不在 192.168.5.0/24（例如 fallback 到 169.254.x.x）。')
    return 0 if got else 1


if __name__ == '__main__':
    sys.exit(main())
