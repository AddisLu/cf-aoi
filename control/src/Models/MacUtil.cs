using System;
using System.Text;

namespace CfAoiControl.Models;

/// <summary>
/// MAC 正規化。⚠️ 行為必須與 grab 端 <c>CamManager::normalize_mac</c>
/// （grab/src/cam_manager.cpp:15-24）**完全一致**：
/// 去除 ':'、'-'、'.'、空白後轉大寫。
///
/// 為什麼要有這個：array_topology.json 的 expected_mac 由人手填，
/// LIST_CAMERAS 回的 mac 由 pylon 產生，兩邊格式不保證相同
/// （"00:30:53:2A:0B:03" vs "003053 2a-0b03" 是同一台）。
/// 不正規化就比對 = 每台都判成「MAC 不符」的假告警。
/// </summary>
public static class MacUtil
{
    public static string Normalize(string? mac)
    {
        if (string.IsNullOrEmpty(mac)) return "";
        var sb = new StringBuilder(12);
        foreach (var c in mac)
        {
            if (c is ':' or '-' or '.' || char.IsWhiteSpace(c)) continue;
            sb.Append(char.ToUpperInvariant(c));
        }
        return sb.ToString();
    }

    /// <summary>兩個 MAC 是否為同一台（正規化後比對）。任一為空 → false（空不算相符）。</summary>
    public static bool Same(string? a, string? b)
    {
        var na = Normalize(a);
        var nb = Normalize(b);
        return na.Length > 0 && na == nb;
    }
}
