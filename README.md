# uexcorp

Star Citizen 工具集整合仓库：交易 skill、共享数据库 MCP、游戏内终端价格采集工具。

> **克隆提示**：本仓库包含 git 子模块，请使用
> `git clone --recurse-submodules <url>` 克隆；已有仓库执行
> `git submodule update --init --recursive`。

## 仓库结构

```
uexcorp/
├── skills/
│   └── uexcorp-sc/          # UEXcorp 交易顾问 skill（vendor，MIT）
├── mcp/
│   └── scdb-mcp/            # SC 共享数据库 MCP server（子模块，GPL-3.0）
├── tools/
│   └── kioskprobe/          # 游戏终端截图价格采集工具（子模块）
├── .mcp.json                # Claude Code 项目级 MCP 配置（scdb）
└── README.md
```

## 组件说明

### 1. `skills/uexcorp-sc` — UEXcorp 交易顾问 Skill

来源：[RikyZ90/openclaw-skill-uexcorp-sc](https://github.com/RikyZ90/openclaw-skill-uexcorp-sc)（MIT）

纯文档型 skill，让 agent 成为星际公民交易顾问：实时商品价格查询、贸易路线优化、
采矿数据、市场挂牌（含图像识别上架）。数据源为 [UEXcorp API 2.0](https://uexcorp.space/api/documentation/)。

**安装**（本机已通过符号链接装好，仓库更新即自动生效）：

```bash
ln -sfn "$(pwd)/skills/uexcorp-sc" ~/.claude/skills/uexcorp-sc    # Claude Code
ln -sfn "$(pwd)/skills/uexcorp-sc" ~/.openclaw/skills/uexcorp-sc  # OpenClaw
```

**配置**：需 UEXcorp API token，在 [uexcorp.space/api/apps](https://uexcorp.space/api/apps) 申请，
OpenClaw 侧写入 `uexcorp.apiToken` 配置项。限速 10 请求/分钟、14,400/天。

### 2. `mcp/scdb-mcp` — SC 共享数据库 MCP Server

来源：[scfools/scdb-mcp](https://github.com/scfools/scdb-mcp)（GPL-3.0）

连接 Star Citizen 社区共享的 SpacetimeDB 实例，提供数据查询、公式查找、
库存报告、本地/远程比对、协作消息等 10 个工具。要求 Node.js >= 20。

**安装**（`.mcp.json` 已配置，进入本仓库目录即自动生效）：

```bash
# 配套 skills 安装到 ./.claude/skills/（首次使用需执行）
npx @scfools/scdb-mcp install-skills
```

**首次使用**：Claude 会运行 `scdb-onboarding` skill 完成身份生成与鉴权，
推送类工具（import_to_scdb / post_message）需要现有协作者授权。

环境变量均有默认值，无需配置；详见 [scdb-mcp README](mcp/scdb-mcp/README.md)。

### 3. `tools/kioskprobe` — 游戏终端价格采集工具

来源：[dolejska-daniel/starcitizen-kioskprobe](https://github.com/dolejska-daniel/starcitizen-kioskprobe)

从游戏内交易终端截图提取商品价格（OpenAI 视觉模型），校验后上传 UEX Corp。
要求 Python 3.10+。

**安装**（本机已装好 venv）：

```bash
cd tools/kioskprobe
python3.12 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python main.py
```

**配置**：复制 `config/openai.sample.json` → `openai.json` 填入 OpenAI API key，
复制 `config/uex.sample.json` → `uex.json` 填入 UEX Corp 应用 token 与用户 token。
单张截图约 $0.0039 OpenAI 费用。

## 许可证说明

| 组件 | 许可证 | 整合方式 |
|------|--------|----------|
| uexcorp-sc | MIT（上游 README 声明） | vendor 拷贝 |
| scdb-mcp | GPL-3.0 | git 子模块引用 |
| kioskprobe | 未声明 | git 子模块引用（不复制源码） |

子模块仅引用上游仓库，不含源码拷贝；各组件遵循其原始许可证。
