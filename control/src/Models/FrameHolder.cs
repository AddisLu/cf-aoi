using System.Threading;
using CfAoiControl.Models;

namespace CfAoiControl.Models;

/// <summary>
/// 持有 Control 端當前 frame 的 8-bit 灰階 bytes（Step 1 offline 模式下，Control 本地載入 MIL/TIFF 影像）。
/// CF_CHECK_ALIGN 到達時，從此取 frame bytes 裁切搜尋 ROI 後傳 IP（釘點 4：傳 ROI 非全張）。
/// </summary>
public sealed class FrameHolder
{
    private byte[]? _frameBytes;
    private int _width;
    private int _height;
    private readonly object _lock = new();

    public bool HasFrame { get { lock (_lock) return _frameBytes is not null; } }

    public void Set(byte[] frameBytes, int width, int height)
    {
        lock (_lock) { _frameBytes = frameBytes; _width = width; _height = height; }
    }

    public void Clear()
    {
        lock (_lock) { _frameBytes = null; _width = 0; _height = 0; }
    }

    /// <summary>裁切搜尋 ROI（釘點 4）：以 ReferX/ReferY 為中心裁 SearchWidth×SearchHeight。
    /// 回傳裁切的 Mono8 bytes，或 null（無 frame 或 ROI 越界）。</summary>
    public byte[]? CropSearchRoi(AlignRoiModel alignRoi)
    {
        lock (_lock)
        {
            if (_frameBytes is null) return null;
            int rx = alignRoi.ReferX, ry = alignRoi.ReferY;
            int sw = alignRoi.SearchWidth, sh = alignRoi.SearchHeight;
            if (rx < 0 || ry < 0 || sw <= 0 || sh <= 0) return null;

            int x0 = rx - sw / 2;
            int y0 = ry - sh / 2;
            // 若 ROI 超出影像邊界，回 null（由 CHECK_ALIGN 失敗路徑處理）
            if (x0 < 0 || y0 < 0 || x0 + sw > _width || y0 + sh > _height) return null;

            var roi = new byte[sw * sh];
            for (int row = 0; row < sh; row++)
                System.Buffer.BlockCopy(_frameBytes, (y0 + row) * _width + x0, roi, row * sw, sw);
            return roi;
        }
    }
}
