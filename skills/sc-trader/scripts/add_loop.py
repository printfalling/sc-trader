"""sc-trader 环路建模：多商品单航次（一次航程同时载多种货，各站分别装卸）。

与 add_route.py 的区别：后者按"一段一商品"建模，会把同一航程里的多个商品
算成多次跨星系/往返，高估行程。本脚本按**停靠序列**建模，才符合实际跑法。

用法：
    add_loop.py --ship starlancer --budget 3000000 --itinerary '{
      "stops": [
        {"terminal":"Ashland","buy":[{"commodity":"E'"'"'tam","qty":32},{"commodity":"Amioshi Plague","qty":32}]},
        {"terminal":"Brio'"'"'s Breaker Yard","sell":[{"commodity":"E'"'"'tam","qty":32}]},
        {"terminal":"Ashland","sell":[{"commodity":"Amioshi Plague","qty":32}]}
      ]}'

停靠按数组顺序执行；相邻停靠之间计入行程（含跨星系星门）；首尾同一终端即闭合。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402
from add_route import pick_commodity, pick_terminal  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--itinerary", required=True, help="JSON（含 stops 数组）")
    parser.add_argument("--ship", default=None)
    parser.add_argument("--budget", type=int, default=None)
    args = parser.parse_args()

    spec = json.loads(args.itinerary)
    stops_spec = spec["stops"]
    profile = common.load_profile()
    ship_key = args.ship or profile.get("default_ship")
    profile["ship"] = profile["ships"][ship_key]
    ship = profile["ship"]
    budget = args.budget or profile.get("budget_default")
    tm = profile.get("time_model") or {}
    ctx = common.build_ctx()
    client = common.get_client()

    # 玩家实测★
    try:
        from uexinfo.cache.scan_prices import ScanPriceStore
        mine = ScanPriceStore()._load()
    except Exception:
        mine = {}

    # 收集商品与终端
    com_names, term_names = set(), []
    for s in stops_spec:
        term_names.append(s["terminal"])
        for d in list(s.get("buy") or []) + list(s.get("sell") or []):
            com_names.add(d["commodity"])
    coms = {n: pick_commodity(ctx, n) for n in com_names}
    terms = [pick_terminal(ctx, n) for n in term_names]

    price_rows = {c.id: common.throttled_get(client, "commodities_prices",
                                             {"id_commodity": c.id})
                  for c in coms.values()}
    by_t = {}
    for cid, rows in price_rows.items():
        for r in rows or []:
            by_t[(cid, r.get("id_terminal"))] = r

    def price_of(cid, tid, side):
        r = by_t.get((cid, tid)) or {}
        obs = (mine.get(str(tid)) or {}).get(str(cid)) or {}
        if side == "buy":
            return float(obs.get("price_buy") or r.get("price_buy") or 0), r
        return float(obs.get("price_sell") or r.get("price_sell") or 0), r

    # 逐笔配对：买站的买入 与 之后首个卖站的卖出 一一对应（FIFO）
    pending, trades = {}, []
    for ci, st in enumerate(stops_spec):
        for d in st.get("buy") or []:
            pending.setdefault(d["commodity"], []).append(
                {"buy_ci": ci, "qty": int(d.get("qty") or ship["scu_total"])})
        for d in st.get("sell") or []:
            name = d["commodity"]
            need = int(d.get("qty") or sum(x["qty"] for x in pending.get(name, [])))
            while need > 0:
                if not pending.get(name):
                    raise SystemExit(f"ERROR: {name} 在 {st['terminal']} 买入量不足")
                b = pending[name][0]
                take = min(b["qty"], need)
                trades.append({"commodity": name, "buy_ci": b["buy_ci"], "sell_ci": ci,
                               "qty": take})
                b["qty"] -= take
                need -= take
                if b["qty"] <= 0:
                    pending[name].pop(0)
    for name, left in pending.items():
        if left:
            raise SystemExit(f"ERROR: {name} 买入后没有对应卖站")

    # 每笔：取价 → 数量封顶（库存/状态/箱型/现金）
    cash = budget
    for tr in trades:
        c = coms[tr["commodity"]]
        bt, stp = terms[tr["buy_ci"]], terms[tr["sell_ci"]]
        pb, rb = price_of(c.id, bt.id, "buy")
        ps, rs = price_of(c.id, stp.id, "sell")
        if not pb or not ps:
            raise SystemExit(f"ERROR: {tr['commodity']} 在 {bt.name}/{stp.name} 无有效价")
        qty = min(tr["qty"], ship["scu_total"],
                  int(rb.get("scu_buy") or tr["qty"]), max(0, cash // int(pb)))
        qty = common.stock_capped_qty(ship["scu_total"], qty,
                                      rb.get("status_buy"), rs.get("status_sell"))
        qty = common.snap_to_crate(qty, rb.get("container_sizes"), ship.get("max_box", 32))
        if qty <= 0:
            raise SystemExit(f"ERROR: {tr['commodity']} 可交易量为 0")
        sizes = [b for b in common.parse_box_sizes(rb.get("container_sizes"))
                 if 0 < b <= ship.get("max_box", 32)]
        box = max(sizes) if sizes else 1
        tr.update(pb=int(pb), ps=int(ps), qty=qty, box=box, crates=-(-qty // box),
                  profit=qty * (int(ps) - int(pb)), buy_t=bt.name, sell_t=stp.name)
        cash += tr["profit"]

    # 逐站结算：现金 / 箱数 / 停靠时间
    base = float(tm.get("base_stop_min", 4))
    mc, me = float(tm.get("manual_crate_min", 1.0)), float(tm.get("manual_extra_min", 3.0))
    ac, ae = float(tm.get("auto_crate_min", 0.0)), float(tm.get("auto_load_extra_min", 5.0))
    cash = budget
    stops_out = []
    for ci, st in enumerate(stops_spec):
        t = terms[ci]
        buys = [{"commodity": x["commodity"], "qty": x["qty"], "price": x["pb"]}
                for x in trades if x["buy_ci"] == ci]
        sells = [{"commodity": x["commodity"], "qty": x["qty"], "price": x["ps"]}
                 for x in trades if x["sell_ci"] == ci]
        crates = sum(x["crates"] for x in trades
                     if x["buy_ci"] == ci or x["sell_ci"] == ci)
        spend = sum(b["qty"] * b["price"] for b in buys)
        income = sum(s["qty"] * s["price"] for s in sells)
        # 同站先卸货收款、再装货付款（卖了才有钱买）
        if spend > cash + income:
            raise SystemExit(f"ERROR: 第{ci+1}站 {t.name} 现金不足"
                             f"（需 {spend:,}，手头 {cash:,} + 本站卖得 {income:,}）")
        cash += income - spend
        hangar = bool(getattr(t, "id_space_station", 0) or getattr(t, "id_city", 0))
        auto = ship.get("auto_loading_capable") and hangar
        handling = crates * (ac if auto else mc) + (ae if auto else me)
        stops_out.append({
            "terminal": {"id": t.id, "name": t.name,
                         "system": getattr(t, "star_system_name", ""), "auto": hangar},
            "buy": buys, "sell": sells, "crates": crates,
            "stop_min": round(base + handling, 1), "loading": "auto" if auto else "manual",
            "cash_after": cash,
        })

    # 行程
    spg = float(tm.get("seconds_per_gm", 5.0))
    jm = float(tm.get("jump_gate_min", 5.0))
    fb = float(tm.get("fallback_leg_min", 15.0))
    travel = 0.0
    hops = []
    for a, b in zip(terms, terms[1:]):
        d = common.travel_seconds(ctx, a.name, getattr(a, "star_system_name", ""),
                                  b.name, getattr(b, "star_system_name", ""),
                                  seconds_per_gm=spg, jump_min=jm, fallback_min=fb) / 60
        hops.append({"from": a.name, "to": b.name, "min": round(d, 1)})
        travel += d
    closed = terms[-1].id == terms[0].id
    repo = 0.0
    if not closed:
        repo = common.travel_seconds(ctx, terms[-1].name, getattr(terms[-1], "star_system_name", ""),
                                     terms[0].name, getattr(terms[0], "star_system_name", ""),
                                     seconds_per_gm=spg, jump_min=jm, fallback_min=fb) / 60
        hops.append({"from": terms[-1].name, "to": terms[0].name, "min": round(repo, 1)})
    total_time = travel + repo + sum(s["stop_min"] for s in stops_out)
    loop_profit = sum(t["profit"] for t in trades)

    # 兼容 update_prices 的合成 legs
    legs = []
    for tr in trades:
        c = coms[tr["commodity"]]
        legs.append({
            "commodity": tr["commodity"], "id_commodity": c.id,
            "buy_terminal": {"id": terms[tr["buy_ci"]].id, "name": terms[tr["buy_ci"]].name},
            "sell_terminal": {"id": terms[tr["sell_ci"]].id, "name": terms[tr["sell_ci"]].name},
            "price_buy": tr["pb"], "price_sell": tr["ps"], "qty": tr["qty"],
            "box_sizes": common.parse_box_sizes(
                (by_t.get((c.id, terms[tr["buy_ci"]].id)) or {}).get("container_sizes")),
            "leg_profit": tr["profit"], "scu_stock": (
                by_t.get((c.id, terms[tr["buy_ci"]].id)) or {}).get("scu_buy") or 0,
            "qt_sec": None, "source": "itinerary",
        })

    watchlist = common.load_watchlist()
    watchlist["next_id"] = watchlist.get("next_id", 1)
    route = {
        "id": f"r{watchlist['next_id']}", "type": "itinerary", "status": "candidate",
        "risk": "pyro" if any((s["terminal"].get("system") or "") == "Pyro" for s in stops_out)
                else "normal",
        "ship": ship_key, "source": "manual",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stops": stops_out, "hops": hops, "legs": legs,
        "loop_profit": loop_profit, "loop_time_min": round(total_time, 1),
        "profit_per_hour": int(loop_profit / total_time * 60) if total_time else None,
        "closed": closed,
        "snapshots": [{"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "price_buy": l["price_buy"], "price_sell": l["price_sell"],
                       "scu_stock": l["scu_stock"], "source": "itinerary"} for l in legs],
    }
    watchlist["next_id"] += 1
    watchlist["routes"].append(route)
    common.save_watchlist(watchlist)

    print(f"已添加环路 {route['id']} | 圈利润 +{loop_profit:,} | 圈时间 {round(total_time,1)}min | "
          f"{route['profit_per_hour']}/h | {'闭合' if closed else '需摆渡'}")
    for i, s in enumerate(stops_out, 1):
        b = ','.join(f"{x['commodity'][:10]}{x['qty']}" for x in s['buy']) or '—'
        sl = ','.join(f"{x['commodity'][:10]}{x['qty']}" for x in s['sell']) or '—'
        print(f"  {i}. {s['terminal']['name'][:30]:<30} 买[{b}] 卖[{sl}] "
              f"{s['crates']}箱 {s['stop_min']}min ({s['loading']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
