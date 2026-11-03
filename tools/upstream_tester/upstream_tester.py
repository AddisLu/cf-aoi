#!/usr/bin/env python3
"""
上位機對測工具（CF_ 協議）— 在 Grab 桌面扮演「上位機 Controller（client）」連 Control:8787 對測。

依據：L8A CF IP Control Handshake Spec_20240704.xlsx（命令/參數表）
      20250217_CF_AOI_IP_說明.pptx「CF Command Flow (Alignment)」（命令順序）
  Controller = Client、AOI(Control) = Server；'|' 分隔、'\\r\\n' 結尾。
  Request ：CMD|Param1|…|Param9
  Response：Result(OK/Error)|Param1|…|Param9（Param9 = Error Msg）
  流程    ：CF_LOAD_RECIPE → CF_GRAB_START → CF_CHECK_ALIGN → CF_SET_ALIGN → CF_GET_RESULT

只用 Python 標準函式庫（Grab 不需另裝套件）。介面是本機網頁（同 cam_align 做法）：
  python3 upstream_tester.py                    # 開 UI（預設連 192.168.10.1:8787）
  python3 upstream_tester.py --host 127.0.0.1   # 改預設目標
  python3 upstream_tester.py --flow             # 不開 UI，跑一次完整流程（headless / SSH）
  python3 upstream_tester.py --send "CF_READY"  # 不開 UI，送一條命令

每次收發都記到 ~/cfaoi_upstream_tester/logs/YYYYMMDD.log（對測完可附給對方）。
每筆回應都會對照規格檢查格式，不符處標 ⚠（不擋送出——對測就是要看到真實差異）。
"""
import argparse
import datetime as dt
import json
import os
import re
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DEFAULT_HOST = "192.168.10.1"   # Control（Windows）控制網位址，見 docs/STATUS.md 控制網
DEFAULT_PORT = 8787             # UpstreamServer 監聽 port
UI_PORT = 8790
LOG_DIR = os.path.expanduser("~/cfaoi_upstream_tester/logs")

SPEC_CMDS = ["CF_LOAD_RECIPE", "CF_GRAB_START", "CF_CHECK_ALIGN", "CF_SET_ALIGN", "CF_GET_RESULT"]
EXT_CMDS = ["CF_READY", "CF_STOP"]   # Control 有實作、規格表沒有的擴充命令


# ─────────────────────────── 規格檢查 ───────────────────────────
def strip_key(v):
    """規格表欄位寫成 'DefectCnt=9000,20' / 'Result file path=…'；收的時候容許有或沒有 key= 前綴。"""
    v = v.strip()
    m = re.match(r"^[A-Za-z][A-Za-z _]*=(.*)$", v)
    return m.group(1).strip() if m else v


def to_float(v):
    try:
        return float(strip_key(v))
    except ValueError:
        return None


def build_request(cmd, params):
    """CMD|P1|…|P9（補滿 9 參數，與規格表欄位一致）。"""
    p = [str(x) for x in params][:9]
    p += [""] * (9 - len(p))
    return "|".join([cmd] + p)


def check_response(cmd, resp):
    """回傳 (fields, checks, extra)。checks = [(level, msg)]，level ∈ ok / warn。"""
    f = resp.split("|")
    checks = []
    extra = {}
    if len(f) != 10:
        checks.append(("warn", f"欄位數 {len(f)}，規格為 10（Result + Param1..Param9）"))
    res = f[0].strip() if f else ""
    if res not in ("OK", "Error", "ERR"):
        checks.append(("warn", f"Result='{res}'，規格只有 OK / Error"))
    if res == "ERR":
        checks.append(("warn", "Control 回 'ERR'；規格表寫 'OK/Error' — 需與上位機確認錯誤字串"))
    P = lambda i: f[i] if i < len(f) else ""
    if res != "OK":
        if not P(9).strip():
            checks.append(("warn", "失敗時 Param9 應帶 Error Msg，但是空的"))
        return f, checks, extra

    if cmd == "CF_CHECK_ALIGN":
        # 規格：Param1=ShiftX、Param2=ShiftY
        if to_float(P(1)) is not None and to_float(P(2)) is not None:
            extra = {"shiftX": to_float(P(1)), "shiftY": to_float(P(2))}
            checks.append(("ok", f"ShiftX={P(1)}  ShiftY={P(2)}（規格位置 Param1/Param2）"))
        elif to_float(P(2)) is not None and to_float(P(3)) is not None:
            extra = {"shiftX": to_float(P(2)), "shiftY": to_float(P(3))}
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


# ─────────────────────────── TCP client ───────────────────────────
class Tester:
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.sock = None
        self.buf = b""
        self.lock = threading.Lock()        # 一次只送一條命令
        self.log = []                       # [{seq, t, kind, text, ...}]
        self.log_lock = threading.Lock()
        self.flow_running = False
        os.makedirs(LOG_DIR, exist_ok=True)

    # 紀錄（UI 輪詢 + 寫檔）
    def add(self, kind, text, **kw):
        now = dt.datetime.now()
        with self.log_lock:
            e = {"seq": len(self.log), "t": now.strftime("%H:%M:%S.%f")[:-3], "kind": kind, "text": text}
            e.update(kw)
            self.log.append(e)
        line = f"{now:%Y-%m-%d %H:%M:%S.%f}"[:-3] + f" [{kind}] {text}"
        if kw.get("ms") is not None:
            line += f"  ({kw['ms']} ms)"
        for lv, msg in kw.get("checks", []):
            line += f"\n    {'✓' if lv == 'ok' else '⚠'} {msg}"
        try:
            with open(os.path.join(LOG_DIR, f"{now:%Y%m%d}.log"), "a", encoding="utf-8") as fp:
                fp.write(line + "\n")
        except OSError:
            pass
        print(line, flush=True)

    @property
    def connected(self):
        return self.sock is not None

    def connect(self, host=None, port=None, timeout=3.0):
        self.disconnect(quiet=True)
        if host:
            self.host = host
        if port:
            self.port = int(port)
        try:
            s = socket.create_connection((self.host, self.port), timeout=timeout)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock, self.buf = s, b""
            self.add("info", f"已連線 Control {self.host}:{self.port}")
            return True
        except OSError as e:
            self.add("error", f"連不上 {self.host}:{self.port}：{e}（Control 有開嗎？網路線/防火牆 8787？）")
            return False

    def disconnect(self, quiet=False):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
            if not quiet:
                self.add("info", "已斷線")

    def _drain_unsolicited(self):
        """送命令前先收掉 Control 主動送來的資料（記錄下來，不混進下一條回應）。"""
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
            self.sock.setblocking(True)
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            self.add("warn", f"收到未對應任何命令的資料：{line.decode('utf-8', 'replace').rstrip()!r}")

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

    def send(self, line, timeout=60.0):
        """送一條命令、讀一行回應。回 dict（ok/resp/fields/checks/extra/ms/error）。"""
        with self.lock:
            if not self.sock and not self.connect():
                return {"ok": False, "error": "未連線"}
            cmd = line.split("|", 1)[0].strip().upper()
            try:
                self._drain_unsolicited()
                t0 = time.monotonic()
                self.sock.sendall((line + "\r\n").encode("utf-8"))
                self.add("tx", line, cmd=cmd)
                resp = self._readline(timeout)
                ms = int((time.monotonic() - t0) * 1000)
            except (OSError, TimeoutError, ConnectionError) as e:
                self.add("error", f"{cmd}：{e}")
                if not isinstance(e, TimeoutError):
                    self.disconnect()
                return {"ok": False, "error": str(e)}
            fields, checks, extra = check_response(cmd, resp)
            ok = fields[0].strip() == "OK"
            self.add("rx", resp, cmd=cmd, ms=ms, ok=ok, checks=checks)
            return {"ok": ok, "resp": resp, "fields": fields, "checks": checks, "extra": extra, "ms": ms}

    def run_flow(self, a, stop_on_error=True, step_delay_ms=0, timeout=60.0):
        """依 pptx「CF Command Flow (Alignment)」順序跑一片。a = 各命令參數 dict。"""
        self.flow_running = True
        self.add("info", "── 流程開始：LOAD_RECIPE → GRAB_START → CHECK_ALIGN → SET_ALIGN → GET_RESULT ──")
        allok = True
        try:
            shift = (a.get("shiftX", ""), a.get("shiftY", ""))
            for cmd in SPEC_CMDS:
                if cmd == "CF_SET_ALIGN":
                    line = set_align_line(shift[0], shift[1], a.get("setAlignFmt", "spec"))
                else:
                    line = make_line(cmd, a)
                r = self.send(line, timeout)
                if cmd == "CF_CHECK_ALIGN" and r.get("extra"):
                    shift = (fmt_num(r["extra"]["shiftX"]), fmt_num(r["extra"]["shiftY"]))
                if not r.get("ok"):
                    allok = False
                    if stop_on_error:
                        self.add("error", f"── 流程中止於 {cmd}（勾選了「遇錯即停」）──")
                        return False
                if step_delay_ms > 0:
                    time.sleep(step_delay_ms / 1000)
            self.add("info" if allok else "warn", "── 流程結束：" + ("全部 OK" if allok else "有命令回 Error") + " ──")
            return allok
        finally:
            self.flow_running = False


def fmt_num(x):
    return f"{x:g}" if isinstance(x, float) else str(x)


def now_stamp():
    return dt.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")


def make_line(cmd, a):
    if cmd == "CF_LOAD_RECIPE":
        # Param1 RecipeName / Param2 PanelID / Param3 Time / Param9 DetectMode(0 inline, 1 loadImage)
        return build_request(cmd, [a.get("recipe", ""), a.get("panel", ""), a.get("time") or now_stamp(),
                                   "", "", "", "", "", a.get("detectMode", "0")])
    if cmd == "CF_GRAB_START":
        # 規格無參數；Control 接受 Param1=timeoutMs（空 → 40000）
        return build_request(cmd, [a.get("grabTimeout", "")])
    if cmd == "CF_SET_ALIGN":
        return set_align_line(a.get("shiftX", ""), a.get("shiftY", ""), a.get("setAlignFmt", "spec"))
    return build_request(cmd, [])


def set_align_line(sx, sy, fmt):
    if fmt == "control":
        # Control 現行解析：Param1=result、Param2=ShiftX、Param3=ShiftY（UpstreamServer.cs）
        return build_request("CF_SET_ALIGN", ["Cs_AlignSet", sx, sy])
    return build_request("CF_SET_ALIGN", [sx, sy])     # 規格 20240704：Param1=ShiftX、Param2=ShiftY


# ─────────────────────────── 網頁 UI ───────────────────────────
PAGE = r"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<title>上位機對測（CF_ 協議）</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--fg:#1d2330;--mut:#667085;--line:#d8dce3;--ok:#127a3a;--err:#b42318;--warn:#a15c00;--acc:#1f5fbf}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 "Noto Sans CJK TC","Noto Sans TC",sans-serif;background:var(--bg);color:var(--fg)}
header{padding:10px 16px;background:#1d2330;color:#fff;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header h1{font-size:16px;margin:0 12px 0 0}
#dot{width:12px;height:12px;border-radius:50%;background:#888;display:inline-block}
main{display:grid;grid-template-columns:430px 1fr;gap:12px;padding:12px;height:calc(100vh - 52px)}
@media(max-width:900px){main{grid-template-columns:1fr;height:auto}}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-bottom:10px}
.card h2{font-size:14px;margin:0 0 8px}
.row{display:flex;gap:6px;align-items:center;margin:4px 0;flex-wrap:wrap}
label{color:var(--mut);font-size:12px;min-width:74px}
input,select{font:inherit;padding:4px 6px;border:1px solid var(--line);border-radius:4px;min-width:0}
input.w{flex:1}button{font:inherit;padding:5px 10px;border-radius:4px;border:1px solid var(--acc);background:var(--acc);color:#fff;cursor:pointer}
button.sec{background:#fff;color:var(--acc)}button:disabled{opacity:.45;cursor:default}
.cmd{border-top:1px dashed var(--line);padding-top:6px;margin-top:6px}
.cmd b{font-family:monospace}.note{color:var(--mut);font-size:12px}
#left{overflow:auto}#logwrap{display:flex;flex-direction:column;min-height:0}
#log{flex:1;overflow:auto;background:#0f1420;color:#d6dbe6;border-radius:8px;padding:8px;font:13px/1.5 monospace;min-height:300px}
.e{white-space:pre-wrap;word-break:break-all}.tx{color:#7fb2ff}.rx.ok{color:#7ee2a0}.rx.bad{color:#ff8a80}
.info{color:#9aa4b5}.error{color:#ff8a80}.warn{color:#ffcc66}.chk{padding-left:2em}.chk.ok{color:#7ee2a0}.chk.warn{color:#ffcc66}
.fields{padding-left:2em;color:#9aa4b5}
</style></head><body>
<header><h1>上位機對測（CF_ 協議）</h1><span id="dot"></span><span id="st">未連線</span>
<span class="note" style="color:#aab">規格 20240704 · Controller=Client → Control=Server</span></header>
<main>
<div id="left">
 <div class="card"><h2>連線</h2>
  <div class="row"><label>Control</label><input id="host" class="w"><input id="port" size="5">
   <button onclick="api('connect',{host:v('host'),port:v('port')})">連線</button>
   <button class="sec" onclick="api('disconnect')">斷線</button></div>
  <div class="row"><label>回應逾時</label><input id="timeout" value="60" size="4"> 秒</div>
 </div>
 <div class="card"><h2>一鍵流程（pptx CF Command Flow）</h2>
  <div class="note">LOAD_RECIPE → GRAB_START → CHECK_ALIGN → SET_ALIGN（自動帶入 CHECK_ALIGN 的 Shift）→ GET_RESULT</div>
  <div class="row"><label>步驟間隔</label><input id="delay" value="0" size="5"> ms
   <label style="min-width:0"><input type="checkbox" id="stop" checked> 遇錯即停</label></div>
  <div class="row"><button id="flowbtn" onclick="flow()">▶ 跑一片</button></div>
 </div>
 <div class="card"><h2>單一命令</h2>
  <div class="cmd"><div class="row"><b>CF_LOAD_RECIPE</b><button onclick="one('CF_LOAD_RECIPE')">送出</button></div>
   <div class="row"><label>P1 Recipe</label><input id="recipe" class="w" value="DEFAULT"></div>
   <div class="row"><label>P2 PanelID</label><input id="panel" class="w" value="TEST0001"></div>
   <div class="row"><label>P3 Time</label><input id="time" class="w" placeholder="空白=送出當下 yyyy-MM-dd-HH-mm-ss"></div>
   <div class="row"><label>P9 Detect</label><select id="detectMode"><option value="0">0：inline</option><option value="1">1：loadImage（offline 讀圖）</option></select></div></div>
  <div class="cmd"><div class="row"><b>CF_GRAB_START</b><button onclick="one('CF_GRAB_START')">送出</button></div>
   <div class="row"><label>P1 逾時 ms</label><input id="grabTimeout" class="w" placeholder="規格無參數；空白=不帶（Control 預設 40000）"></div></div>
  <div class="cmd"><div class="row"><b>CF_CHECK_ALIGN</b><button onclick="one('CF_CHECK_ALIGN')">送出</button></div>
   <div class="note">回應 Param1=ShiftX、Param2=ShiftY；收到後自動填入下方 SET_ALIGN</div></div>
  <div class="cmd"><div class="row"><b>CF_SET_ALIGN</b><button onclick="one('CF_SET_ALIGN')">送出</button></div>
   <div class="row"><label>ShiftX</label><input id="shiftX" size="8" value="0"><label style="min-width:0">ShiftY</label><input id="shiftY" size="8" value="0"></div>
   <div class="row"><label>格式</label><select id="setAlignFmt" class="w">
    <option value="spec">規格 20240704：P1=ShiftX、P2=ShiftY</option>
    <option value="control">Control 現行：P1=Cs_AlignSet、P2=ShiftX、P3=ShiftY</option></select></div></div>
  <div class="cmd"><div class="row"><b>CF_GET_RESULT</b><button onclick="one('CF_GET_RESULT')">送出</button></div>
   <div class="note">回應 Param1=結果檔路徑（逗號分隔，每 IP 一個）、Param2=DefectCnt（逗號分隔）</div></div>
  <div class="cmd"><div class="row"><b>擴充（規格表沒有）</b>
   <button class="sec" onclick="one('CF_READY')">CF_READY</button><button class="sec" onclick="one('CF_STOP')">CF_STOP</button></div></div>
  <div class="cmd"><div class="row"><b>自訂原始字串</b></div>
   <div class="row"><input id="raw" class="w" placeholder="例：CF_LOAD_RECIPE|R1|P1|2026-11-04-10-00-00||||||0"><button onclick="raw()">送出</button></div></div>
 </div>
</div>
<div id="logwrap" class="card" style="margin:0"><div class="row"><h2 style="margin:0;flex:1">收發紀錄</h2>
 <span class="note" id="logpath"></span><button class="sec" onclick="document.getElementById('log').innerHTML='';">清畫面</button></div>
 <div id="log"></div></div>
</main>
<script>
const $=id=>document.getElementById(id), v=id=>$(id).value.trim();
let seq=0;
function args(){const a={};for(const k of ['recipe','panel','time','detectMode','grabTimeout','shiftX','shiftY','setAlignFmt'])a[k]=v(k);
 a.timeout=+v('timeout')||60;return a}
async function api(p,body){const r=await fetch('/api/'+p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
 const j=await r.json();if(j.extra&&j.extra.shiftX!==undefined){$('shiftX').value=j.extra.shiftX;$('shiftY').value=j.extra.shiftY}return j}
function one(cmd){api('cmd',Object.assign(args(),{cmd}))}
function raw(){if(v('raw'))api('raw',{line:v('raw'),timeout:+v('timeout')||60})}
function flow(){api('flow',Object.assign(args(),{stop:$('stop').checked,delay:+v('delay')||0}))}
function esc(s){return s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function render(e){const d=document.createElement('div');d.className='e';
 if(e.kind==='tx')d.innerHTML=`<span class="tx">${e.t} → ${esc(e.text)}</span>`;
 else if(e.kind==='rx'){const f=e.text.split('|');
  d.innerHTML=`<span class="rx ${e.ok?'ok':'bad'}">${e.t} ← ${esc(e.text)}  (${e.ms} ms)</span>`+
  `<div class="fields">${f.map((x,i)=>x?`${i?'P'+i:'Result'}=${esc(x)}`:'').filter(Boolean).join('  ')}</div>`+
  (e.checks||[]).map(c=>`<div class="chk ${c[0]}">${c[0]==='ok'?'✓':'⚠'} ${esc(c[1])}</div>`).join('')}
 else d.innerHTML=`<span class="${e.kind}">${e.t} ${esc(e.text)}</span>`;
 const L=$('log');const atEnd=L.scrollTop+L.clientHeight>=L.scrollHeight-20;L.appendChild(d);if(atEnd)L.scrollTop=L.scrollHeight}
async function poll(){try{const j=await (await fetch('/api/state?since='+seq)).json();
 for(const e of j.log){render(e);seq=e.seq+1}
 $('dot').style.background=j.connected?'#2ecc71':'#888';$('st').textContent=j.connected?`已連線 ${j.host}:${j.port}`:'未連線';
 $('flowbtn').disabled=j.flow;$('logpath').textContent='紀錄檔：'+j.logdir;
 if(!$('host').value){$('host').value=j.host;$('port').value=j.port}}catch(e){$('st').textContent='工具後端已關閉'}
 setTimeout(poll,500)}
poll();
</script></body></html>"""


def make_handler(t: Tester):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/":
                b = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            elif u.path == "/api/state":
                since = int(parse_qs(u.query).get("since", ["0"])[0])
                with t.log_lock:
                    log = t.log[since:]
                self._json({"connected": t.connected, "host": t.host, "port": t.port,
                            "flow": t.flow_running, "logdir": LOG_DIR, "log": log})
            else:
                self.send_error(404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                a = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json({"ok": False, "error": "bad json"}, 400)
            p = urlparse(self.path).path
            to = float(a.get("timeout") or 60)
            if p == "/api/connect":
                return self._json({"ok": t.connect(a.get("host") or None, a.get("port") or None)})
            if p == "/api/disconnect":
                t.disconnect()
                return self._json({"ok": True})
            if p == "/api/cmd":
                cmd = a.get("cmd", "")
                if cmd not in SPEC_CMDS + EXT_CMDS:
                    return self._json({"ok": False, "error": "unknown cmd"}, 400)
                return self._json(t.send(make_line(cmd, a), to))
            if p == "/api/raw":
                return self._json(t.send(a.get("line", ""), to))
            if p == "/api/flow":
                if t.flow_running:
                    return self._json({"ok": False, "error": "流程執行中"})
                threading.Thread(target=t.run_flow, daemon=True,
                                 args=(a, bool(a.get("stop", True)), int(a.get("delay") or 0), to)).start()
                return self._json({"ok": True})
            self.send_error(404)
    return H


def serve_ui(t, ui_port, open_browser=True):
    url = f"http://127.0.0.1:{ui_port}/"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", ui_port), make_handler(t))
    except OSError:
        # 已經開著一份（例如桌面圖示點兩次）→ 直接開瀏覽器到那一份
        print(f"UI 已在執行：{url}")
        if open_browser:
            webbrowser.open(url)
        return 0
    print(f"上位機對測工具 UI：{url}（目標 Control {t.host}:{t.port}；Ctrl+C 結束）")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        t.disconnect(quiet=True)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="上位機對測工具（CF_ 協議，扮演 Controller 連 Control）")
    ap.add_argument("--host", default=DEFAULT_HOST, help=f"Control 位址（預設 {DEFAULT_HOST}）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Control port（預設 {DEFAULT_PORT}）")
    ap.add_argument("--ui-port", type=int, default=UI_PORT, help=f"本機網頁 port（預設 {UI_PORT}）")
    ap.add_argument("--no-browser", action="store_true", help="只起 UI 後端，不自動開瀏覽器")
    ap.add_argument("--flow", action="store_true", help="不開 UI：跑一次完整流程後結束（exit 0=全 OK）")
    ap.add_argument("--send", metavar="LINE", help="不開 UI：送一條原始命令後結束")
    ap.add_argument("--recipe", default="DEFAULT")
    ap.add_argument("--panel", default="TEST0001")
    ap.add_argument("--detect-mode", default="0", choices=["0", "1"], help="0=inline、1=loadImage")
    ap.add_argument("--set-align-format", default="spec", choices=["spec", "control"],
                    help="spec=P1 ShiftX/P2 ShiftY（規格）；control=P1 Cs_AlignSet/P2/P3（Control 現行）")
    ap.add_argument("--timeout", type=float, default=60.0, help="每條命令等回應秒數")
    a = ap.parse_args(argv)

    t = Tester(a.host, a.port)
    if a.send:
        r = t.send(a.send, a.timeout)
        t.disconnect(quiet=True)
        return 0 if r.get("ok") else 1
    if a.flow:
        ok = t.run_flow({"recipe": a.recipe, "panel": a.panel, "detectMode": a.detect_mode,
                         "setAlignFmt": a.set_align_format}, timeout=a.timeout)
        t.disconnect(quiet=True)
        return 0 if ok else 1
    return serve_ui(t, a.ui_port, not a.no_browser)


if __name__ == "__main__":
    sys.exit(main())
