#!/usr/bin/env python3
"""find_loop.py — 自动搜索多角贸易闭环（2~5 角）。

与 recommend 的区别：recommend 先挑单段利润最高的腿，再试着补一段回程——
选出来的卖点常常是"死胡同"（纯收购站，不卖货），于是回程只能空载。
这里反过来：**直接搜索整环**，只有能回到起点的路径才算候选。

数据来自本地算路引擎已拉的全市场价（commodities_prices_all，一次调用），
环搜索过程**不再调 API**；量子时间用本地交通图（qt_seconds，带缓存）。

用法：
  tools/uexinfo/venv/bin/python skills/sc-trader/scripts/find_loop.py \
      --ship caterpillar --budget 5000000 [--max-stops 3] [--auto-load-only] [--top 5] [--save]
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

TOP_SELL_PER_COMMODITY = 8     # 每个商品只看最好的几个卖点
BEAM_WIDTH = 6                 # 每层保留的路径数


def build_indexes(price_rows, term_by_id, system_filter=None, require_auto_load=False):
    """buy_at: tid -> {commodity: (price, stock, sizes, cid)}；sell_index: commodity -> [(tid, price)]."""
    buy_at: dict = defaultdict(dict)
    sell_at: dict = defaultdict(dict)
    for r in price_rows or []:
        tid = r.get("id_terminal")
        t = term_by_id.get(tid)
        if t is None:
            continue
        if system_filter and getattr(t, "id_star_system", 0) != system_filter:
            continue
        if require_auto_load and not getattr(t, "is_auto_load", 0):
            pass      # 自动装载限制只作用于**买点**，卖点不受限（前哨站也能收）
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
            if prev is None or pb < prev[0]:
                buy_at[tid][cn] = (pb, r.get("scu_buy"), r.get("container_sizes"),
                                   r.get("id_commodity"), tid, r.get("status_buy"))
        if ps > 0:
            cur = sell_at[tid].get(cn)
            if cur is None or ps > cur[0]:
                sell_at[tid][cn] = (ps, r)

    sell_index: dict = defaultdict(list)
    for tid, goods in sell_at.items():
        for cn, (ps, _row) in goods.items():
            sell_index[cn].append((tid, ps))
    for cn in sell_index:
        sell_index[cn].sort(key=lambda x: -x[1])
        del sell_index[cn][TOP_SELL_PER_COMMODITY:]
    return buy_at, sell_at, sell_index


def leg_for(buy_at, sell_at, sell_price, tid_a, tid_b, cn, budget, profile,
            min_supply_ratio: float = 1.0, ctx=None):
    """计算 A 买 cn、B 卖 cn 的可实现数量与利润；不可行返回 None。"""
    entry = buy_at.get(tid_a, {}).get(cn)
    if not entry:
        return None
    pb, stock, sizes, cid, _, status_buy = entry
    if ctx is not None and not C.is_tradeable(ctx, cid):
        return None      # 只可卖不可买的脏数据（如 Audio-Visual Equipment）买不到
    if sell_price <= pb:
        return None
    scu_total = profile["ship"]["scu_total"]
    max_box = (profile.get("ship") or {}).get("max_box", 32)
    box_sizes = C.parse_box_sizes(sizes)
    capacity = C.packable_scu(box_sizes, profile)
    if capacity <= 0:
        if box_sizes:
            return None
        capacity = scu_total
    qty = min(scu_total, capacity, budget // int(pb), int(stock) if stock else scu_total)
    # 与 recommend 一致：按买卖两端库存状态系数收口（买端越满越能买、卖端越空越能吸收）。
    # 少了这一步会按现货数虚估可装量——实测 Medium(4) 只能买到 60%（470 现货只放 67 SCU）。
    srow = (sell_at.get(tid_b, {}).get(cn) or (None, None))[1] or {}
    qty = C.stock_capped_qty(scu_total, qty, status_buy, srow.get("status_sell"))
    qty = C.snap_to_crate(qty, sizes, max_box)
    if qty <= 0:
        return None
    # 供应倍数：现货至少够跑 N 圈（默认 1 圈）。没有现货数据的按满载放行。
    if stock and min_supply_ratio > 1 and int(stock) < qty * min_supply_ratio:
        return None
    return {
        "commodity": cn, "id_commodity": cid,
        "buy_terminal": tid_a, "sell_terminal": tid_b,
        "price_buy": int(pb), "price_sell": int(sell_price),
        "qty": qty, "box_sizes": box_sizes,
        "scu_stock": int(stock) if stock else 0,      # 供应：买点现货
        "status_buy": status_buy,
        "sell_row": (sell_at.get(tid_b, {}).get(cn) or (None, None))[1],
        "leg_profit": qty * (int(sell_price) - int(pb)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="搜索多角贸易闭环")
    ap.add_argument("--ship", default=None)
    ap.add_argument("--budget", type=int, default=None)
    ap.add_argument("--max-stops", type=int, default=3,
                    help="最多几角（2=来回，3=三角，最大 5）")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--min-supply-ratio", type=float, default=1.0,
                    help="买点现货至少要够跑几圈（默认 1；2=至少两圈的量）")
    ap.add_argument("--detail", action="store_true",
                    help="逐站展开耗时分解（量子 / 买端停靠 / 卖端停靠）")
    ap.add_argument("--sort", choices=["profit", "hour"], default="profit",
                    help="按圈利润还是时薪排序（默认 profit）")
    ap.add_argument("--auto-load-only", action="store_true",
                    help="买点必须支持自动装载（UEX is_auto_load）")
    ap.add_argument("--include-pyro", action="store_true")
    ap.add_argument("--system", default=None, help="限定星系 id（默认 profile.origin）")
    ap.add_argument("--save", action="store_true", help="写入 watchlist")
    args = ap.parse_args()

    profile = C.load_profile()
    ship_key = args.ship or profile.get("default_ship")
    if ship_key not in (profile.get("ships") or {}):
        print(f"ERROR: 未知船型 '{ship_key}'", file=sys.stderr)
        return 1
    profile["ship"] = profile["ships"][ship_key]
    budget = args.budget or profile["budget_default"]
    if args.include_pyro:
        profile["include_pyro"] = True

    client = C.get_client()
    ctx = C.build_ctx()
    term_by_id = {t.id: t for t in ctx.cache.terminals}
    price_rows = C.throttled_get(client, "commodities_prices_all")

    if (args.system or "").lower() == "all":
        system_id = None            # 不限星系
    elif (args.system or "").isdigit():
        system_id = int(args.system)
    else:
        system_id = profile["origin"].get("id_star_system") or 68
    buy_at, sell_at, sell_index = build_indexes(
        price_rows, term_by_id, system_filter=system_id,
        require_auto_load=args.auto_load_only)

    qt_cache: dict = {}

    def qt(a, b):
        if (a, b) not in qt_cache:
            qt_cache[(a, b)] = C.qt_seconds(
                ctx, term_by_id[a].name, term_by_id[b].name) or 0
        return qt_cache[(a, b)]

    def auto_load_ok(tid):
        return bool(getattr(term_by_id[tid], "is_auto_load", 0))

    # 起点：该星系内有可买商品、且（若要求）能自动装载的终端
    starts = [tid for tid in buy_at
              if (not args.auto_load_only or auto_load_ok(tid))]
    print(f"起点候选 {len(starts)} 个（星系 {system_id}），搜索 {args.max_stops} 角闭环…",
          file=sys.stderr)

    results = []
    for start in starts:
        # beam: (profit, time_sec, path_tids, legs)
        beam = [(0, 0, [start], [])]
        for depth in range(args.max_stops):
            nxt = []
            for profit, secs, path, legs in beam:
                cur = path[-1]
                cands = []
                # 向前：从 cur 买货卖到某站
                for cn, entry in buy_at.get(cur, {}).items():
                    for tid_b, ps in sell_index.get(cn, []):
                        if tid_b == cur or tid_b in path:
                            continue
                        lg = leg_for(buy_at, sell_at, ps, cur, tid_b, cn, budget, profile,
                                min_supply_ratio=args.min_supply_ratio, ctx=ctx)
                        if not lg:
                            continue
                        if args.auto_load_only and not auto_load_ok(cur):
                            continue
                        cands.append((lg["leg_profit"], tid_b, lg))
                # 收口：从 cur 买货卖回起点（闭环）
                for cn, (ps, _r) in sell_at.get(start, {}).items():
                    lg = leg_for(buy_at, sell_at, ps, cur, start, cn, budget, profile,
                                min_supply_ratio=args.min_supply_ratio, ctx=ctx)
                    if not lg:
                        continue
                    if args.auto_load_only and not auto_load_ok(cur):
                        continue
                    total_profit = profit + lg["leg_profit"]
                    total_sec = secs + qt(cur, start)
                    if len(path) >= 2:      # 至少两角才叫环
                        stops = path + [start]
                        # 旋转归一化：同一个环从不同起点出发只算一条
                        ring = stops[:-1]
                        rot = min(range(len(ring)),
                                  key=lambda i: tuple(ring[i:] + ring[:i]))
                        key = (tuple(ring[rot:] + ring[:rot]), len(ring))
                        results.append({
                            "stops": stops,
                            "legs": legs + [lg],
                            "profit": total_profit,
                            "sec": total_sec,
                            "key": key,
                        })
                cands.sort(key=lambda x: -x[0])
                for p, tid_b, lg in cands[:BEAM_WIDTH]:
                    nxt.append((profit + p, secs + qt(cur, tid_b),
                                path + [tid_b], legs + [lg]))
            nxt.sort(key=lambda x: -x[0])
            beam = nxt[:BEAM_WIDTH]
            if not beam:
                break

    if not results:
        print("未找到闭环（试试放宽 --max-stops、去掉 --auto-load-only，或换 --system）")
        return 0

    # 旋转去重
    seen = set()
    uniq = []
    for r in results:
        if r["key"] in seen:
            continue
        seen.add(r["key"])
        uniq.append(r)
    results = uniq

    # 完整圈时间 = 量子 + 每站起降/装卸（卖端按 sell_unload_min）
    for r in results:
        legs_full = [{
            "qt_sec": r["_legs_sec"][i] if False else None,
        } for i in range(0)]
        legs_full = []
        for lg in r["legs"]:
            ta, tb = term_by_id[lg["buy_terminal"]], term_by_id[lg["sell_terminal"]]
            legs_full.append({
                "qt_sec": qt(lg["buy_terminal"], lg["sell_terminal"]),
                "qty": lg["qty"], "box_sizes": lg["box_sizes"],
                "buy_terminal": {"name": ta.name,
                                 "system": getattr(ta, "star_system_name", ""),
                                 "auto": bool(getattr(ta, "is_auto_load", 0))},
                "sell_terminal": {"name": tb.name,
                                  "system": getattr(tb, "star_system_name", ""),
                                  "auto": bool(getattr(tb, "is_auto_load", 0))},
            })
        r["loop_time_min"] = C.route_time_min(profile, legs_full)
        r["per_hour"] = int(r["profit"] / r["loop_time_min"] * 60) if r["loop_time_min"] else None

        tm = profile.get("time_model", {})
        base = tm.get("base_stop_min", 4)
        manual_crate = float(tm.get("manual_crate_min", 1.0))
        manual_extra = float(tm.get("manual_extra_min", 3.0))
        crate_min = float(tm.get("auto_crate_min", 1.0))
        extra = float(tm.get("auto_load_extra_min", 3.0))
        ship_auto = bool(profile["ship"].get("auto_loading_capable")
                         or tm.get("auto_loading"))
        detail = []
        for lg, lf in zip(r["legs"], legs_full):
            sizes = [b for b in (lg["box_sizes"] or []) if isinstance(b, int) and b > 0]
            box = max([b for b in sizes
                       if b <= (profile["ship"].get("max_box", 32))] or [0]) or 1
            crates = -(-lg["qty"] // box)
            sr = lg.get("sell_row") or {}
            row = {"commodity": lg["commodity"], "qty": lg["qty"], "box": box,
                   "crates": crates,
                   "supply": lg.get("scu_stock"),
                   "buy_status": lg.get("status_buy"),
                   "demand_stock": sr.get("scu_sell_stock"),
                   "sell_status": sr.get("status_sell"),
                   "buy": term_by_id[lg["buy_terminal"]].name,
                   "sell": term_by_id[lg["sell_terminal"]].name,
                   "qt_min": round((lf["qt_sec"] or 0) / 60, 1)}
            # 卖端：停船(base) + 卸货(sell_unload_min，按用户校准为 0)
            row["sell_stop"] = round(base + float(tm.get("sell_unload_min", 0.0)), 1)
            row["sell_unload"] = round(float(tm.get("sell_unload_min", 0.0)), 1)
            # 买端：停船(base) + 装载(搬运)
            term = lf["buy_terminal"]
            auto = ship_auto and (bool(term.get("auto"))
                                  or C.terminal_loading_mode(term["name"], profile))
            handling = (crates * crate_min + extra) if auto else (
                crates * manual_crate + manual_extra)
            row["buy_stop"] = round(base + handling, 1)
            row["load"] = round(handling, 1)
            row["auto_load"] = auto
            row["leg_min"] = round(row["qt_min"] + row["buy_stop"] + row["sell_stop"], 1)
            detail.append(row)
        r["detail"] = detail

    if args.sort == "hour":
        results.sort(key=lambda r: -(r["per_hour"] or 0))
    else:
        results.sort(key=lambda r: -r["profit"])
    print(f"\n找到 {len(results)} 个闭环，按圈利润取前 {args.top}：\n")
    shown = results[:args.top]
    def st_name(v):
        return C.UEX_STATUS_NAMES.get(v, "-")

    for i, r in enumerate(shown, 1):
        stops = " \u2192 ".join(term_by_id[t].name for t in r["stops"])
        ph = f"{r['per_hour']:,}/h" if r.get("per_hour") else "\u2014"
        print(f"### \u73af {i} \u00b7 \u5708\u5229\u6da6 +{r['profit']:,} \u00b7 "
              f"\u5708\u65f6\u95f4 {r['loop_time_min']}min \u00b7 \u65f6\u85aa {ph}")
        print(f"    {stops}")
        print()
        print("| \u6bb5 | \u5546\u54c1 | \u4e70 \u2192 \u5356 | \u5229\u6da6 | \u98de\u884c |")
        print("|---|---|---|---|---|")
        for n, (lg, d) in enumerate(zip(r["legs"], r["detail"]), 1):
            bn = term_by_id[lg["buy_terminal"]].name
            sn = term_by_id[lg["sell_terminal"]].name
            print(f"| {n} | {lg['commodity']} {lg['qty']} SCU | "
                  f"{bn} {lg['price_buy']:,} \u2192 {sn} {lg['price_sell']:,} | "
                  f"+{lg['leg_profit']:,} | {d['qt_min']}min |")
        qt = round(sum(d["qt_min"] for d in r["detail"]), 1)
        print(f"| | | **\u5408\u8ba1** | **+{r['profit']:,}** | **{qt}min** |")
        print()
        parts = []
        for n, d in enumerate(r["detail"], 1):
            sup = d.get("supply")
            dst = d.get("demand_stock")
            parts.append(
                f"\u6bb5{n} \u4f9b\u5e94 {sup if sup is not None else '-'}"
                f"({st_name(d.get('buy_status'))}) / \u5356\u7aef\u56e4 "
                f"{dst if dst is not None else '-'}({st_name(d.get('sell_status'))})")
        print("- \u8865\u7ed9/\u9700\u6c42\uff1a" + "\uff1b".join(parts))
        print()
        if args.detail:
            print("| \u6bb5 | \u7bb1\u578b | \u7bb1\u6570 | \u91cf\u5b50 | \u505c\u8239 | "
                  "\u88c5\u8f7d | \u5378\u8d27 | \u4e70\u7aef | \u5356\u7aef | \u5c0f\u8ba1 |")
            print("|---|---|---|---|---|---|---|---|---|---|")
            for n, d in enumerate(r["detail"], 1):
                print(f"| {n} | {d['box']} SCU | {d['crates']} | {d['qt_min']}min | "
                      f"{base}min | {d.get('load', 0)}min | {d.get('sell_unload', 0)}min | "
                      f"{d['buy_stop']}min | {d['sell_stop']}min | {d['leg_min']}min |")
            qts = round(sum(d["qt_min"] for d in r["detail"]), 1)
            bs = round(sum(d["buy_stop"] for d in r["detail"]), 1)
            ss = round(sum(d["sell_stop"] for d in r["detail"]), 1)
            print(f"| | | | **{qts}min** | **{len(r['detail']) * base}min** | "
                  f"**{round(sum(d.get('load', 0) for d in r['detail']), 1)}min** | "
                  f"**{round(sum(d.get('sell_unload', 0) for d in r['detail']), 1)}min** | "
                  f"**{bs}min** | **{ss}min** | **{r['loop_time_min']}min** |")
            print()
            print(f"  \u53e3\u5f84\uff1a\u505c\u8239={base}min/\u7ad9\uff08\u8d77\u964d\u00b7"
              "\u7535\u68af\u00b7\u7ec8\u7aef\u4ea4\u6613\uff0c\u4e70\u5356\u5404\u4e00\u6b21\uff09\uff1b"
              "\u88c5\u8f7d=\u81ea\u52a8 5min/\u6b21\uff0c\u624b\u52a8 \u7bb1\u6570\u00d71+3\uff1b"
              "\u5378\u8d27=sell_unload_min(0)")
            print("  \u4f9b\u5e94=\u4e70\u70b9 scu_buy\uff08\u73b0\u8d27\uff09\uff1b"
                  "\u9700\u6c42(\u56e4)=\u5356\u70b9 scu_sell_stock\uff1b"
                  "\u72b6\u6001 1=Out\u2026 7=Max\uff08\u5356\u7aef\u8d8a\u7a7a\u8d8a\u80fd\u5438\u6536\uff09")
        print()
    if args.save:
        out = []
        for r in shown:
            legs = [{
                "commodity": lg["commodity"], "id_commodity": lg["id_commodity"],
                "buy_terminal": {"id": lg["buy_terminal"],
                                 "name": term_by_id[lg["buy_terminal"]].name,
                                 "system": getattr(term_by_id[lg["buy_terminal"]],
                                                   "star_system_name", "")},
                "sell_terminal": {"id": lg["sell_terminal"],
                                  "name": term_by_id[lg["sell_terminal"]].name,
                                  "system": getattr(term_by_id[lg["sell_terminal"]],
                                                    "star_system_name", "")},
                "price_buy": lg["price_buy"], "price_sell": lg["price_sell"],
                "qty": lg["qty"], "box_sizes": lg["box_sizes"],
                "scu_stock": lg["scu_stock"], "leg_profit": lg["leg_profit"],
                "pyro": getattr(term_by_id[lg["sell_terminal"]],
                                "star_system_name", "").lower() == "pyro",
            } for lg in r["legs"]]
            out.append({
                "type": "loop", "status": "candidate",
                "ship": ship_key, "legs": legs,
                "loop_profit": r["profit"],
                "created_at": C.datetime.now(C.timezone.utc).isoformat(timespec="seconds"),
            })
        wl = C.load_watchlist()
        for route in out:
            route["id"] = f"r{wl.get('next_id', 1)}"
            wl["next_id"] = int(wl.get("next_id", 1)) + 1
            wl.setdefault("routes", []).append(route)
        C.save_watchlist(wl)
        print(f"已写入 watchlist：{len(out)} 条闭环")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
