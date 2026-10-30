#!/usr/bin/env python3
"""mix_loop.py — 对一条给定的闭环做**混装**优化。

find_loop 每段只挑一个利润最高的商品，库存不够时空着飞（如 Cobalt 只有 57 SCU
而货舱 120）。这里改成：每段在买点把**多种商品**按单位利润贪心装填，直到装满
货舱或花完预算，把空舱利用起来。

用法：
  tools/uexinfo/venv/bin/python skills/sc-trader/scripts/mix_loop.py \\
      --ship freelancer_max --budget 3000000 \\
      --stops "Admin - Port Tressler,Admin - Baijini Point,Admin - Starlight Service,Levski"
      （最后一个站会回到第一个站成环）

约束与 find_loop 一致：is_tradeable 闸门、箱型/箱位、库存状态收口（stock_capped_qty）、
预算，另加 --auto-load-only（买点须 is_auto_load）与 --min-supply-ratio。
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402


def pick_terminal(ctx, name):
    """精确匹配优先，其次子串。否则 'Levski' 会先命中 'Conscientious Objects - Levski'（商店，无货）。"""
    n = name.strip().lower()
    for t in ctx.cache.terminals:
        if t.name.lower() == n:
            return t
    for t in ctx.cache.terminals:
        if n in t.name.lower():
            return t
    raise SystemExit(f"ERROR: 找不到终端 '{name}'")


def build(ctx, price_rows, system_ids=None):
    buy_at, sell_at = defaultdict(dict), defaultdict(dict)
    for r in price_rows or []:
        tid = r.get("id_terminal")
        t = {x.id: x for x in ctx.cache.terminals}.get(tid)
        if t is None:
            continue
        if system_ids and getattr(t, "id_star_system", 0) not in system_ids:
            continue
        cn = r.get("commodity_name")
        if not cn:
            continue
        try:
            pb = float(r.get("price_buy") or 0)
            ps = float(r.get("price_sell") or 0)
        except (TypeError, ValueError):
            continue
        if pb > 0:
            prev = buy_at[tid].get(cn)
            if prev is None or pb < prev[0][0]:
                buy_at[tid][cn] = ((pb, r), r)
        if ps > 0:
            prev = sell_at[tid].get(cn)
            if prev is None or ps > prev[0]:
                sell_at[tid][cn] = (ps, r)
    return buy_at, sell_at


def mixed_leg(buy_at, sell_at, tid_a, tid_b, budget, profile, ctx,
              require_auto_load=False, min_supply_ratio=1.0):
    """在 A 买、B 卖，混装多种商品把货舱填满。返回 (items, total_qty, total_profit)。"""
    ta = {x.id: x for x in ctx.cache.terminals}.get(tid_a)
    if require_auto_load and not getattr(ta, "is_auto_load", 0):
        return None
    scu_total = profile["ship"]["scu_total"]
    max_box = (profile.get("ship") or {}).get("max_box", 32)
    cands = []
    for cn, ((pb, br), _row) in buy_at.get(tid_a, {}).items():
        got = sell_at.get(tid_b, {}).get(cn)
        if not got:
            continue
        ps, srow = got
        if ps <= pb:
            continue
        if not C.is_tradeable(ctx, br.get("id_commodity")):
            continue
        sizes = C.parse_box_sizes(br.get("container_sizes"))
        capacity = C.packable_scu(sizes, profile)
        if capacity <= 0:
            if sizes:
                continue      # 箱型都超过船的箱位上限
            capacity = scu_total
        stock = br.get("scu_buy")
        qty = min(scu_total, capacity, budget // int(pb),
                  int(stock) if stock else scu_total)
        qty = C.stock_capped_qty(scu_total, qty, br.get("status_buy"),
                                 srow.get("status_sell"))
        qty = C.snap_to_crate(qty, br.get("container_sizes"), max_box)
        if qty <= 0:
            continue
        if stock and min_supply_ratio > 1 and int(stock) < qty * min_supply_ratio:
            continue
        cands.append({
            "commodity": cn, "price_buy": int(pb), "price_sell": int(ps),
            "qty": qty, "unit": int(ps) - int(pb),
            "supply": int(stock) if stock else None,
            "buy_status": br.get("status_buy"), "sell_status": srow.get("status_sell"),
            "sizes": sizes,
        })
    cands.sort(key=lambda c: -c["unit"])      # 单位利润高的先装
    remaining, cash = scu_total, budget
    items = []
    for c in cands:
        if remaining <= 0:
            break
        take = min(c["qty"], remaining, cash // c["price_buy"] if c["price_buy"] else 0)
        take = C.snap_to_crate(take, c["sizes"], max_box)
        if take <= 0:
            continue
        items.append({**c, "qty": take,
                      "profit": take * (c["price_sell"] - c["price_buy"])})
        remaining -= take
        cash -= take * c["price_buy"]
    if not items:
        return None
    return items, sum(i["qty"] for i in items), sum(i["profit"] for i in items)


def main() -> int:
    ap = argparse.ArgumentParser(description="闭环混装优化")
    ap.add_argument("--ship", default=None)
    ap.add_argument("--budget", type=int, default=None)
    ap.add_argument("--stops", required=True, help="逗号分隔的站点名，首尾自动成环")
    ap.add_argument("--auto-load-only", action="store_true")
    ap.add_argument("--min-supply-ratio", type=float, default=1.0)
    ap.add_argument("--compare", action="store_true",
                    help="同时列出单商品（不混装）的结果做对比")
    args = ap.parse_args()

    profile = C.load_profile()
    ship_key = args.ship or profile.get("default_ship")
    if ship_key not in (profile.get("ships") or {}):
        print(f"ERROR: 未知船型 '{ship_key}'", file=sys.stderr)
        return 1
    profile["ship"] = profile["ships"][ship_key]
    budget = args.budget or profile["budget_default"]

    client = C.get_client()
    ctx = C.build_ctx()
    price_rows = C.throttled_get(client, "commodities_prices_all")
    buy_at, sell_at = build(ctx, price_rows)

    stops = [pick_terminal(ctx, s) for s in args.stops.split(",")]
    ring = stops + [stops[0]]
    print(f"船 {ship_key} · 货舱 {profile['ship']['scu_total']} SCU · 预算 {budget:,}")
    print("环：" + " → ".join(t.name for t in ring))
    print()

    total_profit = 0
    total_qty = 0
    rows = []
    for a, b in zip(ring, ring[1:]):
        res = mixed_leg(buy_at, sell_at, a.id, b.id, budget, profile, ctx,
                        args.auto_load_only, args.min_supply_ratio)
        if not res:
            print(f"  {a.name} → {b.name}: 无可运货物")
            continue
        items, qty, profit = res
        total_profit += profit
        total_qty += qty
        qt = (C.qt_seconds(ctx, a.name, b.name) or 0) / 60
        rows.append((a, b, items, qty, profit, qt))
        print(f"航程 {len(rows)}: {a.name} → {b.name}   （装载 {qty} SCU，+{profit:,}）")
        for it in items:
            print(f"    - {it['commodity']} ×{it['qty']}  "
                  f"{it['price_buy']:,}→{it['price_sell']:,}  +{it['profit']:,}  "
                  f"[供应 {it['supply']} {C.UEX_STATUS_NAMES.get(it['buy_status'], '-')}]")
        print()

    print(f"混装后：总装载 {total_qty} SCU · 圈利润 +{total_profit:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
