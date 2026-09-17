#!/usr/bin/env python3
"""用 GVCP 設定 GigE Vision 裝置的 IP —— 不需要 eBUS SDK、不需要 root。

兩階段，每一階段都會驗證後才進行下一步：

  1. FORCEIP_CMD：立即把裝置切到目標 IP（重開機後失效）
  2. --persist：寫入持久 IP 暫存器並開啟 PersistentIP 旗標（重開機保留）

用法:
    python3 gvcp_setip.py <iface> <本機IP> <目標IP> [--mask M] [--gw G] [--persist]

範例:
    python3 gvcp_setip.py enp0s31f6 192.168.5.2 192.168.5.10 --persist
"""
import argparse
import socket
import struct
import subprocess
import sys
import time

GVCP_PORT = 3956
IP_PKTINFO = 8

DISCOVERY_CMD, DISCOVERY_ACK = 0x0002, 0x0003
FORCEIP_CMD, FORCEIP_ACK = 0x0004, 0x0005
READREG_CMD, READREG_ACK = 0x0080, 0x0081
WRITEREG_CMD, WRITEREG_ACK = 0x0082, 0x0083

# GigE Vision bootstrap 暫存器
REG_NET_IF_CAPABILITY = 0x0010   # 支援哪些 IP 組態方式
REG_NET_IF_CONFIG = 0x0014       # 目前啟用哪些（bit0=PersistentIP, bit1=DHCP, bit2=LLA）
REG_PERSISTENT_IP = 0x064C
REG_PERSISTENT_MASK = 0x065C
REG_PERSISTENT_GW = 0x066C
REG_CCP = 0x0A00                 # Control Channel Privilege

CFG_PERSISTENT, CFG_DHCP, CFG_LLA = 0x1, 0x2, 0x4

STATUS = {0x0000: 'SUCCESS', 0x8001: 'NOT_IMPLEMENTED', 0x8002: 'INVALID_PARAMETER',
          0x8003: 'INVALID_ADDRESS', 0x8004: 'WRITE_PROTECT', 0x8005: 'BAD_ALIGNMENT',
          0x8006: 'ACCESS_DENIED', 0x8007: 'BUSY', 0x800E: 'INVALID_HEADER'}


def cfg_str(v):
    on = [n for bit, n in ((CFG_PERSISTENT, 'PersistentIP'), (CFG_DHCP, 'DHCP'), (CFG_LLA, 'LLA'))
          if v & bit]
    return f'0x{v:08x} (' + ('+'.join(on) if on else 'none') + ')'


class Gvcp:
    def __init__(self, ifname, srcip):
        self.ifindex = socket.if_nametoindex(ifname)
        self.srcip = srcip
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.s.bind(('0.0.0.0', 0))
        self.s.settimeout(0.5)
        pktinfo = struct.pack('I4s4s', self.ifindex, socket.inet_aton(srcip), b'\x00' * 4)
        self.anc = [(socket.IPPROTO_IP, IP_PKTINFO, pktinfo)]
        self.req_id = 0

    def _send(self, cmd, payload, dest, broadcast):
        self.req_id = self.req_id % 0xffff + 1
        flags = 0x11 if broadcast else 0x01   # 0x10 = allow broadcast ACK
        pkt = struct.pack('>BBHHH', 0x42, flags, cmd, len(payload), self.req_id) + payload
        if broadcast:
            self.s.sendmsg([pkt], self.anc, 0, (dest, GVCP_PORT))
        else:
            self.s.sendto(pkt, (dest, GVCP_PORT))
        return self.req_id

    def xact(self, cmd, payload=b'', dest='255.255.255.255', broadcast=True, timeout=2.0,
             expect_ack=None):
        """送出一個 GVCP 命令，回傳 (status, payload) 或 None。"""
        expect_ack = expect_ack if expect_ack is not None else cmd + 1
        req_id = self._send(cmd, payload, dest, broadcast)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, _ = self.s.recvfrom(2048)
            except socket.timeout:
                continue
            if len(data) < 8:
                continue
            status, ack_cmd, length, ack_id = struct.unpack('>HHHH', data[:8])
            if ack_cmd != expect_ack or ack_id != req_id:
                continue
            return status, data[8:8 + length]
        return None

    def discover(self):
        """回傳所有裝置 {ip: info}。"""
        found = {}
        for _ in range(2):
            req_id = self._send(DISCOVERY_CMD, b'', '255.255.255.255', True)
            deadline = time.time() + 1.5
            while time.time() < deadline:
                try:
                    data, addr = self.s.recvfrom(2048)
                except socket.timeout:
                    continue
                if len(data) < 8 + 0xf8:
                    continue
                status, ack_cmd, _, ack_id = struct.unpack('>HHHH', data[:8])
                if status != 0 or ack_cmd != DISCOVERY_ACK or ack_id != req_id:
                    continue
                p = data[8:]
                found[socket.inet_ntoa(p[0x24:0x28])] = dict(
                    mac=':'.join('%02x' % b for b in p[0x0a:0x10]),
                    ip=socket.inet_ntoa(p[0x24:0x28]),
                    mask=socket.inet_ntoa(p[0x34:0x38]),
                    gw=socket.inet_ntoa(p[0x44:0x48]),
                    model=p[0x68:0x88].split(b'\x00')[0].decode('latin-1'),
                    version=p[0x88:0xa8].split(b'\x00')[0].decode('latin-1'),
                    ip_cfg=struct.unpack('>I', p[0x14:0x18])[0],
                    ip_cap=struct.unpack('>I', p[0x10:0x14])[0],
                )
            if found:
                break
        return found

    def force_ip(self, mac, ip, mask, gw):
        payload = (b'\x00' * 2 + bytes(int(x, 16) for x in mac.split(':'))
                   + b'\x00' * 12 + socket.inet_aton(ip)
                   + b'\x00' * 12 + socket.inet_aton(mask)
                   + b'\x00' * 12 + socket.inet_aton(gw))
        assert len(payload) == 56, len(payload)
        return self.xact(FORCEIP_CMD, payload, '255.255.255.255', True, timeout=4.0)

    def read_reg(self, dest, *addrs):
        r = self.xact(READREG_CMD, b''.join(struct.pack('>I', a) for a in addrs),
                      dest, broadcast=False)
        if r is None or r[0] != 0:
            return None
        return list(struct.unpack('>%dI' % len(addrs), r[1][:4 * len(addrs)]))

    def write_reg(self, dest, pairs):
        payload = b''.join(struct.pack('>II', a, v) for a, v in pairs)
        return self.xact(WRITEREG_CMD, payload, dest, broadcast=False)


def show(tag, info):
    print(f'  {tag}: {info["model"]}  fw={info["version"]}')
    print(f'      MAC={info["mac"]}  IP={info["ip"]}/{info["mask"]}  GW={info["gw"]}')
    print(f'      支援={cfg_str(info["ip_cap"] & 0x7)}  目前={cfg_str(info["ip_cfg"])}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('iface')
    ap.add_argument('srcip')
    ap.add_argument('target_ip')
    ap.add_argument('--mask', default='255.255.255.0')
    ap.add_argument('--gw', default='0.0.0.0')
    ap.add_argument('--persist', action='store_true', help='同時寫入持久 IP 設定')
    a = ap.parse_args()

    g = Gvcp(a.iface, a.srcip)

    print('=== 1. 探索目前狀態 ===')
    devs = g.discover()
    if not devs:
        print('找不到裝置'); return 1
    if len(devs) > 1:
        print(f'找到多台裝置 {list(devs)}，請先只接一台'); return 1
    info = list(devs.values())[0]
    show('目前', info)

    if info['ip'] == a.target_ip:
        print(f'\n已經是 {a.target_ip}，跳過 FORCEIP')
    else:
        print(f'\n=== 2. FORCEIP -> {a.target_ip}/{a.mask} ===')
        r = g.force_ip(info['mac'], a.target_ip, a.mask, a.gw)
        if r is None:
            print('  沒收到 FORCEIP_ACK（有些韌體不回 ACK，繼續驗證）')
        else:
            print(f'  FORCEIP_ACK status=0x{r[0]:04x} ({STATUS.get(r[0], "?")})')
            if r[0] != 0:
                return 1
        time.sleep(2.0)
        devs = g.discover()
        if a.target_ip not in devs:
            print(f'  驗證失敗，裝置未出現在 {a.target_ip}（目前: {list(devs)}）'); return 1
        info = devs[a.target_ip]
        show('切換後', info)

    print('\n=== 3. 連通性 ===')
    ping = subprocess.run(['ping', '-c', '3', '-W', '1', a.target_ip],
                          capture_output=True, text=True)
    print('  ' + (ping.stdout.strip().splitlines() or ['(無輸出)'])[-2:][0])
    if ping.returncode != 0:
        print('  ping 失敗'); return 1
    print('  ping OK')

    if not a.persist:
        print('\n未指定 --persist：此設定重開機後會失效')
        return 0

    print('\n=== 4. 寫入持久 IP 設定 ===')
    dest = a.target_ip
    if not (info['ip_cap'] & CFG_PERSISTENT):
        print('  裝置不支援 PersistentIP，略過'); return 1

    r = g.write_reg(dest, [(REG_CCP, 0x00000002)])   # 取得 control access
    if r is None or r[0] != 0:
        print(f'  取得控制權失敗: {r}'); return 1
    print('  已取得 control channel')

    try:
        target = (CFG_PERSISTENT | CFG_LLA) & (info['ip_cap'] & 0x7)
        writes = [(REG_PERSISTENT_IP, struct.unpack('>I', socket.inet_aton(a.target_ip))[0]),
                  (REG_PERSISTENT_MASK, struct.unpack('>I', socket.inet_aton(a.mask))[0]),
                  (REG_PERSISTENT_GW, struct.unpack('>I', socket.inet_aton(a.gw))[0])]
        for addr, val in writes:
            r = g.write_reg(dest, [(addr, val)])
            ok = r is not None and r[0] == 0
            print(f'  寫 0x{addr:04X} = {socket.inet_ntoa(struct.pack(">I", val))}  '
                  f'{"OK" if ok else f"失敗 {r}"}')
            if not ok:
                return 1
        r = g.write_reg(dest, [(REG_NET_IF_CONFIG, target)])
        ok = r is not None and r[0] == 0
        print(f'  寫 0x{REG_NET_IF_CONFIG:04X} = {cfg_str(target)}  '
              f'{"OK" if ok else f"失敗 {r}"}')
        if not ok:
            return 1

        print('\n=== 5. 讀回驗證 ===')
        vals = g.read_reg(dest, REG_NET_IF_CONFIG, REG_PERSISTENT_IP,
                          REG_PERSISTENT_MASK, REG_PERSISTENT_GW)
        if vals is None:
            print('  讀回失敗'); return 1
        cfg, pip, pmask, pgw = vals
        print(f'  0x0014 IP config     {cfg_str(cfg)}')
        print(f'  0x064C Persistent IP {socket.inet_ntoa(struct.pack(">I", pip))}')
        print(f'  0x065C Persistent 遮罩 {socket.inet_ntoa(struct.pack(">I", pmask))}')
        print(f'  0x066C Persistent 閘道 {socket.inet_ntoa(struct.pack(">I", pgw))}')
        good = (cfg == target
                and socket.inet_ntoa(struct.pack('>I', pip)) == a.target_ip
                and socket.inet_ntoa(struct.pack('>I', pmask)) == a.mask)
        print('\n' + ('持久設定已生效，重開機後保留 ' + a.target_ip if good
                      else '讀回值與預期不符，請檢查'))
        return 0 if good else 1
    finally:
        g.write_reg(dest, [(REG_CCP, 0x00000000)])   # 釋放控制權
        print('  (已釋放 control channel)')


if __name__ == '__main__':
    sys.exit(main())
