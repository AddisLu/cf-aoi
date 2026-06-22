#!/usr/bin/env python3
"""
verify_rdma_replay.py — 舊機台存圖 → RDMA → Spark IP 運算 的回放驅動（在 damac 上跑）

相機/Switch 未到貨前，用舊圖驗證「RDMA 傳輸 + Spark AOI」整條流程是否塞車。
damac 無 OpenCV/numpy，只有 PIL：本驅動用 PIL 解 TIF → Mono8 raw bytes → 經 stdin 餵給
C++ `image_replay_sender`（它負責 RDMA 送出）。排除 macOS `._*` resource-fork 垃圾檔。

用法（damac）：
  python3 verify_rdma_replay.py --sender ./grab/build/image_replay_sender \
      --dir ~/Addis/T550QVN10_TGT_G --spark 192.168.3.1 --port 18515 \
      [--ccds IP01,IP02] [--per-ccd N]   # 子集測試；預設全 24 CCD × 全部影像

每幀寫入 sender stdin：「cam_id seq\n」+ width*height bytes。cam_id 取自夾名 IPnn → nn；seq 全域遞增。
背壓：RDMA credit 耗盡 → sender 不讀 stdin → 此處 write 阻塞 → 自然限速（pipe 背壓鏈）。
"""
import argparse, glob, os, subprocess, sys, time
from PIL import Image

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sender", required=True, help="image_replay_sender 執行檔路徑")
    ap.add_argument("--dir", required=True, help="影像根目錄（內含 IP01..IP24）")
    ap.add_argument("--spark", default="192.168.3.1"); ap.add_argument("--port", default="18515")
    ap.add_argument("--ccds", default="", help="逗號分隔子集（如 IP01,IP02）；空=全部")
    ap.add_argument("--per-ccd", type=int, default=0, help="每 CCD 限張數；0=全部")
    a = ap.parse_args()

    root = os.path.expanduser(a.dir)
    ccd_dirs = sorted(d for d in os.listdir(root)
                      if d.startswith("IP") and os.path.isdir(os.path.join(root, d)))
    if a.ccds:
        want = set(a.ccds.split(","))
        ccd_dirs = [d for d in ccd_dirs if d in want]
    if not ccd_dirs:
        print("找不到 CCD 夾"); sys.exit(1)

    # 探一張取 W/H（排除 ._）
    def tifs(d):
        return sorted(f for f in glob.glob(os.path.join(root, d, "*.tif"))
                      if not os.path.basename(f).startswith("._"))
    first = tifs(ccd_dirs[0])[0]
    W, H = Image.open(first).size
    print(f"[replay-drv] {len(ccd_dirs)} CCD, frame={W}x{H}, sender={a.sender}")

    proc = subprocess.Popen([a.sender, a.spark, a.port, str(W), str(H)], stdin=subprocess.PIPE)
    seq, t0 = 0, time.time()
    try:
        for d in ccd_dirs:
            cam = int(d[2:])              # IP01 → 1
            files = tifs(d)
            if a.per_ccd > 0: files = files[:a.per_ccd]
            for f in files:
                raw = Image.open(f).convert("L").tobytes()
                if len(raw) != W * H:
                    print(f"  WARN {os.path.basename(f)} size {len(raw)} != {W*H}, skip"); continue
                proc.stdin.write(f"{cam} {seq}\n".encode())
                proc.stdin.write(raw)
                seq += 1
            print(f"[replay-drv] {d}: 送出 {len(files)} 張（累計 {seq}）")
    finally:
        try: proc.stdin.close()
        except Exception: pass
        rc = proc.wait()
    dt = time.time() - t0
    print(f"[replay-drv] 完成：{seq} 幀，{dt:.1f}s，sender rc={rc}")
    sys.exit(0 if rc == 0 else 1)

if __name__ == "__main__":
    main()
