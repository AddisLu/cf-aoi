"""provision.py — 相機 CCD 身分配置（名稱 CCDnn + persistent IP），raL8192 與 L803K(iPORT) 共用。

規則與 grab 的 cam_provision（raL8192）/ iport_provision（iPORT）一致（docs/CLAUDE.md §2）：
  名稱 = "CCDnn"（1 開頭；grab 以此決定 cam_id = nn）
  IP 尾碼 = 編號：raL8192 → 192.168.5.nn；iPORT（下方 L803K）→ 192.168.4.nn；/24、無閘道
  名稱或 IP 撞到網路上任何一台 → 拒絕（要交換編號時先把其中一台設到暫時位址，例 .101）

做法（全部走 GigE Vision 標準 bootstrap 暫存器 + WRITEREG，不走 GenICam/WRITEMEM：
iPORT 韌體 WRITEMEM 寫 GenICam 區會「回 SUCCESS 但寫入 0」，見 README）：
  1. 目前 IP ≠ 目標 → FORCEIP（廣播，不受網段限制：出廠在 192.168.30.x / 169.254.x 的新相機也能改）
  2. 取控制權 CCP=2（被 cfaoi_grab / pylon Viewer / eBUS Player 開著 → 拒絕，不搶）
  3. 寫 0x00E8–0x00F7 名稱、0x064C/065C/066C persistent IP/mask/gw、0x0014 = PersistentIP+LLA
  4. 讀回比對 → 釋放 CCP
"""
import re
import socket
import struct
import time

from gvcp_setip import (CFG_LLA, CFG_PERSISTENT, REG_CCP, REG_NET_IF_CONFIG, REG_PERSISTENT_GW,
                        REG_PERSISTENT_IP, REG_PERSISTENT_MASK, STATUS)

REG_USER_NAME = 0x00E8          # 16 bytes（4 個 32-bit 暫存器，大端序：第一個字元在最高位元組）
MASK = '255.255.255.0'
GW = '0.0.0.0'


def camera_kind(model, manufacturer=''):
    """'iport'（L803K 經 Pleora iPORT）/ 'basler'（raL8192 等原生 GigE）/ ''（無法判斷）。"""
    m, v = (model or '').lower(), (manufacturer or '').lower()
    if 'iport' in m or 'pleora' in v or 'ebus' in m:
        return 'iport'
    if 'basler' in v or m.startswith('ra') or m.startswith('ac') or m.startswith('a2'):
        return 'basler'
    return ''


def parse_ccd(name):
    """'CCD38' → 38；格式錯丟 ValueError（訊息給人看）。"""
    m = re.fullmatch(r'CCD([0-9]{2})', name or '')
    if not m:
        raise ValueError(f'名稱須為 CCDnn（例 CCD05、CCD38），收到「{name}」')
    n = int(m.group(1))
    if n == 0:
        raise ValueError('編號自 01 起（IP 尾碼 = 編號，.0 是網段位址）')
    return n


def default_ip(kind, n):
    if kind == 'iport':
        return f'192.168.4.{n}'
    if kind == 'basler':
        return f'192.168.5.{n}'
    raise ValueError('無法從型號判斷是 raL8192 還是 iPORT，請自行輸入 IP')


def valid_ip(ip):
    try:
        b = socket.inet_aton(ip)
    except OSError:
        return False
    return len(ip.split('.')) == 4 and b[3] not in (0, 255)


def check_conflicts(devices, target_mac, ccd, ip):
    """devices：掃描結果（dict 含 mac/ip/user_id/model/serial）。撞名或撞 IP → 回錯誤訊息，否則 None。"""
    for d in devices:
        if d.get('mac', '').lower() == target_mac.lower():
            continue
        who = f"{d.get('model', '?')} {d.get('serial', '')}（{d.get('ip', '?')}）".strip()
        if d.get('user_id') == ccd:
            return f'名稱 {ccd} 已被 {who} 使用'
        if d.get('ip') == ip:
            return f'IP {ip} 已被 {who} 使用 → 先把它改到暫時位址（例 .101）'
    return None


def pack_name(name):
    raw = name.encode('ascii')[:15].ljust(16, b'\x00')
    return [(REG_USER_NAME + i, struct.unpack('>I', raw[i:i + 4])[0]) for i in range(0, 16, 4)]


def unpack_name(vals):
    return b''.join(struct.pack('>I', v) for v in vals).split(b'\x00')[0].decode('latin-1')


def ip_u32(ip):
    return struct.unpack('>I', socket.inet_aton(ip))[0]


def u32_ip(v):
    return socket.inet_ntoa(struct.pack('>I', v))


def _status(r):
    if r is None:
        return '無回應'
    return STATUS.get(r[0], f'0x{r[0]:04x}')


def provision(gvcp, dev, ccd, ip, sleep=time.sleep):
    """dev：掃描結果（mac/ip/model…）；gvcp：gvcp_setip.Gvcp（綁在該相機所在網卡）。
    回傳 (ok, [log…])。不檢查撞號（呼叫端先 check_conflicts）。"""
    log = []
    mac = dev['mac']
    cur = dev.get('ip', '')
    # 1) FORCEIP：先把相機搬到目標位址，之後才能單播寫暫存器（跨網段也行）
    if cur != ip:
        r = gvcp.force_ip(mac, ip, MASK, GW)
        log.append(f'ForceIP {cur} → {ip}：{_status(r)}')
        if r is None or r[0] != 0:
            return False, log + ['✗ 改 IP 失敗（相機被其他程式控制中時不接受 ForceIP）']
        for _ in range(10):                      # 等相機在新位址回應（含 ARP 重解）
            sleep(0.3)
            if gvcp.read_reg(ip, REG_CCP) is not None:
                break
        else:
            return False, log + [f'✗ 相機改 IP 後在 {ip} 沒有回應（主機在該網段有位址嗎？）']
    # 2) 控制權
    r = gvcp.write_reg(ip, [(REG_CCP, 2)])
    if r is None or r[0] != 0:
        return False, log + [f'✗ 取得控制權失敗（{_status(r)}）：被其他程式使用中？'
                             '先停產線 Grab、關 pylon Viewer / eBUS Player']
    try:
        # 3) 寫入（逐個 WRITEREG，任何一個失敗就停）
        writes = pack_name(ccd) + [
            (REG_PERSISTENT_IP, ip_u32(ip)),
            (REG_PERSISTENT_MASK, ip_u32(MASK)),
            (REG_PERSISTENT_GW, ip_u32(GW)),
            (REG_NET_IF_CONFIG, CFG_PERSISTENT | CFG_LLA),
        ]
        for addr, val in writes:
            r = gvcp.write_reg(ip, [(addr, val)])
            if r is None or r[0] != 0:
                return False, log + [f'✗ 寫入 0x{addr:04X} 失敗（{_status(r)}）']
        log.append(f'寫入：名稱={ccd}、persistent IP={ip}/24、IP 組態=PersistentIP+LLA')
        # 4) 讀回比對
        names = gvcp.read_reg(ip, *[a for a, _ in pack_name(ccd)])
        rb = gvcp.read_reg(ip, REG_PERSISTENT_IP, REG_NET_IF_CONFIG)
        if names is None or rb is None:
            return False, log + ['✗ 讀回失敗']
        rb_name, rb_ip, rb_cfg = unpack_name(names), u32_ip(rb[0]), rb[1]
        log.append(f'讀回：名稱={rb_name}、persistent IP={rb_ip}、IP 組態=0x{rb_cfg:X}')
        if rb_name != ccd or rb_ip != ip or (rb_cfg & 0x7) != (CFG_PERSISTENT | CFG_LLA):
            return False, log + ['✗ 讀回與寫入不符（韌體不接受？用 gvcp_setip.py 交叉確認）']
    finally:
        gvcp.write_reg(ip, [(REG_CCP, 0)])
    log.append(f'✓ 完成：{dev.get("model", "")} → {ccd} @ {ip}（斷電重開後仍保留）')
    return True, log
