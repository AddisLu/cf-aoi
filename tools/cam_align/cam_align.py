#!/usr/bin/env python3
"""光學調機工具 v2 — 支援任何 GigE Vision 相機（最多 6 台同時）。

支援機型：Basler raL8192-12gm（原生 GigE）、L803K（經 iPORT CL-GigE）、
OPT 等任何 GigE Vision / GenICam 相機 —— Gain/曝光/行率等參數從相機
自己的 GenICam XML 解出（genicam_client.py），不寫死任何機型。

零相依：標準庫 http.server + 自製 GVCP/GVSP（WRITEREG 路徑）。
桌面圖示雙擊 → 本機伺服器(127.0.0.1:8765) → 自動開瀏覽器。

用法：
  python3 cam_align.py           # 啟動並開瀏覽器
  python3 cam_align.py --test    # 無 GUI 自我測試
"""
import argparse
import io
import ipaddress
import signal
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gvcp_setip import REG_CCP  # noqa: E402
from gvcp_discover import discover as gvcp_discover  # noqa: E402
from genicam_client import GevDevice, CURATED  # noqa: E402
from l800_serial import attach as l800_attach, set_line_rate_hz  # noqa: E402
from PIL import Image  # noqa: E402

# GigE Vision bootstrap（所有相機一致）
REG_SCP0, REG_SCPS0, REG_SCDA0 = 0x0D00, 0x0D04, 0x0D18
DO_NOT_FRAGMENT = 0x40000000
GVSP_LEADER, GVSP_TRAILER, GVSP_PAYLOAD = 1, 2, 3

PORT = 8765
MAX_CAMS = 6
COLORS = ['#4fc3f7', '#ffb74d', '#81c784', '#e57373', '#ba68c8', '#fff176']
CFG = {'row': 0.5, 'interval': 0.3}
CFG_LOCK = threading.Lock()
ORIENT_STORE = os.path.expanduser('~/.config/cam_align/orient.json')


def load_orients():
    try:
        with open(ORIENT_STORE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_orient(mac, orient):
    os.makedirs(os.path.dirname(ORIENT_STORE), exist_ok=True)
    d = load_orients()
    d[mac] = orient
    with open(ORIENT_STORE, 'w') as f:
        json.dump(d, f)


def transform_img(img, orient):
    """依安裝方向轉顯示影像：先鏡像再順時針旋轉。"""
    if orient.get('flip'):
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    rot = orient.get('rot', 0) % 360
    if rot == 90:
        img = img.transpose(Image.ROTATE_270)   # PIL ROTATE_* 為逆時針
    elif rot == 180:
        img = img.transpose(Image.ROTATE_180)
    elif rot == 270:
        img = img.transpose(Image.ROTATE_90)
    return img


def display_to_source(fx, fy, orient):
    """顯示座標(比例) -> 感測器座標(比例)，transform_img 的逆映射。"""
    rot = orient.get('rot', 0) % 360
    if rot == 90:
        x, y = fy, 1 - fx
    elif rot == 180:
        x, y = 1 - fx, 1 - fy
    elif rot == 270:
        x, y = 1 - fy, fx
    else:
        x, y = fx, fy
    if orient.get('flip'):
        x = 1 - x
    return x, y


def iface_addrs():
    out = subprocess.run(['ip', '-o', '-4', 'addr'], capture_output=True, text=True).stdout
    res = {}
    for line in out.splitlines():
        f = line.split()
        name, cidr = f[1], f[3]
        if name == 'lo' or name.startswith(('tailscale', 'docker', 'virbr')):
            continue
        ip, pfx = cidr.split('/')
        res.setdefault(name, []).append((ip, int(pfx)))
    return res


def pick_src(addrs, devip):
    d = ipaddress.ip_address(devip)
    for ip, pfx in addrs:
        if d in ipaddress.ip_network(f'{ip}/{pfx}', strict=False):
            return ip
    return None


class SerialFeat:
    """L803K 經 CL 序列埠的參數，介面上與 GenICam 參數同樣操作。"""

    def __init__(self, name, unit, vmin, vmax, inc, getter, setter):
        self.name, self.unit = name, unit
        self.kind, self.access = 'float', 'RW'
        self.vmin, self.vmax, self.inc = vmin, vmax, inc
        self.entries = {}
        self._get, self._set = getter, setter

    def as_dict(self):
        return dict(name=self.name, kind=self.kind, access=self.access,
                    min=self.vmin, max=self.vmax, inc=self.inc,
                    unit=self.unit, entries={})


class Cam:
    """一台相機：GenICam 裝置 + worker + 統計。"""

    def __init__(self, info):
        self.info = info                   # ip/mac/model/version/iface/srcip/reachable
        self.ip = info['ip']
        self.dev = GevDevice(info['iface'], info['srcip'], self.ip)
        self.gvcp_lock = threading.RLock()  # worker 與設定 API 共用同一 GVCP socket
        self.feats = None                   # key -> (label, Feature)
        self.ctrl = {}
        self.feat_err = ''
        self.worker = None
        self.lock = threading.Lock()
        self.latest = None                  # (seq, bytes, w, h)
        self.seq = 0
        self.stats = {'dur_ms': 0, 'lost': 0, 'mean': 0, 'frames': 0, 'err': ''}
        self.packet = 9000
        self.orient = load_orients().get(info['mac'], {'rot': 0, 'flip': False})
        self.serial = None                  # (bridge, L800) —— L803K 序列埠
        self.attach_busy = False

    def _attach_serial(self, feats):
        """iPORT 機型：經 CL 序列埠把 L803K 的 Gain/曝光/行率 變成一般參數。
        全程持 gvcp_lock（與 worker 共用同一 socket，不加鎖會交錯收發）；
        CCP 只在無人持有時暫取，worker 取像中則沿用它的控制權、絕不釋放。"""
        g = self.dev.g
        with self.gvcp_lock:
            live = self.running()
            if not live:
                r = g.write_reg(self.ip, [(REG_CCP, 2)])
                if r is None or r[0] != 0:
                    raise RuntimeError('取得控制權失敗（eBUS Player 開著？）')
            try:
                br, cam = l800_attach(g, self.ip, self.info['srcip'])
                gmin, gmax = cam.gain_limits()
                emin, emax = cam.expo_limits()
                pmin, pmax = cam.line_period_limits()
            finally:
                if not live:
                    g.write_reg(self.ip, [(REG_CCP, 0)])
        self.serial = (br, cam)
        feats['gain'] = ('增益 Gain', SerialFeat(
            'L800 Gain (CL 序列)', 'dB', round(gmin, 2), round(gmax, 2), 0.1,
            cam.gain_db, cam.set_gain_db))
        feats['expo'] = ('曝光 Exposure', SerialFeat(
            'L800 ExposureTime (CL 序列)', 'µs', round(emin, 2), round(emax, 2), 1.0,
            cam.expo_us, cam.set_expo_us))
        # 上限用 L803k 規格值：Absolute Min 是動態的（受目前曝光牽制），
        # 用它當上限會誤導；實際 clamp 交給 set_line_rate_hz（會先收曝光）
        feats['lrate'] = ('行率 LineRate', SerialFeat(
            'L800 LineRate (CL 序列)', 'Hz', round(1e6 / pmax), 14100, 100,
            lambda: 1e6 / cam.line_period_us(),
            lambda v: set_line_rate_hz(cam, v)))

    def load_features(self):
        try:
            with self.gvcp_lock:
                if 'iPORT' in self.info.get('model', ''):
                    _sanitize_iport(self)     # 鎖內消毒，避免與其他執行緒搶 socket
                self.dev.load()
                feats = {}
                for key, label, names in CURATED:
                    f = self.dev.resolve_first(names)
                    if f is not None:
                        feats[key] = (label, f)
                for n in ('AcquisitionMode', 'AcquisitionStart', 'AcquisitionStop',
                          'AcquisitionFrameCount', 'TLParamsLocked', 'PixelFormat',
                          'Width', 'Height'):
                    f = self.dev.resolve(n)
                    if f is not None:
                        self.ctrl[n] = f
            if 'gain' not in feats and 'iPORT' in self.info.get('model', ''):
                try:
                    self._attach_serial(feats)
                except Exception as e:
                    self.feat_err = f'序列埠附掛失敗: {e}'
            self.feats = feats               # 附掛完成後才發佈（ready 才轉真）
        except Exception as e:
            self.feat_err = str(e)

    def kick_reattach(self):
        """背景重試序列附掛（開設定視窗時自動觸發）。冪等。"""
        if self.attach_busy or not self.ready() or 'gain' in self.feats \
                or 'iPORT' not in self.info.get('model', ''):
            return False
        self.attach_busy = True

        def run():
            try:
                feats = dict(self.feats)
                self._attach_serial(feats)
                self.feats = feats
                self.feat_err = ''
            except Exception as e:
                self.feat_err = f'序列埠附掛失敗: {e}'
            finally:
                self.attach_busy = False
        threading.Thread(target=run, daemon=True).start()
        return True

    def ready(self):
        return self.feats is not None

    def running(self):
        return bool(self.worker and self.worker.is_alive())

    def list_features(self):
        out = []
        if not self.ready():
            return out
        with self.gvcp_lock:
            for key, (label, f) in self.feats.items():
                try:
                    if isinstance(f, SerialFeat):
                        v = self._serial_op(f._get)
                    else:
                        v = self.dev.get(f)
                    if isinstance(v, float):
                        v = round(v, 2)
                except Exception:
                    v = None
                # GenICam 參數的上下限可能是公式（隨其他參數變動）→ 用 describe 取當下值
                d = f.as_dict() if isinstance(f, SerialFeat) else self.dev.describe(f)
                d.update(key=key, label=label, value=v)
                out.append(d)
        return out

    def _serial_op(self, fn, *args):
        """序列埠操作需要 CCP；worker 執行中已持有（同 socket），否則暫取。
        韌體在 CCP 釋放時會清掉 message channel，因此每次都先 rearm。"""
        live = self.running()
        if not live:
            self.dev.g.write_reg(self.ip, [(REG_CCP, 2)])
        try:
            self.serial[0].rearm()
            return fn(*args)
        finally:
            if not live:
                self.dev.g.write_reg(self.ip, [(REG_CCP, 0)])

    def set_feature(self, key, value):
        if not self.ready() or key not in self.feats:
            return False, None, '此相機沒有這個參數'
        _, f = self.feats[key]
        with self.gvcp_lock:
            if isinstance(f, SerialFeat):
                try:
                    back = self._serial_op(f._set, float(value))
                    return True, round(back, 2), ''
                except Exception as e:
                    return False, None, str(e)
            live = self.running()
            if not live:                    # 無 worker 持有控制權 → 暫取
                self.dev.g.write_reg(self.ip, [(REG_CCP, 2)])
            try:
                ok, back = self.dev.set(f, value)
            finally:
                if not live:
                    self.dev.g.write_reg(self.ip, [(REG_CCP, 0)])
        return ok, back, '' if ok else '寫入被拒'


class Worker(threading.Thread):
    """SingleFrame 快照輪詢；GVCP 控制與設定 API 共用 cam.dev 同一 socket。"""

    def __init__(self, cam):
        super().__init__(daemon=True)
        self.cam = cam
        self.stop_flag = threading.Event()
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32 * 1024 * 1024)
        self.rx.bind((cam.info['srcip'], 0))
        self.rx.settimeout(0.5)
        self.orig = {}

    def _cmd(self, name):
        f = self.cam.ctrl.get(name)
        return bool(f) and self.cam.dev.set(f, None)[0]

    def _setup(self):
        c = self.cam
        if not c.info.get('reachable', True):
            raise RuntimeError('主機無同網段位址')
        if not c.ready():
            raise RuntimeError(c.feat_err or '參數載入中，稍候再按開始')
        g = c.dev.g
        r = g.write_reg(c.ip, [(REG_CCP, 2)])
        if r is None:
            raise RuntimeError('裝置沒回應')
        if r[0] != 0:
            raise RuntimeError('取得控制權失敗（被其他軟體占用？）')
        for n in ('AcquisitionMode', 'PixelFormat', 'TLParamsLocked'):
            f = c.ctrl.get(n)
            if f and f.kind != 'command':
                try:
                    self.orig[n] = c.dev.get(f)
                except Exception:
                    pass
        b = g.read_reg(c.ip, REG_SCP0, REG_SCPS0, REG_SCDA0)
        if b is None:
            raise RuntimeError('讀 bootstrap 失敗')
        self.orig['scp'], self.orig['scps'], self.orig['scda'] = b
        pf = c.ctrl.get('PixelFormat')
        if pf and pf.entries and self.orig.get('PixelFormat') != 'Mono8' \
                and 'Mono8' in pf.entries:
            c.dev.set(pf, 'Mono8')          # 顯示假設 8-bit
        am = c.ctrl.get('AcquisitionMode')
        if am and am.entries:
            if 'SingleFrame' in am.entries:
                c.dev.set(am, 'SingleFrame')
            elif 'MultiFrame' in am.entries:
                c.dev.set(am, 'MultiFrame')
                fc = c.ctrl.get('AcquisitionFrameCount')
                if fc:
                    c.dev.set(fc, 1)
        tl = c.ctrl.get('TLParamsLocked')
        if tl:
            c.dev.set(tl, 1)
        port = self.rx.getsockname()[1]
        g.write_reg(c.ip, [(REG_SCPS0, DO_NOT_FRAGMENT | c.packet)])
        g.write_reg(c.ip, [(REG_SCDA0,
                            struct.unpack('>I', socket.inet_aton(c.info['srcip']))[0])])
        g.write_reg(c.ip, [(REG_SCP0, (self.orig['scp'] & ~0xFFFF) | port)])
        w = c.ctrl.get('Width')
        if w and not c.dev.get(w):
            raise RuntimeError('Width=0（相機端無訊號）')

    def _teardown(self):
        c = self.cam
        with c.gvcp_lock:
            self._cmd('AcquisitionStop')
            tl = c.ctrl.get('TLParamsLocked')
            if tl and 'TLParamsLocked' in self.orig:
                try:
                    c.dev.set(tl, self.orig['TLParamsLocked'])
                except Exception:
                    pass
            for n in ('AcquisitionMode', 'PixelFormat'):
                f = c.ctrl.get(n)
                if f and self.orig.get(n) is not None:
                    try:
                        c.dev.set(f, self.orig[n])
                    except Exception:
                        pass
            if 'scp' in self.orig:
                g = c.dev.g
                g.write_reg(c.ip, [(REG_SCPS0, self.orig['scps'])])
                g.write_reg(c.ip, [(REG_SCP0, self.orig['scp'])])
                g.write_reg(c.ip, [(REG_SCDA0, self.orig['scda'])])
            c.dev.g.write_reg(c.ip, [(REG_CCP, 0)])
        self.rx.close()

    def _grab_one(self, timeout=4.0):
        c = self.cam
        try:
            while True:
                self.rx.recv(16384)
        except (socket.timeout, BlockingIOError):
            pass
        with c.gvcp_lock:
            if not self._cmd('AcquisitionStart'):
                c.stats['err'] = 'AcquisitionStart 失敗'
                return None
        t0 = time.time()
        parts, maxpid, done = {}, 0, False
        w = h = 0
        while time.time() < t0 + timeout:
            try:
                pkt = self.rx.recv(c.packet + 64)
            except socket.timeout:
                continue
            parsed = parse_gvsp(pkt)
            if parsed is None:
                continue
            fmt, pid, body = parsed
            if fmt == GVSP_LEADER:
                w, h = struct.unpack('>II', body[16:24])
            elif fmt == GVSP_PAYLOAD:
                parts[pid] = body
                maxpid = max(maxpid, pid)
            elif fmt == GVSP_TRAILER:
                done = True
                break
        if not done or not w:
            return None
        data = b''.join(parts[i] for i in sorted(parts))
        expect = w * h
        data = (data + b'\x00' * expect)[:expect]
        return data, w, h, maxpid - len(parts), time.time() - t0

    def run(self):
        c = self.cam
        try:
            with c.gvcp_lock:
                self._setup()
        except Exception as e:
            c.stats['err'] = str(e)
            try:
                self._teardown()
            except Exception:
                pass
            return
        last_hb = time.time()
        fell_back = False
        try:
            while not self.stop_flag.is_set():
                if time.time() - last_hb > 1.0:
                    with c.gvcp_lock:
                        c.dev.g.read_reg(c.ip, REG_CCP)   # heartbeat（同 socket 才有效）
                    last_hb = time.time()
                r = self._grab_one()
                if r is None:
                    if not fell_back and c.packet > 1500:
                        fell_back = True                  # jumbo 不通 → 自動降 1500
                        c.packet = 1500
                        with c.gvcp_lock:
                            c.dev.g.write_reg(c.ip, [(REG_SCPS0,
                                                      DO_NOT_FRAGMENT | 1500)])
                        c.stats['err'] = '9000 逾時，已改用 1500'
                        continue
                    c.stats['err'] = c.stats['err'] or '取像逾時'
                    continue
                data, w, h, lost, dur = r
                sample = data[::997]
                mean = sum(sample) / max(1, len(sample))
                with c.lock:
                    c.seq += 1
                    c.latest = (c.seq, data, w, h)
                    c.stats.update(dur_ms=round(dur * 1000), lost=lost,
                                   mean=round(mean, 1), err='',
                                   frames=c.stats['frames'] + 1)
                with CFG_LOCK:
                    iv = CFG['interval']
                self.stop_flag.wait(iv)
        finally:
            self._teardown()


def parse_gvsp(pkt):
    """GVSP 封包 → (fmt, packet_id, body)；不是有效資料封包回 None。

    表頭長度依 ID 型式而不同：標準 ID 8 bytes、擴充 ID 20 bytes。trailer 本體只有
    8 bytes → 標準 ID 的 trailer 整包僅 16 bytes。舊碼一律要求 >=20 會把它丟掉，
    raL8192（標準 ID）因此永遠等不到 trailer = 「取像逾時」；iPORT 走擴充 ID 才沒踩到。
    """
    if len(pkt) < 8:
        return None
    st = struct.unpack('>H', pkt[:2])[0]
    # 0x4xxx 為 GEV 警告類狀態（如 Extended Status Codes 開啟時的 0x4008），資料有效
    if st != 0 and (st & 0xC000) != 0x4000:
        return None
    if pkt[4] & 0x80:                      # 擴充 ID
        if len(pkt) < 20:
            return None
        return pkt[4] & 0x0F, struct.unpack('>I', pkt[16:20])[0], pkt[20:]
    return pkt[4] & 0x0F, int.from_bytes(pkt[5:8], 'big'), pkt[8:]


def _sanitize_iport(cam):
    """清掉先前 session 硬殺留下的事件/序列殘設定（否則韌體對著
    已關閉的埠重送事件，累積會把 GVCP 服務打掛）。只在 CCP 空閒時做。"""
    g = cam.dev.g
    try:
        ccp = g.read_reg(cam.ip, REG_CCP)
        if ccp is None or ccp[0] != 0:
            return
        if g.write_reg(cam.ip, [(REG_CCP, 2)]) is None:
            return
        for reg, val in ((0x00016000, 0),    # Bulk0 事件通知關
                         (0x0B00, 0),        # message channel port 歸零
                         (0x20017830, 0)):   # loopback 關
            g.write_reg(cam.ip, [(reg, val)])
        g.write_reg(cam.ip, [(REG_CCP, 0)])
    except Exception:
        pass


CAMS = {}
STATE_LOCK = threading.Lock()
LAST_SEEN = [time.time()]     # 最近一次收到頁面請求
LAST_TICK = [0.0]             # 最近一次 /api/status（頁面 tick 心跳）
BYE_AT = [None]               # 頁面明確關閉的時間


def do_discover():
    found = {}
    ifmap = iface_addrs()
    for _ in range(2):
        for ifname, addrs in ifmap.items():
            try:
                for ip, info in gvcp_discover(ifname, addrs[0][0]).items():
                    src = pick_src(addrs, ip)
                    found[ip] = dict(ip=ip, mac=info['mac'], version=info['version'],
                                     model=(info['model'].strip() or '未知型號'),
                                     iface=ifname, srcip=src or addrs[0][0],
                                     reachable=src is not None)
            except Exception:
                pass
    with STATE_LOCK:
        for ip in list(CAMS):
            if ip not in found and not CAMS[ip].running():
                del CAMS[ip]
        for ip, info in sorted(found.items())[:MAX_CAMS]:
            if ip not in CAMS:
                cam = Cam(info)
                CAMS[ip] = cam
                threading.Thread(target=cam.load_features, daemon=True).start()
            else:
                CAMS[ip].info.update(info)
    return list(CAMS.values())


def start_cams(ips):
    started = []
    with STATE_LOCK:
        for ip in ips:
            c = CAMS.get(ip)
            if not c or c.running():
                continue
            c.stats['err'] = ''
            c.worker = Worker(c)
            c.worker.start()
            started.append(ip)
    return started


def stop_cams():
    with STATE_LOCK:
        ws = [c.worker for c in CAMS.values() if c.worker]
    for w in ws:
        if w.is_alive():
            w.stop_flag.set()
    for w in ws:
        w.join(timeout=8)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def parse_request(self):
        ok = super().parse_request()
        if ok:
            LAST_SEEN[0] = time.time()
            if self.path.startswith('/api/status'):
                LAST_TICK[0] = time.time()     # 活頁面的心跳
            if self.path != '/api/bye':
                BYE_AT[0] = None          # 任何新請求都取消關閉倒數
        return ok

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _img(self, img, fmt='JPEG'):
        buf = io.BytesIO()
        img.save(buf, fmt, quality=82)
        b = buf.getvalue()
        self.send_response(200)
        self.send_header('Content-Type', f'image/{fmt.lower()}')
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(b)

    def _latest(self, ip):
        c = CAMS.get(ip)
        if not c:
            return None
        with c.lock:
            return c.latest

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == '/':
            b = PAGE.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(b)))
            self.send_header('Cache-Control', 'no-store')   # 永遠拿最新版頁面
            self.end_headers()
            self.wfile.write(b)
        elif u.path == '/api/devices':
            with STATE_LOCK:
                devs = [dict(c.info, running=c.running(), ready=c.ready(),
                             feat_err=c.feat_err, color=COLORS[i % len(COLORS)],
                             packet=c.packet, orient=c.orient)
                        for i, c in enumerate(CAMS.values())]
            self._json(dict(devices=devs, cfg=CFG))
        elif u.path == '/api/status':
            out = {}
            with STATE_LOCK:
                cams = list(CAMS.values())
            for c in cams:
                with c.lock:
                    out[c.ip] = dict(c.stats, seq=c.seq, running=c.running(),
                                     ready=c.ready())
            self._json(out)
        elif u.path == '/api/features':
            c = CAMS.get(q.get('ip', ''))
            if not c:
                self._json({'err': 'no cam'}, 404)
                return
            note = ''
            if c.ready() and c.serial:
                note = ('Gain/曝光/行率 經 CL 序列埠直達 L803K 相機本體（設定存於相機）。'
                        '提高行率會自動把曝光收進允許範圍。')
            elif c.ready() and 'gain' not in c.feats and 'iPORT' in c.info['model']:
                c.kick_reattach()
                note = ('Gain/曝光 序列埠附掛中… 幾秒後重開此視窗即可。'
                        if c.attach_busy else
                        'CL 序列埠附掛失敗，關閉後重開此視窗會自動重試。')
            self._json({'ready': c.ready(), 'err': c.feat_err,
                        'features': c.list_features(), 'note': note})
        elif u.path == '/api/frame':
            c = CAMS.get(q.get('ip', ''))
            f = self._latest(q.get('ip', ''))
            if not f or not c:
                self._json({'err': 'no frame'}, 404)
                return
            _, data, w, h = f
            img = transform_img(Image.frombytes('L', (w, h), data), c.orient)
            size = (420, 720) if (c.orient.get('rot', 0) % 360) in (90, 270) \
                else (720, 420)
            self._img(img.resize(size, Image.NEAREST))
        elif u.path == '/api/crop':
            c = CAMS.get(q.get('ip', ''))
            f = self._latest(q.get('ip', ''))
            if not f or not c:
                self._json({'err': 'no frame'}, 404)
                return
            _, data, w, h = f
            sx, sy = display_to_source(float(q.get('fx', .5)),
                                       float(q.get('fy', .5)), c.orient)
            rot = c.orient.get('rot', 0) % 360
            cw, ch = (300, 520) if rot in (90, 270) else (520, 300)
            x0 = max(0, min(max(0, w - cw), int(sx * w) - cw // 2))
            y0 = max(0, min(max(0, h - ch), int(sy * h) - ch // 2))
            crop = Image.frombytes('L', (w, h), data).crop(
                (x0, y0, min(w, x0 + cw), min(h, y0 + ch)))
            self._img(transform_img(crop, c.orient), 'PNG')
        elif u.path == '/api/profile':
            f = self._latest(q.get('ip', ''))
            if not f:
                self._json({'err': 'no frame'}, 404)
                return
            _, data, w, h = f
            with CFG_LOCK:
                row = min(h - 1, int(CFG['row'] * h))
            line = data[row * w:(row + 1) * w]
            step = max(1, w // 1632)
            self._json(dict(w=w, row=row, v=list(line[::step])))
        else:
            self._json({'err': 'not found'}, 404)

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(n) or b'{}') if n else {}
        u = urlparse(self.path)
        if u.path == '/api/discover':
            with STATE_LOCK:
                busy = any(c.running() for c in CAMS.values())
            if busy:
                self._json({'err': '請先按「停止」再掃描'}, 409)
                return
            self._json({'devices': [c.info for c in do_discover()]})
        elif u.path == '/api/start':
            ips = body.get('ips') or list(CAMS)
            self._json({'started': start_cams(ips[:MAX_CAMS])})
        elif u.path == '/api/stop':
            stop_cams()
            self._json({'ok': True})
        elif u.path == '/api/set':
            tgt, key, val = body.get('ip'), body.get('key'), body.get('value')
            results = {}
            with STATE_LOCK:
                cams = list(CAMS.values())
            for c in cams:
                if tgt != '__all__' and c.ip != tgt:
                    continue
                if not c.ready() or key not in (c.feats or {}):
                    if tgt != '__all__':
                        results[c.ip] = dict(ok=False, err='此相機沒有這個參數')
                    continue
                ok, back, err = c.set_feature(key, val)
                results[c.ip] = dict(ok=ok, value=back, err=err)
            self._json({'results': results})
        elif u.path == '/api/orient':
            c = CAMS.get(body.get('ip', ''))
            if not c:
                self._json({'err': 'no cam'}, 404)
                return
            c.orient = {'rot': int(body.get('rot', 0)) % 360,
                        'flip': bool(body.get('flip', False))}
            save_orient(c.info['mac'], c.orient)
            self._json({'orient': c.orient})
        elif u.path == '/api/config':
            with CFG_LOCK:
                if 'row' in body:
                    CFG['row'] = min(1.0, max(0.0, float(body['row'])))
                if 'interval' in body:
                    CFG['interval'] = max(0.0, float(body['interval']))
            self._json({'cfg': CFG})
        elif u.path == '/api/bye':
            BYE_AT[0] = time.time()       # 頁面關閉（sendBeacon），3 秒後熄燈
            self._json({'ok': True})
        elif u.path == '/api/snapshot':
            ts = time.strftime('%Y%m%d_%H%M%S')
            outdir = os.path.expanduser(f'~/CamAlign_{ts}')
            saved = []
            with STATE_LOCK:
                cams = list(CAMS.values())
            for c in cams:
                with c.lock:
                    f = c.latest
                if not f:
                    continue
                os.makedirs(outdir, exist_ok=True)
                _, data, w, h = f
                base = os.path.join(outdir, f'cam_{c.ip.replace(".", "_")}')
                with open(base + '.raw', 'wb') as fh:
                    fh.write(data)
                Image.frombytes('L', (w, h), data).save(base + '.png')
                saved.append(base + '.png')
            self._json({'saved': saved, 'dir': outdir if saved else ''})
        else:
            self._json({'err': 'not found'}, 404)


PAGE = r'''<!DOCTYPE html><html lang="zh-TW"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>相機調機</title><style>
:root{color-scheme:dark;
 --bg:#101318;--panel:#191e26;--panel2:#20262f;--line:#2b323d;
 --tx:#e8edf4;--dim:#8b96a5;--acc:#4f8ef7;--ok:#3fb960;--warn:#e8a13c;--bad:#e5534b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
 font:15px/1.6 "Noto Sans TC",system-ui,sans-serif}
header{display:flex;gap:10px;align-items:center;padding:10px 16px;
 background:var(--panel);border-bottom:1px solid var(--line);
 position:sticky;top:0;z-index:10}
header h1{font-size:17px;margin:0 14px 0 0;font-weight:700}
.btn{border:1px solid var(--line);background:var(--panel2);color:var(--tx);
 border-radius:10px;padding:9px 18px;font-size:15px;cursor:pointer;
 display:inline-flex;align-items:center;gap:7px}
.btn:hover{border-color:var(--acc)}
.btn:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.btn.primary{background:var(--acc);border-color:var(--acc);color:#0b1220;font-weight:700}
.btn.primary:hover{filter:brightness(1.08)}
.btn.danger{background:transparent;border-color:var(--bad);color:#ff9d97}
.btn.danger:hover{background:#2b1a1a}
.btn:disabled{opacity:.4;cursor:default}
.mono{font-family:ui-monospace,"SF Mono",Consolas,monospace}
.num{font-variant-numeric:tabular-nums}
.pill{display:inline-flex;align-items:center;gap:6px;border-radius:999px;
 padding:2px 10px;font-size:12px;flex:none;border:1px solid var(--line);
 background:var(--panel2);color:var(--dim)}
.pill::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--dim)}
.pill.run{color:#9fe0b2;border-color:#2c4a34}
.pill.run::before{background:var(--ok)}
.pill.err{color:#ff9d97;border-color:#553030}
.pill.err::before{background:var(--bad)}
.pill.load{color:#f2c98c;border-color:#54401f}
.pill.load::before{background:var(--warn)}
#msg{margin-left:auto;color:var(--dim);font-size:13px;max-width:40%;
 white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));
 gap:10px;padding:10px 16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;
 overflow:hidden;border-top:4px solid var(--line)}
.card .hd{display:flex;align-items:center;gap:8px;padding:8px 12px}
.card .num{width:26px;height:26px;border-radius:8px;display:flex;
 align-items:center;justify-content:center;font-weight:700;color:#10131a}
.card .who{flex:1;min-width:0}
.card .who b{display:block;font-size:14px;white-space:nowrap;
 overflow:hidden;text-overflow:ellipsis}
.card .who span{font-size:12px;color:var(--dim)}
.card .imgwrap{position:relative;background:#000;cursor:crosshair;
 aspect-ratio:16/8}
.card img.live{width:100%;height:100%;object-fit:fill;display:block;
 image-rendering:pixelated}
.rowline{position:absolute;left:0;right:0;border-top:1.5px dashed #ff5252;
 pointer-events:none}
.midline{position:absolute;top:0;bottom:0;left:50%;
 border-left:1px dashed rgba(120,180,255,.35);pointer-events:none}
.card .ft{display:flex;align-items:center;gap:8px;padding:8px 12px}
.card .st{flex:1;font-size:12.5px;color:var(--dim);white-space:nowrap;
 overflow:hidden;text-overflow:ellipsis}
.card .st.err{color:#ff8a80}
.sbtn{border:1px solid var(--line);background:var(--panel2);color:var(--tx);
 border-radius:8px;padding:5px 12px;font-size:13px;cursor:pointer}
.sbtn:hover{border-color:var(--acc)}
#low{display:flex;gap:10px;padding:0 16px 14px;flex-wrap:wrap}
.box{background:var(--panel);border:1px solid var(--line);border-radius:14px;
 padding:12px}
.box .t{font-size:13px;color:var(--dim);margin-bottom:8px;display:flex;
 gap:12px;align-items:center}
#profbox{flex:2;min-width:420px}
#zoombox{flex:1;min-width:340px}
canvas{display:block;background:#0b0e13;border-radius:8px;width:100%}
#zoom{width:100%;background:#000;border-radius:8px;image-rendering:pixelated}
input[type=range]{accent-color:var(--acc)}
dialog{border:1px solid var(--line);border-radius:16px;background:var(--panel);
 color:var(--tx);padding:0;min-width:430px;max-width:94vw}
dialog::backdrop{background:rgba(0,0,0,.55)}
.dlg-hd{display:flex;align-items:center;padding:14px 18px;
 border-bottom:1px solid var(--line);font-weight:700}
.dlg-hd .x{margin-left:auto;cursor:pointer;color:var(--dim);font-size:20px;
 background:none;border:none}
.dlg-bd{padding:8px 18px 16px}
.frow{display:flex;align-items:center;gap:8px;padding:9px 0;
 border-bottom:1px solid var(--line)}
.frow:last-child{border-bottom:none}
.frow .lb{flex:1}
.frow .lb small{display:block;color:var(--dim)}
.frow input.val{width:110px;text-align:center;background:var(--panel2);
 border:1px solid var(--line);color:var(--tx);border-radius:8px;padding:7px}
.frow select{background:var(--panel2);border:1px solid var(--line);
 color:var(--tx);border-radius:8px;padding:7px}
.step{width:38px;height:38px;font-size:18px;border-radius:8px;
 border:1px solid var(--line);background:var(--panel2);color:var(--tx);
 cursor:pointer}
.note{font-size:12.5px;color:#ffcf86;background:#2a2312;border:1px solid #4a3d1c;
 border-radius:8px;padding:8px 10px;margin-top:10px}
.applyall{display:flex;align-items:center;gap:8px;margin-top:12px;
 font-size:13px;color:var(--dim)}
.ro{color:var(--dim);font-size:13px}
</style></head><body>
<header>
 <h1>📷 相機調機</h1>
 <button class="btn" onclick="discover()">🔍 掃描相機</button>
 <button class="btn primary" onclick="startAll()">▶ 全部開始</button>
 <button class="btn danger" onclick="stopAll()">⏹ 停止</button>
 <button class="btn" onclick="snapshot()">💾 存快照</button>
 <span id="msg">啟動中…</span>
</header>
<div id="grid"></div>
<div id="low">
 <div class="box" id="profbox">
  <div class="t">📈 亮度剖面疊圖 — 各相機同一列的曲線，特徵位置對齊 = 直
   <span style="margin-left:auto">剖面列
    <input type="range" id="row" min="0" max="100" value="50"
      oninput="setRow(this.value)" style="width:150px;vertical-align:middle">
    <span id="rowv">50%</span></span></div>
  <canvas id="prof" height="230"></canvas>
 </div>
 <div class="box" id="zoombox">
  <div class="t" id="zoomtitle">🔎 1:1 放大 — 點任一相機畫面</div>
  <img id="zoom">
 </div>
</div>
<dialog id="dlg">
 <div class="dlg-hd"><span id="dlg-title">設定</span>
  <button class="x" onclick="dlg.close()">✕</button></div>
 <div class="dlg-bd" id="dlg-body">載入中…</div>
</dialog>
<script>
let DEVS=[], zoomIp=null, zoomF={fx:.5,fy:.5};
function rowLineCss(o,pct){          // 感測器「列」在顯示上的位置
  const rot=(o&&o.rot||0)%360;
  if(rot===90)  return {v:true,  pos:100-pct};
  if(rot===270) return {v:true,  pos:pct};
  if(rot===180) return {v:false, pos:100-pct};
  return {v:false, pos:pct};
}
function applyRowLines(){
  DEVS.forEach((d,i)=>{const e=$('rl_'+i); if(!e)return;
    const c=rowLineCss(d.orient, +$('row').value);
    if(c.v){e.style.top='0';e.style.bottom='0';e.style.left=c.pos+'%';e.style.right='auto';
            e.style.borderTop='none';e.style.borderLeft='1.5px dashed #ff5252';e.style.height='auto';}
    else{e.style.left='0';e.style.right='0';e.style.top=c.pos+'%';e.style.bottom='auto';
         e.style.borderLeft='none';e.style.borderTop='1.5px dashed #ff5252';}
  });
}
const $=id=>document.getElementById(id), dlg=$('dlg');
const jget=async u=>(await fetch(u)).json();
const jpost=async(u,b)=>(await fetch(u,{method:'POST',body:JSON.stringify(b||{})})).json();
const say=t=>$('msg').textContent=t;

async function discover(){
  say('掃描中…請稍候');
  const r=await jpost('/api/discover');
  if(r.err){say(r.err);return}
  await refresh(); say(`找到 ${DEVS.length} 台相機`);
}
async function refresh(){
  const r=await jget('/api/devices'); DEVS=r.devices;
  $('grid').innerHTML=DEVS.map((d,i)=>`
   <div class="card" style="border-top-color:${d.color}">
    <div class="hd">
     <div class="num" style="background:${d.color}">${i+1}</div>
     <div class="who"><b title="${d.model}">${d.model}</b>
      <span class="mono">${d.ip}　fw ${d.version}</span></div>
     <span class="pill" id="pl_${i}">待機</span>
    </div>
    <div class="imgwrap" style="aspect-ratio:${(d.orient&&(d.orient.rot%180))?'9/16':'16/8'}"
      onclick="pick(event,'${d.ip}')">
     <img class="live" id="img_${i}">
     <div class="rowline" id="rl_${i}" style="top:50%"></div>
     <div class="midline"></div>
    </div>
    <div class="ft">
     <span class="st" id="st_${i}">待機</span>
     <button class="sbtn" onclick="startOne('${d.ip}')">▶ 單獨</button>
     <button class="sbtn" onclick="openCfg('${d.ip}')">⚙ 設定</button>
    </div>
   </div>`).join('');
  applyRowLines();
}
async function startAll(){const r=await jpost('/api/start',{});
  say(r.started.length?`已開始 ${r.started.length} 台`:'沒有可開始的相機')}
async function startOne(ip){await jpost('/api/start',{ips:[ip]});say(ip+' 已開始')}
async function stopAll(){say('停止中…');await jpost('/api/stop');
  say('已停止（相機設定已還原）')}
async function snapshot(){const r=await jpost('/api/snapshot');
  say(r.dir?`已存 ${r.saved.length} 張 → ${r.dir}`:'沒有影像可存（先按開始）')}
function setRow(v){$('rowv').textContent=v+'%';jpost('/api/config',{row:v/100});
  applyRowLines()}
function pick(ev,ip){const r=ev.currentTarget.getBoundingClientRect();
  zoomIp=ip;zoomF={fx:(ev.clientX-r.left)/r.width,fy:(ev.clientY-r.top)/r.height};
  $('zoomtitle').textContent=`🔎 1:1 放大 — ${ip}`}

// ---- 設定視窗 ----
let cfgIp=null;
async function openCfg(ip){
  cfgIp=ip; $('dlg-title').textContent='設定 — '+ip;
  $('dlg-body').innerHTML='載入中…'; dlg.showModal();
  const r=await jget('/api/features?ip='+ip);
  if(!r.ready){$('dlg-body').innerHTML=
    (r.err?'參數載入失敗：'+r.err:'參數載入中，請稍候再開')+
    '<div style="text-align:right;margin-top:10px">'+
    '<button class="sbtn" onclick="openCfg(cfgIp)">重試</button></div>';return}
  let h=r.features.map(f=>{
    const id='f_'+f.key;
    if(f.kind==='enum'){
      const opts=Object.keys(f.entries).map(e=>
        `<option ${e==f.value?'selected':''}>${e}</option>`).join('');
      return `<div class="frow"><div class="lb">${f.label}
        <small>${f.name}</small></div>
        <select id="${id}">${opts}</select>
        <button class="sbtn" onclick="apply('${f.key}','enum')">套用</button></div>`;
    }
    if(f.access==='RO')return `<div class="frow"><div class="lb">${f.label}
        <small>${f.name}</small></div><span class="ro">${f.value}（唯讀）</span></div>`;
    const rng=(f.min!=null&&f.max!=null)?`${f.min} ~ ${f.max}`:'';
    return `<div class="frow"><div class="lb">${f.label}
      <small>${f.name}${f.unit?' ('+f.unit+')':''}　${rng}</small></div>
      <button class="step" onclick="nudge('${id}',-1,${f.inc||1})">−</button>
      <input class="val" id="${id}" value="${f.value??''}">
      <button class="step" onclick="nudge('${id}',1,${f.inc||1})">＋</button>
      <button class="sbtn" onclick="apply('${f.key}','num')">套用</button></div>`;
  }).join('');
  if(!h)h='<div class="ro" style="padding:12px 0">此相機沒有可調參數</div>';
  const dv=DEVS.find(d=>d.ip===ip)||{}, o=dv.orient||{rot:0,flip:false};
  h+=`<div class="frow"><div class="lb">顯示方向<small>依相機安裝方式，只影響顯示（依 MAC 記住）</small></div>
    <select id="o_rot">${[0,90,180,270].map(r=>
      `<option value="${r}" ${r===(o.rot||0)?'selected':''}>旋轉 ${r}°</option>`).join('')}</select>
    <label style="display:flex;align-items:center;gap:5px;font-size:13px">
      <input type="checkbox" id="o_flip" ${o.flip?'checked':''}>鏡像</label>
    <button class="sbtn" onclick="setOrient()">套用</button></div>`;
  if(r.note)h+=`<div class="note">💡 ${r.note}</div>`;
  h+=`<label class="applyall"><input type="checkbox" id="applyall">
      同時套用到全部相機（相同參數）</label>`;
  $('dlg-body').innerHTML=h;
}
function nudge(id,dir,inc){const e=$(id);
  e.value=(parseFloat(e.value||0)+dir*inc).toString()}
async function setOrient(){
  await jpost('/api/orient',{ip:cfgIp,rot:+$('o_rot').value,flip:$('o_flip').checked});
  await refresh(); say('顯示方向已更新');
}
async function apply(key,kind){
  const e=$('f_'+key);
  const val=kind==='enum'?e.value:parseFloat(e.value);
  const tgt=$('applyall').checked?'__all__':cfgIp;
  const r=await jpost('/api/set',{ip:tgt,key,value:val});
  const res=r.results[cfgIp]||Object.values(r.results)[0]||{};
  if(res.ok){say(`已套用 ${key} = ${res.value??val}`);
    if(res.value!=null&&kind!=='enum')e.value=res.value;}
  else say('套用失敗：'+(res.err||'未知'));
}

// ---- 更新迴圈 ----
async function tick(){
  try{
    const st=await jget('/api/status');
    let n=0;
    for(let i=0;i<DEVS.length;i++){
      const d=DEVS[i],s=st[d.ip],el=$('st_'+i);
      if(!el)continue;
      const pl=$('pl_'+i);
      if(!s){el.textContent='—';continue}
      if(s.err){el.textContent='⚠ '+s.err;el.className='st err';
        if(pl){pl.className='pill err';pl.textContent='錯誤'}continue}
      el.className='st';
      if(!s.running){
        el.textContent=s.ready?'待機':'正在讀取相機參數…';
        if(pl){pl.className=s.ready?'pill':'pill load';
               pl.textContent=s.ready?'待機':'載入中'}
        continue}
      n++;
      if(pl){pl.className='pill run';pl.textContent='取像中'}
      el.innerHTML=`第 <span class="num">${s.frames}</span> 張・`+
        `<span class="num">${s.dur_ms}</span>ms・掉包 <span class="num">${s.lost}</span>`+
        `・亮度 <span class="num">${s.mean}</span>`;
      const im=$('img_'+i);
      if(im.dataset.seq!=s.seq){im.dataset.seq=s.seq;
        im.src=`/api/frame?ip=${d.ip}&s=${s.seq}`}
    }
    drawProfiles(st);
    if(zoomIp&&st[zoomIp]&&st[zoomIp].running)
      $('zoom').src=`/api/crop?ip=${zoomIp}&fx=${zoomF.fx}&fy=${zoomF.fy}&s=${st[zoomIp].seq}`;
  }catch(e){}
  setTimeout(tick,500);
}
async function drawProfiles(st){
  const c=$('prof'); c.width=c.clientWidth*devicePixelRatio;
  const W=c.width,H=c.height,g=c.getContext('2d');
  g.clearRect(0,0,W,H); g.strokeStyle='#242b36'; g.lineWidth=1; g.beginPath();
  for(let y=1;y<4;y++){g.moveTo(0,y*H/4);g.lineTo(W,y*H/4)} g.stroke();
  g.fillStyle='#5b6675';g.font='11px ui-monospace';
  g.fillText('255',6,14);g.fillText('128',6,H/2+4);g.fillText('0',6,H-6);
  for(let i=0;i<DEVS.length;i++){
    const d=DEVS[i];if(!st[d.ip]||!st[d.ip].running)continue;
    try{
      const p=await jget('/api/profile?ip='+d.ip);
      if(!p.v)continue;
      g.strokeStyle=d.color;g.lineWidth=1.6;g.beginPath();
      const n=p.v.length;
      for(let k=0;k<n;k++){const x=k/(n-1)*W,y=H-4-(p.v[k]/255)*(H-8);
        k?g.lineTo(x,y):g.moveTo(x,y)}
      g.stroke();
    }catch(e){}
  }
}
addEventListener('pagehide',()=>navigator.sendBeacon('/api/bye',''));
refresh().then(()=>{if(!DEVS.length)discover();else say('就緒')});
tick();
</script></body></html>'''


def run_test():
    devs = do_discover()
    print(f'找到 {len(devs)} 台：')
    for c in devs:
        print(f"  {c.ip}  {c.info['model']}  fw={c.info['version']}")
    if not devs:
        return 1
    t0 = time.time()
    while time.time() - t0 < 30 and not all(c.ready() or c.feat_err for c in devs):
        time.sleep(0.5)
    for c in devs:
        if c.ready():
            print(f'  {c.ip} 參數: ' + ', '.join(
                f"{d['label']}={d['value']}" for d in c.list_features()))
            if c.feat_err:
                print(f'  {c.ip} !! {c.feat_err}')
        else:
            print(f'  {c.ip} 參數載入失敗: {c.feat_err}')
    start_cams([c.ip for c in devs])
    time.sleep(6)
    ok = True
    for c in devs:
        with c.lock:
            s, f = dict(c.stats), c.latest
        print(f'  {c.ip}: ' + (f'{s["frames"]} 張 {s["dur_ms"]}ms/張 掉包{s["lost"]} '
                               f'亮度{s["mean"]}' if f else f'無影像 ({s["err"]})'))
        ok = ok and f is not None
    c = devs[0]
    if c.ready() and 'height' in c.feats:
        _, back, _ = c.set_feature('height', 500)
        print(f'  執行中 set height=500 -> 讀回 {back}')
        time.sleep(2)
        with c.lock:
            print(f'  下一張尺寸: {c.latest[2]}x{c.latest[3]}' if c.latest else '  無')
        c.set_feature('height', 1000)
    stop_cams()
    print('測試通過' if ok else '測試失敗')
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--test', action='store_true')
    ap.add_argument('--no-browser', action='store_true')
    a = ap.parse_args()
    if a.test:
        return run_test()
    try:
        srv = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    except OSError:
        print(f'PORT {PORT} 已被占用（另一份伺服器在跑）—— 本次啟動放棄，只開瀏覽器分頁')
        webbrowser.open(f'http://127.0.0.1:{PORT}/')
        return 0
    def watchdog():
        # 頁面關閉（beacon）3 秒、或 90 秒無任何請求（涵蓋背景分頁節流）→ 收攤
        while True:
            time.sleep(1)
            now = time.time()
            if BYE_AT[0] is not None and now - BYE_AT[0] > 3:
                if now - LAST_TICK[0] < 5:
                    BYE_AT[0] = None       # 還有別的分頁在輪詢 → 忽略這次 bye
                else:
                    break
            if now - LAST_SEEN[0] > 90:
                break
        print('頁面已關閉，停止相機並結束伺服器')
        stop_cams()
        srv.shutdown()

    signal.signal(signal.SIGTERM, lambda *a: srv.shutdown())   # kill 也走優雅清理
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=do_discover, daemon=True).start()
    print(f'調機介面: http://127.0.0.1:{PORT}/')
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(f'http://127.0.0.1:{PORT}/')).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_cams()
    return 0


if __name__ == '__main__':
    sys.exit(main())
