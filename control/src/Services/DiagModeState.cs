namespace CfAoiControl.Services;

/// <summary>
/// 機況助手（診斷模式）狀態：Spark 上 LoopEngineering 在跑或大模型佔著記憶體時為 true。
/// 運作模式（2026-10-05 定案）：生產（run 貨）只跑 Control / Grab / IP；機台有問題或調機時才開 Loop + 大模型。
/// 大模型佔 Spark 約 80% 統一記憶體（實測與 IP 生產並存只剩約 5GB）→ 診斷模式中 CF_READY 回未就緒，
/// 上位機不會在這狀態下送料。由「系統狀態」頁每 5 秒從節點代理更新（NodeAgentsViewModel）。
/// </summary>
public sealed class DiagModeState
{
    private volatile bool _active;
    private volatile string _reason = "";

    public bool Active => _active;
    public string Reason => _reason;

    public void Set(bool active, string reason)
    {
        _reason = reason;
        _active = active;
    }
}
