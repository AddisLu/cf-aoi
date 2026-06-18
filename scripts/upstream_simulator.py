#!/usr/bin/env python3
"""
上位機（Master Controller）模擬器 — 端到端驗 Control 的 CF_/8787 交握（L3）。

用途：在家端到端驗「真 Control（已 .Start UpstreamServer）↔ 模擬上位機」的 CF_ 格式/交握。
  - 模擬器扮演「上位機 client」，連 Control:8787，送 CF_ 命令、讀 9 參數回應（| 分隔、\\r\\n）。
  - 驗格式/交握：CF_READY/CF_LOAD_RECIPE/CF_GET_RESULT 應回 OK；CF_CHECK_ALIGN/CF_SET_ALIGN
    應回 **ERR（誠實失敗，非假 OK）**——offline 未執行對位。

⚠️ 誠實分級：本模擬器驗的是「Control 端 CF_ 格式/交握」(L3，需真 Control 在跑)。
   **真上位機協議認帳（欄位/序列/μm 是否如實機預期）= L4，非本工具能證**；μm 契約(#5)為 IP 片面提議(L4)。
   模擬器 ≠ 真上位機認帳。

用法：
  python3 scripts/upstream_simulator.py --host 127.0.0.1 --port 8787 \\
      --recipe DEFAULT --panel panelA
"""
import argparse
import socket
import sys


def send(sock, line, timeout=5.0):
    """送一條 CF_ 命令（\\r\\n 結尾），讀回一行 9 參數回應。"""
    sock.sendall((line + "\r\n").encode("utf-8"))
    sock.settimeout(timeout)
    buf = bytearray()
    while True:
        b = sock.recv(1)
        if not b:
            break
        if b == b"\n":
            break
        if b != b"\r":
            buf += b
    return buf.decode("utf-8", errors="replace")


def expect(label, resp, want_prefix):
    ok = resp.startswith(want_prefix)
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}: 期望 {want_prefix}* → 實得  {resp!r}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--recipe", default="DEFAULT")
    ap.add_argument("--panel", default="panelA")
    a = ap.parse_args()

    print(f"連 Control 上位機介面 {a.host}:{a.port} …")
    try:
        s = socket.create_connection((a.host, a.port), timeout=5.0)
    except OSError as e:
        print(f"  連線失敗：{e}（Control 是否在跑、UpstreamServer 是否 .Start？）")
        return 2

    results = []
    with s:
        # CF_LOAD_RECIPE|{recipe}|{panelId}|{datetime}|||||||{detectMode}
        results.append(expect("CF_READY", send(s, "CF_READY"), "OK"))
        results.append(expect(
            "CF_LOAD_RECIPE",
            send(s, f"CF_LOAD_RECIPE|{a.recipe}|{a.panel}|2026-06-19-00-00-00|||||||0"),
            "OK"))
        results.append(expect("CF_GET_RESULT", send(s, "CF_GET_RESULT"), "OK"))
        # 對位 offline 未支援 → 誠實失敗（非假 OK）
        results.append(expect("CF_CHECK_ALIGN (應誠實失敗)", send(s, "CF_CHECK_ALIGN"), "ERR"))
        results.append(expect("CF_SET_ALIGN (應誠實失敗)",
                              send(s, "CF_SET_ALIGN|Cs_AlignSet|1|2"), "ERR"))

    allok = all(results)
    print("✓ 全部 CF_ 交握符合（格式 L3；真上位機認帳/μm = L4 非本工具能證）"
          if allok else "✗ 有不符")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
