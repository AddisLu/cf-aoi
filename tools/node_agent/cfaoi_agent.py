#!/usr/bin/env python3
"""
cfaoi_agent — CF-AOI 節點代理（Grab 主機 / Spark 各跑一支；商業化階段 2）

讓 Control（Windows，線上人員唯一的螢幕）遠端管理 Linux 節點：查狀態、重啟服務、
IP 切生產/調參、看 log、收診斷包、重開機。主程式（cfaoi_grab / cfaoi_ip）就算卡死，
代理仍在，Control 照樣救得回來。

協定：TCP 一行一個 JSON（與 Grab 8100 / IP 8200 相同）
  →  {"cmd": "STATUS", "seq": 1, "params": {...}}
  ←  {"seq": 1, "status": "OK"|"ERR", "data": {...}, "error": "..."}

安全：
  - 只接受白名單命令；SERVICE/LOGS 只接受本機角色的 cfaoi-* 服務；不提供任意 shell。
  - 只接受 ALLOW 網段來源（控制網 / RDMA 直連 / 本機 / 開發用 Tailscale）。
  - 以一般帳號執行；啟停服務、重開機靠 polkit 規則（install_linux_services.sh 安裝）。

設定（/etc/default/cfaoi-agent，systemd EnvironmentFile）：
  ROLE=grab|ip   REPO=<repo 路徑>   OUTPUT_DIR=<結果/日誌目錄>   PORT=8300
  ALLOW=127.0.0.0/8,192.168.10.0/24,192.168.3.0/24,100.64.0.0/10
  機況助手（只在跑 LoopEngineering 的那台 Spark 設；scripts/deploy/setup_loop_mode.sh 寫入）：
  LOOP_ENABLED=1  LOOP_UNIT=loop-engineering（使用者層服務）  LOOP_URL=http://127.0.0.1:4711
  LOOP_MODEL=<Loop 本地模型 id>  LOOP_PUBLIC_URL=http://192.168.3.1:4711（Windows 開的網址）
  LOOP_ENV_FILE=<LoopEngineering .env，讀 LOOP_API_TOKEN>
  LOOP_PROXY_LISTEN=192.168.3.1:4711  Loop 只聽 127.0.0.1 → 代理在 RDMA 網段開轉送口給 Windows（只放行 ALLOW；
                                      不改 Loop 本身的設定，Tailscale 存取照舊）

運作模式（2026-10-05 定案）：生產（run 貨）只跑 Control / Grab / IP；機台有問題或調機時，由 Windows
Control「機況助手」→ LOOP start：啟動 Loop + 載入大模型（兩台 Spark 叢集）；結束 → LOOP stop 釋放記憶體。
大模型佔 Spark 約 80% 統一記憶體，與 IP 生產並存時只剩約 5GB → 生產時不得載入。

只用 Python 標準函式庫（產線機無網路，不裝 pip 套件）。
"""
import base64
import io
import ipaddress
import json
import os
import shutil
import socket
import socketserver
import subprocess
import tarfile
import threading
import time

ROLE = os.environ.get("ROLE", "grab")
REPO = os.environ.get("REPO", os.path.expanduser("~/Addis/cf-aoi"))
OUTPUT_DIR = os.path.expanduser(os.environ.get("OUTPUT_DIR", "~/cfaoi_output"))
PORT = int(os.environ.get("PORT", "8300"))
ALLOW = [ipaddress.ip_network(n.strip()) for n in
         os.environ.get("ALLOW", "127.0.0.0/8,192.168.10.0/24,192.168.3.0/24,100.64.0.0/10").split(",")
         if n.strip()]

# 各角色可管理的服務（顯示順序 = 這裡的順序）
UNITS = {
    "grab": {"cfaoi-grab": "Grab 取像"},
    "ip":   {"cfaoi-ip-production": "IP 生產（rdma-process）",
             "cfaoi-ip-offline":    "IP 調參（offline-tcp）"},
}.get(ROLE, {})
SERVICE_ACTIONS = ("start", "stop", "restart")

# 機況助手（LoopEngineering + 本地大模型）：只在 LOOP_ENABLED=1 的 Spark 上提供
LOOP_ENABLED = os.environ.get("LOOP_ENABLED", "0") == "1"
LOOP_UNIT = os.environ.get("LOOP_UNIT", "loop-engineering")
LOOP_URL = os.environ.get("LOOP_URL", "http://127.0.0.1:4711").rstrip("/")
LOOP_MODEL = os.environ.get("LOOP_MODEL", "")
LOOP_PUBLIC_URL = os.environ.get("LOOP_PUBLIC_URL", "").rstrip("/")
LOOP_ENV_FILE = os.path.expanduser(os.environ.get("LOOP_ENV_FILE", "~/Addis/LoopEngineering/.env"))
LOOP_PROXY_LISTEN = os.environ.get("LOOP_PROXY_LISTEN", "")
MAX_LINE = 64 * 1024
DIAG_CAP = 30 * 1024 * 1024          # 診斷包上限（壓縮後）
VERSION = "1"


def run(args, timeout=30):
    """執行固定參數的指令（不經 shell），回傳 (rc, stdout, stderr)。"""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"逾時 {timeout}s：{' '.join(args)}"
    except FileNotFoundError:
        return 127, "", f"找不到指令：{args[0]}"


def git_version():
    rc, out, _ = run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"], 5)
    if rc != 0:
        return ""
    ver = out.strip()
    rc, _, _ = run(["git", "-C", REPO, "diff", "--quiet", "HEAD"], 5)
    return ver + ("-dirty" if rc == 1 else "")


def service_info(unit):
    props = ["ActiveState", "SubState", "UnitFileState", "NRestarts", "MainPID",
             "ActiveEnterTimestamp", "LoadState"]
    rc, out, _ = run(["systemctl", "show", unit, "-p", ",".join(props)], 5)
    d = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return {
        "unit": unit,
        "label": UNITS.get(unit, unit),
        "installed": d.get("LoadState") == "loaded",
        "active": d.get("ActiveState", "unknown"),
        "sub": d.get("SubState", ""),
        "enabled": d.get("UnitFileState", ""),
        "restarts": int(d.get("NRestarts", "0") or 0),
        "pid": int(d.get("MainPID", "0") or 0),
        "since": d.get("ActiveEnterTimestamp", ""),
    }


def timesync():
    """校時狀態：Grab 跑 chrony（校時伺服器），Spark 跑 systemd-timesyncd（跟 Grab 對時）。"""
    rc, out, _ = run(["chronyc", "-c", "tracking"], 3)
    if rc == 0 and out:
        f = out.strip().split(",")       # 0:ref id 1:ref name 2:stratum 4:system time offset(s)
        try:
            # 7F7F0101 = chrony `local stratum`：沒有上游（fab 內）時以本機時鐘當基準 ——
            # Grab 本來就是產線校時主機，這是正常狀態，不算「未同步」
            local = f[0].upper() == "7F7F0101"
            return {"daemon": "chrony", "source": "本機時鐘（校時主機）" if local else f[1],
                    "stratum": int(f[2]), "offset_s": float(f[4]), "local": local,
                    "synced": local or f[1] != ""}
        except (IndexError, ValueError):
            pass
    rc, out, _ = run(["timedatectl", "show-timesync", "-p", "ServerName", "-p", "NTPMessage"], 3)
    rc2, out2, _ = run(["timedatectl", "show", "-p", "NTPSynchronized"], 3)
    server = ""
    for line in out.splitlines():
        if line.startswith("ServerName="):
            server = line.split("=", 1)[1]
    return {"daemon": "timesyncd", "source": server,
            "synced": "NTPSynchronized=yes" in out2}


# ── 機況助手（Loop）────────────────────────────────────────────────────
def _loop_token():
    try:
        with open(LOOP_ENV_FILE, encoding="utf-8") as f:
            for line in f:
                if line.startswith("LOOP_API_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _loop_http(method, path, body=None, timeout=3.0):
    """呼叫本機 LoopEngineering API；回 (http 狀態碼, JSON)；連不上回 (0, {})。"""
    import urllib.error
    import urllib.request
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(LOOP_URL + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    tok = _loop_token()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (OSError, ValueError):
        return 0, {}


def _user_systemctl(*args, timeout=60):
    """使用者層 systemd（Loop 是 auo001 的 user service；代理以同帳號執行，linger 已開）。"""
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
    try:
        p = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "逾時"


def loop_status():
    """給 Control 的機況助手卡片：服務、模型狀態、可用記憶體、開畫面用的網址。"""
    if not LOOP_ENABLED:
        return {"enabled": False}
    rc, out, _ = _user_systemctl("is-active", LOOP_UNIT, timeout=5)
    service = out.strip() or "unknown"
    model = {"status": "idle", "loaded": None, "wanted": None, "error": None, "since": None}
    if service == "active":
        code, d = _loop_http("GET", "/api/local/models", timeout=2.0)
        if code == 200 and isinstance(d.get("state"), dict):
            model.update({k: d["state"].get(k) for k in model})
        elif code == 0:
            model["status"] = "starting"            # 服務剛起、API 還沒聽
    tok = _loop_token()
    url = (LOOP_PUBLIC_URL + "/" + (f"?token={tok}" if tok else "")) if LOOP_PUBLIC_URL else ""
    return {"enabled": True, "service": service, "model": model, "model_id": LOOP_MODEL, "url": url,
            # 診斷模式 = Loop 在跑或模型佔著記憶體 → Control 據此擋 CF_READY（生產只跑三支程式）
            "active": service in ("active", "activating") or model["status"] in ("starting", "ready")}


def _allowed(addr):
    try:
        peer = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(peer in n for n in ALLOW)


class _Pair:
    """一條轉送連線的兩個 socket：兩個方向都結束才關（HTTP/SSE/WebSocket 都是原始 TCP 照轉）。"""

    def __init__(self, a, b):
        self.a, self.b, self.left, self.lock = a, b, 2, threading.Lock()

    def pump(self, src, dst):
        try:
            while True:
                d = src.recv(65536)
                if not d:
                    break
                dst.sendall(d)
        except OSError:
            pass
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        with self.lock:
            self.left -= 1
            done = self.left == 0
        if done:
            for x in (self.a, self.b):
                try:
                    x.close()
                except OSError:
                    pass


def loop_proxy():
    """LOOP_PROXY_LISTEN → LOOP_URL 的 TCP 轉送（Windows Control 開 Loop 畫面用）。綁不上（網卡還沒起）就每 10 秒重試。"""
    from urllib.parse import urlparse
    host, port = LOOP_PROXY_LISTEN.rsplit(":", 1)
    u = urlparse(LOOP_URL)
    target = (u.hostname or "127.0.0.1", u.port or 80)
    while True:
        try:
            ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            ls.bind((host, int(port)))
            ls.listen(64)
            break
        except OSError as e:
            print(f"[agent] 機況助手轉送口 {LOOP_PROXY_LISTEN} 綁定失敗（{e}），10 秒後重試", flush=True)
            time.sleep(10)
    print(f"[agent] 機況助手轉送口 {LOOP_PROXY_LISTEN} → {target[0]}:{target[1]}", flush=True)
    while True:
        try:
            c, addr = ls.accept()
        except OSError:
            continue
        if not _allowed(addr[0]):
            c.close()
            continue
        try:
            t = socket.create_connection(target, timeout=5)
            t.settimeout(None)
        except OSError:
            c.close()                                  # Loop 沒在跑（生產模式）→ 連線直接結束
            continue
        pair = _Pair(c, t)
        threading.Thread(target=pair.pump, args=(c, t), daemon=True).start()
        threading.Thread(target=pair.pump, args=(t, c), daemon=True).start()


def cmd_loop(p):
    if not LOOP_ENABLED:
        raise ValueError("這台沒有設定機況助手（LOOP_ENABLED=0）；Loop 只在主 Spark 上")
    action = p.get("action", "status")
    if action == "status":
        return loop_status()
    if action == "start":
        if not LOOP_MODEL:
            raise ValueError("未設定 LOOP_MODEL（要載入哪個本地模型）")
        rc, out, err = _user_systemctl("start", LOOP_UNIT)
        if rc != 0:
            raise RuntimeError(f"啟動 {LOOP_UNIT} 失敗：{(err or out).strip()}")
        for _ in range(40):                           # 等 Loop API 起來（通常 3–10 秒）
            code, _d = _loop_http("GET", "/api/status", timeout=1.0)
            if code:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Loop 已啟動但 API 20 秒內沒有回應（看 journalctl --user -u loop-engineering）")
        code, d = _loop_http("POST", f"/api/local/models/{LOOP_MODEL}/load", timeout=15.0)
        if code != 200:
            raise RuntimeError(f"載入模型 {LOOP_MODEL} 被拒：{d.get('error') or f'HTTP {code}'}")
        return loop_status()                          # 載入在背景進行（數分鐘），Control 輪詢 STATUS 看進度
    if action == "stop":
        code, d = _loop_http("POST", "/api/local/stop", timeout=200.0)   # 兩台 Spark 的模型容器都停
        if code not in (0, 200, 404):
            raise RuntimeError(f"停止模型失敗：{d.get('error') or f'HTTP {code}'}")
        rc, out, err = _user_systemctl("stop", LOOP_UNIT)
        if rc != 0:
            raise RuntimeError(f"停止 {LOOP_UNIT} 失敗：{(err or out).strip()}")
        return loop_status()
    raise ValueError("action 須為 status / start / stop")


def status():
    du = shutil.disk_usage(OUTPUT_DIR if os.path.isdir(OUTPUT_DIR) else "/")
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0])
    with open("/proc/uptime") as f:
        uptime = float(f.read().split()[0])
    return {
        "agent_version": VERSION,
        "hostname": socket.gethostname(),
        "role": ROLE,
        "version": git_version(),
        "time_epoch": time.time(),
        "uptime_s": uptime,
        "load1": os.getloadavg()[0],
        "mem_total_mb": mem.get("MemTotal", 0) // 1024,
        "mem_avail_mb": mem.get("MemAvailable", 0) // 1024,
        "disk": {"path": OUTPUT_DIR, "total_gb": round(du.total / 1e9, 1),
                 "used_pct": round(du.used * 100 / du.total, 1)},
        "timesync": timesync(),
        "services": [service_info(u) for u in UNITS],
        "loop": loop_status(),
    }


def cmd_service(p):
    unit, action = p.get("unit", ""), p.get("action", "")
    if unit not in UNITS:
        raise ValueError(f"不允許的服務：{unit}（本機可管理：{', '.join(UNITS)}）")
    if action not in SERVICE_ACTIONS:
        raise ValueError(f"不允許的動作：{action}（{'/'.join(SERVICE_ACTIONS)}）")
    # --no-ask-password：沒有 polkit 規則時直接失敗，不會卡在等密碼
    rc, out, err = run(["systemctl", "--no-ask-password", action, unit], 60)
    if rc != 0:
        raise RuntimeError(f"systemctl {action} {unit} 失敗：{(err or out).strip()}")
    time.sleep(1)
    return service_info(unit)


def cmd_logs(p):
    unit = p.get("unit", "")
    if unit not in UNITS and unit not in ("cfaoi-agent", "cfaoi-cleanup"):
        raise ValueError(f"不允許的服務：{unit}")
    lines = max(1, min(int(p.get("lines", 300)), 5000))
    rc, out, err = run(["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso"], 20)
    if rc != 0:
        raise RuntimeError(err.strip() or "journalctl 失敗")
    return {"unit": unit, "text": out}


def cmd_diag(_p):
    """診斷包：各服務 log、系統現況、機台設定、IP 行車紀錄（最近 20 筆）。回傳 tar.gz base64。"""
    buf = io.BytesIO()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    host = socket.gethostname()
    root = f"diag_{host}_{stamp}"

    def add_text(tar, name, text):
        data = text.encode("utf-8", "replace")
        ti = tarfile.TarInfo(f"{root}/{name}")
        ti.size, ti.mtime = len(data), int(time.time())
        tar.addfile(ti, io.BytesIO(data))

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        add_text(tar, "status.json", json.dumps(status(), ensure_ascii=False, indent=2))
        for unit in list(UNITS) + ["cfaoi-agent", "cfaoi-cleanup"]:
            _, out, _ = run(["journalctl", "-u", unit, "-n", "5000", "--no-pager", "-o", "short-iso"], 30)
            add_text(tar, f"journal_{unit}.log", out)
        sysinfo = [
            ["ip", "-br", "addr"], ["ip", "route"], ["df", "-h"], ["free", "-m"], ["uptime"],
            ["timedatectl"], ["chronyc", "tracking"], ["chronyc", "sources"],
            ["systemctl", "--no-pager", "status", "cfaoi-*"],
            ["git", "-C", REPO, "log", "--oneline", "-10"], ["git", "-C", REPO, "status", "--short"],
            ["sysctl", "net.core.rmem_max", "net.ipv4.ip_forward"],
        ]
        text = ""
        for a in sysinfo:
            _, out, err = run(a, 15)
            text += f"$ {' '.join(a)}\n{out}{err}\n"
        add_text(tar, "system.txt", text)
        for path in ("/etc/default/cfaoi-grab", "/etc/default/cfaoi-agent",
                     os.path.join(REPO, "grab", "cam_config.json")):
            if os.path.isfile(path):
                tar.add(path, arcname=f"{root}/config/{os.path.basename(path)}")
        diag_dir = os.path.join(OUTPUT_DIR, "_diag")
        if os.path.isdir(diag_dir):
            files = sorted((os.path.join(diag_dir, f) for f in os.listdir(diag_dir)),
                           key=os.path.getmtime)[-20:]
            for f in files:
                if os.path.isfile(f) and os.path.getsize(f) < 5 * 1024 * 1024:
                    tar.add(f, arcname=f"{root}/ip_diag/{os.path.basename(f)}")
    raw = buf.getvalue()
    if len(raw) > DIAG_CAP:
        raise RuntimeError(f"診斷包過大（{len(raw) // 1024 // 1024}MB > 上限）")
    return {"filename": f"{root}.tar.gz", "size": len(raw),
            "base64": base64.b64encode(raw).decode("ascii")}


def cmd_power(p):
    action = p.get("action", "")
    if action not in ("reboot", "poweroff"):
        raise ValueError("action 須為 reboot 或 poweroff")
    if p.get("confirm") is not True:
        raise ValueError("需 confirm=true（防誤觸）")
    # 先回應再執行（否則 Control 收不到回覆就斷線）
    threading.Timer(1.5, lambda: run(["systemctl", "--no-ask-password", action], 30)).start()
    return {"action": action, "in_s": 1.5}


COMMANDS = {
    "CHECK_HEALTH": lambda p: {"role": ROLE},
    "STATUS": lambda p: status(),
    "SERVICE": cmd_service,
    "LOGS": cmd_logs,
    "DIAG": cmd_diag,
    "POWER": cmd_power,
    "LOOP": cmd_loop,
}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        peer = ipaddress.ip_address(self.client_address[0])
        if not any(peer in n for n in ALLOW):
            print(f"[agent] 拒絕來源 {peer}（不在 ALLOW）", flush=True)
            return
        while True:
            line = self.rfile.readline(MAX_LINE + 1)
            if not line:
                return
            if len(line) > MAX_LINE:
                self._send({"seq": 0, "status": "ERR", "error": "命令過長"})
                return
            seq = 0
            try:
                req = json.loads(line)
                seq = req.get("seq", 0)
                cmd = req.get("cmd", "")
                if cmd not in COMMANDS:
                    raise ValueError(f"不支援的命令：{cmd}")
                data = COMMANDS[cmd](req.get("params") or {})
                if cmd not in ("CHECK_HEALTH", "STATUS"):
                    print(f"[agent] {peer} {cmd} {json.dumps(req.get('params') or {}, ensure_ascii=False)} → OK", flush=True)
                self._send({"seq": seq, "status": "OK", "data": data})
            except Exception as e:  # noqa: BLE001 — 任何錯誤都回 ERR，不讓代理死掉
                print(f"[agent] {peer} → ERR {e}", flush=True)
                self._send({"seq": seq, "status": "ERR", "error": str(e)})

    def _send(self, obj):
        self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.wfile.flush()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    if LOOP_ENABLED and LOOP_PROXY_LISTEN:
        threading.Thread(target=loop_proxy, daemon=True).start()
    with Server(("0.0.0.0", PORT), Handler) as srv:
        print(f"[agent] CF-AOI 節點代理 v{VERSION}  role={ROLE}  port={PORT}  "
              f"units={','.join(UNITS)}  allow={','.join(map(str, ALLOW))}"
              f"{f'  機況助手={LOOP_UNIT}/{LOOP_MODEL}' if LOOP_ENABLED else ''}", flush=True)
        srv.serve_forever()


if __name__ == "__main__":
    main()
