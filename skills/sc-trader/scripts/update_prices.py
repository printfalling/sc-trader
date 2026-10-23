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
    """刷新一条航腿的现价，返回报告行（无变化返回空）。

    新鲜度仲裁：玩家观察时间 vs UEX date_modified，谁新用谁；
    玩家数据更旧时如实采用 UEX 并提示时滞。
    """
    changes = []
    buy_id = int(leg["buy_terminal"]["id"])
    sell_id = int(leg["sell_terminal"]["id"])
    cid = int(leg["id_commodity"] or 0)

    def epoch(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    def fmt_ts(v):
        return datetime.fromtimestamp(int(v)).strftime("%m-%d %H:%M") if v else "?"

    buy_row = uex_rows.get(buy_id, {})
    sell_row = uex_rows.get(sell_id, {})
    stock = buy_row.get("scu_buy_stock") or buy_row.get("scu_buy") or leg.get("scu_stock")

    scan_buy = player_rows_for(buy_id).get(cid)
    scan_sell = player_rows_for(sell_id).get(cid)

    def arbitrate(field, uex_row, scan, side):
        uex_price = (uex_row or {}).get(field)
        uex_ts = epoch((uex_row or {}).get("date_modified"))
        p_price = (scan or {}).get(field)
        p_ts = epoch((scan or {}).get("timestamp"))
        if p_price and (not uex_price or p_ts >= uex_ts):
            return int(p_price), f"player★ {fmt_ts(p_ts)}"
        if p_price and p_price != uex_price and p_ts:
            changes.append(f"  [时滞] 玩家观察({fmt_ts(p_ts)})早于 UEX({fmt_ts(uex_ts)})，"
                           f"{side}采用 UEX")
        return uex_price, (f"uex {fmt_ts(uex_ts)}" if uex_ts else "uex")

    new_buy, src_buy = arbitrate("price_buy", buy_row, scan_buy, "买入")
    new_sell, src_sell = arbitrate("price_sell", sell_row, scan_sell, "卖出")

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
            # 重算圈利润与时间模型
            r["loop_profit"] = sum(x["leg_profit"] for x in r["legs"])
            # 按路线所属船型重算时间，并保留转场/摆渡附加时间
            prof = common.load_profile()
            ship_key = r.get("ship")
            if ship_key and ship_key in (prof.get("ships") or {}):
                prof["ship"] = prof["ships"][ship_key]
            extra = ((r.get("transit_sec") or 0) + (r.get("reposition_sec") or 0)) / 60
            r["loop_time_min"] = common.route_time_min(prof, r["legs"],
                                                       extra_min=extra)
            r["profit_per_hour"] = (int(r["loop_profit"] / r["loop_time_min"] * 60)
                                    if r["loop_time_min"] else None)
        for line in route_changed:
            print(line)
        total_changes += len(route_changed)

    common.save_watchlist(watchlist)

    # ── 组合收益榜 + 调仓建议 ────────────────────────────────────────────────
    ranked = sorted(routes, key=lambda r: r["loop_profit"], reverse=True)
    prev = watchlist.get("ranking_prev") or []
    if ranked:
        print("\n=== 组合收益榜（最新圈利润 / 时薪）===")
        for r in ranked:
            ship = f" [{r.get('ship', '?')}]" if r.get("ship") else ""
            hour = f"（{r['profit_per_hour']:,}/h）" if r.get("profit_per_hour") else ""
            print(f"  {r['id']}{ship}: +{r['loop_profit']:,}{hour}"
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
