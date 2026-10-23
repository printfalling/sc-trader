"""sc-trader 库存检查：某商品在各终端的库存、状态、可交易量、数据龄与玩家实测价。

用法（解释器：macOS/Linux 用 venv/bin/python，Windows 用 venv\\Scripts\\python.exe）：

    # 买点 + 卖点一并看（默认，最常用的"一并"视图）
    stock.py --commodity Compboard

    # 只看两个指定购买站点
    stock.py --commodity Compboard --only buy --terminals "Kaltag,Anvik"

    # 只看所有出售站点
    stock.py --commodity Compboard --only sell

输出口径：
  - 现货/囤货：UEX 原始字段（买端 scu_buy；卖端 scu_sell_stock + scu_sell）
  - 可买/可卖：按 uexinfo 规则，用船舱 SCU × 库存状态系数推算（买端越满越能买、
    卖端越空越能吸收），不是 UEX 原始值
  - 数据龄：date_modified 距现在的小时数；>24h 视为低可信
  - 玩家实测★：record.py 录入的观察值，权威高于 UEX
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402

STALE_H = 24.0


def resolve_commodity(ctx, name: str):
    """按名称解析商品（精确 > 最短包含匹配）；多义时列出候选。"""
    q = name.strip().lower()
    cands = list(ctx.cache.commodities)
    for c in cands:
        if c.name.lower() == q:
            return c
    subs = sorted((c for c in cands if q in c.name.lower()), key=lambda c: len(c.name))
    if not subs:
        print(f"ERROR: 未找到商品 '{name}'（缓存共 {len(cands)} 条）", file=sys.stderr)
        raise SystemExit(1)
    top = [c for c in subs if len(c.name) == len(subs[0].name)]
    if len(top) > 1:
        print(f"ERROR: 商品 '{name}' 有多个匹配：{', '.join(c.name for c in top[:8])}",
              file=sys.stderr)
        raise SystemExit(1)
    return subs[0]


def player_prices(cid: int) -> dict:
    """{terminal_id: {'buy': v, 'sell': v, 'ts': str}} —— 玩家实测价（★）。"""
    try:
        from uexinfo.cache.scan_prices import ScanPriceStore
        data = ScanPriceStore()._load()
    except Exception:
        return {}
    out = {}
    for tid, entries in data.items():
        for cid_key, e in entries.items():
            if str(e.get("commodity_id")) != str(cid):
                continue
            ts = e.get("timestamp")
            out[str(tid)] = {
                "buy": e.get("price_buy"),
                "sell": e.get("price_sell"),
                "ts": (dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "?"),
            }
    return out


def fmt_age(age_h: float | None) -> str:
    if age_h is None:
        return "?"
    mark = "" if age_h <= STALE_H else " ⚠️"
    return f"{age_h:.0f}h{mark}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--commodity", required=True, help="商品名（支持模糊，如 Compboard）")
    parser.add_argument("--only", choices=("buy", "sell", "all"), default="all")
    parser.add_argument("--terminals", default="",
                        help="按终端名子串过滤，逗号分隔，如 \"Kaltag,Anvik\"")
    parser.add_argument("--scu", type=int, default=None,
                        help="按指定船舱 SCU 推算可交易量（默认取 profile 当前船型）")
    parser.add_argument("--fresh-only", action="store_true",
                        help="只显示可信条目：数据龄 ≤ --max-age，或带玩家实测★的")
    parser.add_argument("--max-age", type=float, default=STALE_H,
                        help=f"可信数据龄上限（小时，默认 {STALE_H:.0f}）")
    parser.add_argument("--system", default=None,
                        help="只显示指定星系的终端（默认取 profile 出发星系；"
                             "传 all 不限）")
    args = parser.parse_args()

    profile = common.load_profile()
    # 多船结构：取 default_ship（recommend 注入的 profile['ship'] 可选）
    ship = profile.get("ship") or {}
    if not ship:
        key = profile.get("default_ship")
        ship = (profile.get("ships") or {}).get(key, {})
    scu = args.scu or ship.get("scu_total", 8)
    ctx = common.build_ctx()
    commodity = resolve_commodity(ctx, args.commodity)

    try:
        from uexinfo.rules.risk import sell_risk
        from uexinfo.rules.stock import buy_quantity, sell_quantity
    except Exception:
        buy_quantity = lambda q, s: q * {1: 0, 2: 2, 3: 4, 4: 6, 5: 8, 6: 8, 7: 8}.get(s, 4) // 8  # noqa
        sell_quantity = lambda q, s: q * {1: 8, 2: 8, 3: 6, 4: 4, 5: 2, 6: 2, 7: 0}.get(s, 4) // 8  # noqa
        sell_risk = lambda s, a: 50  # noqa

    filters = [t.strip().lower() for t in args.terminals.split(",") if t.strip()]
    rows = common.throttled_get(common.get_client(), "commodities_prices",
                                {"id_commodity": commodity.id})
    mine = player_prices(commodity.id)
    now = time.time()

    # 终端 → 星系映射：默认只保留出发星系，自动排除 Pyro/Nyx 等越界项
    sys_by_tid = {t.id: getattr(t, "star_system_name", "") for t in ctx.cache.terminals}
    want_sys = args.system or profile.get("origin", {}).get("star_system")
    if (args.system or "").lower() == "all":
        want_sys = None
    scope_txt = want_sys or "全部星系"

    buys, sells = [], []
    skipped = 0
    for r in rows or []:
        tn = r.get("terminal_name") or ""
        if filters and not any(f in tn.lower() for f in filters):
            continue
        if want_sys and sys_by_tid.get(r.get("id_terminal"), "") != want_sys:
            skipped += 1
            continue
        age_h = ((now - int(r["date_modified"])) / 3600) if r.get("date_modified") else None
        obs = mine.get(str(r.get("id_terminal")), {})
        if args.fresh_only and not (
                obs.get("buy") or obs.get("sell")
                or (age_h is not None and age_h <= args.max_age)):
            skipped += 1
            continue
        try:
            pb = float(r.get("price_buy") or 0)
            ps = float(r.get("price_sell") or 0)
        except (TypeError, ValueError):
            continue
        if pb > 0 and args.only in ("buy", "all"):
            buys.append((pb, tn, r, age_h, obs))
        if ps > 0 and args.only in ("sell", "all"):
            sells.append((ps, tn, r, age_h, obs))

    print(f"=== {commodity.name} 库存检查（按 {scu} SCU 船舱推算 · 范围 {scope_txt}"
          f"{' · 仅可信' if args.fresh_only else ''}）===")

    if args.only in ("buy", "all"):
        print("\n【购买站点】买价升序")
        print(f"{'终端':<44} {'买价':>9} {'现货':>7} {'状态':>12} {'可买':>6} {'数据龄':>9}  玩家实测★")
        for pb, tn, r, age_h, obs in sorted(buys):
            ob = f"{obs['buy']:,}({obs['ts']})" if obs.get("buy") else ""
            print(f"{tn[:44]:<44} {int(pb):>9,} {str(r.get('scu_buy')):>7} "
                  f"{common.UEX_STATUS_NAMES.get(r.get('status_buy'), '?') + '(' + str(r.get('status_buy')) + ')':>12} "
                  f"{buy_quantity(scu, r.get('status_buy') or 0):>6} {fmt_age(age_h):>9}  {ob}")

    if args.only in ("sell", "all"):
        print("\n【出售站点】卖价降序")
        print(f"{'终端':<44} {'卖价':>9} {'囤货':>6} {'scu_sell':>9} {'状态':>12} {'可卖':>6} "
              f"{'风险':>6} {'数据龄':>9}  玩家实测★")
        for ps, tn, r, age_h, obs in sorted(sells, reverse=True):
            ob = f"{obs['sell']:,}({obs['ts']})" if obs.get("sell") else ""
            print(f"{tn[:44]:<44} {int(ps):>9,} {str(r.get('scu_sell_stock')):>6} "
                  f"{str(r.get('scu_sell')):>9} "
                  f"{common.UEX_STATUS_NAMES.get(r.get('status_sell'), '?') + '(' + str(r.get('status_sell')) + ')':>12} "
                  f"{sell_quantity(scu, r.get('status_sell') or 0):>6} "
                  f"{sell_risk(r.get('status_sell') or 0, age_h):>6} {fmt_age(age_h):>9}  {ob}")

    # ── 可信最优买卖对（按可交易量封顶后的实际利润排序）──────────────────────
    if buys and sells:
        pairs = []
        for pb, btn, br, bage, bobs in buys:
            for ps, stn, sr, sage, sobs in sells:
                if br.get("id_terminal") == sr.get("id_terminal"):
                    continue
                eff_buy = bobs.get("buy") or pb          # ★ 优先
                eff_sell = sobs.get("sell") or ps
                margin = eff_sell - eff_buy
                if margin <= 0:
                    continue
                qty = min(buy_quantity(scu, br.get("status_buy") or 0),
                          sell_quantity(scu, sr.get("status_sell") or 0))
                if qty <= 0:
                    continue
                pairs.append((margin * qty, qty, margin, btn, stn, eff_buy, eff_sell,
                              bool(bobs.get("buy")), bool(sobs.get("sell")), bage, sage))
        pairs.sort(reverse=True)
        title = ("【可信最优买卖对】仅新鲜/★数据" if args.fresh_only
                 else "【最优买卖对】含陈旧数据，加 --fresh-only 只看可信项")
        print(f"\n{title} —— 按 可交易量×价差 排序（★=你的实测价）")
        if not pairs:
            print("  （无可盈利组合——说明该商品当前不值得跑）")
        for profit, qty, margin, btn, stn, eb, es, fb, fs, bage, sage in pairs[:5]:
            star = ("★买" if fb else "") + (" ★卖" if fs else "")
            print(f"  {btn[:34]:<34} → {stn[:34]:<34} "
                  f"{int(eb):,}>{int(es):,} × {qty}SCU = +{int(profit):,} "
                  f"(买{fmt_age(bage)}/卖{fmt_age(sage)}) {star}")
    if skipped:
        print(f"\n（已过滤 {skipped} 条：越界星系或数据过旧；--system all / 去掉 "
              f"--fresh-only 可查看）")

    print("\n口径：可买/可卖=船舱×库存状态系数（买端越满越能买、卖端越空越能吸收）；"
          f"数据龄>{args.max_age:.0f}h 标 ⚠️；★=你的实测价，权威最高")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
