#!/usr/bin/env python3
"""upstream_tester 自測（不需網路、不需真 Control）：
   python3 tools/upstream_tester/test_upstream_tester.py
Client 模式對「模仿 Control UpstreamServer 回應格式的假 server」；Server 模式由測試扮演上位機連進來。"""
import os
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import upstream_tester as ut  # noqa: E402

TMP = tempfile.mkdtemp(prefix="ut_")
ut.LOG_DIR = os.path.join(TMP, "logs")


def engine(name):
    return ut.Engine(os.path.join(TMP, name + ".json"), echo=False)


def resp(ok, *p, err=""):
    p = list(p) + [""] * (8 - len(p))
    return f"{'OK' if ok else 'ERR'}|{'|'.join(p)}|{err}"


class FakeControl:
    """回應格式照 control/src/Controllers/UpstreamServer.cs（10 欄、ERR、CHECK_ALIGN p1=Cs_AlignSet）。"""
    def __init__(self, port=0, align="control"):
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", port))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self.rx, self.align, self.clients = [], align, []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self.clients.append(c)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        try:
            for raw in c.makefile("rb"):
                line = raw.decode().rstrip("\r\n")
                self.rx.append(line)
                cmd = line.split("|")[0]
                if cmd == "CF_CHECK_ALIGN":
                    r = resp(True, "Cs_AlignSet", "1.25", "-3.50") if self.align == "control" else resp(True, "1.25", "-3.50")
                elif cmd == "CF_GET_RESULT":
                    r = resp(True, "ResultIP0.xml,ResultIP1.xml", "9000,20")
                elif cmd == "CF_SET_ALIGN":
                    r = resp(False, err="SET_ALIGN 未支援")
                else:
                    r = resp(True)
                c.sendall((r + "\r\n").encode())
        except OSError:
            pass

    def kill_clients(self):
        for c in self.clients:
            try:
                c.shutdown(socket.SHUT_RDWR)
                c.close()
            except OSError:
                pass
        self.clients = []

    def close(self):
        self.kill_clients()
        try:
            self.srv.shutdown(socket.SHUT_RDWR)   # 喚醒卡在 accept() 的執行緒，真正停止監聽
        except OSError:
            pass
        self.srv.close()


def wait(cond, sec=5):
    end = time.time() + sec
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ───────────── 純函式 ─────────────
def test_request_format():
    e = engine("fmt")
    c = e.find_cmd("CF_LOAD_RECIPE")
    c["params"][2] = "2024-06-28-14-01-22"
    line = e.cmd_line("CF_LOAD_RECIPE")
    assert line == "CF_LOAD_RECIPE|DEFAULT|TEST0001|2024-06-28-14-01-22||||||0", line
    assert e.cmd_line("CF_GRAB_START") == "CF_GRAB_START|||||||||"
    e.vars.update(shiftX="1.5", shiftY="-2")
    assert e.cmd_line("CF_SET_ALIGN") == "CF_SET_ALIGN|1.5|-2|||||||"
    assert e.resolve("{nope}") == "{nope}"


def test_checks():
    _, c, x = ut.check_response("CF_CHECK_ALIGN", "OK|1.2|3.4|||||||")
    assert x == {"shiftX": "1.2", "shiftY": "3.4"} and c[0][0] == "ok"
    _, c, x = ut.check_response("CF_CHECK_ALIGN", "OK|Cs_AlignSet|1.2|3.4||||||")
    assert x == {"shiftX": "1.2", "shiftY": "3.4"} and c[0][0] == "warn"
    _, c, _ = ut.check_response("CF_GET_RESULT", "OK|Result file path=a.xml,b.xml|DefectCnt=9000,20|||||||")
    assert c == [("ok", "a.xml → 9000；b.xml → 20")], c
    _, c, _ = ut.check_response("CF_GET_RESULT", "OK|a.xml,b.xml|5|||||||")
    assert any("數量應一致" in m for _, m in c)
    _, c, _ = ut.check_response("CF_LOAD_RECIPE", "ERR|||||||||")
    assert any("Error Msg" in m for _, m in c) and any("'ERR'" in m for _, m in c)
    _, c, _ = ut.check_response("CF_LOAD_RECIPE", "OK|x")
    assert any("欄位數 2" in m for _, m in c)
    known = [x["name"] for x in ut.default_commands()]
    c = ut.check_request("CF_LOAD_RECIPE", "CF_LOAD_RECIPE|R|P|2024-06-28-14-01-22||||||0", known)
    assert c == [], c
    c = ut.check_request("CF_LOAD_RECIPE", "CF_LOAD_RECIPE|R|P|bad||||||7", known)
    assert len(c) == 2, c
    c = ut.check_request("CF_FOO", "CF_FOO", known)
    assert any("未定義" in m for _, m in c) and any("欄位數" in m for _, m in c)


def test_validation():
    assert ut.valid_host("192.168.10.1") and ut.valid_host("spark-02")
    assert not ut.valid_host("192.168.10.300") and not ut.valid_host("") and not ut.valid_host("a b")
    assert ut.valid_host("0.0.0.0", allow_any=True)
    assert ut.valid_port("8787") and not ut.valid_port(0) and not ut.valid_port("x")
    e = engine("val")
    assert e.connect("999.1.1.1", 8787)[0] is False
    assert e.save_settings({"client": {"port": 70000}})[0] is False
    assert e.save_settings({"presets": [{"name": "x", "host": "bad host", "port": 1}]})[0] is False


# ───────────── Client ─────────────
def test_client_flow_and_monitor():
    fc = FakeControl()
    e = engine("flow")
    assert e.connect("127.0.0.1", fc.port)[0]
    assert e.conn["state"] == "connected"
    e.cfg["flow"]["stopOnError"] = False
    assert e.run_flow_blocking() is False                          # SET_ALIGN 回 ERR
    assert [l.split("|")[0] for l in fc.rx] == ut.SPEC_CMDS, fc.rx  # 順序照 pptx 流程
    assert fc.rx[3] == "CF_SET_ALIGN|1.25|-3.50|||||||", fc.rx[3]  # 帶入 CHECK_ALIGN 的 Shift
    m = e.monitor["CF_CHECK_ALIGN"]
    assert m["req"]["dir"] == "送出" and m["resp"]["fields"][2] == "1.25" and m["n"] == 1
    assert e.monitor["CF_GET_RESULT"]["resp"]["fields"][1] == "ResultIP0.xml,ResultIP1.xml"
    assert e.flow["fail"] == 1
    # 多輪 + 遇錯即停
    e.cfg["flow"].update(rounds=3, stopOnError=True)
    e.run_flow_blocking()
    assert e.flow["round"] == 1 and len(fc.rx) == 9
    e.disconnect()
    fc.close()


def test_disconnect_detect_and_reconnect():
    port = free_port()
    fc = FakeControl(port)
    e = engine("rc")
    e.cfg["client"]["autoReconnect"] = True
    assert e.connect("127.0.0.1", port)[0]
    assert wait(lambda: fc.clients)
    fc.kill_clients()                                               # Control 主動斷線
    assert wait(lambda: any("連線中斷" in x["text"] for x in e.log.since(0))), e.conn
    assert wait(lambda: e.conn["state"] == "connected", 8), e.conn  # 3 秒內自動重連
    assert e.send_cmd("CF_READY")["ok"]
    fc.close()                                                      # Control 整個關掉 → 黃燈重連中
    assert wait(lambda: e.conn["state"] == "reconnecting" and e.conn["retries"] >= 1, 8), e.conn
    fc = FakeControl(port)                                          # Control 回來 → 自動連回綠燈
    assert wait(lambda: e.conn["state"] == "connected", 8), e.conn
    e.disconnect()
    assert e.conn["state"] == "idle"
    fc.close()


def test_mode_switch_resets():
    fc = FakeControl()
    e = engine("ms")
    e.connect("127.0.0.1", fc.port)
    e.send_cmd("CF_READY")
    assert e.monitor and e.conn["lastTx"]
    e.set_mode("server")
    assert not e.monitor and e.conn["lastTx"] is None and e.conn["state"] == "stopped" and e.sock is None
    fc.close()


def test_connect_refused_state():
    e = engine("ref")
    e.cfg["client"]["autoReconnect"] = False
    ok, err = e.connect("127.0.0.1", free_port())
    assert not ok and e.conn["state"] == "error" and "連不上" in err


def test_heartbeat():
    fc = FakeControl()
    e = engine("hb")
    e.cfg["client"].update(heartbeatSec=0.5, heartbeatCmd="CF_READY")
    e.connect("127.0.0.1", fc.port)
    assert wait(lambda: "CF_READY|||||||||" in fc.rx and e.conn.get("hb", {}).get("ok"), 5)
    e.disconnect()
    fc.close()


# ───────────── 指令管理 ─────────────
def test_command_crud():
    e = engine("crud")
    assert e.save_command({"name": "bad name"})[0] is False
    assert e.save_command({"name": "CF_MY_CMD", "desc": "x", "labels": ["A"], "params": ["v1", "{now}"]})[0]
    c = e.find_cmd("CF_MY_CMD")
    assert c["labels"][0] == "A" and len(c["params"]) == 9 and not c["builtin"]
    assert e.save_command({"name": "CF_MY_CMD"})[0] is True        # 同名=更新
    assert e.save_command({"name": "CF_LOAD_RECIPE"}, original="CF_MY_CMD")[0] is False   # 撞名
    assert e.save_settings({"flow": {"steps": ut.SPEC_CMDS + ["CF_MY_CMD"]}})[0]
    assert e.save_command({"name": "CF_MY_CMD2"}, original="CF_MY_CMD")[0]                # 改名同步流程
    assert e.cfg["flow"]["steps"][-1] == "CF_MY_CMD2"
    assert e.save_command({"name": "CF_X"}, original="CF_LOAD_RECIPE")[0] is False          # 內建不能改名
    assert e.delete_command("CF_LOAD_RECIPE")[0] is False
    assert e.delete_command("CF_MY_CMD2")[0] and "CF_MY_CMD2" not in e.cfg["flow"]["steps"]
    # 設定存檔 → 重開還在
    e.save_command({"name": "CF_KEEP"})
    e.save_settings({"client": {"host": "10.0.0.5", "port": 9000}})
    e2 = ut.Engine(e.cfg_path, echo=False)
    assert e2.find_cmd("CF_KEEP") and e2.cfg["client"]["host"] == "10.0.0.5"
    e2.reset_commands()
    assert not e2.find_cmd("CF_KEEP")


# ───────────── Server ─────────────
def test_server_mode():
    e = engine("srv")
    e.set_mode("server")
    port = free_port()
    assert e.start_server("127.0.0.1", port)[0]
    assert e.conn["state"] == "listening"
    e.save_command({"name": "CF_CHECK_ALIGN", "reply": {"result": "OK", "params": ["0.5", "-0.25"], "delayMs": 0}},
                   original="CF_CHECK_ALIGN")
    e.save_command({"name": "CF_SET_ALIGN", "reply": {"result": "Error", "params": [""] * 8 + ["got {p1},{p2}"]}},
                   original="CF_SET_ALIGN")
    s = socket.create_connection(("127.0.0.1", port), timeout=3)
    f = s.makefile("rb")
    assert wait(lambda: e.conn["state"] == "connected")

    def q(line):
        s.sendall((line + "\r\n").encode())
        return f.readline().decode().rstrip("\r\n")
    assert q("CF_LOAD_RECIPE|R1|P1|2024-06-28-14-01-22||||||0") == "OK|||||||||"
    assert q("CF_CHECK_ALIGN|||||||||") == "OK|0.5|-0.25|||||||"
    assert q("CF_SET_ALIGN|0.5|-0.25|||||||") == "Error|||||||||got 0.5,-0.25"
    assert q("CF_NOPE") == "Error|||||||||unknown command: CF_NOPE"
    m = e.monitor["CF_LOAD_RECIPE"]
    assert m["req"]["dir"] == "收到" and m["req"]["fields"][2] == "P1" and m["resp"]["dir"] == "回覆"
    f.close()
    s.close()
    assert wait(lambda: e.conn["state"] == "listening")
    assert any("斷線" in x["text"] for x in e.log.since(0))
    e.stop_server()
    assert e.conn["state"] == "stopped"
    assert e.send("CF_READY")["ok"] is False                       # Server 模式不主動送


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
    print("全部通過")
