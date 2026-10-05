#!/usr/bin/env python3
"""
cfaoi_triage — CF-AOI 一鍵健檢（在 Grab 上跑；線上人員不懂硬體也能看懂結論）

自動收證據 → 依規則判斷「哪裡壞、可能原因、現場該做什麼、找誰」，輸出一頁報告（Markdown + JSON）。
報告存進機台資料夾（/srv/cfaoi/10_logs/<日期>/grab_triage_<時間>.md），LoopEngineering（機況助手）可直接讀來回答問題。

檢查項目
  1 相機    應到台數 vs 掃描到的（CCDnn）、少的那台在交換機哪個埠、埠有沒有 link/封包 → 線/電源/IP 設定/當機；
            Grab 回報的故障相機；IP 尾碼規則
  2 取像    （產線沒在跑時）每台取數秒：完整度、遺失張數、亮度；掉封包時對照交換機埠錯誤計數、
            Grab 網卡丟包、MTU、埠速率、交換機限速設定；亮度偏暗時對照曝光/增益是否被改
  3 交換機  console、上行（→Grab 100G）、相機埠、設定與基準差異、時鐘
  4 RDMA    Grab↔Spark 直連線：兩端 link、ping、大封包、RoCE 狀態、IP 服務與 18515 → 分辨「線」還是「機器/程式」
  4b 主機   Grab 網卡 MTU、轉送（Control→Spark 經 Grab）、Spark 回程路由、兩台磁碟、Grab↔Spark 時間差
  5 GPU/IP  Spark GPU 狀態、Xid 錯誤分類（硬體 vs 程式）、IP 服務、IP 參數檔是否被改；--gpu 另跑離線單元測試 + 參考圖測圖比對

用法
  cfaoi_triage.py                 全部（取像檢查約 10 秒；產線在跑時自動略過取像）
  cfaoi_triage.py --gpu           另跑 GPU 深度檢查（單元測試 + 參考圖，約 1–2 分鐘；不影響生產 IP）
  cfaoi_triage.py --save-baseline 目前狀態正常時存基準（各 CCD 相機的 MAC、交換機設定）；換相機後重存
  cfaoi_triage.py --only rdma,gpu 只跑指定項目（camera,capture,switch,rdma,host,gpu）
結束碼：0 全正常、1 有注意事項、2 有異常
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, 'tools', 'cam_align'))

SPARK = os.environ.get('SPARK', 'auo001@192.168.3.1')
SPARK_IP = '192.168.3.1'
SPARK_REPO = os.environ.get('SPARK_REPO', '/home/auo001/Addis/cf-aoi')
GRAB_CTRL = ('127.0.0.1', 8100)
IP_CTRL = (SPARK_IP, 8200)
ARCHIVE = os.environ.get('CFAOI_HOME', '/srv/cfaoi')
NOW = dt.datetime.now()

ICON = {'FAIL': '❌', 'WARN': '⚠️', 'INFO': 'ℹ️', 'OK': '✅'}
RANK = {'FAIL': 0, 'WARN': 1, 'INFO': 2, 'OK': 3}
# NVIDIA Xid：硬體/驅動層級（需處理）vs 應用程式自己的錯（看是哪支程式）
XID_HW = {48: 'GPU 記憶體雙位元錯誤（DBE）', 62: 'GPU 內部微控制器停止', 63: 'ECC 頁面退役', 64: 'ECC 頁面退役失敗',
          74: 'NVLink 錯誤', 79: 'GPU 從匯流排掉線', 92: '高單位元 ECC 錯誤率', 94: '可修正 ECC 錯誤（已隔離）',
          95: '不可修正 ECC 錯誤', 119: 'GSP 逾時', 120: 'GSP 錯誤', 122: 'SPI PMU RPC 讀取失敗', 123: 'SPI PMU RPC 寫入失敗',
          140: 'ECC 未修復錯誤'}
XID_APP = {13: '程式執行錯誤（非法指令/越界）', 31: '程式存取非法記憶體（MMU fault）', 43: '程式被 GPU 停止（多半跟在 13/31 後）',
           45: '程式被搶占/結束時清通道', 69: '圖形引擎類別錯誤'}


# ───────────────────────── 共用 ─────────────────────────
class Report:
    def __init__(self):
        self.items = []
        self.facts = {}

    def add(self, area, level, title, evidence='', cause='', action='', who=''):
        self.items.append(dict(area=area, level=level, title=title, evidence=evidence, cause=cause,
                               action=action, who=who))

    def worst(self):
        return min((RANK[i['level']] for i in self.items), default=3)


def run(cmd, timeout=20, shell=False):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=shell)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, '', 'timeout'
    except FileNotFoundError as e:
        return 127, '', str(e)


def ssh_spark(script, timeout=25):
    return run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', SPARK, script], timeout=timeout)


def tcp_cmd(addr, cmd, params=None, timeout=5.0):
    """Grab/IP 控制埠：一行 JSON → 一行 JSON。連不上回 None。"""
    try:
        with socket.create_connection(addr, timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall((json.dumps({'cmd': cmd, 'seq': 1, 'params': params or {}}) + '\n').encode())
            buf = b''
            while not buf.endswith(b'\n'):
                b = s.recv(65536)
                if not b:
                    break
                buf += b
        return json.loads(buf)
    except (OSError, ValueError):
        return None


def env_file(path):
    d = {}
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    d[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return d


def nic_with_ip(prefix):
    rc, out, _ = run(['ip', '-o', '-4', 'addr', 'show'])
    for line in out.splitlines():
        p = line.split()
        if len(p) > 3 and p[3].startswith(prefix):
            return p[1]
    return None


def sysfs(nic, name, default=None):
    try:
        with open(f'/sys/class/net/{nic}/{name}') as f:
            return f.read().strip()
    except OSError:
        return default


def nic_stats(nic):
    out = {}
    for k in ('rx_dropped', 'rx_errors', 'rx_missed_errors', 'rx_crc_errors', 'rx_over_errors', 'rx_fifo_errors'):
        v = sysfs(nic, f'statistics/{k}')
        if v is not None:
            out[k] = int(v)
    return out


def expected_line_rate():
    """Grab 啟動參數的 --line-rate（/etc/default/cfaoi-grab GRAB_EXTRA）；預設 12000；keep/max → 不檢查。"""
    m = re.search(r'--line-rate\s+(\S+)', env_file('/etc/default/cfaoi-grab').get('GRAB_EXTRA', ''))
    if not m:
        return 12000.0
    try:
        return float(m.group(1))
    except ValueError:
        return None


def state_dir():
    d = os.path.join(ARCHIVE, '60_config', 'current') if os.path.isdir(os.path.join(ARCHIVE, '60_config')) \
        else os.path.expanduser('~/cfaoi_logs/triage')
    os.makedirs(d, exist_ok=True)
    return d


def load_baseline():
    try:
        with open(os.path.join(state_dir(), 'grab_triage_baseline.json'), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_baseline(b):
    p = os.path.join(state_dir(), 'grab_triage_baseline.json')
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(b, f, ensure_ascii=False, indent=1)
    return p


def open_switch(rep):
    try:
        from switch_console import Switch, SwitchError
    except Exception as e:  # noqa: BLE001
        rep.add('交換機', 'WARN', '無法載入交換機 console 模組', str(e))
        return None
    try:
        return Switch()
    except SwitchError as e:
        rep.add('交換機', 'WARN', '交換機 console 連不上，交換機相關判斷略過', str(e),
                'USB console 線沒插、或另一個程式（PuTTY/screen）正在用 /dev/ttyUSB0',
                '確認 Grab 的 USB 線接到交換機 console 口；關掉其他終端程式後重跑', '設備工程')
        return None


# ───────────────────────── 1. 相機 ─────────────────────────
def check_cameras(rep, sw, base, args):
    import cam_align as ca
    cfg = env_file('/etc/default/cfaoi-grab')
    n = int(cfg.get('CAM_COUNT', '0') or 0)
    expected = [f'CCD{i:02d}' for i in range(1, n + 1)]
    rep.facts['cam_expected'] = expected
    ca.do_discover()
    devs = list(ca.INVENTORY.values())
    named, dups = {}, {}
    for d in devs:
        if re.fullmatch(r'CCD\d{2}', d.get('user_id') or ''):
            dups.setdefault(d['user_id'], []).append(d)
            named[d['user_id']] = d
    base_macs = base.get('macs', {})
    from switch_console import norm_mac as _nm
    for name, ds in dups.items():
        if len(ds) < 2:
            continue
        # 用基準 MAC 找出「真的 CCDxx」與「被誤命名的那台原本是誰」
        real = [d for d in ds if _nm(d['mac']) == base_macs.get(name)]
        named[name] = real[0] if real else ds[0]
        who = []
        for d in ds:
            orig = next((k for k, v in base_macs.items() if v == _nm(d['mac'])), None)
            who.append(f'{d["ip"]}（MAC {_nm(d["mac"])}' + (f'，基準是 {orig}' if orig else '，基準沒有這台') + '）')
        wrong = [next((k for k, v in base_macs.items() if v == _nm(d['mac'])), None) for d in ds if d not in real]
        rep.add('相機', 'FAIL', f'有 {len(ds)} 台相機都叫 {name}（身分衝突）', '；'.join(who),
                '換相機/設定時名稱寫錯，或複製了別台的設定 → Grab 會綁錯相機、另一個 CCD 變成「不見」',
                f'用相機工具「裝置設定」把多出來的那台改回正確編號' +
                (f'（MAC 對照基準，它應該是 {"、".join(w for w in wrong if w)}）' if any(wrong) else '（看線材標示）'),
                '設備工程')
        for w in wrong:
            if w and w not in named:
                rep.facts.setdefault('cam_renamed', []).append(w)
    rep.facts['cam_found'] = sorted(named)
    rep.facts['_devs'] = devs

    sw_ports = sw.ports() if sw else {}
    sw_macs = sw.macs() if sw else {}
    rep.facts['_sw_ports'], rep.facts['_sw_macs'] = sw_ports, sw_macs
    # 基準只記 CCD ↔ 相機 MAC（相機身分；交換機埠位會變、線材上有 CCD 標示 → 不綁埠位）
    from switch_console import norm_mac
    cam_macs = dict(base.get('macs', {}))
    for name, d in named.items():
        cam_macs[name] = norm_mac(d['mac'])
    base['macs'] = cam_macs
    base.pop('ports', None)

    renamed = set(rep.facts.get('cam_renamed', []))
    missing = [c for c in expected if c not in named and c not in renamed]
    rep.facts['cam_missing'] = missing
    # 名稱在、但 IP 被改到相機網段以外（ForceIP / 設定錯）→ Grab 連不到
    for name, d in sorted(named.items()):
        if name in expected and d.get('kind') == 'basler' and not d['ip'].startswith('192.168.5.'):
            rep.add('相機', 'FAIL', f'{name} 的 IP 被改成 {d["ip"]}（不在相機網段 192.168.5.x）→ Grab 連不到',
                    f'MAC {_nm(d["mac"])}；主機在該網段{"有" if d.get("reachable") else "沒有"}位址',
                    '有人用工具改了相機 IP（暫時或永久）',
                    f'① {name} 斷電 10 秒再上電（暫時改的 IP 會回到設定值）② 仍不對 → 相機工具「裝置設定」寫入 {name}（IP 自動 = 192.168.5.{int(name[3:])}）',
                    '線上人員可先做')
            rep.facts.setdefault('cam_bad_ip', []).append(name)
    if missing and len(missing) == len(expected) and len(expected) > 1:
        grab_nic = nic_with_ip('192.168.5.200')
        car = sysfs(grab_nic, 'carrier', '0') == '1' if grab_nic else False
        rep.add('相機', 'FAIL', f'全部 {len(expected)} 台相機都不見了 → 問題在共用的那一段，不是個別相機',
                f'Grab 相機網卡 {grab_nic or "?"} link {"有" if car else "無"}；交換機 console {"可用" if sw else "不可用"}',
                'Grab ↔ 交換機 100G 上行線斷/模組鬆脫、交換機沒電或當機、或交換機設定被重置' if not car else
                '交換機設定被重置/相機埠被關閉、或相機總電源沒開',
                '① 看交換機電源燈與 Grab 後面 100G 線燈號 ② 重插 Grab ↔ 交換機 100G 線兩端 ③ 相機總電源 ④ 仍不行找工程看交換機設定（見交換機項目）',
                '設備工程')
        missing = []                                         # 不再逐台列（同一個根因）
    for c in missing:
        mac = cam_macs.get(c)
        label = f'請找線材標示「{c}」的那一條'
        if not sw or not mac:
            why = '交換機 console 不可用' if not sw else '這台從未在正常時記錄過（沒有 MAC 基準）'
            rep.add('相機', 'FAIL', f'{c} 不見了（掃描不到）', why,
                    '網路線/光模組鬆脫或損壞、相機沒電、或相機 IP 被改',
                    f'{label}：① 看 {c} 相機燈號（不亮 = 沒電）② 重插這條線兩端 ③ 開相機工具掃描', '設備工程')
            continue
        from switch_console import find_port
        port = find_port(mac, sw_macs)
        if port and sw_ports.get(port, {}).get('link') != 'UP':
            port = None                                   # 埠已斷，MAC 表是殘留的舊紀錄
        if port:
            rep.add('相機', 'FAIL', f'{c} 不見了：線路有訊號，但相機沒回應', f'相機 MAC {mac} 仍在交換機（{port}）',
                    '相機 IP 被改到別的網段、或相機韌體卡住',
                    f'① 開「相機工具」掃描，看 {c} 是否出現在別的 IP ② {c} 相機斷電 10 秒再上電', '設備工程')
        else:
            rep.add('相機', 'FAIL', f'{c} 不見了：線路沒有訊號', f'相機 MAC {mac} 不在交換機（線斷、模組鬆、或相機沒電）',
                    f'{c} 的網路線/光纖或交換機那端的模組鬆脫、損壞；或 {c} 相機沒電（電源線/電源供應器）',
                    f'{label}：① 看 {c} 相機背面燈號是否亮（不亮 = 沒電，查電源線）② 重插這條線兩端（相機端、交換機端模組）'
                    f' ③ 換一條線或交換機換一個空埠（埠位不必固定）', '設備工程')
    if not missing and expected and not rep.facts.get('cam_bad_ip') and not renamed:
        rep.add('相機', 'OK', f'相機 {len(expected)} 台都在（{expected[0]}–{expected[-1]}）')

    for name, d in sorted(named.items()):
        num = int(name[3:])
        if d['ip'].startswith('192.168.5.') and d['ip'].rsplit('.', 1)[1] != str(num):
            rep.add('相機', 'WARN', f'{name} 的 IP {d["ip"]} 不符規則（應 192.168.5.{num}）', '',
                    '換相機後 IP 沒照規則設', '用相機工具「裝置設定」重設 IP', '設備工程')
    unnamed = [d for d in devs if not re.fullmatch(r'CCD\d{2}', d.get('user_id') or '')]
    for d in unnamed:
        rep.add('相機', 'WARN', f'有一台沒命名的相機（{d["model"]} {d["ip"]}）', f'MAC {d["mac"]}',
                '新換上的相機還沒設定 CCD 編號', '用相機工具「裝置設定」命名（CCDnn）', '設備工程')
    extra = [c for c in named if c not in expected]
    if extra and expected:
        rep.add('相機', 'INFO', f'掃到超出設定台數的相機：{", ".join(extra)}', f'Grab 設定 CAM_COUNT={n}')

    h = tcp_cmd(GRAB_CTRL, 'CHECK_HEALTH')
    rep.facts['grab_health'] = (h or {}).get('data')
    if h is None:
        rep.add('相機', 'FAIL', 'Grab 程式沒有回應（8100）', '', 'Grab 服務停止或當掉',
                'Control「系統狀態」→ Grab 取像「重新啟動」', '線上人員可先做')
    else:
        fc = (h.get('data') or {}).get('faulted_cams') or []
        if fc:
            rep.add('相機', 'FAIL', f'Grab 回報取像中斷的相機：{fc}', json.dumps(h.get('data'), ensure_ascii=False)[:300],
                    '取像途中相機斷線（線、電源）', '排除線路後，Control 重新 ARM（或重新開始下一片）', '設備工程')


# ───────────────────────── 2. 取像（掉封包 / 偏暗）─────────────────────────
def check_capture(rep, sw, base, args):
    import cam_align as ca
    gh = rep.facts.get('grab_health') or {}
    if gh.get('armed') or gh.get('grabbing'):
        rep.add('取像', 'INFO', '產線 Grab 正在取像，略過取像檢查（不打擾生產）')
        return
    devs = [d for d in rep.facts.get('_devs', []) if d.get('kind') == 'basler' and d['ip'].startswith('192.168.5.')]
    busy = [d['user_id'] or d['ip'] for d in devs if d.get('busy')]
    devs = [d for d in devs if not d.get('busy')]
    if busy:
        rep.add('取像', 'WARN', f'相機被其他程式占用：{", ".join(busy)}（產線 Grab 沒在用它）',
                '相機的控制權（CCP）被別的程式拿著', '有人開著 pylon Viewer / 相機工具 / eBUS Player 沒關 → 產線開始取像時這台會失敗',
                '關閉 Grab 主機或其他電腦上的 pylon Viewer、相機工具；仍占用 → 該相機斷電重開', '線上人員可先做')
    if not devs:
        return
    nic = nic_with_ip('192.168.5.200')
    mtu = int(sysfs(nic, 'mtu', '0')) if nic else 0
    cam_cfg = {}
    try:
        with open(os.path.join(REPO, 'grab', 'cam_config.json'), encoding='utf-8') as f:
            cam_cfg = {c['cam_id']: c for c in json.load(f).get('cameras', [])}
    except (OSError, ValueError):
        pass
    live_port = {}                                   # CCD → 目前插的交換機埠（MAC 即時查；埠位會變）
    if sw:
        from switch_console import find_port
        macs_now = rep.facts.get('_sw_macs') or sw.macs()
        for d in devs:
            p = find_port(d['mac'], macs_now)
            if p:
                live_port[d.get('user_id') or d['ip']] = p
    results = {}
    for i in range(0, len(devs), ca.MAX_CAMS):
        batch = devs[i:i + ca.MAX_CAMS]
        ips = [d['ip'] for d in batch]
        sw_before = {d['ip']: sw.counters(live_port[d.get('user_id') or d['ip']])
                     for d in batch if sw and (d.get('user_id') or d['ip']) in live_port}
        nic_before = nic_stats(nic) if nic else {}
        for d in batch:                           # 讀曝光/增益要等特徵載入
            c = ca.CAMS.get(d['ip'])
            for _ in range(40):
                if c is None or c.ready():
                    break
                time.sleep(0.2)
        ca.start_cams(ips)
        time.sleep(args.capture_sec)
        snap = {}
        with ca.STATE_LOCK:
            for ip in ips:
                c = ca.CAMS.get(ip)
                if c:
                    with c.lock:
                        snap[ip] = dict(c.stats)
        feats = {}
        for ip in ips:
            c = ca.CAMS.get(ip)
            try:
                feats[ip] = {f['key']: f.get('value') for f in c.list_features()} if c and c.ready() else {}
            except Exception:  # noqa: BLE001
                feats[ip] = {}
        ca.stop_cams()
        nic_after = nic_stats(nic) if nic else {}
        sw_after = {d['ip']: sw.counters(live_port[d.get('user_id') or d['ip']])
                    for d in batch if sw and (d.get('user_id') or d['ip']) in live_port}
        for d in batch:
            results[d['ip']] = dict(dev=d, stats=snap.get(d['ip'], {}), feats=feats.get(d['ip'], {}),
                                    sw0=sw_before.get(d['ip']), sw1=sw_after.get(d['ip']))
        rep.facts.setdefault('nic_delta', {}).update(
            {k: nic_after.get(k, 0) - nic_before.get(k, 0) for k in nic_after})
    nic_delta = rep.facts.get('nic_delta', {})
    nic_drop = sum(v for v in nic_delta.values() if v > 0)
    rep.facts['capture'] = {r['dev'].get('user_id') or ip: {k: r['stats'].get(k) for k in ('frames', 'complete', 'lost', 'mean', 'err')}
                            for ip, r in results.items()}

    means = sorted(r['stats'].get('mean') or 0 for r in results.values() if (r['stats'].get('frames') or 0) > 0)
    median = means[len(means) // 2] if means else 0
    bad_pkt, pkt_items = [], []
    n_items_before = len(rep.items)
    for ip, r in sorted(results.items(), key=lambda x: x[1]['dev'].get('user_id') or ''):
        name = r['dev'].get('user_id') or ip
        s = r['stats']
        frames, comp, lost, mean, err = s.get('frames') or 0, s.get('complete'), s.get('lost') or 0, s.get('mean'), s.get('err')
        port = live_port.get(name)
        if err or frames == 0:
            if mtu and mtu < 9000:
                cause = f'Grab 相機網卡 MTU={mtu}（應 9000）→ 相機的 jumbo 封包全部被丟（見「主機」項目）'
            elif port and r['sw1'] and r['sw1'].get('frame_len', 9416) < 9000:
                cause = f'交換機上 {name} 所插的埠最大框長只有 {r["sw1"]["frame_len"]} → 大封包全被丟'
            else:
                cause = '相機被占用、網路封包全被擋（MTU/交換機）、或相機異常'
            rep.add('取像', 'FAIL', f'{name} 取不到影像', f'錯誤：{err or "（無）"}；{args.capture_sec} 秒內 0 張',
                    cause, f'依原因處理；重跑健檢仍失敗就 {name} 斷電重開', '設備工程')
            continue
        if (comp is not None and comp < 99.5) or lost > 0:
            ev = [f'完整度 {comp}%、遺失 {lost} 張（{frames} 張中）']
            causes = []
            d0, d1 = r['sw0'], r['sw1']
            if d0 and d1:
                de = (d1.get('in_err', 0) - d0.get('in_err', 0))
                dc = (d1.get('crc', 0) - d0.get('crc', 0))
                ev.append(f'交換機埠 {port}：期間 input errors +{de}、CRC +{dc}、速率 {d1.get("speed_mbps")}Mbps')
                if dc > 0 or (de > 0 and de > (d1.get('giants', 0) - d0.get('giants', 0))):
                    causes.append(f'{name} 的線材/接頭/交換機端模組有錯誤封包（CRC/輸入錯誤增加）')
                dg = d1.get('giants', 0) - d0.get('giants', 0)
                if d1.get('frame_len') and d1['frame_len'] < 9000:
                    ev.append(f'交換機該埠最大框長 {d1["frame_len"]}（期間超長封包 +{dg}）')
                    causes.insert(0, f'交換機上 {name} 所插的埠最大框長只有 {d1["frame_len"]}（相機送 jumbo 封包需 ≥ 9000）'
                                     ' → 大封包被丟。常見於換交換機/重置後 jumbo 沒開')
                if d1.get('speed_mbps') and d1['speed_mbps'] < 1000:
                    causes.append(f'{name} 的連線只協商到 {d1["speed_mbps"]}Mbps（線材劣化或模組不良）')
            if sw and port:
                cfgtxt = sw.cmd(f'display current-configuration interface {port}', 3)
                if re.search(r'qos\s+lr|qos\s+car|line-rate', cfgtxt):
                    lim = re.search(r'(qos\s+lr[^\n]*|qos\s+car[^\n]*|line-rate[^\n]*)', cfgtxt)
                    ev.append(f'交換機 {port} 有限速設定：{lim.group(1).strip() if lim else ""}')
                    causes.append(f'交換機上 {name} 所插的埠（{port}）被設定限速（QoS），相機 1G 串流被丟包')
            if nic_drop:
                ev.append(f'Grab 相機網卡 {nic} 丟包計數增加：{ {k: v for k, v in nic_delta.items() if v} }')
                causes.append('Grab 網卡來不及收（緩衝/中斷/CPU）')
            if mtu and mtu < 9000:
                ev.append(f'Grab 相機網卡 MTU {mtu}')
                causes.append(f'Grab 相機網卡 MTU={mtu}（應 9000），相機的大封包被丟')
            if not causes:
                causes.append('交換機與 Grab 網卡都沒記錄到錯誤 → 相機封包大小/間隔設定、或多台同時傳輸塞車')
            bad_pkt.append(name)
            pkt_items.append((name, ev, causes))
            rep.add('取像', 'FAIL' if (comp or 0) < 95 else 'WARN', f'{name} 掉封包（影像不完整）', '；'.join(ev),
                    '；'.join(causes),
                    f'依原因處理：線材/模組問題 → 換標示「{name}」的線或換交換機埠；限速/MTU 設定 → 找工程依基準設定還原；'
                    '換完重跑健檢確認完整度 100%', '設備工程')
        # 參數漂移：曝光/增益和 Grab 設定檔（cam_config.json）不同（調機後沒還原、pylon Viewer 改過…）
        cid = int(name[3:]) if re.fullmatch(r'CCD\d{2}', name) else None
        wantp = cam_cfg.get(cid, {})
        expv, gainv = r['feats'].get('expo'), r['feats'].get('gain')
        drift = []
        if expv is not None and wantp.get('exposure_us') is not None and abs(float(expv) - float(wantp['exposure_us'])) > 0.5:
            drift.append(f'曝光 {expv} µs（設定 {wantp["exposure_us"]}）')
        if gainv is not None and wantp.get('gain_raw') is not None and int(float(gainv)) != int(wantp['gain_raw']):
            drift.append(f'增益 {gainv}（設定 {wantp["gain_raw"]}）')
        if drift:
            rep.add('取像', 'WARN', f'{name} 相機參數和設定不同：{"、".join(drift)}', f'亮度平均 {mean}',
                    '有人調過這台相機沒還原（相機工具 / pylon Viewer），影像會偏暗或偏亮、缺陷數異常',
                    'Control 重新載入配方（Grab ARM 會套回設定值）；或用相機工具設回設定值', '線上人員可先做')
        # 行速率：決定影像比例尺（96mm/s ÷ 8µm/line = 12000 Hz）；各台不同 → 同一片玻璃尺寸/位置不一致
        lr = r['feats'].get('lrate')
        want_lr = expected_line_rate()
        if lr is not None and want_lr and abs(float(lr) - want_lr) / want_lr > 0.01:
            rep.add('取像', 'WARN', f'{name} 行速率 {float(lr):.0f} Hz（應 {want_lr:.0f}）→ 影像比例尺不對',
                    f'相機目前 {lr} Hz；Grab 設定 {want_lr:.0f} Hz（產線 96mm/s ÷ 8µm/line）',
                    '相機行速率被改過（UserSet 鎖住舊值、相機工具調過）；歷史事故：CCD01/02 鎖在 11001 Hz 與其他台差 11%',
                    'Control 重新載入配方（Grab ARM 會把行速率設回）；仍不對 → 工程檢查相機 UserSet', '線上人員可先做')
        # 亮度：和同批其他相機比（實驗室沒光源時全部都暗 → 只提示）
        if mean is not None and median >= 8 and mean < 0.5 * median:
            cam_id = int(name[3:]) if re.fullmatch(r'CCD\d{2}', name) else None
            want = cam_cfg.get(cam_id, {})
            exp, gain = r['feats'].get('expo'), r['feats'].get('gain')
            ev = f'亮度平均 {mean:.1f}，其他相機中位數 {median:.1f}；目前曝光 {exp}、增益 {gain}；設定檔 曝光 {want.get("exposure_us")}、增益 {want.get("gain_raw")}'
            changed = (exp is not None and want.get('exposure_us') is not None and abs(float(exp) - float(want['exposure_us'])) > 0.5) or \
                      (gain is not None and want.get('gain_raw') is not None and int(float(gain)) != int(want['gain_raw']))
            if changed:
                rep.add('取像', 'FAIL', f'{name} 影像偏暗：相機曝光/增益和設定不同', ev,
                        '相機參數被改過（調機後沒還原、或用 pylon Viewer 改過）',
                        'Control 重新載入配方（Grab ARM 會套回設定檔的曝光/增益）；或用相機工具設回設定值', '線上人員可先做')
            else:
                rep.add('取像', 'FAIL', f'{name} 影像偏暗：參數正常，問題在光學', ev,
                        '光源老化/沒亮/角度偏、鏡頭光圈被轉動、鏡頭髒污或被遮擋',
                        f'檢查 {name} 對應的光源是否亮、鏡頭有無遮擋/髒污、光圈環是否被動過', '設備工程')
    # 多台同時掉封包 → 共用段的根因（Grab 網卡 MTU / 上行 / 交換機），收斂成一條，不逐台列（線上人員看得懂）
    if len(bad_pkt) >= 2 and (mtu and mtu < 9000 or len(bad_pkt) == len(results)):
        rep.items[n_items_before:] = [i for i in rep.items[n_items_before:]
                                      if not (i['area'] == '取像' and i['title'].endswith('掉封包（影像不完整）'))]
        shared = (f'Grab 相機網卡 MTU={mtu}（應 9000）→ 所有相機的 jumbo 封包被丟' if mtu and mtu < 9000 else
                  '多台同時掉封包 → 問題在共用的一段：Grab ↔ 交換機上行、交換機本身、或 Grab 相機網卡')
        rep.add('取像', 'FAIL', f'{len(bad_pkt)} 台相機同時掉封包（{"、".join(bad_pkt)}）→ 共用原因，不是個別相機', 
                '\n'.join(f'{n}：' + '；'.join(ev) for n, ev, _ in pkt_items), shared,
                ('找工程把 Grab 相機網卡 MTU 設回 9000（換網卡/重灌後常見）' if mtu and mtu < 9000 else
                 '檢查 Grab ↔ 交換機 100G 線與交換機設定（見交換機、主機項目）；不要逐台換相機線') +
                '；修好後重跑健檢確認完整度 100%', '設備工程')
    if median < 8 and means:
        rep.add('取像', 'INFO', f'全部相機畫面都很暗（亮度中位數 {median:.1f}）',
                '', '光源沒開，或沒有玻璃/背景（實驗室無光源時屬正常）', '量產前確認光源已開')
    good = [n for n in rep.facts['capture'] if n not in bad_pkt]
    if results and not bad_pkt:
        rep.add('取像', 'OK', f'取像測試 {len(results)} 台：影像完整、無掉封包', f'每台 {args.capture_sec} 秒；' +
                '、'.join(f'{n} {v["frames"]}張' for n, v in sorted(rep.facts['capture'].items())))
    elif good:
        rep.facts['capture_good'] = good


# ───────────────────────── 3. 交換機 ─────────────────────────
VOLATILE = re.compile(r'^\s*(#|return|clock|ntp|.*uptime|.*Current|sysname)', re.I)


def check_switch(rep, sw, base, args):
    if not sw:
        return
    ports = rep.facts.get('_sw_ports') or sw.ports()
    macs = rep.facts.get('_sw_macs') or sw.macs()
    from switch_console import find_port
    grab_nic = nic_with_ip('192.168.5.200')
    grab_mac = sysfs(grab_nic, 'address', '') if grab_nic else ''
    live = find_port(grab_mac, macs) if grab_mac else None
    uplink = live if live in ports else base.get('uplink')     # 讀錯的埠名不採用（console 雜訊）
    if uplink:
        base['uplink'] = uplink
    up = ports.get(uplink, {}) if uplink else {}
    if not uplink:
        rep.add('交換機', 'FAIL', '找不到交換機上接 Grab 的埠', '交換機 MAC 表沒有 Grab 相機網卡',
                'Grab ↔ 交換機 100G 線斷了、模組鬆脫、或 Grab 網卡異常 → 所有相機都會不見',
                '檢查 Grab 後面接交換機的 100G 線（兩端模組）', '設備工程')
    elif up.get('link') != 'UP':
        rep.add('交換機', 'FAIL', f'交換機 → Grab 上行埠 {uplink} 斷線（{up.get("link")}）', json.dumps(up, ensure_ascii=False),
                '100G 線/模組鬆脫或損壞、或交換機埠被關閉 → 所有相機都會不見',
                '重插 Grab 與交換機之間的 100G 線兩端；仍不行換線', '設備工程')
    else:
        rep.add('交換機', 'OK', f'交換機上行 {uplink} 正常（{up.get("speed")}）')
    adm = [p for p, v in ports.items() if v.get('link') == 'ADM']
    if adm:
        rep.add('交換機', 'WARN', f'交換機有埠被手動關閉（shutdown）：{", ".join(adm)}', '',
                '有人下了 shutdown，或換交換機後設定不同 → 相機插在這些埠會沒有訊號',
                '相機改插其他埠；或工程人員 undo shutdown', '設備工程')
    c = sw.counters(uplink) if uplink else {}
    if c.get('sys_year') and c['sys_year'] < 2020:
        rep.add('交換機', 'INFO', f'交換機時鐘未設定（顯示 {c["sys_year"]} 年）', '', '交換機沒有對時',
                '有空時 clock datetime 設定（或設 NTP 指向 Grab 192.168.10.21）；不影響取像，只影響交換機 log 時間')
    # 設定比對：console 有雜訊 → 共識比對（存基準讀 4 次、檢查讀 3 次）；只回報「每次都缺」或「多數次都多」的行
    # 實測每次讀約 5% 行有亂碼：讀 2 次 → 每次健檢約 0.75 行「兩次都讀錯」的假差異；讀 3 次 → 約 0.04 行
    stable, union, noisy = sw.config_consensus(4 if args.save_baseline else 3)
    rep.facts['_switch_cfg'] = {'stable': sorted(stable), 'union': sorted(union)}
    if noisy:
        rep.add('交換機', 'INFO', '交換機 console 讀取有雜訊（同一份設定兩次讀出不同字元）', '',
                'console 線/轉接頭品質或接觸不良（不影響交換機運作，只影響讀取）', '有空時換 USB-console 線')
    old = base.get('switch_cfg')
    if not isinstance(old, dict):
        if not args.save_baseline:
            rep.add('交換機', 'INFO', '尚無交換機設定基準', '', '', '一切正常時執行 cfaoi_triage.py --save-baseline')
    else:
        missing = sorted(set(old['stable']) - union)          # 基準有、這次兩次都讀不到
        added = sorted(stable - set(old['union']))             # 這次兩次都讀到、基準從沒出現
        diff = [f'- {l}' for l in missing] + [f'+ {l}' for l in added]
        if diff:
            miss = rep.facts.get('cam_missing') or []
            rep.add('交換機', 'FAIL' if miss else 'WARN',
                    f'交換機設定和基準不同（{len(diff)} 行）' + (f'—很可能就是 {"、".join(miss)} 不見的原因' if miss else ''),
                    '\n'.join(diff[:25]),
                    '有人改過交換機設定，或交換機更換/重置/升級後設定沒還原（例：相機埠少了 speed 1000 → 1G 相機永遠連不上）',
                    '工程人員對照差異還原（相機埠需 speed 1000、上行 100G、jumbo），再 save force；'
                    '換新交換機見 docs/troubleshooting_線上異常處理.md「交換機更換」', '設備工程')
        else:
            rep.add('交換機', 'OK', '交換機設定與基準相同')


# ───────────────────────── 4. RDMA（Grab ↔ Spark）─────────────────────────
SPARK_PROBE = r'''
n=enp1s0f0np0
echo "carrier=$(cat /sys/class/net/$n/carrier 2>/dev/null || echo 0)"
echo "speed=$(cat /sys/class/net/$n/speed 2>/dev/null)"
echo "mtu=$(cat /sys/class/net/$n/mtu 2>/dev/null)"
echo "ip=$(ip -o -4 addr show $n | awk '{print $4}')"
echo "rdma=$(rdma link show 2>/dev/null | grep -w "netdev $n" | grep -o 'state [A-Z]*' | head -1)"
echo "svc=$(systemctl is-active cfaoi-ip-production)"
echo "svc_off=$(systemctl is-active cfaoi-ip-offline)"
# RDMA CM 的監聽 ss 看不到（不是 TCP socket）→ 用 rdma resource 查 cfaoi_ip 在 18515 的 LISTEN
echo "listen=$(rdma resource show cm_id 2>/dev/null | grep 'state LISTEN' | grep -c ':18515')"
'''


def check_rdma(rep, sw, base, args):
    nic = nic_with_ip('192.168.3.2')
    if not nic:
        rep.add('RDMA', 'FAIL', 'Grab 的 RDMA 網卡沒有 192.168.3.2', '',
                'Grab 網路設定（cf-rdma 連線）沒起來或被改', '重開 Grab；仍不行找工程（nmcli 連線設定）', '設備工程')
        return
    car = sysfs(nic, 'carrier', '0') == '1'
    speed = sysfs(nic, 'speed', '?')
    mtu = sysfs(nic, 'mtu', '?')
    rc, out, _ = run(['rdma', 'link', 'show'])
    gstate = re.search(rf'state (\w+).*netdev {nic}\b', out)
    gstate = gstate.group(1) if gstate else '?'
    p1 = run(['ping', '-c', '3', '-W', '1', '-i', '0.2', SPARK_IP], timeout=10)[0] == 0
    p9 = run(['ping', '-c', '2', '-W', '1', '-M', 'do', '-s', '8972', SPARK_IP], timeout=10)[0] == 0 if p1 else False
    sp = {}
    if p1:
        rc, out, _ = ssh_spark(SPARK_PROBE, timeout=20)
        if rc == 0:
            sp = dict(l.split('=', 1) for l in out.splitlines() if '=' in l)
    iph = tcp_cmd(IP_CTRL, 'CHECK_HEALTH') if p1 else None
    ev = (f'Grab {nic}：link {"有" if car else "無"}、{speed}Mb/s、MTU {mtu}、RoCE {gstate}；'
          f'ping Spark {"通" if p1 else "不通"}、大封包 {"通" if p9 else "不通"}；'
          + (f'Spark 端：link {"有" if sp.get("carrier") == "1" else "無"}、MTU {sp.get("mtu")}、RoCE {sp.get("rdma", "?").replace("state ", "")}、'
             f'IP 生產服務 {sp.get("svc")}、調參服務 {sp.get("svc_off")}、RDMA 埠 18515 {"有在聽" if sp.get("listen", "0") != "0" else "沒在聽"}；'
             f'IP 控制埠 {"有回應" if iph else "沒回應"}' if sp else 'Spark 端：連不上（無法查詢）'))
    rep.facts['rdma'] = dict(grab_nic=nic, carrier=car, speed=speed, mtu=mtu, roce=gstate, ping=p1, jumbo=p9, spark=sp,
                             ip_health=bool(iph))
    if not car:
        rep.add('RDMA', 'FAIL', 'Grab ↔ Spark 直連線沒有 link（線的問題，或 Spark 沒開機）', ev,
                '這條線兩端都沒訊號：① Spark 關機/當機（看 Spark 電源燈）② 線或光模組鬆脫/損壞 ③ 網卡故障',
                '① 確認 Spark 電源燈亮、風扇有轉 ② 重插 Grab 與 Spark 之間的線（兩端）③ 換備用線 → 若換線就好 = 線壞；'
                '換線仍無 link 且 Spark 有開機 = 網卡/機器問題', '設備工程')
    elif not p1:
        rep.add('RDMA', 'FAIL', 'Grab ↔ Spark 有 link 但網路不通（設定問題，不是線）', ev,
                '線是好的（有 link），但 IP 位址沒設好：Spark 的 cf-rdma 連線沒起來、或位址被改',
                '重開 Spark（Control 系統狀態 → Spark → 重新開機）；仍不通找工程查 nmcli 設定', '設備工程')
    elif not p9:
        rep.add('RDMA', 'FAIL', 'Grab ↔ Spark 大封包不通（MTU 不一致）', ev,
                '兩端 MTU 應都是 9000，有一端被改小 → 影像傳輸會失敗', '找工程把兩端 MTU 設回 9000', '設備工程')
    elif not sp:
        rep.add('RDMA', 'WARN', '網路通，但無法登入 Spark 查服務狀態', ev, 'Spark SSH 服務異常或金鑰變更',
                '從 Control 系統狀態看 Spark 節點；仍異常重開 Spark', '設備工程')
    elif sp.get('svc') != 'active' and sp.get('svc_off') == 'active':
        rep.add('RDMA', 'FAIL', 'Spark 上的 IP 停在「調參模式」，生產用的收圖沒開（不是線的問題）', ev,
                '調機/調參後沒切回生產模式（生產與調參互斥）',
                'Control 系統狀態 → Spark →「IP 生產」按「切到此模式」', '線上人員可先做')
    elif sp.get('svc') != 'active' and sp.get('svc_off') != 'active':
        rep.add('RDMA', 'FAIL', '線和網路都正常，是 Spark 上的 IP 程式沒在跑（機器/程式問題，不是線）', ev,
                'IP 服務停止或反覆當掉', 'Control 系統狀態 → Spark →「IP 生產」重新啟動；反覆失敗看 log／收診斷包', '線上人員可先做')
    elif sp.get('svc') == 'active' and sp.get('listen', '0') == '0':
        rep.add('RDMA', 'WARN', 'IP 生產服務在跑，但 RDMA 埠還沒在聽', ev, 'IP 正在重啟/初始化，或卡住',
                '等 30 秒重跑健檢；仍沒在聽 → 重新啟動 IP 生產', '線上人員可先做')
    elif (gstate not in ('ACTIVE', '?')) or (sp.get('rdma') and 'ACTIVE' not in sp.get('rdma', '')):
        rep.add('RDMA', 'FAIL', 'RoCE（RDMA 硬體層）狀態不是 ACTIVE', ev, '網卡驅動/韌體異常', '重開 Grab 與 Spark', '設備工程')
    elif not iph:
        rep.add('RDMA', 'WARN', 'RDMA 鏈路正常，但 IP 控制埠（8200）沒回應', ev, 'IP 程式忙碌或卡住',
                '等 30 秒；仍無回應 → 重新啟動 IP', '線上人員可先做')
    else:
        rep.add('RDMA', 'OK', 'Grab ↔ Spark RDMA 正常（線、兩端網卡、IP 程式都正常）', ev)


# ───────────────────────── 4b. 主機（Grab / Spark 系統層）─────────────────────────
HOST_PROBE = r'''
echo "route=$(ip route get 192.168.10.1 2>/dev/null | head -1)"
echo "disk=$(df -P {out} 2>/dev/null | awk 'NR==2{{print $5}}' | tr -d %)"
echo "now=$(date +%s.%N)"
'''


def check_host(rep, sw, base, args):
    # MTU（相機網卡、RDMA 網卡都要 9000）
    for prefix, what in (('192.168.5.200', '相機網卡'), ('192.168.3.2', 'RDMA 網卡')):
        nic = nic_with_ip(prefix)
        mtu = int(sysfs(nic, 'mtu', '0') or 0) if nic else 0
        if nic and mtu < 9000:
            rep.add('主機', 'FAIL', f'Grab {what} {nic} MTU {mtu}（應 9000）',
                    '', '網路設定被改（或重灌後沒設 jumbo）→ 相機大封包/影像傳輸會被丟',
                    '找工程把 MTU 設回 9000（nmcli 該連線 802-3-ethernet.mtu 9000）', '設備工程')
    # Grab 兼路由：Control（192.168.10.x）→ Spark 要靠 ip_forward
    try:
        fwd = open('/proc/sys/net/ipv4/ip_forward').read().strip()
    except OSError:
        fwd = '?'
    if fwd != '1':
        rep.add('主機', 'FAIL', 'Grab 的轉送功能關閉（ip_forward=0）→ Control 連不到 Spark 的 IP',
                '', 'Grab 系統設定被改或重灌後沒設（Control 到 Spark 要經過 Grab 轉送）',
                '找工程開啟（/etc/sysctl.d 的 net.ipv4.ip_forward=1）；暫時可重開 Grab', '設備工程')
    # 磁碟（Grab）
    for path in ('/', ARCHIVE):
        if os.path.isdir(path):
            import shutil
            du = shutil.disk_usage(path)
            pct = du.used * 100 / du.total
            if pct >= 85:
                rep.add('主機', 'FAIL' if pct >= 95 else 'WARN', f'Grab 磁碟 {path} 已用 {pct:.0f}%',
                        f'剩 {du.free / 1e9:.0f} GB', '原始圖/結果累積太多（自動清理沒跑或保留天數太長）',
                        '確認 cfaoi-cleanup / cfaoi-archive 有在跑；找工程調保留天數', '設備工程')
    # Spark：回程路由、磁碟、時間差
    t0 = time.time()
    rc, out, _ = ssh_spark(HOST_PROBE.replace('{out}', '/home/auo001/cfaoi_output'), timeout=15)
    t1 = time.time()
    if rc != 0:
        return                                            # Spark 連不上 → RDMA 項目會說明
    sp = dict(l.split('=', 1) for l in out.splitlines() if '=' in l)
    if 'via 192.168.3.2' not in sp.get('route', ''):
        rep.add('主機', 'FAIL', 'Spark 沒有回 Control 的路由 → Control 的 IP 燈會紅（Grab 正常）',
                sp.get('route', ''), 'Spark 網路設定（cf-rdma 的 192.168.10.0/24 via 192.168.3.2）被改或沒套用',
                '找工程補回 Spark cf-rdma 連線的路由；暫時可重開 Spark', '設備工程')
    try:
        pct = int(sp.get('disk') or 0)
        if pct >= 85:
            rep.add('主機', 'FAIL' if pct >= 95 else 'WARN', f'Spark 檢測結果磁碟已用 {pct}%', '',
                    '結果/原始圖累積太多', '確認 Spark 的 cfaoi-cleanup 有在跑', '設備工程')
    except ValueError:
        pass
    try:
        off = float(sp['now']) - (t0 + t1) / 2
        rep.facts['spark_time_offset_s'] = round(off, 3)
        if abs(off) > 1.0:
            rep.add('主機', 'WARN', f'Spark 和 Grab 時間差 {off:+.1f} 秒', '',
                    '校時沒同步（Spark 應跟 Grab 對時）→ 結果日期、log 會對不上',
                    '找工程檢查 Spark timesyncd（NTP=192.168.3.2）', '設備工程')
    except (KeyError, ValueError):
        pass
    if not [i for i in rep.items if i['area'] == '主機']:
        rep.add('主機', 'OK', '主機設定正常（MTU 9000、轉送、Spark 回程路由、磁碟、時間）',
                f'Spark 時間差 {rep.facts.get("spark_time_offset_s")} 秒')


# ───────────────────────── 5. GPU / IP（Spark）─────────────────────────
GPU_PROBE = r'''
nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu,clocks.sm,power.draw --format=csv,noheader 2>&1 | head -1
echo "---xid"
journalctl -k --since "-7 days" --no-pager -o short-iso 2>/dev/null | grep -i "NVRM: Xid" | tail -300
echo "---mem"
awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo
echo "---vllm"
docker ps --format '{{.Names}}' 2>/dev/null | grep -c vllm
echo "---restarts"
systemctl show cfaoi-ip-production -p NRestarts -p ActiveState --value | tr '\n' ' '
echo
echo "---cfgdiff"
git -C {repo} status --porcelain -- ip/config recipes/DEFAULT 2>/dev/null
'''


def parse_xid(lines):
    out = []
    for l in lines:
        m = re.search(r'Xid \([^)]*\): (\d+)(.*)', l)
        if m:
            name = re.search(r'name=([^,]+)', m.group(2))
            out.append((int(m.group(1)), name.group(1) if name else '', l[:25]))
    return out


def check_gpu(rep, sw, base, args):
    rc, out, err = ssh_spark(GPU_PROBE.replace('{repo}', SPARK_REPO), timeout=25)
    if rc != 0:
        rep.add('GPU', 'WARN', '無法登入 Spark 查 GPU', (err or out)[-200:], '見 RDMA 項目', '')
        return
    sec = {}
    cur = 'smi'
    for l in out.splitlines():
        if l.startswith('---'):
            cur = l[3:]
            continue
        sec.setdefault(cur, []).append(l)
    smi = (sec.get('smi') or [''])[0]
    xids = parse_xid(sec.get('xid', []))
    mem = int((sec.get('mem') or ['0'])[0] or 0)
    vllm = int((sec.get('vllm') or ['0'])[0] or 0)
    rs = (sec.get('restarts') or [''])[0].split()
    rep.facts['gpu'] = dict(smi=smi, xid=[x[:2] for x in xids], mem_avail_mb=mem, vllm=vllm, ip_restarts=rs)
    cfgdiff = [l.strip() for l in sec.get('cfgdiff', []) if l.strip()]
    rep.facts['ip_cfg_modified'] = cfgdiff
    if cfgdiff:
        rep.add('GPU', 'WARN', f'Spark 上的 IP 參數檔被改過（和版本不同）：{", ".join(c.split()[-1] for c in cfgdiff)}',
                '\n'.join(cfgdiff), '有人手動改了預設參數/預設配方（下次 IP 重啟就會生效，影響檢測結果）',
                '工程確認是否有意修改；誤改 → 在 Spark 上 git checkout 該檔還原，再重啟 IP', '軟體工程')
    if 'NVIDIA' not in smi and 'GB10' not in smi:
        rep.add('GPU', 'FAIL', 'Spark 讀不到 GPU（nvidia-smi 失敗）', smi, 'GPU 驅動載入失敗或 GPU 掉線',
                '重開 Spark；仍失敗找工程（驅動/硬體）', '設備工程')
        return
    hw = [x for x in xids if x[0] in XID_HW]
    app = [x for x in xids if x[0] not in XID_HW]
    if hw:
        rep.add('GPU', 'FAIL', f'GPU 硬體錯誤（最近 7 天 Xid {sorted({x[0] for x in hw})}）',
                '；'.join(f'Xid {c}：{XID_HW[c]}（{t}）' for c, _, t in hw[:5]),
                'GPU 記憶體/匯流排/韌體層級錯誤', '重開 Spark 並持續觀察；重複出現 → 送修/換機', '設備工程')
    if app:
        procs = sorted({p for _, p, _ in app if p})
        ip_related = any(p.startswith('cfaoi') for p in procs)
        days = sorted({t[:10] for _, _, t in app})
        rep.add('GPU', 'WARN' if ip_related else 'INFO',
                f'GPU 程式錯誤紀錄（最近 7 天 {len(app)} 筆，來自 {", ".join(procs) or "未知程式"}；日期 {", ".join(days)}）' +
                ('' if ip_related else '——不是 CF-AOI 程式，與目前的問題無關、GPU 沒壞'),
                '；'.join(sorted({f'Xid {c}：{XID_APP.get(c, "程式層錯誤")}' for c, _, _ in app})),
                '應用程式自己的錯（非 GPU 硬體壞）' + ('；含 IP 程式 → 看 IP log/行車紀錄' if ip_related else '；不是 IP 程式（開發/測試程式）'),
                '若為 cfaoi_ip：Control 收診斷包給工程' if ip_related else '不影響生產，可忽略')
    rep.add('GPU', 'OK' if not hw else 'INFO', f'GPU 運作中：{smi}', f'Spark 可用記憶體 {mem // 1024} GB' +
            ('；機況助手大模型載入中/已載入（佔大部分記憶體）' if vllm else ''))
    if vllm and mem < 20000:
        rep.add('GPU', 'WARN', '大模型（機況助手）佔用 Spark 記憶體，生產前要關閉',
                f'可用 {mem // 1024} GB', '診斷模式中', 'Control 系統狀態 →「結束並回生產」')
    if args.gpu:
        gpu_deep(rep, mem)


REF_GLOB = [os.path.join(ARCHIVE, '50_raw', 'reference', '*IP04*'), os.path.expanduser('~/cfaoi_reference/*IP04*')]


def gpu_deep(rep, mem):
    # 1) 離線單元測試（Spark 上已編好的 *_verify；純 CPU/OpenCV 為主，驗演算法基礎沒壞）
    rc, out, _ = ssh_spark(f'cd {SPARK_REPO}/ip/build && for t in crc align coord edge rules; do '
                           f'timeout 120 ./${{t}}_verify >/tmp/triage_$t.log 2>&1; echo "$t=$?"; done', timeout=700)
    res = dict(l.split('=') for l in out.split() if '=' in l)
    bad = [k for k, v in res.items() if v != '0']
    rep.facts['unit_tests'] = res
    if not res:
        rep.add('GPU', 'WARN', '離線單元測試無法執行', out[-200:], 'Spark 上沒有編好的測試程式', '找工程重編 ip/build')
    elif bad:
        rep.add('GPU', 'FAIL', f'IP 離線單元測試失敗：{", ".join(bad)}', json.dumps(res), '程式/函式庫/環境被改壞',
                '收診斷包給工程（/tmp/triage_<項目>.log）', '軟體工程')
    else:
        rep.add('GPU', 'OK', f'IP 離線單元測試 {len(res)} 組全過（{", ".join(res)}）')
    # 2) 參考圖測圖：另起一個離線 IP（不碰生產服務），送已知答案的舊機台原圖，比對缺陷
    if mem < 12000:
        rep.add('GPU', 'WARN', '記憶體不足，略過參考圖測圖', f'Spark 可用 {mem} MB', '大模型佔用中', '結束機況助手後再跑')
        return
    ref = next((d for g in REF_GLOB for d in glob.glob(g)), None)
    img = os.path.join(ref, 'IP04_Origin000027.tif') if ref else None
    ans = os.path.join(ref, 'ip_output_20260615', 'IP04_Origin000027_DEFAULT', 'IP04_Origin000027_DEFAULT_ResultInfo.json') if ref else None
    if not (img and os.path.exists(img) and os.path.exists(ans)):
        rep.add('GPU', 'WARN', '找不到參考圖，略過測圖', '應在 50_raw/reference/', '', '')
        return
    with open(ans, encoding='utf-8') as f:
        want = sorted((d['GlobalPosX'], d['GlobalPosY'], d['Type']) for r in json.load(f)['RoiInfoList'] for d in r['DefectInfoList'])
    port = 8299
    rc, _, err = run(['scp', '-q', '-o', 'BatchMode=yes', img, f'{SPARK}:/tmp/triage_ref.tif'], timeout=60)
    if rc != 0:
        rep.add('GPU', 'WARN', '參考圖傳不到 Spark', err[-200:])
        return
    ssh_spark(f'cd {SPARK_REPO}/ip/build && (nohup timeout 240 ./cfaoi_ip --mode offline-tcp --control-port {port} '
              f'--output /tmp/triage_ip_out > /tmp/triage_ip.log 2>&1 &)', timeout=15)
    t0 = time.time()
    while time.time() - t0 < 60 and not tcp_cmd((SPARK_IP, port), 'CHECK_HEALTH', timeout=2):
        time.sleep(1)
    # 標準答案（2026-06-15）是用 IP 預設參數（ini）跑的，不載配方（DEFAULT 配方門檻不同 → 0 顆）。
    # 同一張圖跑兩次：結果必須逐位一致（不一致 = GPU 記憶體/計算不穩的典型徵兆）。
    runs, ms = [], 0
    try:
        for k in range(2):
            t1 = time.time()
            r = tcp_cmd((SPARK_IP, port), 'REVIEW_LOCAL_IMAGE', {'path': '/tmp/triage_ref.tif', 'panel_id': f'triage_ref{k}'}, timeout=120)
            ms = (time.time() - t1) * 1000
            runs.append(r)
    finally:
        ssh_spark(f'pkill -f "cfaoi_ip --mode offline-tcp --control-port {port}"; rm -f /tmp/triage_ref.tif', timeout=15)
    r = runs[0] if runs else None
    if not r or r.get('status') != 'OK':
        rep.add('GPU', 'FAIL', '參考圖測圖失敗（IP 無法處理影像）', json.dumps(r, ensure_ascii=False)[:300] if r else '離線 IP 沒有回應',
                'GPU/CUDA 異常、或 IP 程式異常', '看 Spark /tmp/triage_ip.log；重開 Spark 後重測；仍失敗找工程', '軟體工程')
        return
    sig = lambda x: sorted((d.get('GlobalPosX'), d.get('GlobalPosY'), d.get('Type'), d.get('Size'))  # noqa: E731
                           for z in (x or {}).get('result', {}).get('RoiInfoList', []) for d in z.get('DefectInfoList', []))
    got = [g[:3] for g in sig(r)]
    rep.facts['ref_test'] = dict(want=want, got=got, ms=round(ms), deterministic=len(runs) == 2 and sig(runs[0]) == sig(runs[1]))
    if len(runs) == 2 and sig(runs[0]) != sig(runs[1]):
        rep.add('GPU', 'FAIL', '同一張參考圖跑兩次結果不同（GPU 計算不穩定）', f'第 1 次 {sig(runs[0])[:5]}；第 2 次 {sig(runs[1])[:5]}',
                'GPU 記憶體/計算錯誤（硬體）或程式有競態', '重開 Spark 重測；仍不一致 → GPU 硬體送修', '設備工程')
        return
    close = len(got) == len(want) and all(g[2] == w[2] and abs(g[0] - w[0]) <= 2 and abs(g[1] - w[1]) <= 2
                                          for g, w in zip(got, want))
    if close:
        exact = got == want
        rep.add('GPU', 'OK', f'參考圖測圖正確：{len(got)} 顆缺陷、類型與位置相符，兩次結果一致（{ms:.0f} ms，含讀檔）',
                f'IP04_Origin000027：標準 {want}；實得 {got}' + ('' if exact else '（座標差 ≤2 px：6/15 後演算法有修改，屬預期）'))
    else:
        modified = rep.facts.get('ip_cfg_modified')
        rep.add('GPU', 'FAIL', '參考圖測圖結果與標準答案不同' + ('—IP 參數檔被改過，很可能就是原因' if modified else ''),
                f'應為 {want}；實得 {got[:10]}',
                ('IP 預設參數檔和版本不同（見上一項）' if modified else 'GPU 計算錯誤、或 IP 演算法/預設參數被改'),
                '確認 ip/config/default_zone.ini 未被改（誤改 → git checkout 還原）；重開 Spark 重測；仍不同找工程', '軟體工程')


# ───────────────────────── 輸出 ─────────────────────────
def render(rep, machine):
    fails = [i for i in rep.items if i['level'] == 'FAIL']
    warns = [i for i in rep.items if i['level'] == 'WARN']
    oks = [i for i in rep.items if i['level'] == 'OK']
    L = [f'# CF-AOI 一鍵健檢 {NOW:%Y-%m-%d %H:%M}（機台 {machine}）', '']
    if fails:
        L.append(f'## 結論：❌ {len(fails)} 項異常、⚠️ {len(warns)} 項注意、✅ {len(oks)} 項正常')
    elif warns:
        L.append(f'## 結論：⚠️ {len(warns)} 項注意、✅ {len(oks)} 項正常（可生產，但請處理注意事項）')
    else:
        L.append(f'## 結論：✅ 全部正常（{len(oks)} 項）')
    L.append('')
    n = 0
    for i in fails + warns:
        n += 1
        L.append(f'{n}. {ICON[i["level"]]} **[{i["area"]}] {i["title"]}**')
        if i['cause']:
            L.append(f'   - 可能原因：{i["cause"]}')
        if i['action']:
            L.append(f'   - 請這樣做：{i["action"]}' + (f'（{i["who"]}）' if i['who'] else ''))
    L += ['', '## 各項明細（給工程師）', '']
    for area in ['相機', '取像', '交換機', 'RDMA', '主機', 'GPU']:
        its = sorted([i for i in rep.items if i['area'] == area], key=lambda x: RANK[x['level']])
        if not its:
            continue
        L.append(f'### {area}')
        for i in its:
            L.append(f'- {ICON[i["level"]]} {i["title"]}')
            if i['evidence']:
                for e in str(i['evidence']).splitlines():
                    L.append(f'  - 證據：{e}')
        L.append('')
    return '\n'.join(L)


def main():
    ap = argparse.ArgumentParser(description='CF-AOI 一鍵健檢')
    ap.add_argument('--gpu', action='store_true', help='另跑 GPU 深度檢查（單元測試 + 參考圖）')
    ap.add_argument('--save-baseline', action='store_true')
    ap.add_argument('--only', default='')
    ap.add_argument('--capture-sec', type=float, default=5.0)
    ap.add_argument('--out', default='')
    ap.add_argument('--json', action='store_true', help='stdout 改印 JSON')
    args = ap.parse_args()
    only = set(filter(None, args.only.split(',')))
    want = lambda k: not only or k in only  # noqa: E731
    rep = Report()
    base = load_baseline()
    sw = open_switch(rep) if (want('camera') or want('capture') or want('switch')) else None
    try:
        for key, fn in [('camera', check_cameras), ('capture', check_capture), ('switch', check_switch),
                        ('rdma', check_rdma), ('host', check_host), ('gpu', check_gpu)]:
            if not want(key):
                continue
            if key == 'capture' and 'camera' not in only and only and '_devs' not in rep.facts:
                check_cameras(rep, sw, base, args)
            try:
                fn(rep, sw, base, args)
            except Exception as e:  # noqa: BLE001 — 一項壞不影響其他項
                rep.add({'camera': '相機', 'capture': '取像', 'switch': '交換機', 'rdma': 'RDMA', 'host': '主機', 'gpu': 'GPU'}[key],
                        'WARN', f'這一項檢查本身出錯：{e.__class__.__name__}', str(e)[:300], '', '回報工程（附這份報告）')
    finally:
        if sw:
            sw.close()
    if args.save_baseline:
        if rep.facts.get('_switch_cfg'):
            base['switch_cfg'] = rep.facts['_switch_cfg']
        base['saved_at'] = NOW.isoformat(timespec='seconds')
        rep.add('相機', 'INFO', f'已存基準：{save_baseline(base)}（相機 MAC {len(base.get("macs", {}))} 台）')
    elif base.get('macs'):
        save_baseline(base)                       # 相機 MAC 隨時更新（不見的台保留舊值）
    machine = env_file('/etc/default/cfaoi-archive').get('MACHINE_ID', socket.gethostname())
    md = render(rep, machine)
    facts = {k: v for k, v in rep.facts.items() if not k.startswith('_')}
    d = args.out or (os.path.join(ARCHIVE, '10_logs', f'{NOW:%Y%m%d}') if os.path.isdir(os.path.join(ARCHIVE, '10_logs'))
                     else os.path.expanduser(f'~/cfaoi_logs/triage/{NOW:%Y%m%d}'))
    os.makedirs(d, exist_ok=True)
    stem = os.path.join(d, f'grab_triage_{NOW:%H%M%S}')
    with open(stem + '.md', 'w', encoding='utf-8') as f:
        f.write(md + '\n')
    with open(stem + '.json', 'w', encoding='utf-8') as f:
        json.dump({'time': NOW.isoformat(timespec='seconds'), 'machine': machine, 'items': rep.items, 'facts': facts},
                  f, ensure_ascii=False, indent=1, default=str)
    print(json.dumps({'report': stem + '.md', 'items': rep.items}, ensure_ascii=False) if args.json else md)
    print(f'\n（報告：{stem}.md）', file=sys.stderr)
    return {0: 2, 1: 1}.get(rep.worst(), 0)


if __name__ == '__main__':
    sys.exit(main())
