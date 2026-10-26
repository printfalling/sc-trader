"""sc-trader 手工添加路线：把用户指定（或外部工具发现的）多段环路纳入监控。

用法：
    add_route.py --legs '[{"commodity":"Bexalite","buy":"Bueno Ravine","sell":"The Golden Riviera","qty":52}, ...]'
                 [--ship c1_spirit] [--budget 2000000] [--system Pyro]

- 终端名支持模糊匹配（唯一匹配即采用，多义报错并列出候选）
- 价格取自 UEX 当前值，若该终端该商品有玩家实测★则优先
- 计算圈利润 / 圈时间（含段间转场、收尾摆渡、跨星系星门），写入 watchlist
  并标记 source=manual，之后可被 update_prices 持续监控
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402
from recommend import annotate_terminals  # noqa: E402


def pick_terminal(ctx, name: str):
    q = name.strip().lower()
    cands = [t for t in ctx.cache.terminals if q in t.name.lower()]
    if not cands:
        raise SystemExit(f"ERROR: 未找到终端 '{name}'")
    exact = [t for t in cands if t.name.lower() == q]
    if exact:
        return exact[0]
    cands.sort(key=lambda t: len(t.name))
    top = [t for t in cands if len(t.name) == len(cands[0].name)]
    if len(top) > 1:
        raise SystemExit(f"ERROR: 终端 '{name}' 有多个匹配：{', '.join(t.name for t in top[:6])}")
    return cands[0]


def pick_commodity(ctx, name: str):
    q = name.strip().lower()
    for c in ctx.cache.commodities:
        if c.name.lower() == q:
            return c
    subs = [c for c in ctx.cache.commodities if q in c.name.lower()]
    if not subs:
        raise SystemExit(f"ERROR: 未找到商品 '{name}'")
    subs.sort(key=lambda c: len(c.name))
    return subs[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--legs", required=True, help="JSON 数组")
    parser.add_argument("--ship", default=None)
    parser.add_argument("--budget", type=int, default=None)
    args = parser.parse_args()

    legs_spec = json.loads(args.legs)
    profile = common.load_profile()
    ship_key = args.ship or profile.get("default_ship")
    profile["ship"] = profile["ships"][ship_key]
    budget = args.budget or profile.get("budget_default")

    ctx = common.build_ctx()
    client = common.get_client()
    p = profile["ship"]
    scu_total = p["scu_total"]

    # 批量取价（每商品一次）
    prices: dict = {}
    for spec in legs_spec:
        com = pick_commodity(ctx, spec["commodity"])
        if com.id not in prices:
            prices[com.id] = common.throttled_get(
                client, "commodities_prices", {"id_commodity": com.id})
    mine = {}
    try:
        from uexinfo.cache.scan_prices import ScanPriceStore
        mine = ScanPriceStore()._load()
    except Exception:
        pass

    now = time.time()
    legs, cash = [], budget
    for spec in legs_spec:
        com = pick_commodity(ctx, spec["commodity"])
        bt = pick_terminal(ctx, spec["buy"])
        st = pick_terminal(ctx, spec["sell"])
        rows = prices.get(com.id) or []
        brow = next((r for r in rows if r.get("id_terminal") == bt.id), {})
        srow = next((r for r in rows if r.get("id_terminal") == st.id), {})
        # ★ 玩家实测优先
        obs_b = (mine.get(str(bt.id)) or {}).get(str(com.id)) or {}
        obs_s = (mine.get(str(st.id)) or {}).get(str(com.id)) or {}
        buy = float(obs_b.get("price_buy") or brow.get("price_buy") or 0)
        sell = float(obs_s.get("price_sell") or srow.get("price_sell") or 0)
        if not buy or not sell:
            raise SystemExit(f"ERROR: {com.name} 在 {bt.name}/{st.name} 无有效买卖价")
        qty = int(spec.get("qty") or scu_total)
        qty = min(qty, scu_total, cash // int(buy))
        qty = common.stock_capped_qty(scu_total, qty,
                                      brow.get("status_buy"), srow.get("status_sell"))
        qty = common.snap_to_crate(qty, brow.get("container_sizes"),
                                   p.get("max_box", 32))
        if qty <= 0:
            raise SystemExit(f"ERROR: {com.name} 可交易量为 0（库存/箱型限制）")
        leg = {
            "commodity": com.name, "id_commodity": com.id,
            "buy_terminal": {"id": bt.id, "name": bt.name},
            "sell_terminal": {"id": st.id, "name": st.name},
            "price_buy": int(buy), "price_sell": int(sell),
            "qty": qty, "box_sizes": brow.get("container_sizes") or [],
            "leg_profit": qty * (int(sell) - int(buy)),
            "scu_stock": brow.get("scu_buy") or 0,
            "status_buy": brow.get("status_buy"),
            "status_sell": srow.get("status_sell"),
            "qt_sec": None, "source": "manual",
        }
        legs.append(leg)
        cash += leg["leg_profit"]

    annotate_terminals(legs, ctx, None, profile)
    tm = profile.get("time_model") or {}
    for i, x in enumerate(legs):
        x["qt_sec"] = common.travel_seconds(
            ctx, x["buy_terminal"]["name"], x["buy_terminal"].get("system"),
            x["sell_terminal"]["name"], x["sell_terminal"].get("system"),
            seconds_per_gm=float(tm.get("seconds_per_gm", 5.0)),
            jump_min=float(tm.get("jump_gate_min", 5.0)),
            fallback_min=float(tm.get("fallback_leg_min", 15.0)))
        if i + 1 < len(legs):
            pass
    # 段间转场 + 收尾摆渡
    transit = 0.0
    for a, b in zip(legs, legs[1:]):
        t = common.travel_seconds(
            ctx, a["sell_terminal"]["name"], a["sell_terminal"].get("system"),
            b["buy_terminal"]["name"], b["buy_terminal"].get("system"),
            seconds_per_gm=float(tm.get("seconds_per_gm", 5.0)),
            jump_min=float(tm.get("jump_gate_min", 5.0)),
            fallback_min=float(tm.get("fallback_leg_min", 15.0)))
        transit += t or 0
    first_buy, last_sell = legs[0]["buy_terminal"], legs[-1]["sell_terminal"]
    closed = last_sell["id"] == first_buy["id"]
    repo = 0.0 if closed else common.travel_seconds(
        ctx, last_sell["name"], last_sell.get("system"),
        first_buy["name"], first_buy.get("system"),
        seconds_per_gm=float(tm.get("seconds_per_gm", 5.0)),
        jump_min=float(tm.get("jump_gate_min", 5.0)),
        fallback_min=float(tm.get("fallback_leg_min", 15.0)))

    loop_profit = sum(x["leg_profit"] for x in legs)
    loop_time = common.route_time_min(profile, legs,
                                      extra_min=(transit + (repo or 0)) / 60)

    watchlist = common.load_watchlist()
    watchlist["next_id"] = watchlist.get("next_id", 1)
    route = {
        "id": f"r{watchlist['next_id']}", "type": "manual-loop",
        "status": "candidate", "risk": "pyro" if any(
            (x["sell_terminal"].get("system") or "") == "Pyro" for x in legs) else "normal",
        "ship": ship_key, "source": "manual",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "legs": legs, "loop_profit": loop_profit,
        "loop_time_min": loop_time,
        "profit_per_hour": int(loop_profit / loop_time * 60) if loop_time else None,
        "closed": closed, "transit_sec": transit or None,
        "reposition_sec": repo or None,
        "snapshots": [{"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "price_buy": x["price_buy"], "price_sell": x["price_sell"],
                       "scu_stock": x["scu_stock"], "source": "manual"} for x in legs],
    }
    watchlist["next_id"] += 1
    watchlist["routes"] = [r for r in watchlist["routes"] if r.get("id") != route["id"]]
    watchlist["routes"].append(route)
    common.save_watchlist(watchlist)

    print(f"已添加 {route['id']} | 圈利润 +{loop_profit:,} | "
          f"{loop_time}min | {route['profit_per_hour']}/h | "
          f"{'闭合' if closed else '需摆渡 %.1fmin' % ((repo or 0)/60)}")
    for i, x in enumerate(legs, 1):
        print(f"  航段{i}: {x['commodity']} {x['price_buy']:,}→{x['price_sell']:,} "
              f"× {x['qty']} = +{x['leg_profit']:,} @ {x['buy_terminal']['name'][:26]} "
              f"→ {x['sell_terminal']['name'][:26]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
