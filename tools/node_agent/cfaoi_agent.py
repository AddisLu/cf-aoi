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
    with Server(("0.0.0.0", PORT), Handler) as srv:
        print(f"[agent] CF-AOI 節點代理 v{VERSION}  role={ROLE}  port={PORT}  "
              f"units={','.join(UNITS)}  allow={','.join(map(str, ALLOW))}", flush=True)
        srv.serve_forever()


if __name__ == "__main__":
    main()
