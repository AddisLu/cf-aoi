#!/usr/bin/env python3
"""GigE Vision (GVCP) 裝置探索 — 不需要 eBUS SDK、不需要 root。

對每張網卡送 GVCP DISCOVERY_CMD 到 255.255.255.255:3956，列出所有回應的
GigE Vision 裝置 —— 包含 IP 不在本機網段的裝置（例如 fallback 到
169.254.x.x link-local 的 iPORT）。

實作重點：socket bind 在 0.0.0.0（否則收不到裝置廣播回來的 ACK），
但用 sendmsg + IP_PKTINFO 指定從哪張網卡送出（否則廣播只會走 default route）。
這兩件事都不需要 root。

用法:  python3 gvcp_discover.py [iface=ip ...]
不給參數時自動掃描所有有 IPv4 的實體網卡。
"""
import socket
import struct
import subprocess
import sys
import time

# key=0x42, flags=0x11 (ACK required + allow broadcast ACK), cmd=0x0002 DISCOVERY, len=0
DISCOVERY_CMD = 0x0002
DISCOVERY_ACK = 0x0003
GVCP_PORT = 3956
IP_PKTINFO = 8


def local_ifaces():
    """回傳 [(iface, ip), ...] —— 一張網卡有多個 IP 時每個都要掃，
    否則掛在其他網段的相機會被漏掉（廣播的來源位址決定它回不回得來）。"""
    out = subprocess.run(['ip', '-o', '-4', 'addr'], capture_output=True, text=True).stdout
    res = []
    for line in out.splitlines():
        f = line.split()
        name, cidr = f[1], f[3]
        if name == 'lo' or name.startswith(('tailscale', 'docker', 'virbr')):
            continue
        res.append((name, cidr.split('/')[0]))
    return res


def parse_ack(payload):
    """解析 DISCOVERY_ACK 的 bootstrap 暫存器區塊。"""
    def z(off, n):
        return payload[off:off + n].split(b'\x00')[0].decode('latin-1').strip()

    return dict(
        mac=':'.join('%02x' % b for b in payload[0x0a:0x10]),
        ip=socket.inet_ntoa(payload[0x24:0x28]),
        subnet=socket.inet_ntoa(payload[0x34:0x38]),
        gateway=socket.inet_ntoa(payload[0x44:0x48]),
        manufacturer=z(0x48, 32),
        model=z(0x68, 32),
        version=z(0x88, 32),
        serial=z(0xd8, 16),
        user_id=z(0xe8, 16),
    )


def discover(ifname, srcip, timeout=2.0):
    ifindex = socket.if_nametoindex(ifname)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    # bind 0.0.0.0：裝置的 ACK 是廣播回來的，bind 在特定 IP 上會收不到
    s.bind(('0.0.0.0', 0))
    s.settimeout(0.3)

    # in_pktinfo{ ipi_ifindex, ipi_spec_dst, ipi_addr } — 指定出口網卡與來源位址
    pktinfo = struct.pack('I4s4s', ifindex, socket.inet_aton(srcip), b'\x00' * 4)
    anc = [(socket.IPPROTO_IP, IP_PKTINFO, pktinfo)]

    found = {}
    deadline = time.time() + timeout
    for req_id in (1, 2):
        pkt = struct.pack('>BBHHH', 0x42, 0x11, DISCOVERY_CMD, 0x0000, req_id)
        try:
            s.sendmsg([pkt], anc, 0, ('255.255.255.255', GVCP_PORT))
        except OSError as e:
            print(f'  送出失敗: {e}')
            break
        while time.time() < deadline:
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                break
            if len(data) < 8:
                continue
            status, ack_cmd, length, _ = struct.unpack('>HHHH', data[:8])
            if status != 0 or ack_cmd != DISCOVERY_ACK or len(data) < 8 + 0xf8:
                continue
            found[addr[0]] = parse_ack(data[8:])
        if found:
            break
    s.close()
    return found


def main():
    if len(sys.argv) > 1:
        ifaces = [tuple(a.split('=', 1)) for a in sys.argv[1:]]
    else:
        ifaces = local_ifaces()

    total = 0
    seen = set()
    for ifname, srcip in ifaces:
        print(f'=== GVCP discovery on {ifname} ({srcip}) ===')
        devices = discover(ifname, srcip)
        if not devices:
            print('  無回應')
        for responder, info in devices.items():
            if responder in seen:
                continue
            seen.add(responder)
            total += 1
            print(f'  來自 {responder}:')
            for k, v in info.items():
                print(f'    {k:14s} {v}')
    print()
    if total:
        print(f'找到 {total} 台 GigE Vision 裝置')
    else:
        print('沒有任何 GigE Vision 裝置回應')
        print('檢查: iPORT 供電 (PoE 802.3af 或 12V) / 網路線 / 網卡 link 狀態')
    return 0 if total else 1


if __name__ == '__main__':
    sys.exit(main())
