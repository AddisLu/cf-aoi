#!/usr/bin/env python3
"""
上位機對測工具（CF_ 協議）— Grab 桌面用，跟 Control / 真上位機對測。

依據：L8A CF IP Control Handshake Spec_20240704.xlsx（命令/參數表）
      20250217_CF_AOI_IP_說明.pptx「CF Command Flow (Alignment)」（命令順序）
  Controller(上位機) = Client、AOI(Control) = Server；'|' 分隔、'\\r\\n' 結尾。
  Request ：CMD|Param1|…|Param9
  Response：Result(OK/Error)|Param1|…|Param9（Param9 = Error Msg）

兩種模式（畫面上切換）：
  Client：扮演上位機 → 連 Control:8787，送命令、看回應          （送出 → 收到）
  Server：扮演 AOI 端 ← 讓真上位機連進來，依設定自動回覆        （收到 → 回覆）

功能：IP/port 設定（常用清單、記住上次）、連線指示（狀態/對端/連線時間/最後收發/斷線偵測/自動重連/心跳）、
      Log 監控視窗（篩選/搜尋/暫停/下載/開資料夾）、指令新增/編輯/刪除、P1..P9 參數編輯與即時監控、
      一鍵流程（順序可編、可多輪）、每筆收發對照規格檢查（⚠ 不擋送出）。

只用 Python 標準函式庫（Grab 不需另裝套件）。介面是本機網頁（同 cam_align 做法）：
  python3 upstream_tester.py                     # 開 UI
  python3 upstream_tester.py --flow              # headless：跑一次流程（exit 0=全 OK）
  python3 upstream_tester.py --send "CF_READY"   # headless：送一條命令
設定檔：~/.config/cfaoi/upstream_tester.json     紀錄檔：~/cfaoi_upstream_tester/logs/YYYYMMDD.log
"""
import argparse
import copy
import datetime as dt
import errno
import ipaddress
import json
import os
import re
import select
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
UI_PORT = 8790
CFG_PATH = os.path.expanduser("~/.config/cfaoi/upstream_tester.json")
LOG_DIR = os.path.expanduser("~/cfaoi_upstream_tester/logs")
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
SPEC_CMDS = ["CF_LOAD_RECIPE", "CF_GRAB_START", "CF_CHECK_ALIGN", "CF_SET_ALIGN", "CF_GET_RESULT"]

E9 = [""] * 9


def _cmd(name, desc, labels=None, params=None, reply_labels=None, reply=None, builtin=True, spec=True):
    pad = lambda xs: (list(xs or []) + E9)[:9]
    rl = pad(reply_labels)
    rl[8] = rl[8] or "Error Msg"
    return {"name": name, "desc": desc, "builtin": builtin, "spec": spec,
            "labels": pad(labels), "params": pad(params),
            "replyLabels": rl, "reply": {"result": "OK", "params": pad(reply), "delayMs": 0}}


def default_commands():
    return [
        _cmd("CF_LOAD_RECIPE", "載入配方（Param9 Detect Mode：0 inline、1 loadImage）",
             ["RecipeName", "PanelID", "Time", "", "", "", "", "", "DetectMode"],
             ["DEFAULT", "TEST0001", "{now}", "", "", "", "", "", "0"]),
        _cmd("CF_GRAB_START", "開始取像（規格無參數；Control 可吃 Param1=逾時 ms）"),
        _cmd("CF_CHECK_ALIGN", "對位檢查（僅傳給作 Alignment 的 IP_PC）；回 ShiftX/ShiftY",
             reply_labels=["ShiftX", "ShiftY"], reply=["0.00", "0.00"]),
        _cmd("CF_SET_ALIGN", "套用對位（Response 後開始影像處理）",
             ["ShiftX", "ShiftY"], ["{shiftX}", "{shiftY}"]),
        _cmd("CF_GET_RESULT", "取結果：Param1 結果檔路徑、Param2 DefectCnt（逗號分隔，每 IP 一個）",
             reply_labels=["Result file path", "DefectCnt"], reply=["ResultIP0.xml,ResultIP1.xml", "0,0"]),
        _cmd("CF_READY", "Control 擴充（規格表沒有）：系統是否可生產", builtin=False, spec=False),
        _cmd("CF_STOP", "Control 擴充（規格表沒有）：中斷取像", builtin=False, spec=False),
    ]


def default_config():
    return {
        "mode": "client",
        "client": {"host": "192.168.10.1", "port": 8787, "timeoutSec": 60, "autoReconnect": True,
                   "autoConnect": False, "heartbeatSec": 0, "heartbeatCmd": "CF_READY"},
        "server": {"bind": "0.0.0.0", "port": 8787, "autoStart": True},
        "presets": [{"name": "Control 控制網（Windows）", "host": "192.168.10.1", "port": 8787},
                    {"name": "本機", "host": "127.0.0.1", "port": 8787}],
        "commands": default_commands(),
        "flow": {"steps": list(SPEC_CMDS), "delayMs": 0, "stopOnError": True, "rounds": 1},
    }


# ─────────────────────────── 共用小工具 ───────────────────────────
def now_stamp():
    return dt.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")


def ts():
    return dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def strip_key(v):
    """規格表欄位寫成 'DefectCnt=9000,20' / 'Time={…}'；容許有或沒有 key= 前綴。"""
    v = (v or "").strip()
    m = re.match(r"^[A-Za-z][A-Za-z _]*=(.*)$", v)
    return m.group(1).strip() if m else v


def to_float(v):
    try:
        return float(strip_key(v))
    except ValueError:
        return None


def build_line(head, params):
    p = [str(x) for x in params][:9]
    p += [""] * (9 - len(p))
    return "|".join([head] + p)


def pad10(fields):
    return (list(fields) + [""] * 10)[:max(10, len(fields))]


def valid_host(h, allow_any=False):
    h = (h or "").strip()
    if allow_any and h == "0.0.0.0":
        return True
    if re.fullmatch(r"[0-9.]+", h):
        try:
            return isinstance(ipaddress.ip_address(h), ipaddress.IPv4Address)
        except ValueError:
            return False
    return bool(re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?", h))


def valid_port(p):
    try:
        return 1 <= int(p) <= 65535
    except (TypeError, ValueError):
        return False


def local_ips():
    ips = []
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=2).stdout
        ips = [x for x in out.split() if ":" not in x]
    except (OSError, subprocess.SubprocessError):
        pass
    return ips


# ─────────────────────────── 規格檢查 ───────────────────────────
def check_response(cmd, resp):
    """Control/AOI 的回應 → (fields, checks, extra)。checks = [(level, msg)]，level ∈ ok/warn。"""
    f = resp.split("|")
    checks, extra = [], {}
    if len(f) != 10:
        checks.append(("warn", f"欄位數 {len(f)}，規格為 10（Result + Param1..Param9）"))
    res = f[0].strip()
    if res not in ("OK", "Error", "ERR"):
        checks.append(("warn", f"Result='{res}'，規格只有 OK / Error"))
    if res == "ERR":
        checks.append(("warn", "回 'ERR'；規格表寫 'OK/Error' — 需與上位機確認錯誤字串"))
    P = lambda i: f[i] if i < len(f) else ""
    if res != "OK":
        if not P(9).strip():
            checks.append(("warn", "失敗時 Param9 應帶 Error Msg，但是空的"))
        return f, checks, extra
    if cmd == "CF_CHECK_ALIGN":
        if to_float(P(1)) is not None and to_float(P(2)) is not None:
            extra = {"shiftX": P(1).strip(), "shiftY": P(2).strip()}
            checks.append(("ok", f"ShiftX={P(1)}  ShiftY={P(2)}（規格位置 Param1/Param2）"))
        elif to_float(P(2)) is not None and to_float(P(3)) is not None:
            extra = {"shiftX": P(2).strip(), "shiftY": P(3).strip()}
            checks.append(("warn", f"Param1='{P(1)}'，ShiftX/Y 在 Param2/Param3 — 規格 20240704 為 "
                                   "Param1=ShiftX、Param2=ShiftY，位置不同"))
        else:
            checks.append(("warn", "找不到數值 ShiftX/ShiftY（規格 Param1/Param2）"))
    elif cmd == "CF_GET_RESULT":
        paths = [x for x in strip_key(P(1)).split(",") if x.strip()]
        cnts = [x for x in strip_key(P(2)).split(",") if x.strip()]
        bad = [c for c in cnts if not re.fullmatch(r"-?\d+", c.strip())]
        if not paths:
            checks.append(("warn", "Param1 沒有結果檔路徑（本片無結果？）"))
        if bad:
            checks.append(("warn", f"DefectCnt 有非整數：{bad}"))
        if paths and len(paths) != len(cnts):
            checks.append(("warn", f"結果檔 {len(paths)} 個、DefectCnt {len(cnts)} 個，數量應一致（每個 IP 一組）"))
        if paths and not bad and len(paths) == len(cnts):
            checks.append(("ok", "；".join(f"{p.strip()} → {c.strip()}" for p, c in zip(paths, cnts))))
    return f, checks, extra


def check_request(cmd, line, known):
    """上位機送來的請求（Server 模式）→ checks。"""
    f = line.split("|")
    checks = []
    if len(f) != 10:
        checks.append(("warn", f"欄位數 {len(f)}，規格為 10（Command + Param1..Param9）"))
    if cmd not in known:
        checks.append(("warn", f"未定義的命令 {cmd}（可在「指令管理」新增）"))
    P = lambda i: f[i] if i < len(f) else ""
    if cmd == "CF_LOAD_RECIPE":
        if not P(1).strip():
            checks.append(("warn", "Param1 RecipeName 是空的"))
        if not P(2).strip():
            checks.append(("warn", "Param2 PanelID 是空的"))
        if not re.fullmatch(r"\d{4}-\d\d-\d\d-\d\d-\d\d-\d\d", strip_key(P(3))):
            checks.append(("warn", f"Param3 Time='{P(3)}'，規格格式 yyyy-MM-dd-HH-mm-ss"))
        if strip_key(P(9)) not in ("0", "1"):
            checks.append(("warn", f"Param9 DetectMode='{P(9)}'，規格為 0(inline) / 1(loadImage)"))
    elif cmd == "CF_SET_ALIGN":
        if to_float(P(1)) is not None and to_float(P(2)) is not None:
            checks.append(("ok", f"ShiftX={P(1)}  ShiftY={P(2)}"))
        elif to_float(P(2)) is not None and to_float(P(3)) is not None:
            checks.append(("warn", f"Param1='{P(1)}'，Shift 在 Param2/Param3 — 規格為 Param1=ShiftX、Param2=ShiftY"))
        else:
            checks.append(("warn", "Param1/Param2 不是數值 ShiftX/ShiftY"))
    return checks


# ─────────────────────────── 紀錄 ───────────────────────────
class Log:
    def __init__(self, echo=True):
        self.items = deque(maxlen=5000)
        self.next = 0
        self.lock = threading.Lock()
        self.echo = echo
        os.makedirs(LOG_DIR, exist_ok=True)

    def add(self, kind, text, **kw):
        now = dt.datetime.now()
        with self.lock:
            e = {"seq": self.next, "t": now.strftime("%H:%M:%S.%f")[:-3], "kind": kind, "text": text}
            e.update(kw)
            self.items.append(e)
            self.next += 1
        line = f"{now:%Y-%m-%d %H:%M:%S.%f}"[:-3] + f" [{kw.get('label') or kind}] {text}"
        if kw.get("ms") is not None:
            line += f"  ({kw['ms']} ms)"
        for lv, msg in kw.get("checks", []):
            line += f"\n    {'✓' if lv == 'ok' else '⚠'} {msg}"
        try:
            with open(os.path.join(LOG_DIR, f"{now:%Y%m%d}.log"), "a", encoding="utf-8") as fp:
                fp.write(line + "\n")
        except OSError:
            pass
        if self.echo:
            print(line, flush=True)
        return e

    def since(self, n):
        with self.lock:
            return [e for e in self.items if e["seq"] >= n]


# ─────────────────────────── 引擎 ───────────────────────────
class Engine:
    def __init__(self, cfg_path=CFG_PATH, echo=True):
        self.cfg_path = cfg_path
        self.cfg = self._load_cfg()
        self.mode = self.cfg["mode"]
        self.mu = threading.RLock()
        self.log = Log(echo)
        self.monitor = {}           # cmd → {"req":…, "resp":…, "n":int}
        self.mon_ver = 0
        self.cfg_ver = 0
        self.vars = {"shiftX": "0", "shiftY": "0", "round": "1"}
        self.conn = {"state": "idle", "text": "未連線", "peer": "", "since": None,
                     "lastTx": None, "lastRx": None, "lastErr": "", "retries": 0}
        # client
        self.sock, self.buf = None, b""
        self.io = threading.Lock()
        self.want = False
        self.last_try = 0.0
        self.last_act = 0.0
        # server
        self.lsock = None
        self.csock = None
        self.srv_gen = 0
        # flow
        self.flow = {"running": False, "stop": False, "step": "", "round": 0, "pass": 0, "fail": 0}
        if self.mode == "server":
            self._set_conn("stopped", "未監聽")
        threading.Thread(target=self._watch, daemon=True).start()

    # ── 設定 ──
    def _load_cfg(self):
        cfg = default_config()
        try:
            with open(self.cfg_path, encoding="utf-8") as fp:
                saved = json.load(fp)
            for k in ("client", "server", "flow"):
                if isinstance(saved.get(k), dict):
                    cfg[k].update(saved[k])
            for k in ("mode", "presets", "commands"):
                if k in saved:
                    cfg[k] = saved[k]
            if cfg["mode"] not in ("client", "server"):
                cfg["mode"] = "client"
            for c in cfg["commands"]:   # 補齊舊檔缺的欄位
                base = _cmd(c.get("name", "CF_X"), "")
                for k, v in base.items():
                    c.setdefault(k, copy.deepcopy(v))
                c["reply"].setdefault("delayMs", 0)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            print(f"設定檔讀取失敗（改用預設）：{e}", file=sys.stderr)
        return cfg

    def save_cfg(self):
        with self.mu:
            self.cfg["mode"] = self.mode
            self.cfg_ver += 1
            data = json.dumps(self.cfg, ensure_ascii=False, indent=2)
        try:
            os.makedirs(os.path.dirname(self.cfg_path), exist_ok=True)
            tmp = self.cfg_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                fp.write(data)
            os.replace(tmp, self.cfg_path)
        except OSError as e:
            self.log.add("error", f"設定檔寫入失敗：{e}")

    def find_cmd(self, name):
        for c in self.cfg["commands"]:
            if c["name"] == name:
                return c
        return None

    def resolve(self, s, extra=None):
        def rep(m):
            k = m.group(1)
            if k == "now":
                return now_stamp()
            if extra and k in extra:
                return extra[k]
            if k in self.vars:
                return str(self.vars[k])
            return m.group(0)
        return re.sub(r"\{(\w+)\}", rep, s or "")

    # ── 狀態 / 監控 ──
    def _set_conn(self, state, text, **kw):
        with self.mu:
            self.conn.update(state=state, text=text, **kw)

    def _mon(self, cmd, part, fields, **kw):
        with self.mu:
            m = self.monitor.setdefault(cmd, {"req": None, "resp": None, "n": 0})
            m[part] = {"t": ts(), "fields": pad10(fields), **kw}
            if part == "req":
                m["n"] += 1
            self.mon_ver += 1

    def clear_monitor(self):
        with self.mu:
            self.monitor = {}
            self.mon_ver += 1

    def state(self, since):
        with self.mu:
            c = dict(self.conn)
            if c["since"]:
                c["uptime"] = int(time.time() - c["since"])
            return {"mode": self.mode, "conn": c, "log": self.log.since(since), "monVer": self.mon_ver,
                    "monitor": self.monitor, "flow": dict(self.flow), "vars": dict(self.vars),
                    "cfgVer": self.cfg_ver, "logdir": LOG_DIR}

    # ── 模式 ──
    def set_mode(self, mode):
        if mode not in ("client", "server") or mode == self.mode:
            return
        if self.mode == "client":
            self.flow["stop"] = True
            self.disconnect(quiet=True)
        else:
            self.stop_server(quiet=True)
        self.mode = mode
        self.log.add("info", "切換為 " + ("Client：扮演上位機 → 連 Control" if mode == "client"
                                         else "Server：扮演 AOI 端，等上位機連入"))
        reset = dict(peer="", since=None, lastTx=None, lastRx=None, lastErr="", retries=0, hb=None, listen="")
        if mode == "client":
            self._set_conn("idle", "未連線", **reset)
        else:
            self._set_conn("stopped", "未監聽", **reset)
        self.clear_monitor()        # 兩模式方向相反（送出→收到 / 收到→回覆），不混在同一張監控表
        self.save_cfg()

    # ── Client ──
    def connect(self, host=None, port=None):
        if self.mode != "client":
            return False, "目前是 Server 模式"
        host = (host or self.cfg["client"]["host"]).strip()
        port = port or self.cfg["client"]["port"]
        if not valid_host(host):
            return False, f"IP/主機名稱格式不對：{host!r}"
        if not valid_port(port):
            return False, f"port 必須是 1–65535：{port!r}"
        with self.mu:
            self.cfg["client"]["host"], self.cfg["client"]["port"] = host, int(port)
        self.save_cfg()
        with self.io:
            self._close_sock()
            self.want = True
            ok = self._connect()
        return ok, "" if ok else self.conn["lastErr"]

    def _connect(self, reconnect=False):
        host, port = self.cfg["client"]["host"], int(self.cfg["client"]["port"])
        self.last_try = time.monotonic()
        self._set_conn("connecting", f"連線中 {host}:{port} …")
        try:
            s = socket.create_connection((host, port), timeout=3.0)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError as e:
            with self.mu:
                self.conn["retries"] += 1
                n = self.conn["retries"]
            err = f"連不上 {host}:{port}：{e}"
            auto = self.want and self.cfg["client"]["autoReconnect"]
            self._set_conn("reconnecting" if auto else "error",
                           f"自動重連中（第 {n} 次）" if auto else "連線失敗", lastErr=err, peer="")
            if not reconnect or n == 1 or n % 20 == 0:
                self.log.add("error", err + ("（Control 有開嗎？網路線/防火牆？）" if n == 1 else f"（已重試 {n} 次）"))
            return False
        self.sock, self.buf = s, b""
        self.last_act = time.monotonic()
        with self.mu:
            n = self.conn["retries"]
            self.conn["retries"] = 0
        self._set_conn("connected", "已連線", peer=f"{host}:{port}", since=time.time(), lastErr="")
        self.log.add("info", f"{'重連成功' if reconnect else '已連線'} Control {host}:{port}"
                     + (f"（重試 {n} 次後）" if reconnect and n else ""))
        return True

    def _close_sock(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def disconnect(self, quiet=False):
        self.want = False
        with self.io:
            had = self.sock is not None
            self._close_sock()
        self._set_conn("idle", "未連線", peer="", since=None, retries=0)
        if had and not quiet:
            self.log.add("info", "已斷線（手動）")

    def _lost(self, reason):
        self._close_sock()
        auto = self.want and self.cfg["client"]["autoReconnect"]
        self._set_conn("reconnecting" if auto else "error", f"斷線：{reason}" + ("，自動重連中" if auto else ""),
                       lastErr=reason, peer="", since=None)
        self.log.add("error", f"連線中斷：{reason}" + ("（自動重連中）" if auto else ""))

    def _take_lines(self, kind_label):
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            text = line.decode("utf-8", "replace").rstrip("\r")
            self.log.add("warn", f"收到未對應任何命令的資料：{text!r}", label=kind_label)

    def _readline(self, timeout):
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buf:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"{timeout:g} 秒內沒收到回應")
            self.sock.settimeout(left)
            d = self.sock.recv(4096)
            if not d:
                raise ConnectionError("Control 關閉連線")
            self.buf += d
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode("utf-8", "replace").rstrip("\r")

    def send(self, line, timeout=None, hb=False):
        """Client：送一條、讀一行回應。"""
        if self.mode != "client":
            return {"ok": False, "error": "目前是 Server 模式（不主動送命令）"}
        line = (line or "").strip()
        if not line:
            return {"ok": False, "error": "空命令"}
        timeout = float(timeout or self.cfg["client"]["timeoutSec"] or 60)
        cmd = line.split("|", 1)[0].strip().upper()
        with self.io:
            if not self.sock:
                self.want = True
                if not self._connect():
                    return {"ok": False, "error": self.conn["lastErr"]}
            try:
                self.sock.setblocking(False)
                try:
                    while True:
                        d = self.sock.recv(4096)
                        if not d:
                            raise ConnectionError("Control 關閉連線")
                        self.buf += d
                except BlockingIOError:
                    pass
                finally:
                    if self.sock:
                        self.sock.setblocking(True)
                self._take_lines("未預期")
                t0 = time.monotonic()
                self.sock.sendall((line + "\r\n").encode("utf-8"))
                self.last_act = time.monotonic()
                self._set_conn(self.conn["state"], self.conn["text"], lastTx=ts())
                self.log.add("tx", line, cmd=cmd, hb=hb, label="送出")
                self._mon(cmd, "req", line.split("|"), dir="送出", hb=hb)
                resp = self._readline(timeout)
                ms = int((time.monotonic() - t0) * 1000)
                self.last_act = time.monotonic()
            except TimeoutError as e:
                self.log.add("error", f"{cmd}：{e}", cmd=cmd, hb=hb)
                self._mon(cmd, "resp", ["(逾時)"], dir="收到", ms=None, ok=False)
                return {"ok": False, "error": str(e)}
            except (OSError, ConnectionError) as e:
                self._lost(f"{cmd} 收發失敗：{e}")
                return {"ok": False, "error": str(e)}
        fields, checks, extra = check_response(cmd, resp)
        ok = fields[0].strip() == "OK"
        if extra:
            with self.mu:
                self.vars.update(extra)
        self._set_conn(self.conn["state"], self.conn["text"], lastRx=ts())
        self.log.add("rx", resp, cmd=cmd, ms=ms, ok=ok, checks=checks, hb=hb, label="收到")
        self._mon(cmd, "resp", fields, dir="收到", ms=ms, ok=ok, checks=checks, hb=hb)
        return {"ok": ok, "resp": resp, "fields": fields, "checks": checks, "extra": extra, "ms": ms}

    def cmd_line(self, name, params=None):
        c = self.find_cmd(name)
        p = params if params is not None else (c["params"] if c else E9)
        return build_line(name, [self.resolve(x) for x in p])

    def send_cmd(self, name, params=None, timeout=None):
        if not self.find_cmd(name):
            return {"ok": False, "error": f"沒有這個指令：{name}"}
        return self.send(self.cmd_line(name, params), timeout)

    def _watch(self):
        while True:
            time.sleep(0.5)
            try:
                if self.mode == "client":
                    self._watch_client()
            except Exception as e:  # 監看執行緒不能死
                self.log.add("error", f"連線監看例外：{e}")

    def _watch_client(self):
        cc = self.cfg["client"]
        if self.sock and self.io.acquire(blocking=False):
            try:
                if self.sock:
                    r, _, _ = select.select([self.sock], [], [], 0)
                    if r:
                        d = self.sock.recv(4096)
                        if not d:
                            self._lost("Control 關閉連線")
                        else:
                            self.buf += d
                            self._take_lines("未預期")
            except OSError as e:
                self._lost(str(e))
            finally:
                self.io.release()
        hb = float(cc.get("heartbeatSec") or 0)
        if (hb > 0 and self.sock and not self.flow["running"]
                and time.monotonic() - self.last_act >= hb and self.find_cmd(cc.get("heartbeatCmd", ""))):
            r = self.send_cmd(cc["heartbeatCmd"], timeout=min(10.0, max(2.0, hb)))
            with self.mu:
                self.conn["hb"] = {"t": ts(), "ok": bool(r.get("ok")), "ms": r.get("ms")}
        if (self.sock is None and self.want and cc.get("autoReconnect")
                and time.monotonic() - self.last_try >= 3.0):
            with self.io:
                if self.sock is None and self.want:
                    self._connect(reconnect=True)

    # ── Flow（Client）──
    def start_flow(self, steps=None, rounds=None, delay_ms=None, stop_on_error=None):
        if self.mode != "client":
            return False, "流程只在 Client 模式"
        with self.mu:
            if self.flow["running"]:
                return False, "流程執行中"
            f = self.cfg["flow"]
            steps = steps or f["steps"]
            bad = [s for s in steps if not self.find_cmd(s)]
            if bad:
                return False, f"流程內有未定義指令：{bad}"
            if not steps:
                return False, "流程是空的"
            self.flow.update(running=True, stop=False, step="", round=0, **{"pass": 0, "fail": 0})
        args = (list(steps), int(f["rounds"] if rounds is None else rounds),
                int(f["delayMs"] if delay_ms is None else delay_ms),
                bool(f["stopOnError"] if stop_on_error is None else stop_on_error))
        threading.Thread(target=self._run_flow, args=args, daemon=True).start()
        return True, ""

    def stop_flow(self):
        self.flow["stop"] = True

    def _run_flow(self, steps, rounds, delay_ms, stop_on_error):
        allok = True
        try:
            r = 0
            while (rounds <= 0 or r < rounds) and not self.flow["stop"]:
                r += 1
                self.flow["round"] = r
                self.vars["round"] = str(r)
                self.log.add("info", f"── 第 {r}{'' if rounds <= 0 else '/' + str(rounds)} 輪：" + " → ".join(steps) + " ──")
                round_ok = True
                for s in steps:
                    if self.flow["stop"]:
                        break
                    self.flow["step"] = s
                    res = self.send_cmd(s)
                    if not res.get("ok"):
                        round_ok = allok = False
                        if stop_on_error:
                            self.log.add("error", f"── 流程中止於 {s}（遇錯即停）──")
                            self.flow["stop"] = True
                            break
                    if delay_ms > 0 and not self.flow["stop"]:
                        time.sleep(delay_ms / 1000)
                self.flow["pass" if round_ok else "fail"] += 1
            st = "已手動停止" if self.flow["stop"] and allok else ("全部 OK" if allok else "有命令失敗")
            self.log.add("info" if allok else "warn",
                         f"── 流程結束：{st}（PASS {self.flow['pass']} / FAIL {self.flow['fail']}）──")
            return allok
        finally:
            self.flow.update(running=False, step="")

    def run_flow_blocking(self):
        ok, err = self.start_flow()
        if not ok:
            self.log.add("error", err)
            return False
        time.sleep(0.05)
        while self.flow["running"]:
            time.sleep(0.05)
        return self.flow["fail"] == 0 and self.flow["pass"] > 0

    # ── Server ──
    def start_server(self, bind=None, port=None):
        if self.mode != "server":
            return False, "目前是 Client 模式"
        bind = (bind or self.cfg["server"]["bind"]).strip()
        port = port or self.cfg["server"]["port"]
        if not valid_host(bind, allow_any=True):
            return False, f"監聽位址格式不對：{bind!r}"
        if not valid_port(port):
            return False, f"port 必須是 1–65535：{port!r}"
        self.stop_server(quiet=True)
        with self.mu:
            self.cfg["server"]["bind"], self.cfg["server"]["port"] = bind, int(port)
        self.save_cfg()
        try:
            ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            ls.bind((bind, int(port)))
            ls.listen(5)
            ls.settimeout(0.5)
        except OSError as e:
            err = f"無法監聽 {bind}:{port}：{e}" + ("（port 被占用？同機的 Control 是否也在聽 8787？）"
                                                   if getattr(e, "errno", 0) == 98 else "")
            self._set_conn("error", "監聽失敗", lastErr=err)
            self.log.add("error", err)
            return False, err
        self.lsock = ls
        self.srv_gen += 1
        ips = ", ".join(f"{ip}:{port}" for ip in local_ips()) or f"{bind}:{port}"
        self._set_conn("listening", "等待上位機連入", peer="", since=None, lastErr="", listen=ips)
        self.log.add("info", f"Server 監聽 {bind}:{port}（上位機請連：{ips}）")
        threading.Thread(target=self._accept_loop, args=(ls, self.srv_gen), daemon=True).start()
        return True, ""

    def stop_server(self, quiet=False):
        self.srv_gen += 1
        had = self.lsock is not None
        for s in (self.csock, self.lsock):
            if s:
                try:
                    s.close()
                except OSError:
                    pass
        self.csock = self.lsock = None
        self._set_conn("stopped", "未監聽", peer="", since=None)
        if had and not quiet:
            self.log.add("info", "Server 停止監聽")

    def _accept_loop(self, ls, gen):
        # 單一條連線出事（上位機連進來又馬上斷等）accept 會丟這些 errno：監聽本身還在，
        # 只能略過重試。舊版一律 break → 執行緒默默結束，listen socket 卻還開著：
        # 上位機 TCP 連得上（核心自己完成三次握手、排進 accept queue），UI 卻永遠停在
        # 「等待上位機連入」，且沒有任何 log。2026-11-04 實際踩到。
        transient = {errno.ECONNABORTED, errno.EINTR, errno.EAGAIN, errno.EWOULDBLOCK,
                     errno.EPROTO, errno.ENOTCONN, errno.ETIMEDOUT, errno.ECONNRESET}
        while gen == self.srv_gen:
            try:
                c, addr = ls.accept()
            except socket.timeout:
                continue
            except OSError as e:
                if gen != self.srv_gen:
                    break                                    # 停止監聽／換 port：正常收工
                if getattr(e, "errno", None) in transient:
                    self.log.add("warn", f"accept 暫時性錯誤，繼續等待：{e}")
                    continue
                err = f"accept 失敗，已停止接受連線：{e}"
                self._set_conn("error", "監聽中斷", lastErr=err)
                self.log.add("error", err + "（請按「重新監聽」）")
                break
            if self.csock:
                self.log.add("warn", f"新上位機 {addr[0]}:{addr[1]} 連入，關閉前一條連線（一次只服務一台）")
                try:
                    self.csock.close()
                except OSError:
                    pass
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.csock = c
            self._set_conn("connected", "上位機已連入", peer=f"{addr[0]}:{addr[1]}", since=time.time())
            self.log.add("info", f"上位機已連入：{addr[0]}:{addr[1]}")
            threading.Thread(target=self._serve_client, args=(c, addr, gen), daemon=True).start()

    def _serve_client(self, c, addr, gen):
        buf = b""
        reason = "上位機關閉連線"
        try:
            while gen == self.srv_gen:
                try:
                    d = c.recv(4096)
                except OSError as e:
                    reason = str(e)
                    break
                if not d:
                    break
                buf += d
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", "replace").rstrip("\r")
                    if line.strip():
                        self._handle_request(c, line)
        finally:
            if self.csock is c:
                self.csock = None
                try:
                    c.close()
                except OSError:
                    pass
                if gen == self.srv_gen:
                    self._set_conn("listening", "等待上位機連入", peer="", since=None)
                    self.log.add("warn", f"上位機 {addr[0]}:{addr[1]} 斷線：{reason}")

    def _handle_request(self, c, line):
        t0 = time.monotonic()
        f = line.split("|")
        cmd = f[0].strip().upper()
        known = [x["name"] for x in self.cfg["commands"]]
        checks = check_request(cmd, line, known)
        self._set_conn(self.conn["state"], self.conn["text"], lastRx=ts())
        self.log.add("rx", line, cmd=cmd, checks=checks, label="收到")
        self._mon(cmd, "req", f, dir="收到", checks=checks)
        d = self.find_cmd(cmd)
        if d is None:
            result, params, delay = "Error", E9[:8] + [f"unknown command: {cmd}"], 0
        else:
            rp = d["reply"]
            extra = {f"p{i}": (f[i] if i < len(f) else "") for i in range(1, 10)}
            result = rp.get("result") or "OK"
            params = [self.resolve(x, extra) for x in rp["params"]]
            if result != "OK" and not params[8]:
                params[8] = "Error"
            delay = int(rp.get("delayMs") or 0)
        if delay > 0:
            time.sleep(delay / 1000)
        resp = build_line(result, params)
        try:
            c.sendall((resp + "\r\n").encode("utf-8"))
        except OSError as e:
            self.log.add("error", f"回覆 {cmd} 失敗：{e}")
            return
        ms = int((time.monotonic() - t0) * 1000)
        self._set_conn(self.conn["state"], self.conn["text"], lastTx=ts())
        self.log.add("tx", resp, cmd=cmd, ms=ms, ok=result == "OK", label="回覆")
        self._mon(cmd, "resp", resp.split("|"), dir="回覆", ms=ms, ok=result == "OK")

    # ── 指令管理 ──
    def save_command(self, data, original=None):
        name = (data.get("name") or "").strip().upper()
        if not NAME_RE.match(name):
            return False, "指令名稱只能用大寫英數與底線、英文字開頭（例：CF_MY_CMD）"
        pad = lambda xs: ([str(x) for x in (xs or [])] + E9)[:9]
        with self.mu:
            cur = self.find_cmd(original or name)
            if original and original != name and self.find_cmd(name):
                return False, f"已有指令 {name}"
            if cur is None:
                if self.find_cmd(name):
                    return False, f"已有指令 {name}"
                cur = _cmd(name, "", builtin=False, spec=False)
                self.cfg["commands"].append(cur)
            elif cur.get("builtin") and name != cur["name"]:
                return False, "規格內建指令不能改名"
            old = cur["name"]
            cur["name"] = name
            for k in ("desc",):
                if k in data:
                    cur[k] = str(data[k])
            for k in ("labels", "params", "replyLabels"):
                if k in data:
                    cur[k] = pad(data[k])
            if "reply" in data:
                r = data["reply"] or {}
                cur["reply"] = {"result": "OK" if r.get("result", "OK") == "OK" else "Error",
                                "params": pad(r.get("params")),
                                "delayMs": max(0, int(r.get("delayMs") or 0))}
            if old != name:
                self.cfg["flow"]["steps"] = [name if s == old else s for s in self.cfg["flow"]["steps"]]
        self.save_cfg()
        return True, ""

    def delete_command(self, name):
        with self.mu:
            c = self.find_cmd(name)
            if not c:
                return False, "沒有這個指令"
            if c.get("builtin"):
                return False, "規格內建指令不能刪除"
            self.cfg["commands"].remove(c)
            self.cfg["flow"]["steps"] = [s for s in self.cfg["flow"]["steps"] if s != name]
        self.save_cfg()
        self.log.add("info", f"已刪除指令 {name}")
        return True, ""

    def reset_commands(self):
        with self.mu:
            self.cfg["commands"] = default_commands()
            self.cfg["flow"]["steps"] = list(SPEC_CMDS)
        self.save_cfg()
        self.log.add("info", "指令已還原為預設")

    def save_settings(self, data):
        with self.mu:
            if "client" in data:
                c = data["client"]
                cc = self.cfg["client"]
                if "host" in c:
                    if not valid_host(c["host"]):
                        return False, f"IP/主機名稱格式不對：{c['host']!r}"
                    cc["host"] = c["host"].strip()
                if "port" in c:
                    if not valid_port(c["port"]):
                        return False, "port 必須是 1–65535"
                    cc["port"] = int(c["port"])
                for k in ("autoReconnect", "autoConnect"):
                    if k in c:
                        cc[k] = bool(c[k])
                for k, lo in (("timeoutSec", 1), ("heartbeatSec", 0)):
                    if k in c:
                        try:
                            cc[k] = max(lo, float(c[k]))
                        except (TypeError, ValueError):
                            return False, f"{k} 必須是數字"
                if "heartbeatCmd" in c:
                    cc["heartbeatCmd"] = str(c["heartbeatCmd"])
            if "server" in data:
                s = data["server"]
                if "bind" in s:
                    if not valid_host(s["bind"], allow_any=True):
                        return False, "監聽位址格式不對"
                    self.cfg["server"]["bind"] = s["bind"].strip()
                if "port" in s:
                    if not valid_port(s["port"]):
                        return False, "port 必須是 1–65535"
                    self.cfg["server"]["port"] = int(s["port"])
                if "autoStart" in s:
                    self.cfg["server"]["autoStart"] = bool(s["autoStart"])
            if "presets" in data:
                ps = []
                for p in data["presets"]:
                    if not valid_host(p.get("host", "")) or not valid_port(p.get("port")):
                        return False, f"常用位址格式不對：{p}"
                    ps.append({"name": str(p.get("name") or p["host"]), "host": p["host"].strip(),
                               "port": int(p["port"])})
                self.cfg["presets"] = ps
            if "flow" in data:
                fl = data["flow"]
                if "steps" in fl:
                    bad = [s for s in fl["steps"] if not self.find_cmd(s)]
                    if bad:
                        return False, f"未定義指令：{bad}"
                    self.cfg["flow"]["steps"] = list(fl["steps"])
                for k in ("delayMs", "rounds"):
                    if k in fl:
                        self.cfg["flow"][k] = max(0, int(fl[k] or 0))
                if "stopOnError" in fl:
                    self.cfg["flow"]["stopOnError"] = bool(fl["stopOnError"])
        self.save_cfg()
        return True, ""

    def autostart(self):
        if self.mode == "server" and self.cfg["server"].get("autoStart"):
            self.start_server()
        elif self.mode == "client" and self.cfg["client"].get("autoConnect"):
            self.connect()


# ─────────────────────────── HTTP ───────────────────────────
def make_handler(eng: Engine):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype, code=200, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", code)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/":
                try:
                    with open(os.path.join(HERE, "ui.html"), "rb") as fp:
                        return self._send(fp.read(), "text/html; charset=utf-8")
                except OSError as e:
                    return self._send(f"ui.html 讀不到：{e}".encode(), "text/plain; charset=utf-8", 500)
            if u.path == "/api/state":
                return self._json(eng.state(int(q.get("since", ["0"])[0])))
            if u.path == "/api/config":
                with eng.mu:
                    return self._json({"cfg": eng.cfg, "cfgVer": eng.cfg_ver, "localIps": local_ips(),
                                       "cfgPath": eng.cfg_path})
            if u.path == "/api/logfile":
                day = q.get("day", [dt.datetime.now().strftime("%Y%m%d")])[0]
                if not re.fullmatch(r"\d{8}", day):
                    return self._json({"ok": False}, 400)
                p = os.path.join(LOG_DIR, f"{day}.log")
                try:
                    with open(p, "rb") as fp:
                        body = fp.read()
                except OSError:
                    body = "（今天還沒有紀錄）\n".encode()
                return self._send(body, "text/plain; charset=utf-8", 200,
                                  {"Content-Disposition": f'attachment; filename="upstream_tester_{day}.log"'})
            self.send_error(404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                a = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json({"ok": False, "error": "bad json"}, 400)
            p = urlparse(self.path).path
            r = lambda ok, err="": self._json({"ok": ok, "error": err})
            if p == "/api/mode":
                eng.set_mode(a.get("mode"))
                return r(True)
            if p == "/api/connect":
                return r(*eng.connect(a.get("host"), a.get("port")))
            if p == "/api/disconnect":
                eng.disconnect()
                return r(True)
            if p == "/api/server/start":
                return r(*eng.start_server(a.get("bind"), a.get("port")))
            if p == "/api/server/stop":
                eng.stop_server()
                return r(True)
            if p == "/api/send":
                return self._json(eng.send_cmd(a.get("name", ""), a.get("params")))
            if p == "/api/raw":
                return self._json(eng.send(a.get("line", "")))
            if p == "/api/flow/start":
                return r(*eng.start_flow())
            if p == "/api/flow/stop":
                eng.stop_flow()
                return r(True)
            if p == "/api/command/save":
                return r(*eng.save_command(a.get("cmd") or {}, a.get("original")))
            if p == "/api/command/delete":
                return r(*eng.delete_command(a.get("name", "")))
            if p == "/api/command/reset":
                eng.reset_commands()
                return r(True)
            if p == "/api/settings":
                return r(*eng.save_settings(a))
            if p == "/api/monitor/clear":
                eng.clear_monitor()
                return r(True)
            if p == "/api/openlogdir":
                try:
                    subprocess.Popen(["xdg-open", LOG_DIR], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return r(True)
                except OSError as e:
                    return r(False, f"無法開啟資料夾：{e}")
            self.send_error(404)
    return H


def serve_ui(eng, ui_port, open_browser=True):
    url = f"http://127.0.0.1:{ui_port}/"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", ui_port), make_handler(eng))
    except OSError:
        # 已經開著一份（例如桌面圖示點兩次）→ 直接開瀏覽器到那一份
        print(f"UI 已在執行：{url}")
        if open_browser:
            webbrowser.open(url)
        return 0
    print(f"上位機對測工具 UI：{url}（Ctrl+C 結束）")
    eng.autostart()
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        eng.disconnect(quiet=True)
        eng.stop_server(quiet=True)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="上位機對測工具（CF_ 協議）")
    ap.add_argument("--host", help="Client：Control 位址（覆寫設定檔）")
    ap.add_argument("--port", type=int, help="Client：Control port（覆寫設定檔）")
    ap.add_argument("--ui-port", type=int, default=UI_PORT, help=f"本機網頁 port（預設 {UI_PORT}）")
    ap.add_argument("--config", default=CFG_PATH, help=f"設定檔（預設 {CFG_PATH}）")
    ap.add_argument("--no-browser", action="store_true", help="只起 UI 後端，不自動開瀏覽器")
    ap.add_argument("--flow", action="store_true", help="不開 UI：依設定檔流程跑（exit 0=全 OK）")
    ap.add_argument("--send", metavar="LINE", help="不開 UI：送一條原始命令後結束")
    a = ap.parse_args(argv)

    eng = Engine(a.config)
    if a.host or a.port:
        eng.cfg["client"]["host"] = a.host or eng.cfg["client"]["host"]
        eng.cfg["client"]["port"] = a.port or eng.cfg["client"]["port"]
    if a.send or a.flow:
        eng.mode = "client"
        eng.want = True
        if a.send:
            ok = eng.send(a.send).get("ok")
        else:
            ok = eng.run_flow_blocking()
        eng.disconnect(quiet=True)
        return 0 if ok else 1
    return serve_ui(eng, a.ui_port, not a.no_browser)


if __name__ == "__main__":
    sys.exit(main())
