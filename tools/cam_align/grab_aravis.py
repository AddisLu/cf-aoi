#!/usr/bin/env python3
"""用 Aravis 從 iPORT CL-GigE 取像並存檔 —— 不需要 eBUS SDK。

Aravis 是開源的 GigE Vision 實作，可以直接跟 iPORT 串流，用來在 eBUS SDK
到位前先確認「相機到底有沒有出影像」。正式系統仍走 eBUS。

安全設計：
  - 只用明確的 feature 讀寫，不用 get_region/set_region
    （後者在讀取失敗回傳 0 時，會把 Width=0 寫進裝置）
  - 每個寫入都讀回驗證，不符就中止
  - 開始前先檢查 Camera Link 連線狀態與 Width/Height，不合理就直接停

用法:
    python3 grab_aravis.py [--frames N] [--height H] [--packet-size S]
                           [--out PREFIX] [--timeout SEC] [--force]
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL = os.path.join(HERE, 'aravis-local', 'usr', 'lib', 'x86_64-linux-gnu')

# 讓本地解壓的 aravis 被找到（不需系統安裝）
if os.path.isdir(LOCAL):
    os.environ['GI_TYPELIB_PATH'] = (os.path.join(LOCAL, 'girepository-1.0') + ':'
                                     + os.environ.get('GI_TYPELIB_PATH', ''))
    if LOCAL not in os.environ.get('LD_LIBRARY_PATH', ''):
        os.environ['LD_LIBRARY_PATH'] = LOCAL + ':' + os.environ.get('LD_LIBRARY_PATH', '')
        os.execv(sys.executable, [sys.executable] + sys.argv)   # 重新載入以套用

import gi  # noqa: E402
gi.require_version('Aravis', '0.8')
from gi.repository import Aravis  # noqa: E402

# ClSafePowerStatus 代表 Camera Link 已連上的值
CL_LINK_OK = {'NonPoClLinkUp', 'NonPoClCameraOrCableDetected',
              'PoClLinkUp', 'PoClCameraAndCableDetected'}


def get_int(dev, name, default=None):
    try:
        return dev.get_integer_feature_value(name)
    except Exception:
        return default


def get_str(dev, name, default=None):
    try:
        return dev.get_string_feature_value(name)
    except Exception:
        return default


def set_int_checked(dev, name, val):
    """寫入並讀回驗證；不符就回 False（絕不假設寫入成功）。"""
    try:
        dev.set_integer_feature_value(name, val)
    except Exception as e:
        print(f'  寫 {name}={val} 失敗: {e}')
        return False
    got = get_int(dev, name)
    if got != val:
        print(f'  寫 {name}={val} 未生效（讀回 {got}）')
        return False
    print(f'  {name} = {val}')
    return True


def buffer_bytes(buf):
    for meth in ('get_data', 'get_image_data'):
        if hasattr(buf, meth):
            d = getattr(buf, meth)()
            if d:
                return bytes(d)
    raise RuntimeError('取不出 buffer 內容')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--frames', type=int, default=3)
    ap.add_argument('--height', type=int, default=None, help='每幀行數（line scan）')
    ap.add_argument('--packet-size', type=int, default=9000)
    ap.add_argument('--out', default='grab')
    ap.add_argument('--timeout', type=float, default=10.0)
    ap.add_argument('--force', action='store_true',
                    help='即使 Camera Link 狀態不正常也硬試')
    a = ap.parse_args()

    Aravis.update_device_list()
    n = Aravis.get_n_devices()
    print(f'=== 找到 {n} 台裝置 ===')
    for i in range(n):
        print(f'  [{i}] {Aravis.get_device_id(i)}')
    if n == 0:
        return 1

    cam = Aravis.Camera.new(None)
    dev = cam.get_device()
    print(f'\n供應商 {cam.get_vendor_name()}   型號 {cam.get_model_name()}')

    # ---- 先體檢，不合格就別動裝置 ----
    print('\n=== 狀態檢查 ===')
    cl = get_str(dev, 'ClSafePowerStatus')
    taps = get_str(dev, 'SensorDigitizationTaps')
    w = get_int(dev, 'Width')
    h = get_int(dev, 'Height')
    payload = get_int(dev, 'PayloadSize')
    pf = get_str(dev, 'PixelFormat')
    scps = get_int(dev, 'GevSCPSPacketSize')
    print(f'  ClSafePowerStatus  {cl}')
    print(f'  SensorDigitizationTaps {taps}')
    print(f'  Width x Height     {w} x {h}')
    print(f'  PixelFormat        {pf}')
    print(f'  PayloadSize        {payload}')
    print(f'  GevSCPSPacketSize  {scps}')

    problems = []
    if cl not in CL_LINK_OK:
        problems.append(f'Camera Link 未連上（ClSafePowerStatus={cl}）')
    if not w or not h:
        problems.append(f'Width/Height 無效（{w}x{h}）')
    if not payload:
        problems.append('PayloadSize = 0')

    if problems:
        print('\n=== 無法取像 ===')
        for p in problems:
            print(f'  - {p}')
        print('\n處理方式：')
        print('  1. 確認 L803K 自己的 12V 電源已開（L800 系列不是 PoCL 供電）')
        print('  2. 確認 MDR-26（相機端）↔ SDR-26（iPORT 端）線兩頭都插緊')
        print('  3. 將 iPORT 斷電再上電，讓 Camera Link 重新初始化')
        print('     （IP 已寫入持久設定，重開機仍是 192.168.5.10）')
        if not a.force:
            return 1
        print('\n--force 指定，繼續嘗試...')

    # ---- 設定 ----
    print('\n=== 設定 ===')
    if a.height and a.height != h:
        if not set_int_checked(dev, 'Height', a.height):
            return 1
        h = a.height
        payload = get_int(dev, 'PayloadSize')
    if scps != a.packet_size:
        if not set_int_checked(dev, 'GevSCPSPacketSize', a.packet_size):
            print('  封包大小維持原值繼續')
    payload = get_int(dev, 'PayloadSize') or payload
    print(f'  最終 {w}x{h}  payload {payload} bytes ({payload/1e6:.1f} MB)')

    stream = cam.create_stream(None, None)
    if stream is None:
        print('建立 stream 失敗')
        return 1
    for _ in range(max(4, a.frames + 1)):
        stream.push_buffer(Aravis.Buffer.new_allocate(payload))

    print(f'\n=== 開始取像（{a.frames} 幀）===')
    cam.set_acquisition_mode(Aravis.AcquisitionMode.CONTINUOUS)
    cam.start_acquisition()

    saved = []
    t_start = time.time()
    try:
        for i in range(a.frames):
            buf = stream.timeout_pop_buffer(int(a.timeout * 1e6))
            if buf is None:
                print(f'  第 {i+1} 幀：逾時 {a.timeout}s，沒收到資料')
                break
            st = buf.get_status()
            if st != Aravis.BufferStatus.SUCCESS:
                print(f'  第 {i+1} 幀：狀態 {st.value_nick}')
                stream.push_buffer(buf)
                continue
            data = buffer_bytes(buf)
            print(f'  第 {i+1} 幀：{len(data)} bytes  t={time.time()-t_start:.2f}s')
            saved.append(data)
            stream.push_buffer(buf)
        elapsed = time.time() - t_start
    finally:
        cam.stop_acquisition()

    n_done, n_fail, n_under = stream.get_statistics()
    print(f'\n串流統計：完成={n_done}  失敗={n_fail}  underrun={n_under}')
    if saved:
        mb = sum(len(d) for d in saved) / 1e6
        print(f'耗時 {elapsed:.2f}s，{len(saved)} 幀，{mb:.1f} MB'
              f'，平均 {mb/elapsed:.1f} MB/s ({mb*8/elapsed:.2f} Gb/s)')
    else:
        print('\n沒取到任何影像。')
        return 1

    print('\n=== 存檔與亮度統計 ===')
    from PIL import Image
    for i, data in enumerate(saved):
        with open(f'{a.out}_{i:02d}.raw', 'wb') as f:
            f.write(data)
        img = Image.frombytes('L', (w, h), data[:w * h])
        png = f'{a.out}_{i:02d}.png'
        img.save(png)
        pw = min(w, 1600)
        img.resize((pw, max(1, int(h * pw / w))), Image.NEAREST).save(
            f'{a.out}_{i:02d}_preview.png')

        hist = img.histogram()
        total = sum(hist)
        mean = sum(v * c for v, c in enumerate(hist)) / total
        nz = [v for v, c in enumerate(hist) if c]
        print(f'  {png}  {w}x{h}')
        print(f'      平均={mean:.1f}  最小={min(nz)}  最大={max(nz)}  '
              f'暗區(<10)={sum(hist[:10])/total*100:.1f}%')
    print(f'\n預覽圖：{a.out}_00_preview.png')
    return 0


if __name__ == '__main__':
    sys.exit(main())
