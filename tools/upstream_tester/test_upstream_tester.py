#!/usr/bin/env python3
"""upstream_tester 自測：起一個模仿 Control UpstreamServer 回應格式的假 server，驗工具的收發與規格檢查。
   python3 tools/upstream_tester/test_upstream_tester.py
（這只驗「工具本身」；與真 Control 對測請直接開工具連 8787。）"""
import os
import socket
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import upstream_tester as ut  # noqa: E402

ut.LOG_DIR = tempfile.mkdtemp(prefix="ut_log_")


def resp(ok, *p, err=""):
    p = list(p) + [""] * (8 - len(p))
    return f"{'OK' if ok else 'ERR'}|{'|'.join(p)}|{err}"


def fake_control(srv, received, align_layout):
    """回應格式照 control/src/Controllers/UpstreamServer.cs（9 參數、ERR、CHECK_ALIGN p1=Cs_AlignSet）。"""
    c, _ = srv.accept()
    f = c.makefile("rb")
    for raw in f:
        line = raw.decode().rstrip("\r\n")
        received.append(line)
        cmd = line.split("|")[0]
        if cmd == "CF_CHECK_ALIGN":
            r = resp(True, "Cs_AlignSet", "1.25", "-3.50") if align_layout == "control" else resp(True, "1.25", "-3.50")
        elif cmd == "CF_GET_RESULT":
            r = resp(True, "ResultIP0.xml,ResultIP1.xml", "9000,20")
        elif cmd == "CF_SET_ALIGN":
            r = resp(False, err="SET_ALIGN 未支援")
        else:
            r = resp(True)
        c.sendall((r + "\r\n").encode())
    c.close()


def start(align_layout="control"):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    received = []
    threading.Thread(target=fake_control, args=(srv, received, align_layout), daemon=True).start()
    return ut.Tester("127.0.0.1", srv.getsockname()[1]), received


def test_request_format():
    line = ut.make_line("CF_LOAD_RECIPE", {"recipe": "R1", "panel": "P1", "time": "2024-06-28-14-01-22", "detectMode": "1"})
    assert line == "CF_LOAD_RECIPE|R1|P1|2024-06-28-14-01-22||||||1", line
    assert line.count("|") == 9
    assert ut.make_line("CF_GRAB_START", {}) == "CF_GRAB_START|||||||||"
    assert ut.set_align_line("1.5", "2", "spec") == "CF_SET_ALIGN|1.5|2|||||||"
    assert ut.set_align_line("1.5", "2", "control") == "CF_SET_ALIGN|Cs_AlignSet|1.5|2||||||"


def test_checks():
    _, c, x = ut.check_response("CF_CHECK_ALIGN", "OK|1.2|3.4|||||||")
    assert x == {"shiftX": 1.2, "shiftY": 3.4} and c[0][0] == "ok"
    _, c, x = ut.check_response("CF_CHECK_ALIGN", "OK|Cs_AlignSet|1.2|3.4||||||")
    assert x == {"shiftX": 1.2, "shiftY": 3.4} and c[0][0] == "warn"
    _, c, _ = ut.check_response("CF_GET_RESULT", "OK|Result file path=a.xml,b.xml|DefectCnt=9000,20|||||||")
    assert c == [("ok", "a.xml → 9000；b.xml → 20")], c
    _, c, _ = ut.check_response("CF_GET_RESULT", "OK|a.xml,b.xml|5|||||||")
    assert any("數量應一致" in m for _, m in c)
    _, c, _ = ut.check_response("CF_LOAD_RECIPE", "ERR|||||||||")
    assert any("Error Msg" in m for _, m in c) and any("'ERR'" in m for _, m in c)
    _, c, _ = ut.check_response("CF_LOAD_RECIPE", "OK|x")
    assert any("欄位數 2" in m for _, m in c)


def test_flow_against_fake_control():
    t, rx = start("control")
    ok = t.run_flow({"recipe": "R1", "panel": "P1", "time": "2026-11-04-10-00-00", "setAlignFmt": "spec"},
                    stop_on_error=False, timeout=5)
    assert not ok                                            # SET_ALIGN 回 ERR
    assert [l.split("|")[0] for l in rx] == ut.SPEC_CMDS, rx  # 順序照 pptx 流程
    assert rx[3] == "CF_SET_ALIGN|1.25|-3.5|||||||", rx[3]   # 帶入 CHECK_ALIGN 的 Shift
    t.disconnect(quiet=True)


def test_flow_stops_on_error():
    t, rx = start("spec")
    ok = t.run_flow({"setAlignFmt": "control"}, stop_on_error=True, timeout=5)
    assert not ok and len(rx) == 4 and rx[3].startswith("CF_SET_ALIGN|Cs_AlignSet|1.25|-3.5")
    t.disconnect(quiet=True)


def test_connect_refused():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    t = ut.Tester("127.0.0.1", port)
    assert t.send("CF_READY", 1)["ok"] is False


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
    print("全部通過")
