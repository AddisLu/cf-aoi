#!/usr/bin/env python3
"""離線測試 cfaoi_agent 的機況助手（LOOP 命令）：不需 Spark / LoopEngineering / systemd。

假的 Loop API（本機 HTTP）+ PATH 裡假的 systemctl（記錄呼叫、模擬 user service 狀態），真的啟動代理行程，
經 TCP 送 STATUS / LOOP start / LOOP stop，驗：
  未設定時拒絕、STATUS 帶 loop 區塊、start = 啟服務 → 等 API → 載入指定模型（帶 token）、
  模型載入中/就緒都算診斷模式、stop = 停模型（兩台）→ 停服務、Loop 拒絕載入時回清楚錯誤、網址帶 token、
  轉送口（Windows 開 Loop 畫面）把 HTTP 原樣轉到 Loop。
跑法：python3 tools/node_agent/test_agent_loop.py    預期「全數通過」、exit 0
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="agent_loop_test_")
FAIL = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + str(detail)) if detail and not cond else ''}")
    if not cond:
        FAIL.append(name)


# ── 假 Loop API ──
LOOP = {"state": {"status": "idle", "loaded": None, "wanted": None, "error": None, "since": None},
        "calls": [], "auth": [], "refuse_load": False}


class FakeLoop(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        LOOP["calls"].append(("GET", self.path))
        LOOP["auth"].append(self.headers.get("Authorization"))
        if self.path == "/api/status":
            return self._send(200, {"paused": False})
        if self.path == "/api/local/models":
            return self._send(200, {"state": LOOP["state"]})
        self._send(404, {"error": "nf"})

    def do_POST(self):
        LOOP["calls"].append(("POST", self.path))
        LOOP["auth"].append(self.headers.get("Authorization"))
        # 同 Fastify：Content-Type JSON 但內容空 → 400（2026-10-05 實機踩到）
        n = int(self.headers.get("Content-Length") or 0)
        if self.headers.get("Content-Type", "").startswith("application/json") and n == 0:
            return self._send(400, {"statusCode": 400, "error": "Bad Request",
                                    "message": "Body cannot be empty when content-type is set to 'application/json'"})
        self.rfile.read(n)
        if self.path.startswith("/api/local/models/") and self.path.endswith("/load"):
            if LOOP["refuse_load"]:
                return self._send(409, {"error": "weights not cached"})
            LOOP["state"].update(status="starting", wanted=self.path.split("/")[4])
            return self._send(200, {"ok": True, "result": "switching"})
        if self.path == "/api/local/stop":
            LOOP["state"].update(status="idle", loaded=None)
            return self._send(200, {"ok": True})
        self._send(404, {"error": "nf"})


srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeLoop)
threading.Thread(target=srv.serve_forever, daemon=True).start()
loop_port = srv.server_address[1]

# ── 假 systemctl（user service 狀態存在檔案裡）──
bindir = os.path.join(TMP, "bin")
os.makedirs(bindir)
state_file = os.path.join(TMP, "unit_state")
calls_file = os.path.join(TMP, "systemctl_calls")
with open(state_file, "w") as f:
    f.write("inactive")
with open(os.path.join(bindir, "systemctl"), "w") as f:
    f.write(f"""#!/bin/bash
echo "$*" >> {calls_file}
if [ "$1" = "--user" ]; then
  case "$2" in
    is-active) cat {state_file}; [ "$(cat {state_file})" = active ];;
    start) echo active > {state_file};;
    stop) echo inactive > {state_file};;
  esac
  exit $?
fi
# 系統層（service_info 用）：回空
exit 0
""")
os.chmod(os.path.join(bindir, "systemctl"), 0o755)
envfile = os.path.join(TMP, "loop.env")
with open(envfile, "w") as f:
    f.write("LOOP_PORT=4711\nLOOP_API_TOKEN=tok123\n")


def start_agent(extra):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = dict(os.environ, PATH=bindir + ":" + os.environ["PATH"], ROLE="ip", PORT=str(port),
               OUTPUT_DIR=TMP, REPO=TMP, ALLOW="127.0.0.0/8", **extra)
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "cfaoi_agent.py")], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    return p, port


def call(port, cmd, params=None):
    with socket.create_connection(("127.0.0.1", port), timeout=30) as c:
        c.sendall((json.dumps({"cmd": cmd, "seq": 1, "params": params or {}}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = c.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf)


print("1. 未設定機況助手的節點")
p, port = start_agent({})
r = call(port, "STATUS")
check("STATUS 帶 loop.enabled=false", r["status"] == "OK" and r["data"]["loop"] == {"enabled": False})
r = call(port, "LOOP", {"action": "start"})
check("LOOP 被拒並說明", r["status"] == "ERR" and "LOOP_ENABLED" in r["error"])
p.terminate(); p.wait()

print("2. 主 Spark（LOOP_ENABLED=1）")
_s = socket.socket(); _s.bind(("127.0.0.1", 0)); proxy_port = _s.getsockname()[1]; _s.close()
p, port = start_agent({"LOOP_PROXY_LISTEN": f"127.0.0.1:{proxy_port}", "LOOP_ENABLED": "1", "LOOP_URL": f"http://127.0.0.1:{loop_port}",
                       "LOOP_MODEL": "deepseek-v4-flash-256k", "LOOP_PUBLIC_URL": "http://192.168.3.1:4711",
                       "LOOP_ENV_FILE": envfile})
try:
    d = call(port, "STATUS")["data"]["loop"]
    check("閒置：服務停止、非診斷模式", d["enabled"] and d["service"] == "inactive" and d["active"] is False, d)
    with open(state_file, "w") as f:
        f.write("active")
    d = call(port, "STATUS")["data"]["loop"]
    check("Loop 服務在跑但沒載模型 → 不算診斷模式（不擋 CF_READY）", d["service"] == "active" and d["active"] is False, d)
    with open(state_file, "w") as f:
        f.write("inactive")
    check("開畫面網址帶 token", d["url"] == "http://192.168.3.1:4711/?token=tok123", d["url"])

    r = call(port, "LOOP", {"action": "start"})
    check("start 回 OK", r["status"] == "OK", r)
    calls = open(calls_file).read()
    check("啟動使用者層 loop-engineering", "--user start loop-engineering" in calls)
    check("載入指定模型", ("POST", "/api/local/models/deepseek-v4-flash-256k/load") in LOOP["calls"])
    check("呼叫 Loop 帶 Bearer token", "Bearer tok123" in LOOP["auth"])
    d = r["data"]
    check("載入中 = 診斷模式", d["model"]["status"] == "starting" and d["active"] is True, d)

    LOOP["state"].update(status="ready", loaded="deepseek-v4-flash-256k")
    d = call(port, "STATUS")["data"]["loop"]
    check("就緒狀態回報", d["model"]["status"] == "ready" and d["model"]["loaded"] == "deepseek-v4-flash-256k")

    r = call(port, "LOOP", {"action": "stop"})
    check("stop 回 OK", r["status"] == "OK", r)
    check("先停模型（叢集）", ("POST", "/api/local/stop") in LOOP["calls"])
    check("再停服務", "--user stop loop-engineering" in open(calls_file).read())
    check("停完 = 回生產（非診斷模式）", r["data"]["active"] is False and r["data"]["service"] == "inactive", r["data"])

    import urllib.request
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{proxy_port}/api/status", timeout=3) as r:
                body = json.loads(r.read())
            break
        except OSError:
            time.sleep(0.1)
    else:
        body = None
    check("轉送口把 HTTP 原樣轉到 Loop", body == {"paused": False}, body)

    LOOP["refuse_load"] = True
    r = call(port, "LOOP", {"action": "start"})
    check("Loop 拒絕載入 → ERR 帶原因", r["status"] == "ERR" and "weights not cached" in r["error"], r)
    r = call(port, "LOOP", {"action": "format-disk"})
    check("未知動作被拒", r["status"] == "ERR")
finally:
    p.terminate(); p.wait()
    srv.shutdown()

import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("全數通過" if not FAIL else f"失敗 {len(FAIL)} 項：{FAIL}")
sys.exit(1 if FAIL else 0)
