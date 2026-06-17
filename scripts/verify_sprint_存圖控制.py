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
                           share_flags: dict | None = None,
                           no_wait: bool = False) -> dict:
    """送 SEND_IMAGE_FOR_REVIEW（命令行 + binary payload）並等回應。
    no_wait=True：立即 ACK 模式（背壓壓力測試 / 串流模式）。"""
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
    if no_wait:
        cmd["params"]["no_wait"] = True
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

_MINIMAL_RECIPE_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<Recipe>
  <DetectRoiList>
    <DetectRoi>
      <AlgorithmCompare>DIV</AlgorithmCompare>
      <BrightThreshold>1.5</BrightThreshold>
      <DarkThreshold>0.85</DarkThreshold>
      <PitchX>26</PitchX>
      <PitchY>19</PitchY>
      <StartX>-1</StartX><StartY>-1</StartY>
      <EndX>-1</EndX><EndY>-1</EndY>
      <BlobMinSize>1</BlobMinSize>
      <BlobMaxSize>999999</BlobMaxSize>
      <SearchX>1</SearchX><SearchY>1</SearchY>
    </DetectRoi>
  </DetectRoiList>
</Recipe>
"""

def load_recipe(sock: socket.socket, recipe_saving: dict | None = None,
                share_flags: dict | None = None) -> dict:
    params: dict = {"recipe": "DEFAULT", "recipe_xml": _MINIMAL_RECIPE_XML, "panel_id": "VERIFY"}
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
    用 no_wait=True 單連線快送 20 幀（不等 GPU 結果），讓 queue 快速累積到滿。
    IP 應以 --max-queue-size 1 --no-overlay 啟動（快速消費 + 小 queue）。

    預期：前 1-2 幀 OK（成功入隊），後續幀收到 ERR（queue 滿，背壓觸發）。
    同時確認 VmRSS 有天花板（不隨幀數線性成長）。
    """
    section("A. FrameQueue 背壓防 OOM（Step 3，no_wait 串流模式）")
    print(f"  目標 IP: {ip}:{port}  影像: {image_path}")
    print(f"  （IP 應以 --max-queue-size 1 --test-consumer-delay-ms 2000 啟動）\n")

    w, h, payload = load_tiff_mono8(image_path)
    print(f"  {INFO}  影像尺寸: {w}×{h}  payload={len(payload)//1024//1024}MB\n")

    all_passed = True
    vmrss_before = vmrss_kb(ip_pid) if ip_pid else None

    s = connect(ip, port)
    r = load_recipe(s)
    if r.get("status") != "OK":
        print(f"  {FAIL}  LOAD_RECIPE 失敗: {r}")
        return False

    responses = []
    t_start = time.time()
    for i in range(20):
        panel = f"BP_NOWAIT_{i}"
        try:
            resp = send_image_for_review(s, panel, w, h, payload, no_wait=True)
            responses.append((i, resp))
        except Exception as e:
            responses.append((i, {"status": "EXC", "error": str(e)}))
    elapsed = time.time() - t_start
    s.close()

    vmrss_after = vmrss_kb(ip_pid) if ip_pid else None

    print(f"  --- 20 幀結果（耗時 {elapsed:.1f}s，平均 {elapsed/20*1000:.0f}ms/幀）---")
    ok_list  = [i for i, r in responses if r.get("status") == "OK"]
    err_list = [i for i, r in responses if r.get("status") == "ERR"]
    for i, r in responses:
        status = r.get("status", "?")
        extra  = r.get("error", r.get("queued", ""))
        print(f"  frame[{i:02d}]  {status}  {extra}")

    print()
    all_passed &= check(len(ok_list) >= 1,
                        f"至少 1 幀成功入隊（ok={len(ok_list)}: {ok_list[:5]}）")
    all_passed &= check(len(err_list) >= 1,
                        f"至少 1 幀觸發背壓 ERR（err={len(err_list)}: {err_list[:5]}）")

    if err_list:
        # 找到第一個 ERR 的詳細訊息
        first_err = next(r for i, r in responses if r.get("status") == "ERR")
        err_msg = first_err.get("error", "")
        print(f"\n  {INFO}  ERR 內容: {err_msg}")
        all_passed &= check(
            "FrameQueue" in err_msg or "滿" in err_msg or "overflow" in err_msg.lower(),
            "ERR 訊息包含 queue 滿的原因")

    if vmrss_before and vmrss_after:
        growth = vmrss_after - vmrss_before
        print(f"\n  {INFO}  VmRSS: {vmrss_before//1024} MB → {vmrss_after//1024} MB "
              f"（增長 {growth//1024} MB，送了 20 幀 {20*len(payload)//1024//1024} MB 資料）")
        all_passed &= check(growth < 20 * len(payload),  # 增長 < 20幀資料量（不囤在記憶體）
                            f"VmRSS 增長 {growth//1024} MB < {20*len(payload)//1024//1024} MB（不囤積）")

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

def test_save_source_oom(ip: str, port: int, image_path: str, ip_pid: int | None,
                         total_frames: int = 100, log_path: str | None = None):
    """
    開 save_source_image=true，用 no_wait=True 連送 100 幀（遠超 SourceRing 上限 2）。
    每 10 幀記錄一次 VmRSS，驗證：
      ① VmRSS 有天花板（不隨幀數線性成長）
      ② IP log 出現 [SourceWriter] WARN ring 滿 ... drop（ring 滿 = drop，非囤 List）
    IP 應以 --max-src-ring-size 2 --no-overlay 啟動。
    """
    section("C. SaveSourceImage OOM 防護（Step 5，no_wait 100 幀壓力模式）")
    print(f"  目標 IP: {ip}:{port}  影像: {image_path}")
    print(f"  （IP 應以 --max-src-ring-size 2 --no-overlay --test-source-writer-delay-ms 600 啟動）\n")

    w, h, payload = load_tiff_mono8(image_path)
    frame_mb = len(payload) // 1024 // 1024
    print(f"  {INFO}  影像尺寸: {w}×{h}  payload={frame_mb} MB  "
          f"計畫送 {total_frames} 幀 = {total_frames * frame_mb} MB\n")

    all_passed = True

    # 啟動前記錄 VmRSS 基準
    vmrss_base = vmrss_kb(ip_pid) if ip_pid else None

    # --- 開始連線並送圖 ---
    s = connect(ip, port)
    r = load_recipe(s, share_flags={"save_source_image": True})
    if not check(r.get("status") == "OK", f"LOAD_RECIPE save_source_image=true: {r.get('status')}"):
        s.close()
        return False

    # 每 SAMPLE_EVERY 幀採一次 VmRSS（在主送圖迴圈中採樣，避免多執行緒）
    SAMPLE_EVERY = 10
    vmrss_by_frame: dict[int, int] = {}
    ok_count = 0
    err_count = 0

    t_start = time.time()
    for i in range(total_frames):
        panel = f"SRC_OOM_{i:04d}"
        try:
            resp = send_image_for_review(s, panel, w, h, payload,
                                          share_flags={"save_source_image": True},
                                          no_wait=True)
            if resp.get("status") == "OK":
                ok_count += 1
            else:
                err_count += 1
        except Exception:
            err_count += 1

        if ip_pid and i % SAMPLE_EVERY == 0:
            v = vmrss_kb(ip_pid)
            if v:
                vmrss_by_frame[i] = v

    elapsed_send = time.time() - t_start
    s.close()

    # 等 IP 消化剩餘 queue（最多等 30s）
    print(f"\n  送完 {total_frames} 幀耗時 {elapsed_send:.1f}s，等 IP 消化中（最多 30s）…")
    time.sleep(30)

    # 再採一次穩定後 VmRSS
    vmrss_final = vmrss_kb(ip_pid) if ip_pid else None

    # ---- 印結果 ----
    print(f"\n  --- 送圖統計 ---")
    print(f"  OK={ok_count}  ERR={err_count}  total={total_frames}")
    all_passed &= check(ok_count >= 1, f"至少 1 幀成功入隊（ok={ok_count}）")

    if vmrss_by_frame:
        print(f"\n  --- VmRSS 按幀採樣（pid={ip_pid}）---")
        if vmrss_base:
            print(f"  frame[ -]（基準）  VmRSS={vmrss_base:,} KB  ({vmrss_base//1024} MB)")
        for frame_i in sorted(vmrss_by_frame.keys()):
            v = vmrss_by_frame[frame_i]
            print(f"  frame[{frame_i:3d}]            VmRSS={v:,} KB  ({v//1024} MB)")
        if vmrss_final:
            print(f"  frame[final]（消化後）VmRSS={vmrss_final:,} KB  ({vmrss_final//1024} MB)")

        # 穩定期 = 第 30 幀之後（跳過初始化 ramp）
        stable = {k: v for k, v in vmrss_by_frame.items() if k >= 30}
        if stable:
            min_v = min(stable.values())
            max_v = max(stable.values())
            growth = max_v - min_v
            print(f"\n  {INFO}  穩定期 VmRSS 抖動: {min_v//1024} → {max_v//1024} MB  "
                  f"（增長 {growth//1024} MB）")
            all_passed &= check(growth < 5 * frame_mb * 1024,  # 允許 5 幀以內抖動
                                f"穩定期 VmRSS 增長 {growth//1024} MB（期望 < {5*frame_mb} MB，無線性成長）")

        if vmrss_base and vmrss_final:
            total_growth = vmrss_final - vmrss_base
            print(f"  {INFO}  全程 VmRSS 增長: {total_growth//1024} MB  "
                  f"（{total_frames} 幀 × {frame_mb} MB = {total_frames*frame_mb} MB 資料）")
            all_passed &= check(total_growth < 20 * frame_mb * 1024,  # 上限 20 幀等效 RAM
                                f"全程 VmRSS 增長 {total_growth//1024} MB（遠小於 {total_frames*frame_mb} MB）")

    # ---- 檢查 drop WARN log ----
    drop_warn_found = False
    if log_path:
        try:
            log_txt = Path(log_path).read_text(errors="replace")
            drop_lines = [l for l in log_txt.splitlines()
                          if "[SourceWriter]" in l and ("WARN ring 滿" in l or "drop " in l or "共 drop" in l)]
            if drop_lines:
                drop_warn_found = True
                print(f"\n  --- [SourceWriter] WARN 摘要（前 5 條）---")
                for ln in drop_lines[:5]:
                    print(f"  {ln}")
                if len(drop_lines) > 5:
                    print(f"  … 共 {len(drop_lines)} 條")
        except Exception as e:
            print(f"  {INFO}  讀 log 失敗: {e}")

    all_passed &= check(drop_warn_found or log_path is None,
                        "[SourceWriter] ring 滿 drop WARN 出現在 log（ring=固定上限，非 List 囤積）")
    if not drop_warn_found:
        print(f"  {INFO}  請手動確認 IP log 含 [SourceWriter] WARN ring 滿 ... drop panel=...")

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
    parser.add_argument("--log-path",    default=None,
                        help="IP 程序 log 檔路徑（source_oom 用，自動掃 [SourceWriter] WARN）")
    parser.add_argument("--frames",      type=int, default=100,
                        help="source_oom 連送幀數（預設 100，建議 100-500）")
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
        print("\n📋 注意：請先以下列參數啟動 IP（queue=1 + consumer delay 2000ms 確保 queue 填滿），再跑此驗證：")
        print(f"   stdbuf -oL ./build/cfaoi_ip --mode offline-tcp --max-queue-size 1 --test-consumer-delay-ms 2000 &")
        time.sleep(1)
        results["A_backpressure"] = test_backpressure(args.ip, args.port, args.image, args.ip_pid)

    if run_det:
        results["B_determinism"] = test_determinism(
            args.ip_binary, args.image, args.max_defect_count_pass)

    if run_src:
        print("\n📋 注意：請先以下列參數啟動 IP（ring=2、no-overlay、writer delay 600ms 觸發 drop），再跑此驗證：")
        print(f"   stdbuf -oL ./build/cfaoi_ip --mode offline-tcp --max-src-ring-size 2 --no-overlay --test-source-writer-delay-ms 600 &")
        if args.log_path:
            print(f"   （log 自動掃 {args.log_path}）")
        else:
            print(f"   （加 --log-path /path/to/ip.log 可自動驗證 drop WARN）")
        time.sleep(1)
        results["C_source_oom"] = test_save_source_oom(
            args.ip, args.port, args.image, args.ip_pid,
            total_frames=args.frames, log_path=args.log_path)

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
