"""sc-trader 单次价格更新：拉 watchlist 内航段的现价，与上次快照 diff 并报告。

用法：
    tools/uexinfo/venv/bin/python skills/sc-trader/scripts/update_prices.py [--keep 10]

数据源优先级：玩家扫描价（record.py 写入，标 ★）> UEX API。每个商品一次 API 调用
（限速保护：内置 6.5s 间隔）。执行后把最新价写入航段并追加快照（保留 --keep 条）。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402

SNAP_KEEP = 10


def player_rows_for(terminal_id) -> dict:
    """该终端的玩家扫描价（commodity_id → row）。"""
    try:
        from uexinfo.cache.scan_prices import ScanPriceStore
        rows = ScanPriceStore().get_rows(str(terminal_id))
        out = {}
        for r in rows:
            cid = r.get("commodity_id")
            if cid:
                out[int(cid)] = r
        return out
    except Exception:
        return {}


def fetch_commodity_rows(client, id_commodity) -> dict:
    """id_terminal → 该终端该商品的 UEX 价格行。"""
    rows = common.throttled_get(client, "commodities_prices",
                                {"id_commodity": id_commodity})
    out = {}
    for r in rows or []:
        tid = r.get("id_terminal")
        if tid is not None:
            out[int(tid)] = r
    return out


def diff_field(old, new) -> tuple[bool, str]:
    if old is None or new is None:
        return (old != new), f"{old}→{new}"
    if int(old) == int(new):
        return False, ""
    pct = (int(new) - int(old)) / int(old) * 100 if old else 0
    return True, f"{int(old):,}→{int(new):,} ({pct:+.1f}%)"


def update_leg(leg: dict, uex_rows: dict, now_iso: str) -> list[str]:
    """刷新一条航腿的现价，返回报告行（无变化返回空）。"""
    changes = []
    buy_id = int(leg["buy_terminal"]["id"])
    sell_id = int(leg["sell_terminal"]["id"])
    cid = int(leg["id_commodity"] or 0)

    buy_row = uex_rows.get(buy_id, {})
    sell_row = uex_rows.get(sell_id, {})
    new_buy = buy_row.get("price_buy")
    new_sell = sell_row.get("price_sell")
    stock = buy_row.get("scu_buy_stock") or buy_row.get("scu_buy") or leg.get("scu_stock")

    src_buy = src_sell = "uex"
    scans = player_rows_for(buy_id)
    scan_buy = scans.get(cid)
    scans_sell = player_rows_for(sell_id).get(cid)
    if scan_buy and scan_buy.get("price_buy"):
        new_buy, src_buy = scan_buy["price_buy"], "player★"
    if scans_sell and scans_sell.get("price_sell"):
        new_sell, src_sell = scans_sell["price_sell"], "player★"

    for label, old, new, src in (
        ("买入", leg.get("price_buy"), new_buy, src_buy),
        ("卖出", leg.get("price_sell"), new_sell, src_sell),
    ):
        changed, text = diff_field(old, new)
        if changed:
            changes.append(f"{label} {text} [{src}]")
            if new is not None:
                leg[f"price_{'buy' if label == '买入' else 'sell'}"] = int(new)

    if stock is not None and leg.get("scu_stock") != stock:
        changes.append(f"库存 {leg.get('scu_stock')}→{stock}")
        leg["scu_stock"] = stock

    # 价格变动后重算本段利润（供圈利润排名/调仓建议使用）
    if (leg.get("price_buy") and leg.get("price_sell")
            and leg.get("price_sell", 0) > leg.get("price_buy", 0)):
        leg["leg_profit"] = int(leg.get("qty") or 0) * (leg["price_sell"] - leg["price_buy"])

    leg.setdefault("snapshots", []).append({
        "ts": now_iso,
        "price_buy": leg.get("price_buy"),
        "price_sell": leg.get("price_sell"),
        "scu_stock": leg.get("scu_stock"),
        "source": "player★" if "player★" in (src_buy, src_sell) else "uex",
    })
    del leg["snapshots"][:-SNAP_KEEP]
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keep", type=int, default=SNAP_KEEP)
    args = parser.parse_args()

    watchlist = common.load_watchlist()
    routes = [r for r in watchlist["routes"] if r["status"] != "retired"]
    if not routes:
        print("watchlist 为空——先跑 recommend.py")
        return 0

    client = common.get_client()
    commodity_ids = sorted({int(l["id_commodity"]) for r in routes for l in r["legs"]
                            if l.get("id_commodity")})
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    uex_by_commodity = {}
    for cid in commodity_ids:
        uex_by_commodity[cid] = fetch_commodity_rows(client, cid)

    total_changes = 0
    print(f"=== 价格更新 {now_iso}（{len(routes)} 路线 / {len(commodity_ids)} 商品）===")
    for r in routes:
        route_changed = []
        for leg in r["legs"]:
            changes = update_leg(leg, uex_by_commodity.get(int(leg["id_commodity"] or 0), {}),
                                 now_iso)
            for c in changes:
                route_changed.append(f"  {leg['commodity']} @ "
                                     f"{leg['buy_terminal']['name']}→{leg['sell_terminal']['name']}: {c}")
            # 重算圈利润
            r["loop_profit"] = sum(x["leg_profit"] for x in r["legs"])
        for line in route_changed:
            print(line)
        total_changes += len(route_changed)

    common.save_watchlist(watchlist)

    # ── 组合收益榜 + 调仓建议 ────────────────────────────────────────────────
    ranked = sorted(routes, key=lambda r: r["loop_profit"], reverse=True)
    prev = watchlist.get("ranking_prev") or []
    if ranked:
        print("\n=== 组合收益榜（最新圈利润）===")
        for r in ranked:
            ship = f" [{r.get('ship', '?')}]" if r.get("ship") else ""
            print(f"  {r['id']}{ship}: +{r['loop_profit']:,}"
                  + ("  ← 首选" if r is ranked[0] else ""))
        if prev and ranked[0]["id"] != prev[0]:
            old_best = next((r for r in ranked if r["id"] == prev[0]), None)
            old_profit = f"+{old_best['loop_profit']:,}" if old_best else "n/a"
            print(f"\n⚠️ 最优路线变更: {prev[0]}（现 {old_profit}） → "
                  f"{ranked[0]['id']}（+{ranked[0]['loop_profit']:,}），建议切换跑商路线")
        watchlist["ranking_prev"] = [r["id"] for r in ranked]
        common.save_watchlist(watchlist)

    print(f"\n{len(routes)} 条路线检查完毕，{total_changes} 项价格变化；"
          f"快照已写入 {common.WATCHLIST_PATH.name}（保留 {args.keep} 条/腿）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
