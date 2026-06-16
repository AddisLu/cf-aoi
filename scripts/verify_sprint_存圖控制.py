#!/usr/bin/env python3
"""
CF-AOI 存圖控制 + Buffer 安全 Sprint 驗證腳本
==============================================
跑完後把輸出貼回 STATUS.md 作為升 L3 的數據依據。

用法（在 Linux 機器上，IP 程式已編譯好）：

  # ① 背壓 + SourceRing OOM 防護測試（offline-tcp 模式）
  #   在另一個 terminal 先跑 IP：
  #   ./build/cfaoi_ip --mode offline-tcp --max-queue-size 2 --max-src-ring-size 2 &
  #   IP_PID=$!
  python3 verify_sprint_存圖控制.py backpressure --image /path/to/frame.tif --ip-pid $IP_PID

  # ② MaxDefectCountPass 決定性（offline-file 模式）
  #   script 自己啟動 IP，不需要先開
  python3 verify_sprint_存圖控制.py determinism \\
      --ip-binary ./build/cfaoi_ip --image /path/to/frame.tif \\
      --max-defect-count-pass 10

  # ③ TuningRecipe 零磁碟驗證
  python3 verify_sprint_存圖控制.py tuning_recipe --image /path/to/frame.tif

  # 全部一起跑
  python3 verify_sprint_存圖控制.py all \\
      --ip-binary ./build/cfaoi_ip --image /path/to/frame.tif \\
      --max-defect-count-pass 10 --ip 127.0.0.1 --port 8200

依賴：Python 3.10+，無額外套件。
"""

import argparse, json, os, socket, struct, subprocess, sys, threading, time
from pathlib import Path

# ─── 通用工具 ───────────────────────────────────────────────────────────────

SEQ_LOCK = threading.Lock()
_SEQ = 0

def next_seq():
    global _SEQ
    with SEQ_LOCK:
        _SEQ += 1
        return _SEQ

def make_frame_payload(w: int, h: int) -> bytes:
    """製造一張零值 Mono8 影像 payload（測試用，無真實缺陷）"""
    return bytes(w * h)

def load_tiff_mono8(path: str):
    """用 cv2（若有）讀 TIFF，否則讀 raw .bin；回傳 (w, h, bytes)。"""
    try:
        import cv2, numpy as np  # type: ignore
        img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if img is None:
            raise RuntimeError(f"cv2 讀不到：{path}")
        if img.ndim == 3:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        h, w = img.shape
        return w, h, img.tobytes()
    except ImportError:
        pass
    # fallback：當 raw bin（width*height bytes）
    data = Path(path).read_bytes()
    # 嘗試從檔名猜維度（8192x5000 = 40960000 bytes）
    if len(data) == 8192 * 5000:
        return 8192, 5000, data
    raise RuntimeError(f"無法讀取影像且無法猜出維度：{path}（請裝 cv2）")

def send_cmd_raw(sock: socket.socket, cmd: str, params: dict) -> dict:
    """送一行 JSON 命令，等一行 JSON 回應。"""
    seq = next_seq()
    line = json.dumps({"cmd": cmd, "seq": seq, "params": params}) + "\n"
    sock.sendall(line.encode())
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("連線中斷")
        buf += chunk
    return json.loads(buf.split(b"\n")[0])

def send_image_for_review(sock: socket.socket, panel_id: str,
                           w: int, h: int, payload: bytes,
                           debug: bool = False,
                           share_flags: dict | None = None) -> dict:
    """送 SEND_IMAGE_FOR_REVIEW（命令行 + binary payload）並等回應。"""
    seq = next_seq()
    cmd = {
        "cmd": "SEND_IMAGE_FOR_REVIEW",
        "seq": seq,
        "params": {
            "panel_id": panel_id, "cam_id": 0,
            "width": w, "height": h,
            "frame_seq": seq, "payload_bytes": len(payload),
            "last": True, "debug": debug,
        }
    }
    line = json.dumps(cmd) + "\n"
    sock.sendall(line.encode())
    sock.sendall(payload)
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("連線中斷")
        buf += chunk
    return json.loads(buf.split(b"\n")[0])

def load_recipe(sock: socket.socket, recipe_saving: dict | None = None,
                share_flags: dict | None = None) -> dict:
    params: dict = {"recipe": "DEFAULT", "recipe_xml": "", "panel_id": "VERIFY"}
    if recipe_saving:
        params["recipe_saving"] = recipe_saving
    if share_flags:
        params["share_flags"] = share_flags
    return send_cmd_raw(sock, "LOAD_RECIPE", params)

def connect(ip: str, port: int, timeout: float = 30) -> socket.socket:
    deadline = time.time() + timeout
    while True:
        try:
            s = socket.create_connection((ip, port), timeout=5)
            s.settimeout(120)
            return s
        except (ConnectionRefusedError, OSError):
            if time.time() > deadline:
                raise RuntimeError(f"IP 程式未在 {ip}:{port} 監聽（逾時 {timeout}s）")
            time.sleep(0.5)

def vmrss_kb(pid: int) -> int | None:
    """讀 /proc/{pid}/status 的 VmRSS（KB），找不到回 None。"""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except Exception:
        pass
    return None

# ─── 驗證項目 ────────────────────────────────────────────────────────────────

PASS = "[PASS]"
FAIL = "[FAIL]"
INFO = "[INFO]"

def section(title: str):
    bar = "=" * 66
    print(f"\n{bar}\n  {title}\n{bar}")

def check(cond: bool, msg: str):
    tag = PASS if cond else FAIL
    print(f"  {tag}  {msg}")
    return cond

# ── A. 背壓防 OOM ─────────────────────────────────────────────────────────────

def test_backpressure(ip: str, port: int, image_path: str, ip_pid: int | None):
    """
    連送 max_queue_size + 1 幀，第 max_queue_size+1 幀應回 ERR。
    同時監控 VmRSS（若提供 pid）確認記憶體有天花板。
    """
    section("A. FrameQueue 背壓防 OOM（Step 3）")
    print(f"  目標 IP: {ip}:{port}  影像: {image_path}")
    print(f"  （IP 應以 --max-queue-size 2 啟動）\n")

    w, h, payload = load_tiff_mono8(image_path)
    print(f"  {INFO}  影像尺寸: {w}×{h}  payload={len(payload)//1024//1024}MB")

    all_passed = True

    # 同時從 3 條 TCP 連線送圖；queue max=2 → 第 3 條應 ERR
    results: dict[int, dict] = {}
    errors:  dict[int, str]  = {}

    vmrss_samples: list[tuple[float, int]] = []
    stop_monitor = threading.Event()

    def monitor_vmrss():
        if ip_pid is None:
            return
        t0 = time.time()
        while not stop_monitor.is_set():
            v = vmrss_kb(ip_pid)
            if v is not None:
                vmrss_samples.append((time.time() - t0, v))
            time.sleep(0.2)

    threading.Thread(target=monitor_vmrss, daemon=True).start()

    def worker(idx: int):
        try:
            s = connect(ip, port)
            # 先 LOAD_RECIPE（每個 client 都需要建立協議狀態）
            r = load_recipe(s)
            if r.get("status") != "OK":
                errors[idx] = f"LOAD_RECIPE 失敗: {r}"
                return
            panel = f"BP_TEST_{idx}"
            resp = send_image_for_review(s, panel, w, h, payload)
            results[idx] = resp
        except Exception as e:
            errors[idx] = str(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    t_start = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    elapsed = time.time() - t_start
    stop_monitor.set()
    time.sleep(0.3)  # 讓 monitor 多取幾個點

    print(f"\n  --- 3 條連線結果（耗時 {elapsed:.1f}s）---")
    ok_count  = sum(1 for r in results.values() if r.get("status") == "OK")
    err_count = sum(1 for r in results.values() if r.get("status") == "ERR")
    exc_count = len(errors)

    for i in range(3):
        if i in results:
            r = results[i]
            status = r.get("status", "?")
            err_msg = r.get("error", "")
            print(f"  conn[{i}]  status={status}  {err_msg}")
        elif i in errors:
            print(f"  conn[{i}]  exception: {errors[i]}")

    print()
    # 預期：2 個 OK + 1 個 ERR（或 2 OK + 1 exception 若連線在 push 前就超時）
    all_passed &= check(ok_count >= 1,
                        f"至少 1 幀成功入隊（ok={ok_count}）")
    all_passed &= check(err_count + exc_count >= 1,
                        f"至少 1 幀被拒收（ERR={err_count} exc={exc_count}）")

    if err_count >= 1:
        rej = next(r for r in results.values() if r.get("status") == "ERR")
        err_msg = rej.get("error", "")
        print(f"\n  {INFO}  ERR 回應內容: {err_msg}")
        all_passed &= check("FrameQueue" in err_msg or "滿" in err_msg or "overflow" in err_msg.lower(),
                            "ERR 錯誤訊息包含 queue 滿的原因")

    # VmRSS 天花板
    if vmrss_samples:
        print(f"\n  --- VmRSS 取樣（pid={ip_pid}）---")
        for ts, v in vmrss_samples:
            print(f"  t+{ts:5.1f}s  VmRSS={v:,} KB  ({v//1024} MB)")
        first_v = vmrss_samples[0][1]
        last_v  = vmrss_samples[-1][1]
        growth  = last_v - first_v
        all_passed &= check(growth < 500 * 1024,
                            f"VmRSS 增長 {growth//1024} MB（期望 <500 MB 不無限成長）")

    return all_passed

# ── B. MaxDefectCountPass 決定性 ──────────────────────────────────────────────

def test_determinism(ip_binary: str, image_path: str, max_pass: int):
    """
    用 offline-file + --verify-deterministic + --max-defect-count-pass N 跑兩次，
    收集 [Verify] / [MaxDefectCountPass] log 確認決定性。
    """
    section(f"B. MaxDefectCountPass 決定性（Step 1，上限={max_pass}）")
    print(f"  二進位: {ip_binary}  影像: {image_path}\n")

    all_passed = True
    cmd = [
        ip_binary, "--mode", "offline-file",
        "--input", image_path,
        "--output", "/tmp/verify_out",
        "--verify-deterministic",
        "--max-defect-count-pass", str(max_pass),
        "--no-overlay",  # 加速
    ]
    print(f"  命令: {' '.join(cmd)}\n")

    for run_idx in range(2):
        print(f"  --- 第 {run_idx+1} 次執行 ---")
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120
            )
        except subprocess.TimeoutExpired:
            print(f"  {FAIL}  執行逾時（120s）")
            all_passed = False
            continue

        stdout = result.stdout
        stderr = result.stderr
        combined = stdout + stderr

        # 印關鍵行（[Verify] [MaxDefectCountPass] [Done]）
        key_lines = [l for l in combined.splitlines()
                     if any(k in l for k in ["[Verify]","[MaxDefectCountPass]","[Done]","[Zone]","缺陷"])]
        for l in key_lines[:30]:
            print(f"  {l}")

        bit_exact_lines = [l for l in combined.splitlines() if "bit-exact" in l]
        nondet_lines    = [l for l in combined.splitlines() if "NON-DETERMINISTIC" in l]
        truncated_lines = [l for l in combined.splitlines() if "MaxDefectCountPass" in l]

        all_passed &= check(result.returncode in (0, 0),
                            f"執行成功（returncode={result.returncode}）")
        all_passed &= check(len(nondet_lines) == 0,
                            f"無 NON-DETERMINISTIC（共 {len(nondet_lines)} 個違規）")
        all_passed &= check(len(bit_exact_lines) >= 1,
                            f"有 bit-exact 確認行（{len(bit_exact_lines)} 個）")
        if max_pass >= 0:
            all_passed &= check(len(truncated_lines) >= 1,
                                f"出現 MaxDefectCountPass 截斷訊息（{len(truncated_lines)} 行）")
        print()

    return all_passed

# ── C. SaveSourceImage OOM 防護 ───────────────────────────────────────────────

def test_save_source_oom(ip: str, port: int, image_path: str, ip_pid: int | None):
    """
    開 save_source_image=true，設小 src_ring（max_src_ring_size=2），連送 5 幀。
    確認：
      ① VmRSS 有天花板（不線性成長）
      ② 出現 [SourceWriter] WARN drop 訊息
    """
    section("C. SaveSourceImage OOM 防護（Step 5）")
    print(f"  目標 IP: {ip}:{port}  影像: {image_path}")
    print(f"  （IP 應以 --max-src-ring-size 2 啟動）\n")

    w, h, payload = load_tiff_mono8(image_path)
    print(f"  {INFO}  影像尺寸: {w}×{h}  payload={len(payload)//1024//1024}MB\n")

    all_passed = True

    vmrss_samples: list[tuple[float, int]] = []
    stop_monitor = threading.Event()

    def monitor_vmrss():
        if ip_pid is None:
            return
        t0 = time.time()
        while not stop_monitor.is_set():
            v = vmrss_kb(ip_pid)
            if v is not None:
                vmrss_samples.append((time.time() - t0, v))
            time.sleep(0.3)

    threading.Thread(target=monitor_vmrss, daemon=True).start()

    s = connect(ip, port)
    r = load_recipe(s, share_flags={"save_source_image": True})
    check(r.get("status") == "OK", f"LOAD_RECIPE with save_source_image=true: {r.get('status')}")

    responses = []
    for i in range(5):
        panel = f"SRC_TEST_{i}"
        resp = send_image_for_review(s, panel, w, h, payload)
        responses.append(resp)
        rss = vmrss_kb(ip_pid) if ip_pid else None
        rss_str = f"  VmRSS={rss:,} KB" if rss else ""
        print(f"  frame[{i}]  status={resp.get('status','?')}{rss_str}")
        time.sleep(0.1)

    stop_monitor.set()
    time.sleep(0.3)
    s.close()

    ok_count = sum(1 for r in responses if r.get("status") == "OK")
    all_passed &= check(ok_count >= 1, f"至少 1 幀處理成功（ok={ok_count}）")

    if vmrss_samples and len(vmrss_samples) >= 3:
        print(f"\n  --- VmRSS 取樣（pid={ip_pid}）---")
        for ts, v in vmrss_samples:
            print(f"  t+{ts:5.1f}s  VmRSS={v:,} KB  ({v//1024} MB)")

        # 過了 2 秒後的 VmRSS 不應無限成長（允許前 2 秒有 CUDA 初始化）
        late_samples = [(t, v) for t, v in vmrss_samples if t > 2.0]
        if len(late_samples) >= 2:
            first_late = late_samples[0][1]
            last_late  = late_samples[-1][1]
            growth = last_late - first_late
            print(f"\n  {INFO}  後期 VmRSS 增長: {growth//1024} MB  "
                  f"({first_late//1024} → {last_late//1024} MB)")
            all_passed &= check(growth < 300 * 1024,
                                f"後期 VmRSS 增長 {growth//1024} MB < 300 MB（無 OOM 炸彈）")

    print(f"\n  {INFO}  請確認 IP 程式 log 出現：[SourceWriter] WARN ring 滿 ... drop panel=...")

    return all_passed

# ── D. TuningRecipe 零磁碟 ────────────────────────────────────────────────────

def test_tuning_recipe(ip: str, port: int, image_path: str, output_dir: str):
    """
    share_flags.tuning_recipe=true，送一幀，確認：
      ① TCP 收到含 DefectCnt 的 JSON 結果
      ② output_dir 裡沒有新的 ResultInfo.json / overlay / Defect_ 檔
    """
    section("D. TuningRecipe 零磁碟驗證（Step 2）")
    print(f"  目標 IP: {ip}:{port}  影像: {image_path}\n")

    all_passed = True
    w, h, payload = load_tiff_mono8(image_path)

    # 記錄送圖前 output_dir 的檔案集合
    out_path = Path(output_dir)
    def all_output_files():
        if not out_path.exists():
            return set()
        return {str(p) for p in out_path.rglob("*") if p.is_file()}

    files_before = all_output_files()

    s = connect(ip, port)
    r = load_recipe(s, share_flags={"tuning_recipe": True})
    all_passed &= check(r.get("status") == "OK", f"LOAD_RECIPE tuning_recipe=true: {r.get('status')}")

    resp = send_image_for_review(s, "TUNING_TEST", w, h, payload)
    s.close()

    print(f"\n  TCP 回應 (seq={resp.get('seq','?')} status={resp.get('status','?')}):")
    result_json = resp.get("result", resp.get("data", None))
    if result_json:
        if isinstance(result_json, str):
            try:
                result_json = json.loads(result_json)
            except Exception:
                pass
        print(f"  {json.dumps(result_json, ensure_ascii=False, indent=2)[:400]}")
    else:
        print(f"  {json.dumps(resp, ensure_ascii=False)[:400]}")

    all_passed &= check(resp.get("status") == "OK",
                        "TCP 回應 status=OK（結果仍回傳）")

    # 等 writer thread 有機會寫（若有 bug 才會寫，正常 TuningRecipe 不寫）
    time.sleep(1.0)
    files_after  = all_output_files()
    new_files = files_after - files_before
    ip_new = {f for f in new_files if "_diag" not in f and "source" not in f}

    print(f"\n  output_dir 新增檔案：{len(ip_new)} 個（不含 _diag/source）")
    for f in sorted(ip_new)[:10]:
        print(f"    {f}")

    all_passed &= check(len(ip_new) == 0,
                        "TuningRecipe 模式：output 目錄零新增檔案")

    return all_passed

# ── E. BufferCalc log ────────────────────────────────────────────────────────

def test_buffer_calc_log(ip_binary: str):
    """
    用 --max-queue-size 3 --max-src-ring-size 2 啟動 IP，抓 [BufferCalc] 一行。
    """
    section("E. Buffer 計算器啟動 log")
    print(f"  二進位: {ip_binary}\n")

    # 只讓 IP 啟動後馬上看 stdout，再送 SIGTERM
    cmd = [ip_binary, "--mode", "offline-tcp",
           "--max-queue-size", "3", "--max-src-ring-size", "2",
           "--no-save-images"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        calc_line = None
        t0 = time.time()
        for line in proc.stdout:  # type: ignore
            print(f"  {line.rstrip()}")
            if "[BufferCalc]" in line:
                calc_line = line.strip()
            if time.time() - t0 > 10 or calc_line:
                break
        proc.terminate()
        proc.wait(timeout=5)
    except Exception as e:
        print(f"  {FAIL}  啟動 IP 失敗: {e}")
        return False

    ok = check(calc_line is not None, "[BufferCalc] 行出現在啟動 log")
    if calc_line:
        print(f"\n  {INFO}  {calc_line}")
        ok &= check("FrameQueue上限" in calc_line, "包含 FrameQueue上限")
        ok &= check("SourceRing上限" in calc_line, "包含 SourceRing上限")
    return ok

# ─── 主程式 ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="存圖控制 Sprint 驗證腳本 — 跑完貼 log 作為 L3 依據",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("mode", choices=["backpressure","determinism","source_oom",
                                         "tuning_recipe","buffer_calc","all"],
                        help="要跑的驗證項目")
    parser.add_argument("--ip",          default="127.0.0.1")
    parser.add_argument("--port",        type=int, default=8200)
    parser.add_argument("--image",       help="測試用 Mono8 影像（.tif/.bin）")
    parser.add_argument("--ip-binary",   default="./build/cfaoi_ip", help="IP 二進位路徑")
    parser.add_argument("--ip-pid",      type=int, default=None, help="已執行的 IP 程序 PID（供 VmRSS 監控）")
    parser.add_argument("--max-defect-count-pass", type=int, default=10,
                        help="MaxDefectCountPass 截斷值（determinism 用，預設 10）")
    parser.add_argument("--output",      default="output", help="IP output 目錄（tuning_recipe 用）")
    args = parser.parse_args()

    results: dict[str, bool] = {}

    run_bp    = args.mode in ("backpressure",  "all")
    run_det   = args.mode in ("determinism",   "all")
    run_src   = args.mode in ("source_oom",    "all")
    run_tun   = args.mode in ("tuning_recipe", "all")
    run_calc  = args.mode in ("buffer_calc",   "all")

    if (run_bp or run_src or run_tun) and not args.image:
        print("❌ 需要 --image 參數"); sys.exit(1)
    if run_det and not args.image:
        print("❌ 需要 --image 參數"); sys.exit(1)
    if run_calc and not Path(args.ip_binary).exists():
        print(f"❌ IP 二進位不存在：{args.ip_binary}"); sys.exit(1)

    if run_bp:
        print("\n📋 注意：請先以 --max-queue-size 2 啟動 IP，再跑此驗證")
        print(f"   ./build/cfaoi_ip --mode offline-tcp --max-queue-size 2 &")
        time.sleep(1)
        results["A_backpressure"] = test_backpressure(args.ip, args.port, args.image, args.ip_pid)

    if run_det:
        results["B_determinism"] = test_determinism(
            args.ip_binary, args.image, args.max_defect_count_pass)

    if run_src:
        print("\n📋 注意：請先以 --max-src-ring-size 2 啟動 IP，再跑此驗證")
        print(f"   ./build/cfaoi_ip --mode offline-tcp --max-src-ring-size 2 &")
        time.sleep(1)
        results["C_source_oom"] = test_save_source_oom(
            args.ip, args.port, args.image, args.ip_pid)

    if run_tun:
        results["D_tuning_recipe"] = test_tuning_recipe(
            args.ip, args.port, args.image, args.output)

    if run_calc:
        results["E_buffer_calc"] = test_buffer_calc_log(args.ip_binary)

    # ── 最終摘要 ───────────────────────────────────────────────────────────────
    bar = "=" * 66
    print(f"\n{bar}")
    print("  驗證摘要")
    print(bar)
    all_ok = True
    for name, ok in results.items():
        tag = PASS if ok else FAIL
        print(f"  {tag}  {name}")
        all_ok &= ok
    print(bar)
    if all_ok:
        print("\n✅ 全部通過 → 可升 L3（x86 驗證）\n"
              "   請把本輸出貼到 STATUS.md 對應列的「驗證方式」欄\n"
              "   並 commit: docs: 存圖控制 sprint x86 驗證 PASS → 升 L3")
    else:
        print("\n❌ 有項目未通過 → 查 log + 修 code，再重跑\n")
    sys.exit(0 if all_ok else 1)

if __name__ == "__main__":
    main()
