"""sc-trader 截图读数入库：把 Claude 从用户截图读出的价格写入玩家扫描价存储。

用法（会话内由 Claude 视觉读图后调用）：
    tools/uexinfo/venv/bin/python skills/sc-trader/scripts/record.py \
        --commodity "Medical Supplies" \
        --terminal "Rayari Kaltag Research Outpost" \
        --price-buy 3343 [--price-sell 5500] [--qty 8] [--source screenshot]

写入 uexinfo 的 scan_prices.json（与 ScanPriceStore.save_result 同格式），
此后 update_prices.py / uexinfo overlay 的价格链会自动以玩家扫描价优先（★）。
同时刷新 watchlist 中匹配航腿的价格并追加快照。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402


def fuzzy_pick(candidates: list, query: str, label: str):
    """名称模糊匹配；多义时按匹配紧密度排序取最优（精确>后缀>前缀>最短名）。"""
    q = query.strip().lower()

    def score(name: str) -> tuple:
        n = name.lower()
        if n == q:
            return (0, len(name))
        if n.endswith(q):
            return (1, len(name))
        if n.startswith(q):
            return (2, len(name))
        if q in n:
            return (3, len(name))
        return (9, len(name))

    matched = sorted((c for c in candidates if q in getattr(c, "name", "").lower()),
                     key=lambda c: score(c.name))
    if not matched:
        print(f"ERROR: {label} '{query}' 未找到（缓存中共 {len(candidates)} 条）", file=sys.stderr)
        raise SystemExit(1)
    top = score(matched[0].name)
    ties = [c for c in matched if score(c.name) == top]
    if len(ties) > 1:
        print(f"ERROR: {label} '{query}' 有多个同级匹配："
              + "; ".join(c.name for c in ties[:8]), file=sys.stderr)
        raise SystemExit(1)
    return matched[0]


def write_scan(terminal_id: int, terminal_name: str, commodity_id: int,
               commodity_name: str, price_buy, price_sell, qty, source: str) -> None:
    """按 ScanPriceStore.save_result 的 JSON 格式写入玩家扫描价。"""
    from uexinfo.cache.scan_prices import ScanPriceStore
    store = ScanPriceStore()
    data = store._load()
    term_key = str(terminal_id)          # 与 migrate_keys 后的键格式一致
    cid_key = str(commodity_id)
    data.setdefault(term_key, {})
    entry = data[term_key].get(cid_key, {})
    entry.update({
        "commodity_name": commodity_name,
        "commodity_id": commodity_id,
        "timestamp": time.time(),
        "validated": True,
        "_source": source,
        "_terminal_name": terminal_name,
    })
    if price_buy:
        entry["price_buy"] = int(price_buy)
    if price_sell:
        entry["price_sell"] = int(price_sell)
    if qty:
        entry["scu_buy"] = int(qty)
    data[term_key][cid_key] = entry
    store._write(data)


def update_watchlist(terminal_id: int, terminal_name: str, commodity_id: int,
                     price_buy, price_sell, source: str) -> int:
    """刷新 watchlist 中匹配航腿并追加快照；返回更新的航腿数。"""
    watchlist = common.load_watchlist()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    touched = 0
    for route in watchlist["routes"]:
        if route["status"] == "retired":
            continue
        for leg in route["legs"]:
            if int(leg.get("id_commodity") or 0) != int(commodity_id):
                continue
            is_buy_side = int(leg["buy_terminal"]["id"] or 0) == int(terminal_id)
            is_sell_side = int(leg["sell_terminal"]["id"] or 0) == int(terminal_id)
            if not (is_buy_side or is_sell_side):
                continue
            # 买端观察刷新买入价；卖端观察刷新卖出价
            fields = (("price_buy", price_buy), ("price_sell", price_sell)) \
                if is_buy_side else (("price_sell", price_sell),)
            for field, new in fields:
                old = leg.get(field)
                if new and old != int(new):
                    print(f"  {leg['commodity']} @ {terminal_name}: "
                          f"{field} {old:,}→{int(new):,}")
                    leg[field] = int(new)
            leg.setdefault("snapshots", []).append({
                "ts": now_iso,
                "price_buy": leg.get("price_buy"),
                "price_sell": leg.get("price_sell"),
                "scu_stock": leg.get("scu_stock"),
                "source": source,
            })
            del leg["snapshots"][:-10]
            route["loop_profit"] = sum(x["leg_profit"] for x in route["legs"])
            touched += 1
    if touched:
        common.save_watchlist(watchlist)
    return touched


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--commodity", required=True)
    parser.add_argument("--terminal", required=True)
    parser.add_argument("--price-buy", type=int, default=None)
    parser.add_argument("--price-sell", type=int, default=None)
    parser.add_argument("--qty", type=int, default=None, help="观察到的可买库存 SCU")
    parser.add_argument("--source", default="screenshot")
    args = parser.parse_args()

    if not (args.price_buy or args.price_sell):
        print("ERROR: --price-buy / --price-sell 至少给一个", file=sys.stderr)
        return 1

    ctx = common.build_ctx()
    terminal = fuzzy_pick(list(ctx.cache.terminals), args.terminal, "终端")
    commodity = fuzzy_pick(list(ctx.cache.commodities), args.commodity, "商品")

    # 录入的商品进入跟踪表：即使被 routes 接口下架也会被补充路径持续监控
    wl = common.load_watchlist()
    wl.setdefault("tracked_commodities", {})[str(commodity.id)] = commodity.name
    common.save_watchlist(wl)

    write_scan(terminal.id, terminal.name, commodity.id, commodity.name,
               args.price_buy, args.price_sell, args.qty, args.source)
    touched = update_watchlist(terminal.id, terminal.name, commodity.id,
                               args.price_buy, args.price_sell, args.source)

    print(f"已记录: {commodity.name} @ {terminal.name} "
          f"(buy={args.price_buy}, sell={args.price_sell}, qty={args.qty}, "
          f"source={args.source})")
    print(f"scan_prices.json 已写入；watchlist 匹配航腿 {touched} 条已刷新")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
