#!/usr/bin/env python3
"""
verify_list_during_grab.py — #2+ follow-up：LIST_CAMERAS 與 取像(GRABBING) 並存不掉幀（damac↔Spark）

STATUS Grab「LIST_CAMERAS 唯讀列舉」列備註：
  「GRABBING 中並存呼叫未驗 → 列舉 vs 取像並存不掉幀」待 RDMA 鏈路就緒後補測。
本腳本（RDMA 鏈路已帶起後）驗：
  1) GRAB_START（相機開始串流 → RDMA 送 Spark 收端 rdma-validate）。
  2) 串流中連續呼叫 LIST_CAMERAS（= CTlFactory::EnumerateDevices，未持 grab 鎖）N 次，
     每次都回傳該相機（並存列舉不崩、不卡死）。
  3) GRAB_STOP，回報已送幀數。
  → 搭配 Spark rdma-validate 輸出（seq 連續 + CRC 全對 → err=0）佐證「並存列舉期間傳輸不掉幀/不損壞」。

用法（在 damac 上跑；grab control_server 在 127.0.0.1:8100）：
  python3 scripts/verify_list_during_grab.py [--host 127.0.0.1] [--port 8100] \
          [--grab-seconds 6] [--list-count 6]
"""
import argparse, json, socket, sys, time
_seq = 0

def tcp_connect(host, port, timeout=15):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout); s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.connect((host, port)); return s

def recv_line(s, timeout=45):
    buf = b""; s.settimeout(timeout)
    while True:
        c = s.recv(1)
        if not c: raise ConnectionError("closed")
        if c == b"\n": return buf.decode()
        buf += c

def send_cmd(s, cmd, params=None):
    global _seq; _seq += 1
    s.sendall((json.dumps({"cmd": cmd, "seq": _seq, "params": params or {}}) + "\n").encode())
    return json.loads(recv_line(s))

results = []
def check(name, cond, detail):
    results.append(cond); print(f"[{'PASS' if cond else 'FAIL'}] {name}\n  {detail}")

def cam_count(resp):
    cams = resp.get("cameras")
    return len(cams) if isinstance(cams, list) else 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1"); ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--grab-seconds", type=float, default=6.0)
    ap.add_argument("--list-count", type=int, default=6)
    a = ap.parse_args()

    s = tcp_connect(a.host, a.port)

    # 0) GRABBING 前先 LIST_CAMERAS 一次（基準：idle 列舉）
    r0 = send_cmd(s, "LIST_CAMERAS")
    n0 = cam_count(r0)
    check("idle LIST_CAMERAS 回相機（基準）", r0.get("status") == "OK" and n0 >= 1,
          f"status={r0.get('status')} cameras={n0}")

    # 1) LOAD_RECIPE + GRAB_START（非阻塞，背景 grab thread 開始串流）
    send_cmd(s, "LOAD_RECIPE", {"recipe": "LISTGRAB", "panel_id": "listgrab"})
    rg = send_cmd(s, "GRAB_START", {"timeout_ms": int(a.grab_seconds * 1000) + 20000})
    check("GRAB_START OK（相機開始串流 + RDMA 連上 Spark）", rg.get("status") == "OK",
          f"status={rg.get('status')} err={rg.get('error','')}")
    if rg.get("status") != "OK":
        s.close()
        print("\n串流未啟動，後續略過。"); sys.exit(1)

    # 2) 串流中連續 LIST_CAMERAS：每次都應回該相機（並存列舉）
    time.sleep(max(0.5, a.grab_seconds * 0.15))   # 先讓串流穩定（基準窗）
    list_ok = 0
    t_burst0 = time.time()
    for i in range(a.list_count):
        ri = send_cmd(s, "LIST_CAMERAS")
        ni = cam_count(ri)
        ok = ri.get("status") == "OK" and ni >= 1
        if ok: list_ok += 1
        print(f"    串流中 LIST_CAMERAS #{i+1}: status={ri.get('status')} cameras={ni} "
              f"{'OK' if ok else 'FAIL'}")
        time.sleep(max(0.2, a.grab_seconds * 0.5 / a.list_count))
    burst_dt = time.time() - t_burst0
    check(f"串流中 {a.list_count} 次 LIST_CAMERAS 全回相機（並存列舉不崩/不卡）",
          list_ok == a.list_count, f"{list_ok}/{a.list_count} 成功；列舉突發歷時 {burst_dt:.1f}s")

    time.sleep(max(0.5, a.grab_seconds * 0.2))     # 列舉後再串流一段（確認未中斷）

    # 3) GRAB_STOP（回報已送幀數）
    rs = send_cmd(s, "GRAB_STOP")
    check("GRAB_STOP OK", rs.get("status") == "OK", f"status={rs.get('status')}")
    s.close()

    ok = all(results)
    print(f"\n{'='*56}\nLIST_CAMERAS-during-grab：{sum(results)}/{len(results)} PASS — "
          f"{'ALL PASS ✓' if ok else 'HAS FAIL ✗'}")
    print("  （掉幀/損壞判定看 Spark rdma-validate 輸出：err=0 且 ok>0 = 並存期間傳輸無損）")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
