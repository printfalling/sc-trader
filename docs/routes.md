# SC 跑商路线手册

> 数据源：[UEXcorp API 2.0](https://api.uexcorp.space/2.0/)（社区报价，有延迟）。
> 刷新方法见文末。载具口径：Aurora Mk II + TS 模块，货舱 = 船体 2 SCU + TS 6 SCU = **8 SCU**
> （槽位 4×1 SCU + 2×2 SCU），预算 100,000 aUEC。

## 循环 A：MicroTech 境内往返（已验证，推荐起步）

**Rayari Kaltag Research Outpost → TDD (New Babbage) → 回 Kaltag**

| 航段 | 商品 | 买入 → 卖出 (aUEC/SCU) | 载货 | 本段利润 |
|------|------|----------------------|------|---------|
| Kaltag → TDD New Babbage | Medical Supplies | 3,343 → 5,500 | 8 SCU（库存恰好 8） | **+17,256** |
| TDD → Kaltag | — | 空载 | 0 | 0 |

- **每圈 ≈ 17,300 aUEC**，单段 ROI 64.5%
- 时间估算：2 次地面操作（~5 min/次：降落、电梯、终端买卖、起飞）+ 2 次短量子跳（~2 min）
  → **约 15–20 min/圈，小时收益 ≈ 5–7 万**（估算口径：Aurora 量子巡航 ~0.2c）
- 全程在 MicroTech 境内，无安全风险

## 候选航段（单段数据已见，链路未完整验证）

| 起点 → 终点 | 商品 | 买 → 卖 | 备注 |
|------------|------|---------|------|
| Shubin SM0 (MicroTech) → Everus Harbor (Crusader) | Waste | 229 → 450 | 8 SCU +1,768/段；回程货未验证 |
| Canard View (Pyro?) → MIC-L1 (MicroTech) | Degnous Root | 44,156 → 64,000 | 预算只能装 2 SCU，+39,688/段；**Pyro 无法律区 PvP 风险**，链路待核 |

多段组合建议直接用 [sc-trade.tools](https://sc-trade.tools/trade-routes)（浏览器打开，
支持船型/装箱方式/多段路径优化），本仓库 skill 与 UEX 数据可交叉核对它的报价。

## 非自动装箱（手动选箱）要点

UEX 返回的**买侧可用箱体规格**决定手动装箱可行性：

| 航段 | 买侧箱体规格 | 手动装箱方案 | 结论 |
|------|-------------|-------------|------|
| Kaltag Medical Supplies | 1, 2, 4, 8, 16 | 2×2-SCU 箱（进 2-SCU 槽）+ 4×1-SCU 箱（进 1-SCU 槽） | ✅ **满载 8 SCU** |
| — | 1×8 或 1×16 整箱 | 整箱大于最大槽位（2 SCU） | ⚠️ **自动装箱选大箱会装不下** |

**操作要领**：购买时手动选箱，单箱规格 ≤ 槽位规格（1-SCU 箱进 1-SCU 槽、2-SCU 箱进
2-SCU 槽）；整箱 4/8/16 SCU 一律不要。混装两种商品时不同商品不能共箱，需按槽位拆分。

## 时间估算方法

UEX `distance` 字段对同行星航线返回 0，不可用。实用估算公式：

```
单圈时间 ≈ 地面站数 × 5 min + 量子跳数 × (距离/0.2c + 2 min 校准)
```

跨行星量子跳（如 MicroTech ⇄ Hurston）实际约 10–15 min；精确值需进游戏实测后回填本表。

## 数据刷新

```bash
# 本循环价格（改 id_planet_origin=190 为 MicroTech 出发全部路线）
curl -s "https://api.uexcorp.space/2.0/commodities_routes?id_planet_origin=190&investment=100000"

# 指定商品价格核对
curl -s "https://api.uexcorp.space/2.0/commodities_prices?commodity_name=Medical%20Supplies"
```

注意：匿名调用限速 10 次/分钟；配置 UEXcorp token（[申请入口](https://uexcorp.space/api/apps)）后
限额提升且可提交价格。路线规划也可用 [sc-trade.tools](https://sc-trade.tools)（纯前端，无公开 API）。

<!-- sc-trader:auto-begin -->
### 监控中的路线

**预算 500,000 aUEC · 生成于 2026-10-23T14:03:04+00:00**

### r183 ★首选 · there-back · 风险 normal
圈利润 **+267,499** · 圈时间 95min · 时薪 168,946/h
- 航段1：买 Taranite 18,792/SCU × 26 SCU @ Samson & Son's Salvage Center [Stanton > ArcCorp > Wala]
  → 卖 29,000/SCU @ TDD - Trade and Development Division - Cloudview Center - Orison [Stanton > Crusader]
  本段利润 +265,408（量子 12min）
- 航段2：买 Medical Supplies 3,409/SCU × 1 SCU @ Hickes Research Outpost [Stanton > Crusader > Cellin]
  → 卖 5,500/SCU @ Admin - ARC-L4 [Stanton > ArcCorp > ArcCorp Lagrange Point 4]
  本段利润 +2,091（库存 1，量子 13min）

### r184 备选 · there-back · 风险 normal
圈利润 **+256,664** · 圈时间 110min · 时薪 140,509/h
- 航段1：买 Taranite 18,792/SCU × 26 SCU @ Samson & Son's Salvage Center [Stanton > ArcCorp > Wala]
  → 卖 28,000/SCU @ TDD - Trade and Development Division - Commons - New Babbage [Stanton > MicroTech]
  本段利润 +239,408（量子 16min）
- 航段2：买 Medical Supplies 3,343/SCU × 8 SCU @ Rayari Kaltag Research Outpost [Stanton > MicroTech > Calliope]
  → 卖 5,500/SCU @ Admin - ARC-L4 [Stanton > ArcCorp > ArcCorp Lagrange Point 4]
  本段利润 +17,256（库存 8，量子 9min）

### r185 备选 · intra · 风险 normal
圈利润 **+189,720** · 圈时间 65min · 时薪 174,857/h
- 航段1：买 Compboard 26,460/SCU × 18 SCU @ Rayari Kaltag Research Outpost [Stanton > MicroTech > Calliope]
  → 卖 37,000/SCU @ Shubin Mining Facility SM0-10 [Stanton > MicroTech]
  本段利润 +189,720（量子 21min）

### r186 备选 · intra · 风险 normal
圈利润 **+189,720** · 圈时间 65min · 时薪 174,857/h
- 航段1：买 Compboard 26,460/SCU × 18 SCU @ Rayari Kaltag Research Outpost [Stanton > MicroTech > Calliope]
  → 卖 37,000/SCU @ Shubin Mining Facility SM0-13 [Stanton > MicroTech]
  本段利润 +189,720（量子 21min）

### r187 备选 · there-back · 风险 normal
圈利润 **+206,824** · 圈时间 191min · 时薪 64,835/h
- 航段1：买 Bioplastic 5,538/SCU × 64 SCU @ Deakins Research Outpost [Stanton > Crusader > Yela]
  → 卖 8,500/SCU @ Rayari McGrath Research Outpost [Stanton > MicroTech > Clio]
  本段利润 +189,568（量子 16min）
- 航段2：买 Medical Supplies 3,343/SCU × 8 SCU @ Rayari Kaltag Research Outpost [Stanton > MicroTech > Calliope]
  → 卖 5,500/SCU @ Admin - CRU-L1 [Stanton > Crusader > Crusader Lagrange Point 1]
  本段利润 +17,256（库存 8，量子 16min）

### r188 备选 · there-back · 风险 normal
圈利润 **+228,642** · 圈时间 97min · 时薪 140,991/h
- 航段1：买 Fresh Food 21,614/SCU × 23 SCU @ Rayari Anvik Research Outpost [Stanton > MicroTech > Calliope]
  → 卖 29,000/SCU @ HDMS-Stanhope [Stanton > Hurston]
  本段利润 +169,878（量子 11min）
- 航段2：买 Ranta Dung 33,206/SCU × 6 SCU @ HDMS-Norgaard [Stanton > Hurston > Aberdeen]
  → 卖 43,000/SCU @ Rayari Deltana Research Outpost [Stanton > MicroTech]
  本段利润 +58,764（库存 6，量子 13min）

<!-- sc-trader:auto-end -->
