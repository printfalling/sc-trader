"""sc-trader 共享工具：uexinfo headless 上下文 + UEX 节流 API + watchlist 读写。

所有脚本必须用 tools/uexinfo 的 venv 解释器运行（依赖 uexinfo 包）：
    tools/uexinfo/venv/bin/python skills/sc-trader/scripts/<script>.py
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent                       # skills/sc-trader
REPO_ROOT = SKILL_DIR.parent.parent                 # uexcorp/
UEXINFO_DIR = REPO_ROOT / "tools" / "uexinfo"

PROFILE_PATH = SKILL_DIR / "data" / "profile.json"
WATCHLIST_PATH = SKILL_DIR / "data" / "watchlist.json"


# ── Windows 兼容 ─────────────────────────────────────────────────────────────

def ensure_utf8_console() -> None:
    """Windows 控制台默认 GBK/cp936，✅/⚠️/→ 等字符会 UnicodeEncodeError。

    强制 UTF-8 输出（无法重配置时静默降级）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def venv_python() -> Path:
    """平台正确的 uexinfo venv 解释器：Windows 是 Scripts\\python.exe。"""
    if platform.system() == "Windows":
        return UEXINFO_DIR / "venv" / "Scripts" / "python.exe"
    return UEXINFO_DIR / "venv" / "bin" / "python"


def make_dir_link(target: Path, source: Path) -> None:
    """跨平台目录链接：优先 symlink，Windows 无特权时回退 junction（mklink /J）。"""
    if target.is_symlink() or target.exists():
        if _dir_link_ok(target, source):
            return
        raise FileExistsError(f"{target} 已存在且不指向 {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.symlink_to(source)
    except OSError:
        if platform.system() == "Windows":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(source)],
                           check=True, capture_output=True)
        else:
            raise


def dir_link_ok(target: Path, source: Path) -> bool:
    """链接有效性（junction 在 Windows 上 is_symlink() 为 False，用 realpath 判定）。"""
    try:
        return os.path.realpath(target) == str(source)
    except OSError:
        return False


# 旧名兼容
_dir_link_ok = dir_link_ok


ensure_utf8_console()

# ── uexinfo 导入 ─────────────────────────────────────────────────────────────

def _ensure_uexinfo_on_path() -> None:
    """venv 里已 pip install -e .，一般无需兜底；直接跑仓库源码时补 path。"""
    if (UEXINFO_DIR / "uexinfo").is_dir():
        if str(UEXINFO_DIR) not in sys.path:
            sys.path.insert(0, str(UEXINFO_DIR))


_ensure_uexinfo_on_path()


def get_client():
    """UEXClient（cloudscraper 会话）。

    关键修正：BASE_URL 切到官方 API 子域 api.uexcorp.space——主域 uexcorp.space/api
    走主站 Cloudflare 防护，大流量会被 403；API 子域不受影响（实测 200）。

    token 优先级：环境变量 UEXCORP_API_TOKEN > data/secrets.json（gitignored）>
    profile.json uexcorp_api_token。配置后附加 Bearer 头，额度 172,800/天
    （匿名 14,400/天）。申请：https://uexcorp.space/api/apps
    """
    from uexinfo.api import uex_client as _uexmod
    from uexinfo.api.uex_client import UEXClient
    _uexmod.BASE_URL = "https://api.uexcorp.space/2.0"
    client = UEXClient()
    token = ""
    try:
        secrets = SKILL_DIR / "data" / "secrets.json"
        if secrets.exists():
            token = json.load(open(secrets, encoding="utf-8")).get("uexcorp_api_token") or ""
    except Exception:
        pass
    try:
        token = load_profile().get("uexcorp_api_token") or token
    except Exception:
        pass
    token = os.environ.get("UEXCORP_API_TOKEN", token)
    if token:
        client.session.headers.update({"Authorization": f"Bearer {token}"})
    return client


def build_ctx():
    """headless AppContext（照抄 overlay/server.py init_context 的最小子集）。"""
    from uexinfo.cache.manager import CacheManager
    from uexinfo.cli.context import AppContext
    from uexinfo.config import settings as uex_settings
    from uexinfo.location.index import LocationIndex
    from uexinfo.models.player import Player

    cfg = uex_settings.load()
    cache = CacheManager(ttl_static=cfg.get("cache", {}).get("ttl_static", 86400))
    try:
        cache.load()
    except Exception:
        pass
    ctx = AppContext(cfg=cfg, cache=cache)
    ctx.location_index = LocationIndex(cache)
    try:
        ctx.player = Player.from_config(cfg.get("player", {}))
    except Exception:
        pass
    return ctx


# ── UEX 匿名限速（10 req/min，留余量取 6.5s 间隔）───────────────────────────

_MIN_INTERVAL = 6.5
_last_call = [0.0]


def throttled_get(client, endpoint: str, params: dict | None = None) -> list:
    """带全局节流的 UEXClient._get。"""
    wait = _last_call[0] + _MIN_INTERVAL - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()
    return client._get(endpoint, params)


# ── profile / watchlist ──────────────────────────────────────────────────────

def load_profile() -> dict:
    with open(PROFILE_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_watchlist() -> dict:
    with open(WATCHLIST_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_watchlist(watchlist: dict) -> None:
    from datetime import datetime, timezone
    watchlist["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
        json.dump(watchlist, f, ensure_ascii=False, indent=2)


# ── 航段时间（TransportGraph 覆盖 Nyx/Stanton/Pyro）──────────────────────────

def _graph_node_names(ctx) -> set[str]:
    """TransportGraph 节点名集合（缓存到 ctx 上避免重复取）。"""
    names = getattr(ctx, "_qt_node_names", None)
    if names is None:
        try:
            names = {str(k) for k in ctx.cache.transport_graph.nodes}
        except Exception:
            names = set()
        ctx._qt_node_names = names
    return names


def _resolve_node(ctx, name: str) -> str | None:
    """UEX 终端名 → 图节点名：先精确，再逐段匹配（剥 "Admin - " 等前缀）。"""
    nodes = _graph_node_names(ctx)
    if not nodes or not name:
        return None
    if name in nodes:
        return name
    lowered = {n.lower(): n for n in nodes}
    if name.lower() in lowered:
        return lowered[name.lower()]
    for seg in str(name).split(" - "):
        seg = seg.strip()
        if seg in nodes:
            return seg
        if seg.lower() in lowered:
            return lowered[seg.lower()]
    return None


def qt_seconds(ctx, from_loc: str, to_loc: str,
               seconds_per_gm: float = 0.0) -> float | None:
    """两点量子航行秒数；查不到节点/图缺失时返回 None。

    上游图数据 duration 常为 0，故优先用 total_distance(Gm) ÷ 0.2c 估算
    （Aurora 级小船量子巡航近似 0.2c；实测后可校准）。
    """
    if not seconds_per_gm:
        try:
            seconds_per_gm = float((load_profile().get("time_model") or {})
                                   .get("seconds_per_gm", 10.0))
        except Exception:
            seconds_per_gm = 10.0
    try:
        src = _resolve_node(ctx, from_loc)
        dst = _resolve_node(ctx, to_loc)
        if not src or not dst:
            return None
        result = ctx.cache.transport_graph.find_shortest_path(src, dst)
        duration = getattr(result, "total_duration", None)
        if duration:
            return float(duration)
        distance_gm = float(getattr(result, "total_distance", 0) or 0)
        if distance_gm > 0:
            # 实测口径：1 Gm ≈ 10 秒（时间模型可配 seconds_per_gm）
            return distance_gm * seconds_per_gm
        return None
    except Exception:
        return None


# ── 箱体适配（手动装箱模型，见 profile.json slots）───────────────────────────

def packable_scu(sizes: list[int], profile: dict) -> int:
    """给定商品可用箱体规格，返回可装进槽位的最大 SCU。

    模型假设（按用户槽位 4×1 + 2×2）：
      - 1-SCU 箱可填满全部槽位（2-SCU 槽按体积并排放 2 箱）→ 8 SCU
      - 2-SCU 箱只能进 2-SCU 槽 → 4 SCU
      - >2-SCU 整箱任何槽位都装不下 → 0
    真实装填行为以后用 record.py 的实测数据校准。
    """
    max_box = (profile.get("ship") or {}).get("max_box") or profile.get("max_container_box", 2)
    slots = profile["ship"]["slots"]
    if not sizes:
        return 0
    best = 0
    for box in sizes:
        if box > max_box:
            continue
        load = 0
        for slot in slots:
            if slot["size"] >= box:
                load += slot["count"] * ((slot["size"] // box) * box)
        best = max(best, load)
    return best


# ── 时间模型（航行 + 地面作业 + 装卸）────────────────────────────────────────

# UEX 库存状态字典（commodities_status 端点，1=空仓 … 7=满仓）
UEX_STATUS_NAMES = {
    1: "Out", 2: "Very Low", 3: "Low", 4: "Medium",
    5: "High", 6: "Very High", 7: "Max",
}


# ── 展示规范：终端全名 + 位置路径（禁止缩写）────────────────────────────────

def terminal_path(ctx, terminal_id: int) -> str:
    """终端位置路径 'Stanton > MicroTech > Wala'（系统 > 行星 > 卫星/轨道/站）。"""
    try:
        t = next(x for x in ctx.cache.terminals if x.id == int(terminal_id))
    except (StopIteration, TypeError, ValueError):
        return ""
    parts = [getattr(t, "star_system_name", ""), getattr(t, "planet_name", "")]
    third = ""
    if getattr(t, "id_moon", 0):
        try:
            third = next(m.name for m in ctx.cache.moons if m.id == t.id_moon)
        except StopIteration:
            third = ""
    if not third:
        third = (getattr(t, "orbit_name", "") or getattr(t, "space_station_name", "")
                 or getattr(t, "city_name", ""))
    if third:
        parts.append(third)
    # 去重相邻重复段（如行星即终端所在地时出现 "MicroTech > MicroTech"）
    dedup = [p for i, p in enumerate(parts) if i == 0 or p != parts[i - 1]]
    return " > ".join(p for p in dedup if p)


def stock_capped_qty(scu_total: int, qty: int, status_buy, status_sell) -> int:
    """按买端/卖端库存状态封顶实际可交易量（uexinfo 规则 D1/D5）。

    买端：库存越满越能买（Max=100%，Out=0）
    卖端：库存越空越能收（Out=100%，Max=0）
    """
    try:
        from uexinfo.rules.stock import buy_quantity, sell_quantity
    except Exception:
        return max(0, int(qty))
    try:
        q = min(int(qty), buy_quantity(int(scu_total), int(status_buy or 0)))
        q = sell_quantity(int(q), int(status_sell or 0))
    except Exception:
        return max(0, int(qty))
    return max(0, int(q))


def gateway_node(system_from: str, system_to: str) -> str:
    """星门节点命名：位于 system_from、通往 system_to 的门（uexinfo 图约定）。"""
    return f"{system_to} Gateway ({system_from})"


def travel_seconds(ctx, from_name: str, from_sys: str, to_name: str, to_sys: str,
                   seconds_per_gm: float = 5.0, jump_min: float = 5.0,
                   fallback_min: float = 15.0) -> float:
    """跨星系行程 = 飞到本星系星门 → 穿越（jump_min）→ 从对方星系门飞到目的地。

    任一段图上无路径时用 fallback_min 兜底。
    """
    if ctx is None:
        return fallback_min * 60
    if not from_sys or not to_sys or from_sys == to_sys:
        t = qt_seconds(ctx, from_name, to_name, seconds_per_gm)
        return t if t is not None else fallback_min * 60
    a = qt_seconds(ctx, from_name, gateway_node(from_sys, to_sys), seconds_per_gm)
    b = qt_seconds(ctx, gateway_node(to_sys, from_sys), to_name, seconds_per_gm)
    total = (a if a is not None else fallback_min * 60)
    total += jump_min * 60
    total += (b if b is not None else fallback_min * 60)
    return total


def snap_to_crate(qty: int, box_sizes, max_box: int = 32) -> int:
    """把数量向下取整到整箱：商品只按箱卖，买不到散装 SCU。

    取不超过 max_box 的最大箱型，再按该箱型整除；无法凑满一箱则返回 0（不可买）。
    """
    sizes = [b for b in (box_sizes or []) if isinstance(b, int) and 0 < b <= max_box]
    if not sizes:
        return int(qty)
    box = max(sizes)
    return (int(qty) // box) * box


def crate_size_for(leg: dict, profile: dict) -> int:
    """该航段的实际箱规格：取终端可售箱型（leg.box_sizes）中不超过船槽上限的最大值。

    商品只卖 1-SCU 箱时就是 1（搬运箱数暴涨）——与游戏内真实装载一致。
    """
    max_box = ((profile.get("ship") or {}).get("max_box")
               or profile.get("max_container_box", 2))
    sizes = [s for s in (leg.get("box_sizes") or []) if isinstance(s, int) and s > 0]
    usable = [s for s in sizes if s <= max_box]
    return max(usable) if usable else (min(sizes) if sizes else max_box)


def terminal_loading_mode(terminal_name: str, profile: dict) -> bool:
    """该终端是否支持自动装载（有货运电梯）。

    判定顺序：profile.loading_overrides 显式覆盖 → 名称启发式
    （Admin-/TDD-/CBD- 等空间站/城市终端=自动；Research Outpost 等前哨=手动）
    → 兜底 profile.time_model.auto_loading。
    """
    name = (terminal_name or "").lower()
    for key, mode in (profile.get("loading_overrides") or {}).items():
        if key.lower() in name:
            return str(mode).lower() == "auto"
    if any(k in name for k in ("admin -", "tdd -", "cbd -", "long forest",
                               "gateway", "station")):
        return True
    if "research outpost" in name or "outpost" in name:
        return False
    return bool(profile.get("time_model", {}).get("auto_loading"))


def route_time_min(profile: dict, legs: list[dict], extra_min: float = 0.0) -> float:
    """单圈时间（分钟）= Σ量子航行 + 每航段两次地面作业。

    地面作业 = 起降/电梯/终端交易（base_stop_min）+ 搬运：
      手动：manual_load_min_per_scu × SCU
      自动：ceil(数量 / 实际箱规格) × auto_crate_min + auto_load_extra_min
    箱规格取自终端可售箱型（container_sizes），不是固定值。
    """
    tm = profile.get("time_model", {})
    base = tm.get("base_stop_min", 4)
    crate_min = tm.get("auto_crate_min", 1.0)
    extra = tm.get("auto_load_extra_min", 3.0)
    manual_per_scu = tm.get("manual_load_min_per_scu", 1.0)
    # 船是否具备自动装载条件（大船货舱网格 + 显式开启）
    ship_auto = bool((profile.get("ship") or {}).get("auto_loading_capable")
                     or tm.get("auto_loading"))
    qty_default = (profile.get("ship") or {}).get("scu_total", 8)

    manual_crate = float(tm.get("manual_crate_min", 1.0))
    manual_extra = float(tm.get("manual_extra_min", 3.0))

    def stop_minutes(term: dict, qty: int, box_sizes) -> float:
        """单次停靠 = base + 搬运；搬运按【箱数】计 = ceil(数量/该箱规格)。"""
        if "auto" in term:
            auto = ship_auto and bool(term["auto"])
        else:
            auto = ship_auto and terminal_loading_mode(term.get("name", ""), profile)
        cap = term.get("max_box") or 32
        usable = [b for b in (box_sizes or []) if 0 < b <= cap]
        size = max(usable) if usable else (min(cap, 32) if cap else None)
        if auto:
            crates = -(-(qty or 0) // max(1, size)) if size else 0
            handling = crates * crate_min + extra
        elif size:
            # 手动：搬的是箱，不是散装 SCU
            crates = -(-(qty or 0) // max(1, size))
            handling = crates * manual_crate + manual_extra
        else:
            handling = manual_per_scu * (qty or 0)   # 无箱型信息时的兜底
        return base + handling

    jump_gate = float(tm.get("jump_gate_min", 12.0))
    total = 0.0
    for leg in legs:
        total += (leg.get("qt_sec") or 0) / 60
        # 跨星系必须过星门：进闸定位 + 充能 + 排队 + 出闸，与距离无关
        bs = (leg.get("buy_terminal") or {}).get("system")
        ss = (leg.get("sell_terminal") or {}).get("system")
        if bs and ss and bs != ss:
            total += jump_gate
        qty = leg.get("qty") or qty_default
        total += stop_minutes(leg["buy_terminal"], qty, leg.get("box_sizes"))
        total += stop_minutes(leg["sell_terminal"], qty, leg.get("box_sizes"))
    return round(total + extra_min, 1)


def parse_box_sizes(raw) -> list[int]:
    """container_sizes_origin 字段容错解析（"1,2,4" / [1,2,4] / None）。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return sorted({int(x) for x in raw if str(x).strip().lstrip("-").isdigit()})
    return sorted({
        int(tok) for tok in str(raw).replace(";", ",").split(",")
        if tok.strip().lstrip("-").isdigit()
    })
