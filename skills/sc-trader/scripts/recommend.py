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

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402


def fetch_routes(client, id_planet: int, budget: int) -> list[dict]:
    rows = common.throttled_get(client, "commodities_routes", {
        "id_planet_origin": id_planet,
        "investment": budget,
    })
    return rows or []


def filter_legs(rows: list[dict], budget: int, profile: dict,
                include_pyro: bool) -> list[dict]:
    """原始路线行 → 合法航腿（带数量/利润/箱体/风险注记）。"""
    scu_total = profile["ship"]["scu_total"]
    origin_planet = profile["origin"]["id_planet"]
    legs = []
    for r in rows:
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
        box_sizes = common.parse_box_sizes(r.get("container_sizes_origin"))
        capacity = common.packable_scu(box_sizes, profile)
        if capacity <= 0:
            continue
        stock = int(r.get("scu_origin") or 0)
        qty = min(scu_total, capacity, budget // int(buy), stock or capacity)
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
                              have_triples: set) -> list[dict]:
    """本地算路引擎（与 sc-trade.tools 同方法）：一次拉全市场价格，本地配对。

    覆盖 routes 配对接口遗漏的航腿：精炼矿物、卖端饱和下架、跨行星货源。
    每商品取最便宜买点 ×2、最贵卖点 ×3，配对去重后按利润排序。
    """
    from collections import defaultdict
    system_id = profile["origin"].get("id_star_system") or 68
    term_by_id = {t.id: t for t in ctx.cache.terminals} if ctx else {}
    rows = common.throttled_get(client, "commodities_prices_all")
    by_commodity = defaultdict(list)
    for r in rows or []:
        t = term_by_id.get(r.get("id_terminal"))
        if t is None or getattr(t, "id_star_system", 0) != system_id:
            continue
        by_commodity[(r.get("id_commodity"), r.get("commodity_name", ""))].append((r, t))

    scu_total = profile["ship"]["scu_total"]
    legs = []
    for (cid, cname), entries in by_commodity.items():
        if not cid:
            continue
        buys, sells = [], []
        for r, t in entries:
            try:
                pb = float(r.get("price_buy") or 0)
                ps = float(r.get("price_sell") or 0)
            except (TypeError, ValueError):
                continue
            if pb > 0:      # 起点不绑定：全星系找最优货源
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
                capacity = common.packable_scu(box_sizes, profile) or scu_total
                qty = min(scu_total, capacity, budget // int(pb))
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
                    "scu_stock": 0,
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
        # 有机库（空间站/城市）才能叫船 + 用货运电梯 → 自动装载；
        # 地表前哨无库 → 只能手搬
        has_hangar = bool(getattr(t, "id_space_station", 0) or getattr(t, "id_city", 0))
        meta[t.id] = {"auto": has_hangar, "max_box": mcs or 32,
                      "hangar": has_hangar}
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
            term["loading_source"] = ("override" if ov is not None
                                      else ("hangar" if m else "heuristic"))


def build_routes(legs: list[dict], budget: int, profile: dict,
                 client, max_calls: int, ship_key: str = "",
                 max_routes: int = 6) -> list[dict]:
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
                calls_left -= 1
                row, closed_terminal = find_return_leg(
                    client,
                    leg.get("id_planet_origin") or profile["origin"]["id_planet"],
                    budget, leg["id_planet_destination"],
                    prefer_terminal_id=leg["buy_terminal"]["id"])
                back = make_return_leg(row, budget, profile) if row else None
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
        for x in legs_pair:
            x["qt_sec"] = None
            if ctx is not None:
                x["qt_sec"] = common.qt_seconds(
                    ctx, x["buy_terminal"]["name"], x["sell_terminal"]["name"])
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
                t = (common.qt_seconds(ctx, prev_["sell_terminal"]["name"],
                                       nxt_["buy_terminal"]["name"])
                     if ctx is not None else None)
                transit_sec += t or 0.0
        route["transit_sec"] = transit_sec or None
        repo_sec = None
        if last_sell["id"] != first_buy["id"]:
            # 未回到起点终端：补一段回起点的时间（有货=摆渡，单程=空载返回）
            repo_sec = (common.qt_seconds(ctx, last_sell["name"], first_buy["name"])
                        if ctx is not None else None)
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
    parser.add_argument("--auto-loading", action="store_true",
                        help="按自动装载估算时间（0.5min/次固定开销）；"
                             "不给则按 profile.time_model.auto_loading（默认手动 1min/SCU）")
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

    legs = filter_legs(rows or [], budget, profile, profile["include_pyro"])
    ctx = None
    try:
        ctx = common.build_ctx()
    except Exception:
        pass
    have_triples = {(l["id_commodity"], l["buy_terminal"]["id"], l["sell_terminal"]["id"])
                    for l in legs}
    api_error = False
    try:
        computed = computed_legs_from_prices(client, ctx, budget, profile, have_triples)
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


if __name__ == "__main__":
    raise SystemExit(main())
