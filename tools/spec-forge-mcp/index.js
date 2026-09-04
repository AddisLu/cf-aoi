#!/usr/bin/env node
// spec-forge MCP server：產生/驗證/入列夜間 auto-coding 的執行級 spec。
// stdio transport，Claude Code 與 VS Code GitHub Copilot 皆可掛載。
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const TEMPLATE_DIR = path.join(HERE, "templates");
// spec 存放根目錄：預設為「啟動時工作目錄」下的 .spec-forge（host 通常以 workspace 為 cwd）
const ROOT = process.env.SPEC_FORGE_ROOT || path.join(process.cwd(), ".spec-forge");
const QUEUE_DIR = path.join(ROOT, "queue");

const DOMAINS = ["cuda", "cpp", "csharp", "python", "docs"];

function readTemplate(name) {
  const p = path.join(TEMPLATE_DIR, name);
  return fs.existsSync(p) ? fs.readFileSync(p, "utf8") : null;
}

function text(s) {
  return { content: [{ type: "text", text: s }] };
}

// ---- spec 驗證：回傳 { errors, warnings } ----
function validateSpec(content) {
  const errors = [];
  const warnings = [];
  const lines = content.split(/\r?\n/);

  if (!/^#\s*TASK-\S+:/m.test(content)) {
    errors.push("缺少標題：第一行需為「# TASK-<id>: <一句話目標>」");
  }
  const domainMatch = content.match(/^domain:\s*(\S+)/m);
  if (!domainMatch) {
    errors.push("缺少「domain:」欄位");
  } else if (!DOMAINS.includes(domainMatch[1])) {
    errors.push(`domain「${domainMatch[1]}」不合法，需為：${DOMAINS.join(" | ")}`);
  }
  if (!/^max_attempts:\s*\d+/m.test(content)) {
    warnings.push("缺少「max_attempts:」，worker 將使用預設值 3");
  }

  const requiredSections = ["目標", "改動範圍", "需求", "驗收命令", "完成定義"];
  for (const s of requiredSections) {
    if (!new RegExp(`^##\\s*${s}`, "m").test(content)) {
      errors.push(`缺少必要章節：「## ${s}」`);
    }
  }

  if (/^##\s*改動範圍/m.test(content) && !/禁止/.test(content)) {
    errors.push("「改動範圍」需包含「禁止：」清單（弱模型必須有明確的不可為）");
  }

  // 需求需為 EARS 格式：每條含 WHEN 與 SHALL
  const reqSection = content.split(/^##\s*需求.*$/m)[1]?.split(/^##\s/m)[0] || "";
  const reqLines = reqSection.split("\n").filter((l) => /^\s*-\s*R\d+/.test(l));
  if (reqLines.length === 0) {
    errors.push("「需求」章節沒有任何 R 編號條目（- R1: ...）");
  } else {
    for (const l of reqLines) {
      if (!/WHEN/.test(l) || !/SHALL/.test(l)) {
        errors.push(`需求非 EARS 格式（需含 WHEN 與 SHALL）：${l.trim()}`);
      }
    }
  }

  // 驗收命令需至少一條編號命令
  const accSection = content.split(/^##\s*驗收命令.*$/m)[1]?.split(/^##\s/m)[0] || "";
  const accLines = accSection.split("\n").filter((l) => /^\s*\d+\.\s/.test(l));
  if (accLines.length === 0) {
    errors.push("「驗收命令」章節沒有任何編號命令（1. <命令>）");
  }
  if (domainMatch?.[1] === "cuda" && !/ncu|nsight|bench/i.test(accSection)) {
    warnings.push("CUDA 任務的驗收命令建議包含 ncu/benchmark 效能比較");
  }

  // 佔位符未填
  const placeholders = lines.filter((l) => /<[^>]*一句話|<id>|<條件>|<命令>|<檔案路徑>/.test(l));
  if (placeholders.length > 0) {
    errors.push(`仍有 ${placeholders.length} 處範本佔位符未填（<...>）`);
  }

  return { errors, warnings };
}

function specWithConstitution(domain) {
  const tpl = readTemplate("task-spec.md");
  const cons = readTemplate(`constitution-${domain}.md`);
  return { tpl, cons };
}

const server = new McpServer({ name: "spec-forge", version: "0.1.0" });

server.registerTool(
  "get_template",
  {
    description:
      "取得執行級 task spec 範本；帶 domain 時一併附上該 domain 的 constitution（house rules）。撰寫 spec 前必先呼叫。",
    inputSchema: { domain: z.enum(DOMAINS).optional().describe("任務主要語言/領域") },
  },
  async ({ domain }) => {
    const { tpl, cons } = specWithConstitution(domain ?? "");
    let out = `## Task Spec 範本\n\n\`\`\`markdown\n${tpl}\`\`\`\n`;
    if (domain) {
      out += cons
        ? `\n## Constitution（${domain}，spec 不需重複其內容，執行時會自動附上）\n\n${cons}`
        : `\n（domain「${domain}」尚無 constitution 檔，執行時只套用 spec 本身）`;
    }
    out +=
      "\n\n## 撰寫要求\n1. 先用 codebase 分析（graph/grep）釘死「改動範圍」白名單。\n2. 每條 R 需求必須對應至少一個驗收命令。\n3. 寫完必須呼叫 validate_spec，全過後用 save_spec 入列。";
    return text(out);
  }
);

server.registerTool(
  "get_constitution",
  {
    description: "取得指定 domain 的 constitution（不隨任務變的 house rules）。",
    inputSchema: { domain: z.enum(DOMAINS).describe("語言/領域") },
  },
  async ({ domain }) => {
    const cons = readTemplate(`constitution-${domain}.md`);
    return text(cons ?? `domain「${domain}」尚無 constitution 檔（可於 templates/ 新增 constitution-${domain}.md）`);
  }
);

server.registerTool(
  "validate_spec",
  {
    description:
      "驗證一份 task spec 是否符合執行級格式（必要章節、EARS 需求、驗收命令、禁止清單、無佔位符）。save_spec 前必先通過。",
    inputSchema: { content: z.string().describe("spec 的完整 markdown 內容") },
  },
  async ({ content }) => {
    const { errors, warnings } = validateSpec(content);
    if (errors.length === 0) {
      return text(
        `✅ 通過驗證${warnings.length ? `（${warnings.length} 個警告）` : ""}\n` +
          warnings.map((w) => `- ⚠️ ${w}`).join("\n")
      );
    }
    return text(
      `❌ 未通過（${errors.length} 個錯誤 / ${warnings.length} 個警告）\n` +
        errors.map((e) => `- ❌ ${e}`).join("\n") +
        (warnings.length ? "\n" + warnings.map((w) => `- ⚠️ ${w}`).join("\n") : "")
    );
  }
);

server.registerTool(
  "save_spec",
  {
    description:
      "將通過驗證的 spec 寫入佇列（.spec-forge/queue/TASK-<id>.md）供夜間 worker 取用。驗證失敗會拒絕寫入。",
    inputSchema: {
      id: z
        .string()
        .regex(/^[A-Za-z0-9._-]+$/, "id 僅允許英數與 . _ -")
        .describe("任務 id（檔名用），例如 20260904-fix-roi-clip"),
      content: z.string().describe("spec 的完整 markdown 內容"),
      overwrite: z.boolean().optional().describe("同 id 已存在時是否覆寫（預設否）"),
    },
  },
  async ({ id, content, overwrite }) => {
    const { errors, warnings } = validateSpec(content);
    if (errors.length > 0) {
      return text(`拒絕入列：spec 未通過驗證\n` + errors.map((e) => `- ❌ ${e}`).join("\n"));
    }
    fs.mkdirSync(QUEUE_DIR, { recursive: true });
    const file = path.join(QUEUE_DIR, `TASK-${id}.md`);
    if (fs.existsSync(file) && !overwrite) {
      return text(`拒絕入列：${file} 已存在（要覆寫請帶 overwrite: true）`);
    }
    fs.writeFileSync(file, content, "utf8");
    return text(
      `✅ 已入列：${file}` + (warnings.length ? "\n" + warnings.map((w) => `- ⚠️ ${w}`).join("\n") : "")
    );
  }
);

server.registerTool(
  "list_specs",
  {
    description: "列出佇列中的 task spec（id、domain、標題）。",
    inputSchema: {},
  },
  async () => {
    if (!fs.existsSync(QUEUE_DIR)) return text(`佇列為空（${QUEUE_DIR} 不存在）`);
    const files = fs.readdirSync(QUEUE_DIR).filter((f) => f.endsWith(".md")).sort();
    if (files.length === 0) return text("佇列為空");
    const rows = files.map((f) => {
      const c = fs.readFileSync(path.join(QUEUE_DIR, f), "utf8");
      const title = c.match(/^#\s*(TASK-\S+:.*)$/m)?.[1] ?? "(無標題)";
      const domain = c.match(/^domain:\s*(\S+)/m)?.[1] ?? "?";
      return `- ${f}  [${domain}]  ${title}`;
    });
    return text(`佇列（${QUEUE_DIR}）共 ${files.length} 筆：\n${rows.join("\n")}`);
  }
);

// MCP prompt：在 Claude Code 顯示為 /mcp__spec-forge__create_spec，
// 在 VS Code Copilot 顯示為 /mcp.spec-forge.create_spec。
server.registerPrompt(
  "create_spec",
  {
    description: "引導產生一份夜間 auto-coding 執行級 spec 並入列",
    argsSchema: { requirement: z.string().describe("原始需求（一句話即可）") },
  },
  ({ requirement }) => ({
    messages: [
      {
        role: "user",
        content: {
          type: "text",
          text: [
            `請把以下需求轉成「執行級 task spec」（給夜間本地弱模型無人值守執行）：`,
            ``,
            `需求：${requirement}`,
            ``,
            `步驟：`,
            `1. 呼叫 spec-forge 的 get_template（帶正確 domain）取得範本與 constitution。`,
            `2. 分析 codebase（優先用 codebase graph / 其次 grep），釘死「改動範圍」白名單與禁止清單——執行模型不會自己探索，範圍必須完整。`,
            `3. 需求含糊處先向我提問澄清，不要自行腦補。`,
            `4. 需求一律寫成 EARS 格式（WHEN ... SHALL ...），每條對應至少一個可執行的驗收命令；CUDA 任務需含 ncu 相對比較、影像管線任務需含 golden set 檢出率比對。`,
            `5. 寫完呼叫 validate_spec；有錯誤就修到全過。`,
            `6. 通過後呼叫 save_spec 入列，並回報佇列檔案路徑。`,
          ].join("\n"),
        },
      },
    ],
  })
);

const transport = new StdioServerTransport();
await server.connect(transport);
