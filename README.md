# uexcorp · Star Citizen 工具集

![License](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Windows-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-informational)

整合 Star Citizen 跑商与数据工具的仓库：**自研交易 skill（sc-trader）** + 三个上游
工具组件（git 子模块引用）+ 一个上游交易顾问 skill（vendor）。核心能力：

- **跑商路线推荐**：船型感知（舱位/槽位/箱体适配）、预算约束、部分装载、组合去重
- **价格监控闭环**：UEX API 轮询 + 用户实测入库（★ 最高权威）→ 收益榜 → 调仓警告
- **本地算路引擎**：一次全量价格本地配对（与 sc-trade.tools 同方法），覆盖精炼矿物、
  饱和下架、跨行星货源

> 数据说明：所有价格为社区来源（UEXcorp），与游戏内实价存在延迟；本仓库的价格权威序为
> **用户实测 ★ > UEX > sc-trade.tools**，同一数据点按观察时间新鲜度仲裁。

## 快速开始

```bash
# 1. 克隆（含子模块）
git clone --recurse-submodules https://github.com/printfalling/sc-trader.git
cd sc-trader

# 2. 环境自检（自动创建 venv、装依赖、装 tesseract、部署 skill 链接）
tools/uexinfo/venv/bin/python skills/sc-trader/scripts/check.py
#   Windows: tools\uexinfo\venv\Scripts\python.exe（venv 尚未创建时先跑
#            py -3.12 -m venv tools/uexinfo/venv 并 pip install -e tools/uexinfo，
#            或直接用 check 的 --fix 全自动流程）

# 3. 推荐路线
tools/uexinfo/venv/bin/python skills/sc-trader/scripts/recommend.py --budget 100000

# 4. 跑商回来更新价格（收益榜 + 调仓建议）
tools/uexinfo/venv/bin/python skills/sc-trader/scripts/update_prices.py
```

首次使用建议在 Claude Code 会话中进行——skill 部署后可直接对话完成全流程。

## 组件总览

| 组件 | 位置 | 许可证 | 整合方式 | 说明 |
|------|------|--------|----------|------|
| **sc-trader** | `skills/sc-trader/` | MIT（本仓库） | 自研 | 跑商推荐/价格更新/实测入库/自检/自更新 |
| uexcorp-sc | `skills/uexcorp-sc/` | MIT | vendor | 上游 UEXcorp 交易顾问 skill（[RikyZ90](https://github.com/RikyZ90/openclaw-skill-uexcorp-sc)） |
| scdb-mcp | `mcp/scdb-mcp/` | GPL-3.0 | 子模块 | SC 共享数据库 MCP（[scfools](https://github.com/scfools/scdb-mcp)） |
| kioskprobe | `tools/kioskprobe/` | 未声明 | 子模块 | 游戏终端截图价格采集（[dolejska-daniel](https://github.com/dolejska-daniel/starcitizen-kioskprobe)） |
| uexinfo | `tools/uexinfo/` | MIT | 子模块 | 游戏内 overlay（[ZolOnTheNet](https://github.com/ZolOnTheNet/uexinfo)），同时作为 sc-trader 的依赖库 |

**子模块仅引用上游、不复制源码**；各组件遵循其原始许可证。`sc-trader` 运行时复用
`uexinfo` 的 UEX 客户端、静态缓存、航路图与玩家扫描价存储。

## sc-trader 详解

### 两步工作流

```
① recommend 检索候选（组合 + 去重 + 时间模型）
     ↓ 你跑商
② update_prices 实价更新 + record 实测入库（★）
     ↓ 收益榜 / 调仓警告
     └→ 回到 ① 或按榜继续跑
```

### 五个命令

| 命令 | 脚本 | 用途 |
|------|------|------|
| `recommend` | `scripts/recommend.py` | 路线推荐：`--ship` 选船、`--budget` 选预算；输出首选/备选组合 |
| `update_prices` | `scripts/update_prices.py` | 单次价格更新：变化报告 + 收益榜（圈利润/时薪）+ 首选易主警告 |
| `record` | `scripts/record.py` | 用户实测入库（截图读数或口述，**必须带观察时间** `--observed-at`） |
| `check` | `scripts/check.py` | 7 项环境自检：子模块/venv/部署/库/OCR/依赖/连通，缺失自动补装 |
| `update` | `scripts/update_skill.py` | skill 自更新：上游组件检查与快进（`--apply`）、依赖刷新、重新部署 |

### 核心设计

- **船型参数化**（`data/profile.json` → `ships`）：舱位、槽位、箱体上限逐船建模；
  推荐数量 = `min(货舱, 预算可买, 买点库存)`，**不满载也成立**
- **手动装箱模型**：箱体规格必须 ≤ 槽位规格（如 Aurora TS 槽位 4×1+2×2 → 整箱 >2 SCU 不可用）；
  自动装箱场景注意终端可能选大箱导致装不下
- **新鲜度仲裁**：同一商品@终端，观察时间新者胜。用户实测带时间入库，比 UEX 旧时诚实
  采用 UEX 并输出 `[时滞]` 提示
- **时间模型**（`profile.time_model`）：圈时间 = 量子航行（航路图计算）+ 起降/终端操作
  + 装卸（手动 1 min/SCU / 自动 0.5 min/次）；表格与收益榜均带**时薪**
- **SkillOpt 迭代**：skill 文本经 7 轮 train/val 门禁迭代（零回退），方法论与运行记录
  见 `~/.tclaude/skills/skillopt_runs/sc-trader/report.md`

### 平台支持

| | macOS | Windows |
|---|-------|---------|
| 解释器 | `venv/bin/python` | `venv\Scripts\python.exe` |
| venv 创建 | `python3.12` | `py -3.12` 启动器 |
| OCR 安装 | brew | winget / choco |
| skill 部署 | symlink | symlink（开发者模式）/ junction 回退 |
| 控制台编码 | UTF-8 原生 | 内置 UTF-8 兜底，乱码时设 `PYTHONUTF8=1` |

## 文档

- [`docs/routes.md`](docs/routes.md) — 跑商路线手册：已验证循环、非自动装箱要点、
  时间估算公式、sc-trader 自动渲染的监控区块
- [`skills/sc-trader/SKILL.md`](skills/sc-trader/SKILL.md) — skill 完整工作流定义（agent 消费）
- 各子模块文档见其上游仓库

## 维护

```bash
# 上游组件有新版本时
tools/uexinfo/venv/bin/python skills/sc-trader/scripts/update_skill.py --apply
# 回归验证后提交子模块指针变更
git add . && git commit -m "chore: bump submodules" && git push
```

## 许可证

本仓库自身代码以 [MIT](LICENSE) 发布；各组件遵循其原始许可证（见组件总览表）。

**免责声明**：本项目为非官方粉丝作品，与 Cloud Imperium Games 无关联；
社区价格数据存在延迟，实际交易以游戏内为准。
