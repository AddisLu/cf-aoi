# spec-forge MCP

產生/驗證/入列「夜間 auto-coding 執行級 spec」的 MCP server（stdio）。
給白天的高階模型（Claude / Copilot）用：套範本寫 spec → 機器驗證格式 → 存入佇列，
供夜間 DGX Spark worker（本地 vLLM agent）無人值守執行。

## 安裝

```bash
cd tools/spec-forge-mcp && npm install
```

## 掛載

### Claude Code（desktop / CLI）
repo 根目錄已有 `.mcp.json`（project scope，全隊共用），開啟本專案即自動載入。
若要掛到使用者層級：

```bash
claude mcp add spec-forge --scope user -- node /絕對路徑/tools/spec-forge-mcp/index.js
```

### VS Code（GitHub Copilot agent mode）
`.vscode/mcp.json` 已就緒，開啟 workspace 後在 MCP 面板 Start 即可。

## 提供的能力

| 類型 | 名稱 | 用途 |
|------|------|------|
| prompt | `create_spec` | 一鍵引導：需求 → 範本 → codebase 分析 → EARS 需求 → 驗證 → 入列。Claude Code 中為 `/mcp__spec-forge__create_spec`，Copilot 中為 `/mcp.spec-forge.create_spec` |
| tool | `get_template` | 取 task spec 範本（帶 domain 附 constitution） |
| tool | `get_constitution` | 取某 domain 的 house rules |
| tool | `validate_spec` | 檢查格式：必要章節、EARS（WHEN/SHALL）、驗收命令、禁止清單、無佔位符 |
| tool | `save_spec` | 驗證通過才寫入 `.spec-forge/queue/TASK-<id>.md` |
| tool | `list_specs` | 列佇列 |

## 設定

- `SPEC_FORGE_ROOT`：spec 存放根目錄，預設為工作目錄下 `.spec-forge/`。
- 範本與各 domain constitution 在 `templates/`，直接改 markdown 即可，不用重啟。

## 與 Loop 的銜接（後續）

夜間 worker 從 `.spec-forge/queue/` 取 spec（或由 loop-engineering 的
`loop_queue_task` 轉發），執行時將 spec + 對應 constitution 一併餵給本地 agent。
