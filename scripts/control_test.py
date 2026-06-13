#!/usr/bin/env python3
"""
CF-AOI Step 1 測試腳本
用途：從 Linux/Mac 送 MIL 影像給 IP 程式做 offline-tcp 分析
不需要 WinForms，可直接在 Linux 或 Mac 上執行

用法：
  python3 control_test.py --ip 127.0.0.1 --image /path/to/frame.tif
  python3 control_test.py --ip 127.0.0.1 --image /path/to/frame.tif --recipe MY_RECIPE
  python3 control_test.py --ip 127.0.0.1 --image /path/to/frame.tif --bth 1.25 --dth 0.80
"""

import socket, json, struct, sys, os, time, argparse
from pathlib import Path

IP_PORT = 8200
SEQ     = 0

def send_cmd(sock, cmd: str, params: dict) -> dict:
    global SEQ
    SEQ += 1
    msg = json.dumps({"cmd": cmd, "seq": SEQ, "params": params}) + "\n"
    sock.sendall(msg.encode())
    resp = b""
    while b"\n" not in resp:
        resp += sock.recv(65536)
    return json.loads(resp.split(b"\n")[0])

def send_image_stream(sock, image_path: str, cam_id: int,
                      recipe: str, override_zone: dict | None = None) -> dict:
    """大影像分塊串流傳送（40MB 的 8192×5000 Mono8）"""
    global SEQ
    SEQ += 1
    data = Path(image_path).read_bytes()

    # 先送 header 命令
    header = json.dumps({
        "cmd": "SEND_IMAGE_STREAM_BEGIN",
        "seq": SEQ,
        "params": {
            "cam_id":        cam_id,
            "recipe":        recipe,
            "panel_id":      f"TEST_{int(time.time())}",
            "file_size":     len(data),
            "filename":      Path(image_path).name,
            "override_zone": override_zone
        }
    }) + "\n"
    sock.sendall(header.encode())

    # 確認 IP 準備好
    ack = b""
    while b"\n" not in ack:
        ack += sock.recv(1024)
    ack_obj = json.loads(ack.split(b"\n")[0])
    if ack_obj.get("status") != "OK":
        raise RuntimeError(f"IP 未準備好接收: {ack_obj}")

    # 分 64KB 區塊傳送
    chunk = 65536
    sent  = 0
    t0    = time.time()
    while sent < len(data):
        n = min(chunk, len(data) - sent)
        sock.sendall(data[sent:sent+n])
        sent += n
        pct = sent * 100 // len(data)
        mb  = sent / 1e6
        print(f"\r  傳送中... {mb:.1f}/{len(data)/1e6:.1f} MB  ({pct}%)", end="", flush=True)
    print(f"\n  傳送完成：{len(data)/1e6:.1f} MB in {time.time()-t0:.2f}s")

    # 等待 IP 回傳結果（GPU 處理需要幾秒）
    print("  等待 GPU 分析...", end="", flush=True)
    resp = b""
    while b"\n" not in resp:
        chunk_r = sock.recv(65536)
        if not chunk_r:
            raise RuntimeError("連線中斷")
        resp += chunk_r
    print(" 完成！")
    return json.loads(resp.split(b"\n")[0])

def print_result(result: dict):
    print("\n" + "="*60)
    data = result.get("data", result)
    if "error" in data:
        print(f"  ❌ 錯誤：{data['error']}")
        return

    print(f"  面板 ID：{data.get('panel_id','—')}")
    print(f"  配方：  {data.get('recipe','—')}")
    print(f"  模式：  {data.get('mode','—')}")
    print(f"  處理時間：{data.get('processing_ms','—'):.1f} ms")
    print()

    for cam in data.get("cam_results", []):
        judge  = cam.get("judge","?")
        n      = cam.get("defect_count", 0)
        ai_ok  = cam.get("ai_ok_count", 0)
        icon   = "✅" if judge == "OK" else "❌"
        print(f"  {icon} cam_id={cam['cam_id']}  判定={judge}  "
              f"缺陷={n}  AI改OK={ai_ok}")
        for d in cam.get("defects", [])[:10]:
            print(f"     #{d['id']:3d} {d['type']:15s} "
                  f"({d['gc_x']:5d},{d['gc_y']:5d}) "
                  f"size={d.get('x_size',0)}×{d.get('y_size',0)}  "
                  f"AI={d.get('ai_result','—')}({d.get('ai_score',1.0):.2f})")
        if len(cam.get("defects",[])) > 10:
            print(f"     ... 共 {n} 個（只顯示前 10 個）")

    summary = data.get("summary", {})
    print()
    print(f"  總計 {summary.get('total_defects',0)} 個缺陷  "
          f"最終判定：{summary.get('final_judge','?')}")
    print("="*60)

def main():
    parser = argparse.ArgumentParser(description="CF-AOI Step 1 測試工具")
    parser.add_argument("--ip",     default="127.0.0.1", help="IP 程式的 IP（預設 127.0.0.1）")
    parser.add_argument("--port",       type=int, default=8200, help="控制命令 port（預設 8200）")
    parser.add_argument("--image-port", type=int, default=9000, help="影像串流 port（預設 9000）")
    parser.add_argument("--image",  required=True, help="MIL 影像路徑（.tif/.bmp/.png）")
    parser.add_argument("--recipe", default="DEFAULT",   help="配方名稱（不存在會自動生成）")
    parser.add_argument("--cam-id", type=int, default=0)
    parser.add_argument("--bth",    type=float, default=None, help="臨時覆蓋 BTH（亮缺陷閾值）")
    parser.add_argument("--dth",    type=float, default=None, help="臨時覆蓋 DTH（暗缺陷閾值）")
    parser.add_argument("--pitch-x",type=int,   default=None, help="臨時覆蓋 PitchX")
    parser.add_argument("--pitch-y",type=int,   default=None, help="臨時覆蓋 PitchY")
    parser.add_argument("--health", action="store_true",      help="只查詢健康狀態")
    args = parser.parse_args()

    if not Path(args.image).exists() and not args.health:
        print(f"❌ 影像不存在：{args.image}")
        sys.exit(1)

    # 建立 override_zone（若有臨時參數）
    override = {}
    if args.bth     is not None: override["ThB"]    = args.bth
    if args.dth     is not None: override["ThD"]    = args.dth
    if args.pitch_x is not None: override["PitchX"] = args.pitch_x
    if args.pitch_y is not None: override["PitchY"] = args.pitch_y

    print(f"連線到 IP 程式 {args.ip}:{args.port} ...")
    try:
        sock = socket.create_connection((args.ip, args.port), timeout=10)
        sock.settimeout(300)  # 最多等 5 分鐘（大影像）
        print("  ✅ 連線成功")
    except Exception as e:
        print(f"  ❌ 連線失敗：{e}")
        print(f"     確認 IP 程式已啟動：./build/cfaoi_ip --mode offline-tcp --control-port 8200")
        sys.exit(1)

    try:
        # 健康檢查
        if args.health:
            r = send_cmd(sock, "CHECK_HEALTH", {})
            print(json.dumps(r, indent=2, ensure_ascii=False))
            return

        # 載入配方
        print(f"載入配方 '{args.recipe}' ...")
        r = send_cmd(sock, "LOAD_RECIPE", {"recipe": args.recipe, "panel_id": "TEST"})
        if r.get("status") == "OK":
            print("  ✅ 配方載入成功")
            if r.get("data", {}).get("is_auto_generated"):
                print("  ⚠️  配方為自動生成！請確認 PitchX/PitchY 參數")
        else:
            print(f"  ⚠️  {r}")

        # 傳送影像 + 分析
        file_size = Path(args.image).stat().st_size
        print(f"\n傳送影像：{args.image}")
        print(f"  大小：{file_size/1e6:.1f} MB")
        if override:
            print(f"  臨時覆蓋參數：{override}")

        result = send_image_stream(
            sock, args.image, args.cam_id, args.recipe,
            override_zone=override if override else None
        )

        print_result(result)

        # 儲存 JSON 結果
        out_path = Path(args.image).stem + "_result.json"
        Path(out_path).write_text(
            json.dumps(result.get("data", result), indent=2, ensure_ascii=False)
        )
        print(f"\n  結果已存至 {out_path}")

    finally:
        sock.close()

if __name__ == "__main__":
    main()
