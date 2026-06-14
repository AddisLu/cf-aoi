using System;
using System.Collections.Generic;

namespace CfAoiControl.Services;

/// <summary>
/// 用 FFT 從影像灰階陣列估算 LCD 子像素週期（PitchX/PitchY）。
/// 移植自 scripts/estimate_pitch.py：取中間 1/3 ROI，對多 row/col 做 FFT 找主頻。
/// 純 managed（自寫 radix-2 FFT），無原生依賴 → Mac/Linux/Windows 共用。
/// </summary>
public static class PitchEstimator
{
    public sealed record Result(
        bool OkX, int PitchX, string ConfX, double SnrX,
        bool OkY, int PitchY, string ConfY, double SnrY);

    public static Result Estimate(byte[] px, int w, int h)
    {
        int y0 = h / 3, y1 = 2 * h / 3, x0 = w / 3, x1 = 2 * w / 3;
        int roiH = y1 - y0, roiW = x1 - x0;
        if (roiW < 16 || roiH < 16)
            return new Result(false, 26, "低", 0, false, 19, "低", 0);

        // 水平（PitchX）：取約 10 條 row
        var px_list = new List<double>(); var snrx = new List<double>();
        int rowStep = Math.Max(1, roiH / 10);
        var rowBuf = new double[roiW];
        for (int r = 0; r < roiH; r += rowStep)
        {
            int baseIdx = (y0 + r) * w + x0;
            for (int x = 0; x < roiW; x++) rowBuf[x] = px[baseIdx + x];
            if (FindPitch(rowBuf, out double p, out double s) && p > 4 && p < 150) { px_list.Add(p); snrx.Add(s); }
        }

        // 垂直（PitchY）：取約 10 條 col
        var py_list = new List<double>(); var snry = new List<double>();
        int colStep = Math.Max(1, roiW / 10);
        var colBuf = new double[roiH];
        for (int c = 0; c < roiW; c += colStep)
        {
            int xCol = x0 + c;
            for (int y = 0; y < roiH; y++) colBuf[y] = px[(y0 + y) * w + xCol];
            if (FindPitch(colBuf, out double p, out double s) && p > 4 && p < 150) { py_list.Add(p); snry.Add(s); }
        }

        var (okX, pitchX, confX, sX) = Summarize(px_list, snrx, 26);
        var (okY, pitchY, confY, sY) = Summarize(py_list, snry, 19);
        return new Result(okX, pitchX, confX, sX, okY, pitchY, confY, sY);
    }

    private static (bool ok, int pitch, string conf, double snr) Summarize(
        List<double> pitches, List<double> snrs, int fallback)
    {
        if (pitches.Count == 0) return (false, fallback, "低", 0);
        pitches.Sort();
        int pitch = (int)Math.Round(pitches[pitches.Count / 2]);   // median
        double snr = 0; foreach (var s in snrs) snr += s; snr /= snrs.Count;
        string conf = snr > 10 ? "高" : snr > 5 ? "中" : "低";
        return (true, pitch, conf, snr);
    }

    // 對單一訊號 FFT，回主頻對應週期(pixel) + SNR。
    private static bool FindPitch(double[] signal, out double pitch, out double snr)
    {
        pitch = 0; snr = 0;
        int L = signal.Length;
        int P = NextPow2(L);
        var re = new double[P]; var im = new double[P];
        Array.Copy(signal, re, L);   // 其餘補 0

        Fft(re, im);

        int half = P / 2;
        var mag = new double[half + 1];
        for (int k = 0; k <= half; k++) mag[k] = Math.Sqrt(re[k] * re[k] + im[k] * im[k]);

        int minIdx = Math.Max(2, P / 200);   // 忽略 DC 與極低頻
        for (int k = 0; k < minIdx && k <= half; k++) mag[k] = 0;

        int peak = minIdx;
        for (int k = minIdx; k <= half; k++) if (mag[k] > mag[peak]) peak = k;
        if (peak <= 0 || mag[peak] <= 0) return false;

        double mean = 0; for (int k = 0; k <= half; k++) mean += mag[k]; mean /= (half + 1);
        pitch = (double)P / peak;            // 週期(pixel) = 總長 / 峰值頻率索引
        snr = mag[peak] / (mean + 1e-9);
        return true;
    }

    private static int NextPow2(int n)
    {
        int p = 1; while (p < n) p <<= 1; return p;
    }

    // 迭代 radix-2 Cooley-Tukey（in-place，n 必為 2 的冪）
    private static void Fft(double[] re, double[] im)
    {
        int n = re.Length;
        for (int i = 1, j = 0; i < n; i++)
        {
            int bit = n >> 1;
            for (; (j & bit) != 0; bit >>= 1) j ^= bit;
            j ^= bit;
            if (i < j) { (re[i], re[j]) = (re[j], re[i]); (im[i], im[j]) = (im[j], im[i]); }
        }
        for (int len = 2; len <= n; len <<= 1)
        {
            double ang = -2.0 * Math.PI / len;
            double wr = Math.Cos(ang), wi = Math.Sin(ang);
            for (int i = 0; i < n; i += len)
            {
                double cwr = 1, cwi = 0;
                for (int k = 0; k < len / 2; k++)
                {
                    int a = i + k, b = a + len / 2;
                    double tr = cwr * re[b] - cwi * im[b];
                    double ti = cwr * im[b] + cwi * re[b];
                    re[b] = re[a] - tr; im[b] = im[a] - ti;
                    re[a] += tr; im[a] += ti;
                    double ncwr = cwr * wr - cwi * wi;
                    cwi = cwr * wi + cwi * wr; cwr = ncwr;
                }
            }
        }
    }
}
