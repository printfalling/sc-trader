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


def qt_seconds(ctx, from_loc: str, to_loc: str) -> float | None:
    """两点量子航行秒数；查不到节点/图缺失时返回 None。

    上游图数据 duration 常为 0，故优先用 total_distance(Gm) ÷ 0.2c 估算
    （Aurora 级小船量子巡航近似 0.2c；实测后可校准）。
    """
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
            return distance_gm * 1e9 / (0.2 * 299_792_458)  # Gm→m，0.2c
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


def route_time_min(profile: dict, legs: list[dict]) -> float:
    """单圈时间（分钟）= Σ量子航行 + 每航段两次地面作业。

    地面作业 = 起降/电梯/终端交易（base_stop_min）+ 装卸：
      手动装箱 1 min/SCU（profile.time_model.manual_load_min_per_scu）
      自动装载固定 0.5 min/次（auto_load_extra_min）
    估算口径，实测后可回填校准。
    """
    tm = profile.get("time_model", {})
    base = tm.get("base_stop_min", 4)
    auto = tm.get("auto_loading", False)
    total = 0.0
    for leg in legs:
        total += (leg.get("qt_sec") or 0) / 60
        handling = tm.get("auto_load_extra_min", 0.5) if auto \
            else tm.get("manual_load_min_per_scu", 1.0) * (leg.get("qty") or 0)
        total += 2 * (base + handling)
    return round(total, 1)


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
