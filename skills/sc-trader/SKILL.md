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
#### 众包交叉核对（sc-trade.tools，免 token）

`mcp/sc-trade-tools-mcp`（工具前缀 `stt_*`）用来**验证 UEX**，不是替代它。

- **口径对应**：众包 `BUYS`（终端收购）= 玩家卖出价 = UEX `price_sell`；`SELLS` = 玩家买入价 = UEX `price_buy`。
- **免 token 可用**：商品、商店、地点、船、物品、众包成交价、排行榜等 15 个端点。
- **实测 400（需 token，别浪费调用）**：`commodity/items/{name}/transactions`、`commodity/shops/{name}/transactions`、`commodity/transactions`、`commodity/reports` —— 它们在 OpenAPI 里没声明 security，但匿名调用一律 400。
- **它跟 UEX 不是独立双源**：sc-trade.tools 网页上的路线数字用的就是 UEX 的价格（实测其 Golden Medmon 卖价 71,000 = UEX Ashland 价，而它自己的众包库只有 54,000）。它自己的众包库更稀疏（大量 `saturation=0.0` / `quantity 0`）。

2026-10-29 实测对照（玩家卖出价）：

| 商品 | UEX | 众包 | 结论 |
|---|---|---|---|
| Bexalite | 30,000 @ Patch City | 30,000 | 一致，可信 |
| Dymantium | 25,000 @ Levski | 25,000 | 一致，可信 |
| Organics | 14,000 @ People's Service | 14,000 | 一致，可信 |
| Taranite | 29,000 @ Orison | 28,000 | 差 1,000 |
| Compboard | 37,000 @ SM0-13 | 34,000 | 差 3,000，以 UEX 为准 |
| Golden Medmon | 71,000 @ Ashland | 54,000 | **差 31%**，转述时提示进游戏核实 |

两边一致 → 放心按 UEX 跑；不一致 → 以 UEX 为准并在转述时标注"进游戏核实"，用户实测仍是最终裁决。

- **判断货源稳不稳用 `stt_stock_range`**（MCP）：UEX 只给 1–7 的离散库存状态，给不出"这个货源会不会枯竭"；sc-trade.tools 的众包数据是**多批次上报**（带 `timestamp`），能看出量随时间的变化并给出 stable / refilling / draining 判定。例：RMC @ Seraphim 三次上报恒在 11,992~12,000（stable）；Titanium @ Baijini Point 5,917→5,782→5,000（draining）。**注意它是社区上报、不是实时**，上游自己声明无法知道实时库存。
- **交叉验证通道**：`mcp/sc-trade-tools-mcp`（工具前缀 `stt_*`，已注册在 `.mcp.json`）可直接查它的**众包价**与只读数据（商品/地点/船/成交记录）。它的三个算路端点需要 `SC_TRADE_TOOLS_TOKEN`（上游只发给 Patreon 支持者），没有 token 时用只读工具即可。**它的数据与 UEX 不同源**（玩家众包），只用于交叉核对 UEX，不覆盖用户实测，也不并入 watchlist。



**用户实际提供（截图/口述，一切用户亲报数据）★ > UEX API > sc-trade.tools。** 依据：
1. sc-trade.tools 的路线数字实测与 UEX 逐位一致（Taranite 18,792/27,000），说明它本身以 UEX 为价格底座、只是计算方法不同；
2. uexinfo 作者（同时集成两源）的权威序同为 scan > UEX > sc-trade；
3. sc-trade 众包库覆盖稀疏（Taranite 0 条提交）。
两边数字不一致时：以 UEX 为基线继续计算，差异项向用户标注"进游戏核实"；用户提供的任何实测价（截图或口述）即最终裁决，record 入库后 ★ 优先于一切外部源。

## 命令一览

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
- **本地算路引擎**：routes 配对接口之外，脚本会一次拉取全市场价格（`commodities_prices_all`）本地配对（每商品最便宜买点 ×2 × 最贵卖点 ×3，输出标注 `[computed]`）——精炼矿物（Taranite/Compboard 等）、卖端饱和下架、跨行星货源靠它覆盖。买点数量按 `min(货舱, 预算, 库存)` 取，不满载也成立；computed 航腿无库存数时按满载口径展示，进游戏以实际为准。**箱型门槛优先于数量**：商品每档箱型都超过本船 `max_box` 时（如 Corundum/Quartz 只有 8/16-SCU 箱、Aurora 上限 2），该航段**直接剔除**——不能按满载虚估利润；只有**箱型信息缺失**时才按满载口径估计。
- **时间模型与效率**：每条路线输出**圈时间**（量子航行 + 起降/终端操作 + 装卸）与**时薪**（圈利润÷圈时间）。装卸分档：手动 1 min/SCU、自动 0.5 min/次（profile.time_model 可调）。首选按圈利润排，但时薪是等量时间下的真实回报——两者冲突时把两个口径都呈给用户。
- **卖端（卸货）不计搬运**（2026-10-29 用户校准）：**卖货不用手动搬箱**——卖端只计 `base_stop_min`（起降/终端操作），搬运按 `time_model.sell_unload_min` 固定值计（当前 0.0），不再套用装货那套搬箱模型。买端装货仍按手动/自动分档照常计算。同一条环因此从 80 min 降到 62 min（时薪 832k → 1,074k）。
- **装货（自动装载）以 `is_auto_load` 为准**（2026-10-29 用户定案）：UEX 该字段是权威判据，缺失时才回退"有机库（`id_space_station / id_city`）"推导。两者在 115 个货运终端上差 7 个——`Locker Room - CRU-L4`、`Maintenance Room - CRU-L5`、星门 `Admin - * Gateway` 这类**有库但不提供装载服务**的终端，机库判据会误判成自动。自动装载依托空间站/城市的机库，地表前哨站没有。
- **卖货（卸货）没有 UEX 判据，以用户实测为准**：UEX **没有**"能否自动卖出"的字段（`is_auto_load` 只管装货——前哨站实测照样能一键卖；`has_loading_dock` / `has_docking_port` / `is_cargo_center` 在货运终端上全是 0，无区分度）。因此默认按"卖货不用手动搬箱"计（`sell_unload_min`，当前 0）；你实测发现某站卖货要手动搬时，在 `profile.unload_overrides` 里按名称子串标 `"manual"`（例：`{"Rustville": "manual"}`），该站就改按搬箱模型算。
- **别用货运电梯推断**：`has_freight_elevator` 与 `is_auto_load` 有 **132 处反例**（电梯=1 却不能自动装载，如 ArcCorp Mining Area、Shepherd's Rest），不是"能否自动装载"的证据。
- **组合语义**：推荐的是一组路线而非单条——利润最高者为**首选**，其余为**备选**；同一供货源（同商品+同买点）最多 2 条（同源价格完全联动，没有对冲价值）。用户问"跑哪条"按首选回答，首选因价格波动劣化时按收益榜改推新首选。
- **预算与库存的关系**：数量 = min(货舱, 预算可买, 买点库存)。**预算加大不一定增产**——货源库存卡死时 500k 和 300k 结果一致是正常现象，如实说明即可，不要当作故障。

#### 航程时间口径（用户质疑圈时间时按此回答）

三条规则，实现在 `common.py: qt_seconds()`，参数都在 `profile.json` 的 `time_model` 里（校准数值改配置，不要改脚本）：

1. **地图优先**：先查 UEX 交通图的距离，有路径就按图距离折算（× `seconds_per_gm`）。例：Rayari Kaltag → Shubin SM0-13 同属 MicroTech，图上绕 Lorville 76 Gm → 6.3 分钟。
2. **同行星系只是兜底**：`intra_system_min`（默认 1 分钟）**只在图上查不到路径时**才用（判定依据是 `planet_name` 相同——同一行星及其卫星算同一系）。不要把它当成同行星系的固定值去覆盖图距离。跨行星系无路径时用行星对典型距离 `pair_median` 兜底。
3. **图距离一律直接采用，不做离群校正**。历史上曾用"行星对典型距离中位数"改写过短的边，已废止：实测确认短边是真的（Deakins → HUR-L3 图上 7 Gm 属实），不要改成 32 Gm。
4. **1 Gm ≈ `seconds_per_gm` 秒**（当前 5.0）。这些数值会随校准变化，**回答时按 profile 里的当前值说，不要背死数字**。
5. **跨星系的星门只计一次**：`travel_seconds()` 算出的 `qt_sec` 已含穿越耗时（`jump_min`），`route_time_min()` **不再重复加** `jump_gate_min`——只有 `qt_sec` 缺失时才用它兜底。所以跨星系航段的圈时间 = 飞到本星系门 + 穿越 + 门到目的地，不要再加一次星门开销。

#### 商品可交易性过滤

只有 **`is_buyable=1` 且 `is_sellable=1`** 的商品才参与推荐（当前 205 个商品中 98 个满足）。只可卖不可买的商品（如 Audio-Visual Equipment，`is_buyable=0`）扫描时会表现为高利润，但游戏里根本买不到、跑不出来。用户问"为什么这个商品价差很大却没被推荐"时用这条回答。

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

### 5b. add_loop — 手工录入路线（外部工具发现的路线）

```bash
tools/uexinfo/venv/bin/python skills/sc-trader/scripts/add_loop.py \
  --ship <key> --budget <aUEC> --itinerary '{"stops":[{"terminal":"X","buy":[{"commodity":"Y","qty":72}]}, ...]}'
```
也接受 `--legs '[{"commodity":..,"buy":..,"sell":..,"qty":..}]'`（旧式按段录入，会自动
合并相邻同站的停靠）。**优先用 `--itinerary`**：按停靠序列建模才符合实际跑法，按段建模
会把同一航程里的多个商品算成多次往返、高估行程。

### 6a. 地面城市停靠更慢（+10min/次）

地面城市（UEX `id_city != 0`：Lorville / Area 18 / New Babbage / Levski 等）每次停靠
额外加 `time_model.surface_city_extra_min`（当前 **10min**）——交易点在城里、要先请求
停船、离场还要爬出重力井。空间站（`id_space_station != 0`，如 Port Tressler、Everus
Harbor、Baijini Point、ARC-Ln、Seraphim）不加。

判据用 `id_city` 而不是名字，别靠终端名猜（"Pyro Gateway (Stanton)" 是空间站，
"Conscientious Objects - Levski" 是地面城市）。

加这条之后，跨 Nyx 且经停 Levski 的环会被**纯空间站环**反超（实测 Freelancer MAX：
跨 Nyx 环 408,000/h vs 纯 Stanton 环 480,926/h）。

### 6b. 星门穿越（跨星系必读）

跨星系的航段 = 飞到本星系星门 → 穿越（`jump_gate_min`，当前 5min）→ 从对方星系门飞到目的地。
**算飞行时间必须用 `common.travel_seconds()`，不能只调 `qt_seconds()`** ——后者只在同一张
交通图里找路，会把 Stanton ⇄ Nyx 算成直达、漏掉星门耗时。闭环搜索早期版本就踩了这个坑
（跨 Nyx 环的时薪因此虚高约 30%）。

往返一趟跨星系大约多花 15~18 分钟。是否划算要看那段货的利润密度：实测 Freelancer MAX 上
跨 Nyx 环 515,613/h vs 纯 Stanton 环 480,926/h——只高 7%，风险（过门、Pyro 区）却多不少。

### 7. recommend --max-stops — 多角闭环搜索（原 find_loop，已合并）

```bash
tools/uexinfo/venv/bin/python skills/sc-trader/scripts/recommend.py \
  --max-stops 3 \
  --ship <ship_key> --budget <aUEC> [--max-stops 3] [--auto-load-only] \
  [--mixed] [--sort profit|hour] [--top 5] [--save]
```

- **与 recommend 的差别**：recommend 先挑单段利润最高的腿再补一段回程，选出来的卖点
  常常是"死胡同"（纯收购站，不卖货），回程只能空载——576 SCU 的 Caterpillar 尤其明显。
  **`--max-stops` 时直接搜索整环**（不给则走旧的「单腿 + 补回程」逻辑），只有能回到起点的路径才算候选，因此每段都能满载。
- **指定 N 角就只输出 N 角的环**。早期版本用 `len(path) >= 2` 收口，导致 `--max-stops 5`
  在深度 1 就收成 2 角环、看上去像"多角没意义"；已改为 `len(path) == N`。
- **选角数不能只看时薪**，还要看两点：
  1. **风险平摊** —— 角数越多用到的商品种类越多，任一商品断货不会让整圈归零
  2. **补货窗口** —— 圈时间越长，绕回第一站时它越有时间回补；短环连跑容易把货源抽干

  实测（Freelancer MAX / 3M / autoload / 混装）：2 角 480,926/h（3 种商品、37min），
  **5 角 479,550/h（9 种商品、102min）**——时薪几乎相同（差 0.3%），但利润 2.7 倍、
  风险更分散、补货窗口更长。想稳健就往多角走；6 角以上时薪开始明显下滑（444,267/h）。
- 数据取自本地算路引擎的全市场价，
  **环搜索过程不再调 API**；量子时间走本地交通图（带缓存）。
- `--sort hour` 按时薪排序（默认 `profit`）；`--save` 写入 watchlist。
- `--auto-load-only` 只从支持自动装载（`is_auto_load`）的站点买货——大船必开。
- `--mixed` **允许每段混装多种商品**填满货舱（默认每段只装一种）。库存不足时空舱飞很浪费
  ——Cobalt 只剩 57 SCU 而货舱 120，混装塞进 Stims 后那段多赚 9 万。注意：混装的装载按
  **每种商品一次**计（自动 5min/种），若实际一次装完则更划算，这个口径待实测校准。
- **实测结论（Caterpillar 576 SCU / 5M）**：3 角是甜点
  （Seraphim → Everus Harbor → ARC-L3 → Seraphim，+2,045,376 / 46.5min / 263 万/h，
  三段各 576 SCU 满载）；5 角利润更高（+3,209,472）但时薪降到 235 万/h。

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

## 行为护栏

- **诊断 ≠ 改代码**：回答提问、做排查时**不要擅自修改仓库脚本或数据文件**。发现代码缺陷就报告用户并给出建议，等确认后再改。（实测教训：一次纯诊断提问中，执行方直接改了 `recommend.py` 的函数签名，属于越界。）
- **数据漂移不是故障**：UEX 是众包数据，商品/航腿会随时间变化。过去存在的航腿现在查不到、价格对不上，先按当前数据如实说明，不要用旧快照硬套。

## 数据一致性

- 脚本写完 watchlist 后如果 JSON 解析失败：不要手工修补，重跑对应命令重建。
- 大数字核对：8 SCU 满载 Medical Supplies（3,343→5,500）单段利润应为 ≈17,256 aUEC；C1 64 SCU 满载同商品应 ≈137,648。量级不符先查船型/箱体配置。

## 验收口径

- **Aurora 100k**：recommend 产出 ≥1 条循环；与 docs/routes.md 循环 A 量级一致。
- **Aurora 200k / 300k、C1 300k / 500k**：不改 skill，换 `--budget` / `--ship` 重跑，应同样通过全部验收项。
