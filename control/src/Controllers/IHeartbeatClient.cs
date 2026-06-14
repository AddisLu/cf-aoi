using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace CfAoiControl.Controllers;

/// <summary>
/// 可做心跳偵測的 TCP 客戶端（IP / Grab 共用）。ConnectionManager 用此介面統一偵測存活與重連。
/// </summary>
public interface IHeartbeatClient
{
    bool IsConnected { get; }                 // socket 認為已連線（不保證對方還活著）
    bool IsBusy { get; }                      // 有命令進行中（視為存活，跳過本回合心跳）
    string Host { get; }
    int Port { get; }
    Task ConnectAsync(string host, int port, CancellationToken ct = default);
    Task<JsonNode?> CheckHealthAsync(CancellationToken ct = default);
    void Disconnect();                        // 關閉 socket（保留可再 ConnectAsync）
}
