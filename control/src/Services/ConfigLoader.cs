using System;
using System.IO;
using CfAoiControl.Models;
using Microsoft.Extensions.Configuration;

namespace CfAoiControl.Services;

/// <summary>讀 appsettings.json → SystemConfigModel（不變式 5：無 hardcode 位址）。</summary>
public static class ConfigLoader
{
    public static SystemConfigModel Load(string? basePath = null)
    {
        basePath ??= AppContext.BaseDirectory;
        var path = Path.Combine(basePath, "appsettings.json");
        if (!File.Exists(path))
        {
            // 退回專案目錄（dotnet run 時 BaseDirectory 為 bin/...）
            var alt = Path.Combine(Directory.GetCurrentDirectory(), "appsettings.json");
            if (File.Exists(alt)) path = alt;
        }

        var cfg = new ConfigurationBuilder()
            .AddJsonFile(path, optional: true)
            .Build();

        var model = new SystemConfigModel();
        cfg.Bind(model);
        return model;
    }
}
