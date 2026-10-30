"""sc-trader 路线推荐：按飞船/预算从 UEX 拉路线，过滤+组装简单循环，写入 watchlist。

用法：
    tools/uexinfo/venv/bin/python skills/sc-trader/scripts/recommend.py \
        [--budget 100000] [--top 5] [--include-pyro] [--max-calls 4]

过滤规则：
  - 价差 = price_destination - price_origin > 0
  - 数量 = min(货舱 SCU, 预算//买入价, 卖点库存)，且 ≤ 箱体适配可装量
  - 箱体适配：container_sizes_origin 里 1-SCU 箱可满载（8），2-SCU 箱 4，>2 不可用
  - 风险：默认排除 Pyro 目的地（--include-pyro 放开，只标注不排除）
循环组装（本期范围）：
  - intra：买卖两端同在出发行星，空载返程即成环
  - there-back：对 top 目的地行星补查返程腿，盈利则双向环，否则空载返程
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402


def fetch_routes(client, id_planet: int, budget: int) -> list[dict]:
    rows = common.throttled_get(client, "commodities_routes", {
        "id_planet_origin": id_planet,
        "investment": budget,
    })
    return rows or []


_AUTO_LOAD_CACHE: dict = {}


def auto_load_by_id(ctx) -> dict:
    """终端 id -> 是否提供自动装载服务（UEX is_auto_load）；ctx 为 None 时返回空表。"""
    if ctx is None:
        return {}
    if not _AUTO_LOAD_CACHE:
        for t in getattr(ctx.cache, "terminals", []) or []:
            _AUTO_LOAD_CACHE[t.id] = bool(getattr(t, "is_auto_load", 0))
    return _AUTO_LOAD_CACHE


def find_return_leg_local(price_rows, term_by_id, from_tid, to_tid, budget: int,
                          profile: dict, ctx=None,
                          require_auto_load: bool = False) -> dict | None:
    """用本地全市场价找「从卖点(from) 回 买点(to)」的回程货。

    与 find_return_leg 的两点区别：
    - **不额外调 API**：数据来自本地算路引擎已拉的全市场价
      （routes 接口会漏掉精炼矿物等，而回程常常正好是这些货）
    - **按可实现的绝对利润排序** = min(货舱, 库存, 预算) × 单位价差，
      而不是单位价差——否则永远选到"价差好看但只剩几十 SCU"的回程货
    """
    if not price_rows or from_tid is None or to_tid is None:
        return None
    ft = term_by_id.get(from_tid)
    tt = term_by_id.get(to_tid)
    if ft is None or tt is None:
        return None
    if require_auto_load and not getattr(ft, "is_auto_load", 0):
        return None      # 回程买点也得能自动装载

    scu_total = profile["ship"]["scu_total"]
    max_box = (profile.get("ship") or {}).get("max_box", 32)

    buy_at_from: dict = {}
    sell_at_to: dict = {}
    for r in price_rows:
        tid = r.get("id_terminal")
        cn = r.get("commodity_name")
        if not cn:
            continue
        if tid == from_tid:
            try:
                pb = float(r.get("price_buy") or 0)
            except (TypeError, ValueError):
                pb = 0
            if pb > 0:
                prev = buy_at_from.get(cn)
                if prev is None or pb < prev[0]:
                    buy_at_from[cn] = (pb, r)
        elif tid == to_tid:
            try:
                ps = float(r.get("price_sell") or 0)
            except (TypeError, ValueError):
                ps = 0
            if ps > 0:
                prev = sell_at_to.get(cn)
                if prev is None or ps > prev:
                    sell_at_to[cn] = ps

    best = None
    for cn, (pb, br) in buy_at_from.items():
        ps = sell_at_to.get(cn)
        if not ps or ps <= pb:
            continue
        box_sizes = common.parse_box_sizes(br.get("container_sizes"))
        capacity = common.packable_scu(box_sizes, profile)
        if capacity <= 0:
            if box_sizes:
                continue      # 箱型都超过船的箱位上限，装不下
            capacity = scu_total
        stock = br.get("scu_buy")
        qty = min(scu_total, capacity, budget // int(pb),
                  int(stock) if stock else scu_total)
        qty = common.snap_to_crate(qty, br.get("container_sizes"), max_box)
        if qty <= 0:
            continue
        profit = qty * (int(ps) - int(pb))
        if best is None or profit > best[0]:
            best = (profit, cn, int(pb), int(ps), qty, box_sizes, br, stock)
    if best is None:
        return None
    profit, cn, pb, ps, qty, box_sizes, br, stock = best
    is_pyro = getattr(tt, "star_system_name", "").lower() == "pyro"
    if is_pyro and not profile.get("include_pyro"):
        return None
    return {
        "commodity": cn,
        "id_commodity": br.get("id_commodity"),
        "buy_terminal": {"id": ft.id, "name": ft.name,
                         "system": getattr(ft, "star_system_name", "")},
        "sell_terminal": {"id": tt.id, "name": tt.name,
                          "system": getattr(tt, "star_system_name", "")},
        "id_planet_origin": getattr(ft, "id_planet", 0),
        "origin_planet": getattr(ft, "planet_name", ""),
        "id_planet_destination": getattr(tt, "id_planet", 0),
        "destination_planet": getattr(tt, "planet_name", ""),
        "destination_system": getattr(tt, "star_system_name", ""),
        "price_buy": pb, "price_sell": ps,
        "scu_stock": int(stock) if stock else 0,
        "status_buy": br.get("status_buy"),
        "status_sell": None,
        "qty": qty, "box_sizes": box_sizes,
        "leg_profit": profit,
        "roi": round((ps - pb) / pb * 100, 1),
        "pyro": is_pyro,
        "source": "computed-return",
    }


def filter_legs(rows: list[dict], budget: int, profile: dict,
                include_pyro: bool, ctx=None,
                require_auto_load: bool = False) -> list[dict]:
    """原始路线行 → 合法航腿（带数量/利润/箱体/风险注记）。

    ctx 用于可交易性过滤（is_buyable & is_sellable）；为 None 时该过滤自动跳过，
    不会误杀商品（见 common.is_tradeable）。

    require_auto_load: 只保留**买点支持自动装载**（UEX `is_auto_load`）的航腿——
    大船（如 576 SCU 的 Caterpillar）在前哨站手动搬整舱不现实。
    ctx 为 None 时无法判定，跳过该过滤（fail-open，避免误杀全部）。
    """
    auto_load = auto_load_by_id(ctx) if require_auto_load else {}
    scu_total = profile["ship"]["scu_total"]
    origin_planet = profile["origin"]["id_planet"]
    legs = []
    for r in rows:
        if auto_load and not auto_load.get(r.get("id_terminal_origin")):
            continue      # 买点不提供自动装载服务，大船装不了
        try:
            buy = float(r["price_origin"])
            sell = float(r["price_destination"])
        except (KeyError, TypeError, ValueError):
            continue
        if buy <= 0 or sell <= buy:
            continue
        if not r.get("id_planet_destination"):
            # 目的地不挂行星（id=0）的行无法组装循环，跳过
            continue
        if not common.is_tradeable(ctx, r.get("id_commodity")):
            continue

        box_sizes = common.parse_box_sizes(r.get("container_sizes_origin"))
        capacity = common.packable_scu(box_sizes, profile)
        if capacity <= 0:
            continue
        stock = int(r.get("scu_origin") or 0)
        qty = min(scu_total, capacity, budget // int(buy), stock or capacity)
        qty = common.stock_capped_qty(scu_total, qty,
                                      r.get("status_origin"), r.get("status_destination"))
        if qty <= 0:
            continue
        is_pyro = str(r.get("destination_star_system_name", "")).lower() == "pyro"
        if is_pyro and not include_pyro:
            continue
        intra = r.get("id_planet_destination") == r.get("id_planet_origin")
        legs.append({
            "commodity": r.get("commodity_name", ""),
            "id_commodity": r.get("id_commodity"),
            "buy_terminal": {"id": r.get("id_terminal_origin"),
                             "name": r.get("origin_terminal_name", "")},
            "sell_terminal": {"id": r.get("id_terminal_destination"),
                              "name": r.get("destination_terminal_name", "")},
            "id_planet_origin": r.get("id_planet_origin"),
            "origin_planet": r.get("origin_planet_name", ""),
            "id_planet_destination": r.get("id_planet_destination"),
            "destination_planet": r.get("destination_planet_name", ""),
            "destination_system": r.get("destination_star_system_name", ""),
            "price_buy": int(buy),
            "price_sell": int(sell),
            "scu_stock": stock,
            "qty": qty,
            "box_sizes": box_sizes,
            "leg_profit": qty * (int(sell) - int(buy)),
            "roi": round((sell - buy) / buy * 100, 1),
            "intra": intra,
            "pyro": is_pyro,
        })
    legs.sort(key=lambda x: x["leg_profit"], reverse=True)
    return legs


def dedup_planets(legs: list[dict], limit: int) -> list[dict]:
    """按目的地行星去重取前 limit 个（保留每行星利润最高腿）。"""
    seen: set = set()
    out = []
    for leg in legs:
        pid = leg["id_planet_destination"]
        if pid in seen:
            continue
        seen.add(pid)
        out.append(leg)
        if len(out) >= limit:
            break
    return out


COMPUTED_CAP = 32  # 本地配对候选上限（纯本地计算，不增加 API 调用）
TOP_BUYS = 5   # 候选买点数（本地计算，不增加 API 调用）
TOP_SELLS = 5  # 候选卖点数


def computed_legs_from_prices(client, ctx, budget: int, profile: dict,
                              have_triples: set,
                              system_id="auto",
                              require_auto_load: bool = False,
                              sink: dict | None = None) -> list[dict]:
    """本地算路引擎（与 sc-trade.tools 同方法）：一次拉全市场价格，本地配对。

    覆盖 routes 配对接口遗漏的航腿：精炼矿物、卖端饱和下架、跨行星货源。
    每商品取最便宜买点 ×2、最贵卖点 ×3，配对去重后按利润排序。
    """
    from collections import defaultdict
    # "auto"=出发星系；None=不限星系
    if system_id == "auto":
        system_id = profile["origin"].get("id_star_system") or 68
    term_by_id = {t.id: t for t in ctx.cache.terminals} if ctx else {}
    rows = common.throttled_get(client, "commodities_prices_all")
    if sink is not None:
        sink["rows"] = rows          # 供回程腿复用，避免再打一次 API
    by_commodity = defaultdict(list)
    for r in rows or []:
        t = term_by_id.get(r.get("id_terminal"))
        if t is None:
            continue
        if system_id is not None and getattr(t, "id_star_system", 0) != system_id:
            continue
        by_commodity[(r.get("id_commodity"), r.get("commodity_name", ""))].append((r, t))

    scu_total = profile["ship"]["scu_total"]
    legs = []
    excluded = {str(e).lower() for e in (profile.get("excluded_commodities") or [])}
    for (cid, cname), entries in by_commodity.items():
        if not cid or str(cname).lower() in excluded:
            continue
        if not common.is_tradeable(ctx, cid):
            continue      # 只考虑真实可买卖的商品
        buys, sells = [], []
        for r, t in entries:
            try:
                pb = float(r.get("price_buy") or 0)
                ps = float(r.get("price_sell") or 0)
            except (TypeError, ValueError):
                continue
            if pb > 0:      # 起点不绑定：全星系找最优货源
                if require_auto_load and not getattr(t, "is_auto_load", 0):
                    continue      # 买点不提供自动装载服务，大船装不了
                buys.append((pb, r, t))
            if ps > 0:
                sells.append((ps, r, t))
        if not buys or not sells:
            continue
        buys.sort(key=lambda x: x[0])
        sells.sort(key=lambda x: -x[0])
        for pb, br, bt in buys[:TOP_BUYS]:
            for ps, sr, st in sells[:TOP_SELLS]:
                if st.id == bt.id or ps <= pb:
                    continue
                if (cid, bt.id, st.id) in have_triples:
                    continue
                box_sizes = common.parse_box_sizes(br.get("container_sizes"))
                capacity = common.packable_scu(box_sizes, profile)
                if capacity <= 0:
                    if box_sizes:
                        # 有箱型信息但每档都超过本船箱位上限 → 这船根本装不下，
                        # 不能按满载虚估利润（Corundum/Quartz 只有 8/16-SCU 箱、Aurora 上限 2）
                        continue
                    # 无箱型信息：维持满载口径，进游戏以实际为准
                    capacity = scu_total
                stock = br.get("scu_buy")
                qty = min(scu_total, capacity, budget // int(pb),
                          int(stock) if stock else scu_total)
                qty = common.stock_capped_qty(scu_total, qty,
                                              br.get("status_buy"), sr.get("status_sell"))
                # 只按整箱卖：向下取整到箱型（如 Osoian Hides 仅 4-SCU 箱）
                qty = common.snap_to_crate(qty, br.get("container_sizes"),
                                           (profile.get("ship") or {}).get("max_box", 32))
                if qty <= 0:
                    continue
                is_pyro = getattr(st, "star_system_name", "").lower() == "pyro"
                if is_pyro and not profile.get("include_pyro"):
                    continue
                legs.append({
                    "commodity": cname, "id_commodity": cid,
                    "buy_terminal": {"id": bt.id, "name": bt.name},
                    "sell_terminal": {"id": st.id, "name": st.name},
                    "id_planet_origin": bt.id_planet,
                    "origin_planet": getattr(bt, "planet_name", ""),
                    "id_planet_destination": st.id_planet,
                    "destination_planet": getattr(st, "planet_name", ""),
                    "destination_system": getattr(st, "star_system_name", ""),
                    "price_buy": int(pb), "price_sell": int(ps),
                    "scu_stock": int(stock) if stock else 0,
                    "status_buy": br.get("status_buy"),
                    "status_sell": sr.get("status_sell"),
                    "qty": qty, "box_sizes": box_sizes,
                    "leg_profit": qty * (int(ps) - int(pb)),
                    "roi": round((ps - pb) / pb * 100, 1),
                    "intra": bt.id_planet == st.id_planet,
                    "pyro": is_pyro,
                    "source": "computed",
                })
    legs.sort(key=lambda x: x["leg_profit"], reverse=True)
    return legs[:COMPUTED_CAP]


def find_return_leg(client, origin_planet: int, budget: int,
                    dest_planet: int,
                    prefer_terminal_id: int | None = None) -> tuple[dict | None, bool]:
    """从目的地行星找回出发行星的盈利腿。

    优先选「卖点 == 头段买点」的腿（真闭环）；没有则取同行星内利润最高的
    （需摆渡）。返回 (行, 是否同终端闭合)。
    """
    rows = fetch_routes(client, dest_planet, budget)
    exact, fallback = None, None
    for r in rows:
        if r.get("id_planet_destination") != origin_planet:
            continue
        try:
            buy = float(r["price_origin"])
            sell = float(r["price_destination"])
        except (TypeError, ValueError):
            continue
        if buy <= 0 or sell <= buy:
            continue
        better = lambda cand: cand is None or (sell - buy) > (
            float(cand["price_destination"]) - float(cand["price_origin"]))
        if prefer_terminal_id and r.get("id_terminal_destination") == prefer_terminal_id:
            if better(exact):
                exact = r
        elif better(fallback):
            fallback = r
    if exact is not None:
        return exact, True
    return fallback, False


def make_return_leg(row: dict, budget: int, profile: dict) -> dict | None:
    """把返程原始行整理成 leg 结构（不做箱体强校验，宽松处理）。"""
    buy = int(float(row["price_origin"]))
    sell = int(float(row["price_destination"]))
    if buy <= 0 or sell <= buy:
        return None
    stock = int(row.get("scu_origin") or 0)
    qty = min(profile["ship"]["scu_total"], budget // buy, stock or profile["ship"]["scu_total"])
    if qty <= 0:
        return None
    return {
        "commodity": row.get("commodity_name", ""),
        "id_commodity": row.get("id_commodity"),
        "buy_terminal": {"id": row.get("id_terminal_origin"),
                         "name": row.get("origin_terminal_name", "")},
        "sell_terminal": {"id": row.get("id_terminal_destination"),
                          "name": row.get("destination_terminal_name", "")},
        "id_planet_destination": row.get("id_planet_destination"),
        "destination_planet": row.get("destination_planet_name", ""),
        "destination_system": row.get("destination_star_system_name", ""),
        "price_buy": buy,
        "price_sell": sell,
        "scu_stock": stock,
        "qty": qty,
        "box_sizes": common.parse_box_sizes(row.get("container_sizes_origin")),
        "leg_profit": qty * (sell - buy),
        "roi": round((sell - buy) / buy * 100, 1),
        "intra": True,
        "pyro": False,
    }


def snapshot_of(leg: dict, source: str) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "price_buy": leg["price_buy"],
        "price_sell": leg["price_sell"],
        "scu_stock": leg["scu_stock"],
        "source": source,
    }


def annotate_terminals(legs: list[dict], ctx, freight_by_id: dict | None = None,
                      profile: dict | None = None) -> None:
    """给每个终端标注自动装载能力：以「有无机库」为准。

    游戏内需先在机库叫出飞船、再用货运电梯自动装箱；地表前哨无机库 → 手搬。
    优先级：用户实测覆盖 > 机库判定（id_space_station / id_city）。
    """
    if ctx is None:
        return
    meta: dict = {}
    for t in getattr(ctx.cache, "terminals", []) or []:
        mcs = getattr(t, "max_container_size", 0) or 0
        # 有机库（空间站/城市）才能叫船 + 用货运电梯 → 自动装载；地表前哨无库 → 只能手搬。
        # 权威判据是 UEX 的 is_auto_load（用户定案 2026-10-29）；该字段缺失时才回退机库判据。
        has_hangar = bool(getattr(t, "id_space_station", 0) or getattr(t, "id_city", 0))
        ial = getattr(t, "is_auto_load", None)
        can_auto_load = bool(ial) if ial is not None else has_hangar
        meta[t.id] = {"auto": can_auto_load, "max_box": mcs or 32,
                      "hangar": has_hangar,
                      "system": getattr(t, "star_system_name", "") or ""}
    # 用户实测覆盖（权威最高）→ UEX 数据 → 名称启发式
    overrides = (profile or {}).get("loading_overrides") or {}
    for leg in legs:
        for side in ("buy_terminal", "sell_terminal"):
            term = leg.get(side, {})
            name = term.get("name", "")
            ov = next((v for k, v in overrides.items() if k.lower() in name.lower()), None)
            m = meta.get(term.get("id"))
            if ov is not None:
                term["auto"] = str(ov).lower() == "auto"
            elif m:
                term["auto"] = m["auto"]
            if m:
                term["max_box"] = m["max_box"]
                term["system"] = m.get("system", "")
            term["loading_source"] = ("override" if ov is not None
                                      else ("hangar" if m else "heuristic"))


def build_routes(legs: list[dict], budget: int, profile: dict,
                 client, max_calls: int, ship_key: str = "",
                 max_routes: int = 6, ctx=None,
                 require_auto_load: bool = False,
                 price_rows: list | None = None,
                 term_by_id: dict | None = None) -> list[dict]:
    """组装简单循环并生成 watchlist route 结构。

    组合多样性：同一供货源（同商品+同买点）最多 2 条路线——同源价格完全联动，
    超量收录没有备选对冲价值。
    """
    ctx = None
    try:
        ctx = common.build_ctx()
    except Exception:
        pass

    routes = []
    calls_left = max_calls - 1  # 第一次路线查询已用掉 1
    intra_seen = set()
    source_count: dict = {}

    for leg in legs[:COMPUTED_CAP]:
        source = (leg["id_commodity"], leg["buy_terminal"]["id"])
        if source_count.get(source, 0) >= 2:
            continue
        if leg["intra"]:
            key = (leg["buy_terminal"]["id"], leg["sell_terminal"]["id"])
            if key in intra_seen:
                continue
            intra_seen.add(key)
            legs_pair = [leg]
            loop_type = "intra"
        else:
            if calls_left <= 0:
                legs_pair = [leg]
                loop_type = "one-way"
            else:
                back = None
                # 先用本地全市场价找回程（覆盖 routes 接口漏掉的商品，且不额外调 API）
                if price_rows and term_by_id:
                    back = find_return_leg_local(
                        price_rows, term_by_id,
                        leg["sell_terminal"]["id"], leg["buy_terminal"]["id"],
                        budget, profile, ctx,
                        require_auto_load=require_auto_load)
                    if back is not None:
                        back["closed_terminal"] = True   # 回到头段买点，真闭环
                if back is None and calls_left > 0:
                    calls_left -= 1
                    row, closed_terminal = find_return_leg(
                        client,
                        leg.get("id_planet_origin") or profile["origin"]["id_planet"],
                        budget, leg["id_planet_destination"],
                        prefer_terminal_id=leg["buy_terminal"]["id"])
                    back = make_return_leg(row, budget, profile) if row else None
                    if back is not None and require_auto_load:
                        # 回程买点也必须能自动装载，否则大船装不了
                        if auto_load_by_id(ctx) and not auto_load_by_id(ctx).get(
                                back["buy_terminal"]["id"]):
                            back = None
                    if back is not None:
                        back["closed_terminal"] = closed_terminal
                legs_pair = [leg] + ([back] if back else [])
                loop_type = "there-back" if back else "one-way"

        # 资金流模拟：每段买入花掉手中的钱，卖出回款后才能买下一段
        cash = budget
        scu_cap = (profile.get("ship") or {}).get("scu_total", 8)
        for x in legs_pair:
            buy = int(x["price_buy"] or 0)
            if buy > 0:
                affordable = cash // buy
                if affordable < x["qty"]:
                    x["qty"] = max(0, int(affordable))
                elif not x.get("scu_stock") and affordable > x["qty"]:
                    # 回程用回款全款买：库存未知时最多补到船舱上限
                    x["qty"] = min(int(scu_cap), int(affordable))
                x["leg_profit"] = x["qty"] * (int(x["price_sell"] or 0) - buy)
            cash += x["leg_profit"]
        legs_pair = [x for x in legs_pair if x.get("qty", 0) > 0]
        if not legs_pair:
            continue
        route = {
            "id": None,  # 由调用方分配
            "type": loop_type,
            "status": "candidate",
            "risk": "pyro" if leg["pyro"] else "normal",
            "ship": ship_key,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "legs": legs_pair,
            "loop_profit": sum(x["leg_profit"] for x in legs_pair),
        }
        source_count[source] = source_count.get(source, 0) + 1
        tm = profile.get("time_model") or {}
        fallback_min = float(tm.get("fallback_leg_min", 15.0))
        jump_min = float(tm.get("jump_gate_min", 5.0))
        seconds_per_gm = float(tm.get("seconds_per_gm", 5.0))
        for x in legs_pair:
            x["qt_sec"] = common.travel_seconds(
                ctx, x["buy_terminal"]["name"], x["buy_terminal"].get("system"),
                x["sell_terminal"]["name"], x["sell_terminal"].get("system"),
                seconds_per_gm=seconds_per_gm, jump_min=jump_min,
                fallback_min=fallback_min)
            if x["buy_terminal"].get("system") and x["sell_terminal"].get("system") \
                    and x["buy_terminal"]["system"] != x["sell_terminal"]["system"]:
                x["cross_system"] = True
                # 展示规范：每个终端附位置路径（全名不缩写）
                for side in ("buy_terminal", "sell_terminal"):
                    x[side]["path"] = common.terminal_path(ctx, x[side].get("id"))
        route["qt_sec_total"] = sum(x["qt_sec"] or 0 for x in legs_pair) or None
        # 闭环校验：尾段卖点应回到头段买点，否则补一段摆渡（量子时间）
        first_buy = legs_pair[0]["buy_terminal"]
        last_sell = legs_pair[-1]["sell_terminal"]
        # 段间转场：上一段卖点 → 下一段买点（多段路线必有的空驶/转场）
        transit_sec = 0.0
        for prev_, nxt_ in zip(legs_pair, legs_pair[1:]):
            if prev_["sell_terminal"]["id"] != nxt_["buy_terminal"]["id"]:
                t = common.travel_seconds(
                    ctx, prev_["sell_terminal"]["name"],
                    prev_["sell_terminal"].get("system"),
                    nxt_["buy_terminal"]["name"],
                    nxt_["buy_terminal"].get("system"),
                    seconds_per_gm=seconds_per_gm, jump_min=jump_min,
                    fallback_min=fallback_min)
                transit_sec += t or 0.0

        route["transit_sec"] = transit_sec or None
        repo_sec = None
        if last_sell["id"] != first_buy["id"]:
            # 未回到起点终端：补一段回起点的时间（有货=摆渡，单程=空载返回）
            repo_sec = common.travel_seconds(
                ctx, last_sell["name"], last_sell.get("system"),
                first_buy["name"], first_buy.get("system"),
                seconds_per_gm=seconds_per_gm, jump_min=jump_min,
                fallback_min=fallback_min)
        route["closed"] = last_sell["id"] == first_buy["id"]
        route["reposition_sec"] = repo_sec
        route["reposition_kind"] = (None if route["closed"]
                                    else ("empty-return" if len(legs_pair) == 1
                                          else "ferry"))
        route["loop_time_min"] = common.route_time_min(
            profile, legs_pair,
            extra_min=(repo_sec or 0) / 60 + transit_sec / 60)
        route["profit_per_hour"] = (int(route["loop_profit"] / route["loop_time_min"] * 60)
                                    if route["loop_time_min"] else None)
        route["snapshots"] = [snapshot_of(leg, "uex") for leg in legs_pair]
        routes.append(route)
        if len(routes) >= max_routes:
            break
    return routes


def merge_watchlist(watchlist: dict, new_routes: list[dict]) -> tuple[dict, int]:
    """推荐即重建组合：本次刷新过的路线保留更新，未刷新的活跃旧路线标记 retired。

    （跨船/跨预算的旧路线不得滞留活跃组合——不同船的利润不可比。）
    """
    def key_of(route):
        l0 = route["legs"][0]
        return (l0["buy_terminal"]["id"], l0["sell_terminal"]["id"], l0["id_commodity"])

    existing = {key_of(r): r for r in watchlist["routes"] if r["status"] != "retired"}
    seen_keys = set()
    added = 0
    for route in new_routes:
        k = key_of(route)
        seen_keys.add(k)
        if k in existing:
            old = existing[k]
            old["legs"] = route["legs"]
            old["loop_profit"] = route["loop_profit"]
            old["qt_sec_total"] = route.get("qt_sec_total")
            old["loop_time_min"] = route.get("loop_time_min")
            old["profit_per_hour"] = route.get("profit_per_hour")
            old["ship"] = route.get("ship", old.get("ship"))
            continue
        watchlist["next_id"] = watchlist.get("next_id", 1)
        route["id"] = f"r{watchlist['next_id']}"
        watchlist["next_id"] += 1
        watchlist["routes"].append(route)
        existing[k] = route
        added += 1
    # 未被本次刷新的活跃路线 → retired（历史快照保留在文件里）
    for k, old in existing.items():
        if k not in seen_keys:
            old["status"] = "retired"
    return watchlist, added


def render_watchlist_md(watchlist: dict, budget: int) -> str:
    """展示规范（SKILL.md 展示规范节）：结构化块、终端全名、位置路径、不缩写。"""
    active = [r for r in watchlist["routes"] if r["status"] != "retired"]
    best_id = max(active, key=lambda r: r["loop_profit"])["id"] if active else None
    lines = [f"**预算 {budget:,} aUEC · 生成于 {watchlist['updated_at']}**", ""]
    for r in active:
        role = "★首选" if r["id"] == best_id else "备选"
        time_txt = f"{r['loop_time_min']:.0f}min" if r.get("loop_time_min") else "n/a"
        hour_txt = f"{r['profit_per_hour']:,}/h" if r.get("profit_per_hour") else "n/a"
        lines.append(f"### {r['id']} {role} · {r['type']} · 风险 {r['risk']}")
        lines.append(f"圈利润 **+{r['loop_profit']:,}** · 圈时间 {time_txt} · 时薪 {hour_txt}")
        for i, l in enumerate(r["legs"]):
            bp = l["buy_terminal"].get("path") or ""
            sp = l["sell_terminal"].get("path") or ""
            lines.append(
                f"- 航段{i + 1}：买 {l['commodity']} {l['price_buy']:,}/SCU × {l['qty']} SCU"
                f" @ {l['buy_terminal']['name']}" + (f" [{bp}]" if bp else ""))
            lines.append(
                f"  → 卖 {l['price_sell']:,}/SCU @ {l['sell_terminal']['name']}"
                + (f" [{sp}]" if sp else ""))
            extra = []
            if l.get("scu_stock"):
                extra.append(f"库存 {l['scu_stock']}")
            if l.get("partial"):
                extra.append("部分装载")
            if l.get("qt_sec"):
                extra.append(f"量子 {l['qt_sec'] / 60:.0f}min")
            lines.append(f"  本段利润 +{l['leg_profit']:,}"
                         + (f"（{'，'.join(extra)}）" if extra else ""))
        lines.append("")
    return "\n".join(lines)


AUTO_BEGIN = "<!-- sc-trader:auto-begin -->"
AUTO_END = "<!-- sc-trader:auto-end -->"


def update_docs(routes_md: str) -> None:
    docs = common.REPO_ROOT / "docs" / "routes.md"
    text = docs.read_text(encoding="utf-8") if docs.exists() else ""
    block = f"{AUTO_BEGIN}\n### 监控中的路线\n\n{routes_md}\n{AUTO_END}"
    if AUTO_BEGIN in text and AUTO_END in text:
        pre = text.split(AUTO_BEGIN)[0]
        post = text.split(AUTO_END)[1]
        text = pre + block + post
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    docs.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--budget", type=int, default=None)
    parser.add_argument("--ship", default=None,
                        help="船型 key（profile.json ships 里的键，默认 default_ship）")
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--include-pyro", action="store_true")
    parser.add_argument("--system", default=None,
                        help="限定星系（默认 profile 出发星系；传 all 不限）")
    parser.add_argument("--auto-loading", action="store_true",
                        help="按自动装载估算时间（0.5min/次固定开销）；"
                             "不给则按 profile.time_model.auto_loading（默认手动 1min/SCU）")
    parser.add_argument("--max-stops", type=int, default=None,
                        help="启用闭环搜索：2=来回，3=三角，最大 6。不给则走单腿+回程的旧逻辑")
    parser.add_argument("--mixed", action="store_true",
                        help="闭环搜索时每段允许混装多种商品填满货舱")
    parser.add_argument("--min-supply-ratio", type=float, default=1.0,
                        help="闭环搜索：买点现货至少够跑几圈")
    parser.add_argument("--sort", choices=["profit", "hour"], default="profit")
    parser.add_argument("--detail", action="store_true",
                        help="闭环搜索时展开逐段耗时/供应/需求")
    parser.add_argument("--auto-load-only", action="store_true",
                        help="只保留买点支持自动装载（UEX is_auto_load）的航腿。"
                             "大船（如 576 SCU）在前哨站手动搬整舱不现实时用")
    parser.add_argument("--max-calls", type=int, default=6,
                        help="本次最多 UEX API 调用数（限速保护）")
    args = parser.parse_args()

    profile = common.load_profile()
    ship_key = args.ship or profile.get("default_ship")
    ships = profile.get("ships") or {}
    if ship_key not in ships:
        print(f"ERROR: 未知船型 '{ship_key}'，可选：{', '.join(ships) or '(profile 未定义 ships)'}",
              file=sys.stderr)
        return 1
    profile["ship"] = ships[ship_key]
    ship_name = ships[ship_key].get("name", ship_key)
    budget = args.budget or profile["budget_default"]
    if args.include_pyro:
        profile["include_pyro"] = True
    if args.auto_loading:
        profile.setdefault("time_model", {})["auto_loading"] = True

    if args.max_stops:
        # 闭环搜索模式（原 find_loop.py）：直接搜能回到起点的环
        return run_loop_search(args)

    client = common.get_client()
    watchlist = common.load_watchlist()
    origin_planet = profile["origin"]["id_planet"]
    try:
        rows = fetch_routes(client, origin_planet, budget)
    except Exception as e:
        print(f"WARN: routes 接口失败（{str(e)[:120]}），走本地算路引擎", file=sys.stderr)
        rows = []

    freight_by_id: dict = {}
    for r in rows or []:
        if r.get("id_terminal_origin") is not None:
            freight_by_id[r["id_terminal_origin"]] = r.get("has_freight_elevator_origin")
        if r.get("id_terminal_destination") is not None:
            freight_by_id[r["id_terminal_destination"]] = r.get("has_freight_elevator_destination")

    ctx = None
    try:
        ctx = common.build_ctx()
    except Exception:
        pass
    legs = filter_legs(rows or [], budget, profile, profile["include_pyro"], ctx,
                      require_auto_load=args.auto_load_only)
    have_triples = {(l["id_commodity"], l["buy_terminal"]["id"], l["sell_terminal"]["id"])
                    for l in legs}
    want_system = None if (args.system or "").lower() == "all" else (
        int(args.system) if (args.system or "").isdigit()
        else (profile["origin"].get("id_star_system") or 68))
    api_error = False
    try:
        computed_sink: dict = {}
        term_by_id = ({t.id: t for t in ctx.cache.terminals}
                      if ctx else {})
        computed = computed_legs_from_prices(client, ctx, budget, profile,
                                             have_triples, system_id=want_system,
                                             require_auto_load=args.auto_load_only,
                                             sink=computed_sink)
    except Exception as e:
        api_error = True
        print(f"WARN: 本地算路失败（{str(e)[:120]}）", file=sys.stderr)
        computed = []
    if computed:
        print(f"[computed] 本地算路补充 {len(computed)} 条航腿（sc-trade.tools 同方法，价格源 UEX）",
              file=sys.stderr)
        legs = sorted(legs + computed, key=lambda x: x["leg_profit"], reverse=True)
    annotate_terminals(legs, ctx, freight_by_id, profile)
    if not legs:
        print("ERROR: 过滤后无可用航腿。", file=sys.stderr)
        if api_error:
            print("API 访问异常（可能被临时 403 封禁）："
                  "等待 10-30 分钟冷却，或在 profile.json 配置 uexcorp_api_token"
                  "（https://uexcorp.space/api/apps 申请，限额更高）后重试。", file=sys.stderr)
        return 1

    new_routes = build_routes(legs, budget, profile, client, args.max_calls,
                              ctx=ctx, require_auto_load=args.auto_load_only,
                              price_rows=computed_sink.get("rows"),
                              term_by_id=term_by_id,
                              ship_key=ship_key, max_routes=max(1, args.top))
    watchlist, added = merge_watchlist(watchlist, new_routes)
    # 登记跟踪商品：下次即使被 routes 接口下架，也能走部分装载补充路径
    tracked = watchlist.setdefault("tracked_commodities", {})
    for r in watchlist["routes"]:
        for l in r["legs"]:
            if l.get("id_commodity"):
                tracked[str(l["id_commodity"])] = l["commodity"]
    # 组合重建后重置调仓基线：当前最优即起始首选
    active = [r for r in watchlist["routes"] if r["status"] != "retired"]
    if active:
        watchlist["ranking_prev"] = [r["id"] for r in
                                     sorted(active, key=lambda x: x["loop_profit"],
                                            reverse=True)]
    common.save_watchlist(watchlist)
    update_docs(render_watchlist_md(watchlist, budget))

    print(f"[ship: {ship_name}] 新增 {added} 条路线，watchlist 共 "
          f"{sum(1 for r in watchlist['routes'] if r['status'] != 'retired')} 条有效")
    print()
    print(render_watchlist_md(watchlist, budget))
    return 0

# ── 闭环搜索（原 find_loop.py 合并进来的能力）─────────────────────────────
import argparse
import sys
from collections import defaultdict

C = common  # find_loop 合并后复用 recommend 的 common 别名

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


def mixed_leg_for(buy_at, sell_at, tid_a, tid_b, budget: int, profile: dict,
                  ctx=None, min_supply_ratio: float = 1.0,
                  require_auto_load: bool = False) -> dict | None:
    """混装：在 A 买**多种**商品、B 卖，按单位利润贪心把货舱填满。

    单商品腿（leg_for）在库存不足时会空舱飞（Cobalt 只剩 57 SCU 而货舱 120）。
    混装把剩余舱位用次优商品补上。返回结构与单商品腿一致，另带 items 列表。
    """
    ta = {t.id: t for t in ctx.cache.terminals}.get(tid_a) if ctx is not None else None
    if require_auto_load and ta is not None and not getattr(ta, "is_auto_load", 0):
        return None
    scu_total = profile["ship"]["scu_total"]
    max_box = (profile.get("ship") or {}).get("max_box", 32)
    cands = []
    for cn in buy_at.get(tid_a, {}):
        got = sell_at.get(tid_b, {}).get(cn)
        if not got:
            continue
        one = leg_for(buy_at, sell_at, got[0], tid_a, tid_b, cn, budget, profile,
                      min_supply_ratio=min_supply_ratio, ctx=ctx)
        if one:
            cands.append(one)
    if not cands:
        return None
    cands.sort(key=lambda g: -(g["price_sell"] - g["price_buy"]))
    remaining, cash = scu_total, budget
    items = []
    for g in cands:
        if remaining <= 0:
            break
        take = min(g["qty"], remaining, cash // g["price_buy"] if g["price_buy"] else 0)
        take = C.snap_to_crate(take, g["box_sizes"], max_box)
        if take <= 0:
            continue
        items.append({**g, "qty": take,
                      "leg_profit": take * (g["price_sell"] - g["price_buy"])})
        remaining -= take
        cash -= take * g["price_buy"]
    if not items:
        return None
    head = items[0]
    return {
        **head,
        "commodity": " + ".join(i["commodity"] for i in items) if len(items) > 1
                     else head["commodity"],
        "qty": sum(i["qty"] for i in items),
        "leg_profit": sum(i["leg_profit"] for i in items),
        "items": items,
        "mixed": len(items) > 1,
    }



def run_loop_search(args) -> int:
    ap = argparse.ArgumentParser(description="搜索多角贸易闭环")
    ap.add_argument("--ship", default=None)
    ap.add_argument("--budget", type=int, default=None)
    ap.add_argument("--max-stops", type=int, default=3,
                    help="最多几角（2=来回，3=三角，最大 5）")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--min-supply-ratio", type=float, default=1.0,
                    help="买点现货至少要够跑几圈（默认 1；2=至少两圈的量）")
    ap.add_argument("--mixed", action="store_true",
                    help="允许每段混装多种商品填满货舱（默认每段只装一种）")
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
        """航程秒数。跨星系必须走 travel_seconds（飞到星门 → 穿越 → 对方星门飞出），
        qt_seconds 只在同一张交通图里找路，会把跨星系算成直达、漏掉星门耗时。"""
        if (a, b) not in qt_cache:
            ta, tb = term_by_id[a], term_by_id[b]
            qt_cache[(a, b)] = C.travel_seconds(
                ctx, ta.name, getattr(ta, "star_system_name", ""),
                tb.name, getattr(tb, "star_system_name", "")) or 0
        return qt_cache[(a, b)]

    def auto_load_ok(tid):
        return bool(getattr(term_by_id[tid], "is_auto_load", 0))

    # 起点：该星系内有可买商品、且（若要求）能自动装载的终端
    starts = [tid for tid in buy_at
              if (not args.auto_load_only or auto_load_ok(tid))]
    print(f"起点候选 {len(starts)} 个（星系 {system_id}），搜索 {args.max_stops} 角闭环…",
          file=sys.stderr)

    results = []
    mixed = args.mixed
    for start in starts:
        # beam: (profit, time_sec, path_tids, legs)
        beam = [(0, 0, [start], [])]
        for depth in range(args.max_stops):
            nxt = []
            for profit, secs, path, legs in beam:
                cur = path[-1]
                cands = []
                cands = []
                if mixed:
                    # 混装：按目的站聚合，每个目的站一次算出最优商品组合
                    dests = set()
                    for cn in buy_at.get(cur, {}):
                        for tid_b, ps in sell_index.get(cn, []):
                            if tid_b == cur or tid_b in path:
                                continue
                            dests.add(tid_b)
                    for tid_b in dests:
                        if args.auto_load_only and not auto_load_ok(cur):
                            continue
                        lg = mixed_leg_for(buy_at, sell_at, cur, tid_b, budget, profile,
                                           ctx, args.min_supply_ratio, args.auto_load_only)
                        if lg:
                            cands.append((lg["leg_profit"], tid_b, lg))
                else:
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
                if mixed:
                    close_legs = [mixed_leg_for(buy_at, sell_at, cur, start, budget, profile,
                                                ctx, args.min_supply_ratio,
                                                args.auto_load_only)]
                else:
                    close_legs = []
                    for cn, (ps, _r) in sell_at.get(start, {}).items():
                        close_legs.append(
                            leg_for(buy_at, sell_at, ps, cur, start, cn, budget, profile,
                                    min_supply_ratio=args.min_supply_ratio, ctx=ctx))
                for lg in close_legs:
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
                                 "auto": bool(getattr(ta, "is_auto_load", 0)),
                                 "is_city": bool(getattr(ta, "id_city", 0))},
                "sell_terminal": {"name": tb.name,
                                  "system": getattr(tb, "star_system_name", ""),
                                  "auto": bool(getattr(tb, "is_auto_load", 0)),
                                  "is_city": bool(getattr(tb, "id_city", 0))},
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
        print("| \u6bb5 | \u5546\u54c1 | \u4e70 \u2192 \u5356 | \u5229\u6da6 | \u4f9b\u5e94 | \u9700\u6c42(\u56e4) |")
        print("|---|---|---|---|---|---|")
        for n, (lg, d) in enumerate(zip(r["legs"], r["detail"]), 1):
            bn = term_by_id[lg["buy_terminal"]].name
            sn = term_by_id[lg["sell_terminal"]].name
            sup = d.get("supply")
            dst = d.get("demand_stock")
            print(f"| {n} | {lg['commodity']} {lg['qty']} SCU | "
                  f"{bn} {lg['price_buy']:,} \u2192 {sn} {lg['price_sell']:,} | "
                  f"+{lg['leg_profit']:,} | "
                  f"{sup if sup is not None else '-'}({st_name(d.get('buy_status'))}) | "
                  f"{dst if dst is not None else '-'}({st_name(d.get('sell_status'))}) |")
        print(f"| | | **\u5408\u8ba1** | **+{r['profit']:,}** | | |")
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
