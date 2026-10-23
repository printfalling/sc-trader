---
name: sc-trader
description: Star Citizen 跑商助手。按飞船/预算推荐并记录跑商路线（recommend）、单次价格更新与变化报告（update_prices）、游戏截图读数入库（record）、环境自检补装（check）。触发词：跑商、路线推荐、价格更新、录入价格、环境自检。
---

# sc-trader — SC 跑商推荐与价格更新

为 Star Citizen 玩家提供：路线推荐 → 记录到 watchlist → 价格变化感知（UEX API + 玩家截图读数）。

## 硬性前提

- **解释器**：所有脚本必须用仓库自带 venv 跑（依赖 uexinfo 包，普通 python3 没有该依赖）：
  - macOS / Linux：`tools/uexinfo/venv/bin/python`
  - Windows：`tools\uexinfo\venv\Scripts\python.exe`（venv 由 check 自动创建，Windows 用 `py -3.12`）
- **Windows 注意**：控制台乱码时设置 `PYTHONUTF8=1`（脚本已内置 UTF-8 输出兜底）；部署 symlink 需开发者模式，无权限时 check 自动回退 junction；tesseract 通过 winget/choco 自动安装。
- 所有脚本调用处按平台替换解释器路径，其余命令参数不变。
- **数据文件**：`skills/sc-trader/data/profile.json`（船型表/预算/出发地）；`skills/sc-trader/data/watchlist.json`（路线 + 价格快照，git 跟踪）。
- **限速**：UEX 匿名 10 次/分钟。脚本内置 6.5s 调用间隔；**单任务 API 总调用 ≤5 次**。遇 `UEXError`/HTTP 400/429：等 60s 重试一次，再失败就如实报告并停止，不要连续重试。

## 工作流（两步闭环）

1. **检索候选路径**：recommend 产出路线组合（去重：同货源≤2、配对三元组唯一）
2. **实价更新收益**：update_prices 拉最新价 + record 录入用户实测 → 收益榜/调仓建议
   两者循环：推荐 → 跑商 → 实测入库 → 更新 → 调仓

## 价格权威 = 新鲜度仲裁（2026-10-23 定案）

**同一数据点（商品@终端），观察时间更新者胜**；静态来源不天然权威：

- 用户提供的价格必须带**观察时间**（record `--observed-at`，默认现在）——录的是"何时看到的"，不是"何时录入的"
- 玩家观察比 UEX 新 → `player★ 时间戳` 生效；比 UEX 旧 → 诚实采用 UEX 并输出 `[时滞]` 提示
- 基线调研：sc-trade.tools 的路线数字与 UEX 逐位一致（其价格底座即 UEX），两边差异项向用户标注"进游戏核实"，用户实测即最终裁决



**用户实际提供（截图/口述，一切用户亲报数据）★ > UEX API > sc-trade.tools。** 依据：
1. sc-trade.tools 的路线数字实测与 UEX 逐位一致（Taranite 18,792/27,000），说明它本身以 UEX 为价格底座、只是计算方法不同；
2. uexinfo 作者（同时集成两源）的权威序同为 scan > UEX > sc-trade；
3. sc-trade 众包库覆盖稀疏（Taranite 0 条提交）。
两边数字不一致时：以 UEX 为基线继续计算，差异项向用户标注"进游戏核实"；用户提供的任何实测价（截图或口述）即最终裁决，record 入库后 ★ 优先于一切外部源。

## 四个命令

### 1. check — 环境自检（缺失自动补装）

```bash
tools/uexinfo/venv/bin/python skills/sc-trader/scripts/check.py [--no-fix]
```

检查 6 项：git 子模块 / uexinfo venv / uexinfo 库导入 / tesseract（brew）/ playwright extra / UEX API 连通。
**验收**：输出 `N/N 项通过`。FAIL 项在非 `--no-fix` 模式下会自动补装；补装后重跑一次确认全绿，仍 FAIL 才报告用户。

### 2. recommend — 路线推荐（支持多船型）

```bash
tools/uexinfo/venv/bin/python skills/sc-trader/scripts/recommend.py \
  --budget <aUEC> [--ship <ship_key>] [--top 5] [--include-pyro]
```

- `--ship` 取 `profile.json` 里 `ships` 的键（如 `aurora_ts`、`c1_spirit`）；缺省用 `default_ship`。输出首行会标注 `[ship: <船名>]`。
- 行为：按出发行星拉 UEX 路线 → 数量/箱体/风险过滤（按所选船的槽位算可装量）→ 组装简单循环（同行星对折或双向往返）→ 写 `watchlist.json` + 渲染 `docs/routes.md`。
- **验收**：stdout 出现以 `| ID | 类型` 开头的表格；`watchlist.json` 是合法 JSON、routes 非空、每条 loop_profit>0；qty 不超过所选船 `scu_total`。
- **重复推荐**：先清空 `routes`（保留 `next_id` 字段）再跑，得到干净候选集；重建后 `ranking_prev` 基线自动重置。
- **部分装载与跟踪商品**：数量按实际库存取，**不必凑满货舱**（3 SCU 库存就装 3 SCU，利润线性缩放）。被推荐过/录入过的商品登记在 `tracked_commodities`；即使被 routes 配对接口下架（低库存/卖端饱和），recommend 也会用价格行重建航腿（输出标注 `[partial]`），库存未知时 qty 按满载口径展示、进游戏以实际为准。
- **本地算路引擎**：routes 配对接口之外，脚本会一次拉取全市场价格（`commodities_prices_all`）本地配对（每商品最便宜买点 ×2 × 最贵卖点 ×3，输出标注 `[computed]`）——精炼矿物（Taranite/Compboard 等）、卖端饱和下架、跨行星货源靠它覆盖。买点数量按 `min(货舱, 预算, 库存)` 取，不满载也成立；computed 航腿无库存数时按满载口径展示，进游戏以实际为准。
- **时间模型与效率**：每条路线输出**圈时间**（量子航行 + 起降/终端操作 + 装卸）与**时薪**（圈利润÷圈时间）。装卸分档：手动 1 min/SCU、自动 0.5 min/次（profile.time_model 可调）。首选按圈利润排，但时薪是等量时间下的真实回报——两者冲突时把两个口径都呈给用户。
- **组合语义**：推荐的是一组路线而非单条——利润最高者为**首选**，其余为**备选**；同一供货源（同商品+同买点）最多 2 条（同源价格完全联动，没有对冲价值）。用户问"跑哪条"按首选回答，首选因价格波动劣化时按收益榜改推新首选。
- **预算与库存的关系**：数量 = min(货舱, 预算可买, 买点库存)。**预算加大不一定增产**——货源库存卡死时 500k 和 300k 结果一致是正常现象，如实说明即可，不要当作故障。

### 3. update_prices — 单次价格更新

```bash
tools/uexinfo/venv/bin/python skills/sc-trader/scripts/update_prices.py
```

输出：每条价格变化一行，格式 `商品 @ 买端→卖端: 买入 旧→新 (±%) [来源]`；无变化时也要报告"检查了 N 条路线、0 项变化"。
`[player★]` = 截图/扫描价（record.py 写入的），**优先于 UEX 数据**；`[uex]` = 拉回的真实价会自动覆盖本地任何被篡改/过期的值——这是预期纠偏行为，不是异常。
更新完输出**组合收益榜**（全部路线按最新圈利润排序，标注首选）；与上次排名对比，**首选易主时输出** `⚠️ 最优路线变更: <旧>（现 +X） → <新>（+Y），建议切换跑商路线`——这是用户调整路线的信号，必须原样转述给用户。
执行后 watchlist 已更新（快照保留最近 10 条/腿，`ranking_prev` 记录最新排名），无需再手动改。

### 4. record — 截图读数入库

会话流程：用户实际提供价格时（发游戏截图 → 你视觉读数，或直接口述价格）→ 整理出 商品名 / 终端名 / 买入价 / 卖出价 / 库存 → 逐条调用：

```bash
tools/uexinfo/venv/bin/python skills/sc-trader/scripts/record.py \
  --commodity "Medical Supplies" --terminal "Rayari Kaltag Research Outpost" \
  --price-buy 3343 [--price-sell 5500] [--qty 8] [--observed-at "14:30"]
```
**必问观察时间**：用户报 价 时确认是多久前看到的（`--observed-at` 支持 HH:MM / 月-日 时:分）——它决定新鲜度仲裁的胜负，不是可选项。

输出先逐条列出 `商品 @ 终端: price_buy/price_sell 旧→新` 的变化行，再给 `已记录: ...` 确认。终端匹配按**买端或卖端**双向：在买点扫到的价刷新买入价，在卖点扫到的价刷新卖出价。
**验收**：输出 `已记录: ...` 且 `watchlist 匹配航腿 N 条已刷新`（N>0）。名称模糊匹配报 ERROR 时，改用截图里出现的**完整终端名**重试。
record 写入 uexinfo 的 scan_prices.json，此后 update_prices 和 uexinfo overlay 都会以该价优先（★）。

### 6. stock — 库存检查

```bash
tools/uexinfo/venv/bin/python skills/sc-trader/scripts/stock.py \
  --commodity <商品> [--only buy|sell|all] [--terminals "Kaltag,Anvik"] [--scu 8]
```

输出买点（买价升序）/卖点（卖价降序）两张表：价格、**现货/囤货**、库存状态、
**可买/可卖 SCU**（船舱×状态系数：买端越满越能买、卖端越空越能吸收）、滞销风险、
数据龄（>24h 标 ⚠️）、玩家实测★。
**用户问"某商品库存/某站能收多少"时用这个**，不要靠推算或旧数据口头回答；
`--terminals` 支持逗号分隔子串过滤，可只查关心的站点。

## 展示规范（用户定案：路径全名，禁止缩写）

1. **终端/地点一律用 UEX terminal_name 原文全名**——禁止自造缩写。
   反例：把 `TDD - Trade and Development Division - Commons - New Babbage` 写成 `New Babbage TDD`。
2. **终端首次出现附位置路径** `[系统 > 行星 > 卫星/轨道/站]`（脚本自动生成，如
   `Samson & Son's Salvage Center [Stanton > ArcCorp > Wala]`）。
3. **路线以结构化块展示**（recommend 的 render 格式）：每条路线一个块——
   头行 `### <ID> <★首选/备选> · 类型 · 风险`，次行 圈利润/圈时间/时薪，
   再逐航段三行（买入行、卖出行、利润行，含库存/部分装载/量子时间注记）。
4. **三处同一套格式**：脚本 stdout、docs/routes.md 渲染、聊天转述。聊天里转述路线时
   同样用全名+位置路径，不要为了简洁改写地名。

## 数据一致性

- 脚本写完 watchlist 后如果 JSON 解析失败：不要手工修补，重跑对应命令重建。
- 大数字核对：8 SCU 满载 Medical Supplies（3,343→5,500）单段利润应为 ≈17,256 aUEC；C1 64 SCU 满载同商品应 ≈137,648。量级不符先查船型/箱体配置。

## 验收口径

- **Aurora 100k**：recommend 产出 ≥1 条循环；与 docs/routes.md 循环 A 量级一致。
- **Aurora 200k / 300k、C1 300k / 500k**：不改 skill，换 `--budget` / `--ship` 重跑，应同样通过全部验收项。
