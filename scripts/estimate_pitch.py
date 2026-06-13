#!/usr/bin/env python3
"""
scripts/estimate_pitch.py
用 FFT 從 MIL 影像估算 LCD 子像素週期（PitchX / PitchY）
這是配方設定中最重要的參數，必須從實際影像量測

用法：python3 scripts/estimate_pitch.py /path/to/frame.tif
"""
import sys, numpy as np
from pathlib import Path

def estimate_pitch(image_path: str):
    try:
        from PIL import Image
    except ImportError:
        print("安裝 Pillow：pip3 install Pillow")
        sys.exit(1)

    img = np.array(Image.open(image_path).convert('L'))
    h, w = img.shape
    print(f"影像尺寸：{w} × {h} 像素")

    # 取中間 1/3 的區域做 FFT（避免邊緣影響）
    y_start, y_end = h//3, 2*h//3
    x_start, x_end = w//3, 2*w//3
    roi = img[y_start:y_end, x_start:x_end]

    def find_pitch(signal):
        n    = len(signal)
        fft  = np.abs(np.fft.rfft(signal.astype(float)))
        freq = np.fft.rfftfreq(n)
        # 忽略 DC 和極低頻（< 1/200 像素^-1）
        min_freq_idx = max(2, int(n / 200))
        # 找最強峰值
        fft[:min_freq_idx] = 0
        peak_idx = np.argmax(fft)
        if freq[peak_idx] == 0:
            return None
        pitch = 1.0 / freq[peak_idx]
        # 計算峰值信噪比（判斷可信度）
        snr = fft[peak_idx] / (np.mean(fft) + 1e-9)
        return pitch, snr

    # 水平方向（PitchX）：取多行平均
    pitches_x, snrs_x = [], []
    for row_idx in range(0, roi.shape[0], roi.shape[0]//10):
        result = find_pitch(roi[row_idx])
        if result:
            p, s = result
            if 4 < p < 150:  # 合理範圍
                pitches_x.append(p)
                snrs_x.append(s)

    # 垂直方向（PitchY）：取多列平均
    pitches_y, snrs_y = [], []
    for col_idx in range(0, roi.shape[1], roi.shape[1]//10):
        result = find_pitch(roi[:, col_idx])
        if result:
            p, s = result
            if 4 < p < 150:
                pitches_y.append(p)
                snrs_y.append(s)

    print()
    if pitches_x:
        px = round(np.median(pitches_x))
        snr_x = np.mean(snrs_x)
        confidence_x = "高" if snr_x > 10 else "中" if snr_x > 5 else "低"
        print(f"PitchX 估算：{px} px  (可信度：{confidence_x}，SNR={snr_x:.1f})")
    else:
        px = 26
        print(f"PitchX：無法估算，使用預設值 {px} px")

    if pitches_y:
        py = round(np.median(pitches_y))
        snr_y = np.mean(snrs_y)
        confidence_y = "高" if snr_y > 10 else "中" if snr_y > 5 else "低"
        print(f"PitchY 估算：{py} px  (可信度：{confidence_y}，SNR={snr_y:.1f})")
    else:
        py = 20
        print(f"PitchY：無法估算，使用預設值 {py} px")

    print()
    print("=" * 50)
    print(f"建議在 Control 的配方設定中輸入：")
    print(f"  PitchX = {px}")
    print(f"  PitchY = {py}")
    print()
    print("⚠️  這只是估算值，請目視確認缺陷檢測結果後微調")
    print("   BTH/DTH 建議先用預設值 1.30/0.75，再根據誤報率調整")
    print("=" * 50)

    return px, py

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"用法：python3 {sys.argv[0]} /path/to/image.tif")
        sys.exit(1)
    estimate_pitch(sys.argv[1])
