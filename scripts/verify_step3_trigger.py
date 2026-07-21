#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_step3_trigger.py — Step 3 觸發鏈實機驗證（Switch + 多相機到貨日用）

驗證 37 CCD 軟體觸發設計（docs plan「37 CCD 觸發設計」）：
  GRAB_ARM（預熱：開陣列+套參數+RDMA connect，冷啟重活）與
  GRAB_START（觸發本體：僅 start_all，ms 級）拆分後，網路觸發延遲落在
  40mm/張窗口內（<100ms 目標）。

前置：
  damac:  ./cfaoi_grab --rdma-dest <SPARK_IP>:18515 --cam-count ALL [--ctrl-port 8100]
  Spark:  ./cfaoi_ip --mode rdma-process --ini <含 [EdgeCheck] enabled=1 的 ini> ...
          （IP log 的「[EdgeCheck] camX slice0 前緣=NNNN」行號 spread = 37 台啟動 skew 實測）

用法：
  python3 verify_step3_trigger.py <grab_host> [port=8100] [frames_per_panel=5] [expect_cams=0]
  expect_cams=0 → 不檢查台數（用 LIST_CAMERAS 實際值）

階段（各貼數據）：
  A LIST_CAMERAS       → 列舉台數
  B GRAB_ARM（首次）   → OK + armed=true；量測冷啟預熱耗時（此成本已移出觸發路徑）
  C GRAB_ARM（再次）   → OK；量測冪等重呼耗時（應遠小於首次）
  D GRAB_START         → 量測命令往返 ms（= 網路觸發延遲上界，目標 <100ms）；
                         等收滿 N 張自動停 → grabbed/sent_frames/dropped 對帳
  E GRAB_START（第二片，不重 ARM）→ 連續生產節拍可行性
  F GRAB_STOP          → teardown：armed=false grabbing=false

上位機端到端（另跑）：scripts/upstream_simulator.py 對 Control 8787 發
  CF_LOAD_RECIPE →（Control 自動 Grab LOAD_RECIPE+GRAB_ARM 預熱）→ CF_GRAB_START → CF_STOP。
"""
import json
import socket
import sys
import time


class GrabConn:
    def __init__(self, host: str, port: int):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.f = self.sock.makefile("r", encoding="utf-8")
        self.seq = 0

    def cmd(self, name: str, params: dict | None = None, timeout: float = 60.0):
        self.seq += 1
        req = {"cmd": name, "seq": self.seq, "params": params or {}}
        self.sock.settimeout(timeout)
        self.sock.sendall((json.dumps(req) + "\n").encode())
        line = self.f.readline()
        if not line:
            raise RuntimeError(f"{name}: 連線中斷")
        return json.loads(line)

    def health(self) -> dict:
        r = self.cmd("CHECK_HEALTH")
        return r.get("data", {}) if isinstance(r.get("data"), dict) else {}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    host = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8100
    n_frames = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    expect_cams = int(sys.argv[4]) if len(sys.argv) > 4 else 0

    ok_all = True

    def check(name: str, cond: bool, detail: str):
        nonlocal ok_all
        print(f"[{'PASS' if cond else 'FAIL'}] {name}\n  {detail}")
        ok_all &= cond

    c = GrabConn(host, port)

    # A) LIST_CAMERAS
    r = c.cmd("LIST_CAMERAS")
    cams = r.get("cameras", [])
    n_cams = len(cams)
    check("A LIST_CAMERAS", r.get("status") == "OK" and n_cams > 0 and
          (expect_cams == 0 or n_cams == expect_cams),
          f"列舉 {n_cams} 台" + (f"（期望 {expect_cams}）" if expect_cams else "") +
          "：" + ", ".join(f"cam{x.get('cam_id')}={x.get('serial')}" for x in cams[:8]) +
          ("…" if n_cams > 8 else ""))

    # B) GRAB_ARM 首次（冷啟預熱——此成本已移出觸發路徑）
    t0 = time.monotonic()
    r = c.cmd("GRAB_ARM", timeout=120.0)
    arm_ms = (time.monotonic() - t0) * 1000
    st = c.health()
    check("B GRAB_ARM 首次（冷啟預熱）", r.get("status") == "OK" and st.get("armed") is True,
          f"耗時 {arm_ms:.0f}ms（重活在此，不在觸發路徑）；armed={st.get('armed')} cams={st.get('cams')}")

    # C) GRAB_ARM 再次（冪等）
    t0 = time.monotonic()
    r = c.cmd("GRAB_ARM", timeout=60.0)
    rearm_ms = (time.monotonic() - t0) * 1000
    check("C GRAB_ARM 冪等重呼", r.get("status") == "OK" and rearm_ms < arm_ms,
          f"耗時 {rearm_ms:.0f}ms（首次 {arm_ms:.0f}ms）")

    # D) GRAB_START 觸發 + 收滿自動停
    t0 = time.monotonic()
    r = c.cmd("GRAB_START", {"timeout_ms": 40000, "frames_per_panel": n_frames})
    trig_ms = (time.monotonic() - t0) * 1000
    check("D1 GRAB_START 觸發延遲（命令往返 = 上界）",
          r.get("status") == "OK" and trig_ms < 100.0,
          f"{trig_ms:.1f}ms（目標 <100ms；40mm/張窗口 @96mm/s ≈ 417ms）")

    deadline = time.monotonic() + 120
    st = {}
    while time.monotonic() < deadline:
        st = c.health()
        if st.get("running", -1) == 0:
            break
        time.sleep(0.5)
    total_expected = n_cams * n_frames
    check("D2 收滿自動停 + 對帳",
          st.get("running") == 0 and st.get("grabbed", 0) >= total_expected and
          st.get("sent_frames", 0) == total_expected and st.get("dropped", 0) == 0,
          f"grabbed={st.get('grabbed')} sent_frames={st.get('sent_frames')} "
          f"(期望 {n_cams}台×{n_frames}張={total_expected}) dropped={st.get('dropped')} "
          f"running={st.get('running')}")

    # E) 第二片（不重 ARM，連續節拍）
    t0 = time.monotonic()
    r = c.cmd("GRAB_START", {"timeout_ms": 40000, "frames_per_panel": n_frames})
    trig2_ms = (time.monotonic() - t0) * 1000
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        st = c.health()
        if st.get("running", -1) == 0:
            break
        time.sleep(0.5)
    check("E 第二片 GRAB_START（不重 ARM）",
          r.get("status") == "OK" and trig2_ms < 100.0 and st.get("running") == 0 and
          st.get("sent_frames", 0) == 2 * total_expected,   # sender 計數跨片累積；grabbed 每片歸零
          f"觸發 {trig2_ms:.1f}ms；sent_frames={st.get('sent_frames')}（兩片累積，期望 {2 * total_expected}）"
          f" grabbed={st.get('grabbed')}（本片，期望 {total_expected}）")

    # F) GRAB_STOP teardown
    c.cmd("GRAB_STOP")
    st = c.health()
    check("F GRAB_STOP teardown", st.get("armed") is False and st.get("grabbing") is False,
          f"armed={st.get('armed')} grabbing={st.get('grabbing')}")

    print("=" * 60)
    print("結果:", "全 PASS ✓" if ok_all else "有 FAIL ✗")
    print("補充：IP 端 log grep '\\[EdgeCheck\\] cam.*slice0 前緣' → 37 台行號 spread = 啟動 skew 實測；")
    print("      spread(行)×8µm = skew 換算行程，須 << 40mm。")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
