using System;
using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;
using CfAoiControl.Models;
using Microsoft.Extensions.Configuration;

namespace CfAoiControl.Services;

/// <summary>讀/寫 appsettings.json ↔ SystemConfigModel（不變式 5：無 hardcode 位址）。</summary>
public static class ConfigLoader
{
    /// <summary>解析 appsettings.json 路徑：優先 BaseDirectory，退回 CWD（dotnet run）。</summary>
    public static string ResolvePath(string? basePath = null)
    {
        basePath ??= AppContext.BaseDirectory;
        var path = Path.Combine(basePath, "appsettings.json");
        if (!File.Exists(path))
        {
            var alt = Path.Combine(Directory.GetCurrentDirectory(), "appsettings.json");
            if (File.Exists(alt)) path = alt;
        }
        return path;
    }

    public static SystemConfigModel Load(string? basePath = null)
    {
        var cfg = new ConfigurationBuilder()
            .AddJsonFile(ResolvePath(basePath), optional: true)
            .Build();

        var model = new SystemConfigModel();
        cfg.Bind(model);
        return model;
    }

    /// <summary>把 ShareSetting 區塊寫回 appsettings.json（只改該節點，保留其餘設定）。</summary>
    public static void SaveShareSetting(ShareSettingModel share, string? basePath = null)
    {
        var path = ResolvePath(basePath);
        JsonObject root;
        try
        {
            root = (File.Exists(path) ? JsonNode.Parse(File.ReadAllText(path)) as JsonObject : null)
                   ?? new JsonObject();
        }
        catch { root = new JsonObject(); }

        // 以 ShareSettingModel 序列化結果覆寫 "ShareSetting" 節點（屬性名 = 鍵）
        var json = JsonSerializer.Serialize(share);
        root["ShareSetting"] = JsonNode.Parse(json);

        File.WriteAllText(path, root.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));
    }
}
