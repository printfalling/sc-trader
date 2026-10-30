#!/usr/bin/env python3
"""serve_routes.py — 本地 Web 服务：主页选参数，查不同船的多角路线图。

不依赖任何 Web 框架（venv 里没装 Flask/FastAPI），只用标准库 http.server。
后端复用 recommend.py（搜环 + Markdown）与 route_png.py（PIL 渲染 + 按环拆图）。

启动：
  tools/uexinfo/venv/bin/python skills/sc-trader/scripts/serve_routes.py [--port 8765]
然后浏览器打开 http://127.0.0.1:<port>/

接口：
  GET /api/ships                      → 船型清单（含货舱/箱型/默认船/默认预算/星系选项）
  GET /api/search?ship=&budget=...    → 起一个后台检索任务，立刻返回 {job}
  GET /api/progress?job=              → 轮询进度 {state,step,total,pct,label,result}
  GET /api/routes?ship=&budget=...    → 同步版（一次性拿结果，兼容旧调用）
  GET /img/<file>.png                → 生成的图片
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import pathlib
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
WEB_DIR = SCRIPT_DIR.parent / "web"
CACHE_DIR = WEB_DIR / ".cache"
RECOMMEND = SCRIPT_DIR / "recommend.py"

sys.path.insert(0, str(SCRIPT_DIR))
import route_png  # noqa: E402  （复用 render 与 split_sections）

DEFAULT_PORT = 8765
SYSTEM_CHOICES = {"68": "Stanton", "64": "Pyro", "all": "不限（跨星系）"}
STOPS_CHOICES = (2, 3, 4, 5, 6)

RECOMMEND_TIMEOUT_SEC = 300      # 单次搜索上限，超时不再干等
CACHE_MAX_FILES = 120            # .cache 里最多保留多少个文件（按 mtime 淘汰）
TOP_MAX = 20                     # 「每个角数出几条」上限（放宽自 3）
JOB_MAX = 20                     # 最多保留多少个检索任务


def load_profile() -> dict:
    return json.loads((SCRIPT_DIR.parent / "data" / "profile.json").read_text(encoding="utf-8"))


def ships_payload() -> dict:
    prof = load_profile()
    ships = [
        {
            "key": k,
            "name": v.get("name", k),
            "scu": v.get("scu_total"),
            "max_box": v.get("max_box"),
            "auto_load": bool(v.get("auto_loading_capable")),
        }
        for k, v in prof.get("ships", {}).items()
    ]
    return {
        "ships": ships,
        "default_ship": prof.get("default_ship"),
        "budget_default": prof.get("budget_default"),
        "systems": [{"id": k, "name": v} for k, v in SYSTEM_CHOICES.items()],
        "stops": list(STOPS_CHOICES),
    }


SORT_FIELDS = ("hourly", "profit", "time", "stops")


def metrics_of(title: str) -> dict:
    """从 "环 1 · 圈利润 +X · 圈时间 Ymin · 时薪 Z/h" 里抽出可排序的数值。"""
    m = {"profit": 0, "time": 0.0, "hourly": 0}
    mp = re.search(r"\u5708\u5229\u6da6 \+([\d,]+)", title)
    if mp:
        m["profit"] = int(mp.group(1).replace(",", ""))
    mt = re.search(r"\u5708\u65f6\u95f4 ([\d.]+)min", title)
    if mt:
        m["time"] = float(mt.group(1))
    mh = re.search(r"\u65f6\u85aa ([\d,]+)/h", title)
    if mh:
        m["hourly"] = int(mh.group(1).replace(",", ""))
    return m


def run_recommend(ship, budget, stops, system, top, mixed, auto, sort="hour",
                  on_progress=None) -> str:
    """跑一次 recommend，并把它 stderr 里的 [progress] 行实时转给 on_progress。"""
    cmd = [
        sys.executable, str(RECOMMEND),
        "--ship", ship, "--budget", str(budget),
        "--max-stops", str(stops), "--system", system,
        "--sort", sort, "--top", str(top), "--detail",
    ]
    if mixed:
        cmd.append("--mixed")
    if auto:
        cmd.append("--auto-load-only")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)
    err_lines: list = []

    def pump_err():
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if not line:
                    continue
                m = re.match(r"\[progress\]\s*(.*)", line)
                if m:
                    if on_progress:
                        on_progress(m.group(1))
                    continue          # 进度标记不进错误文本
                err_lines.append(line)
        except (ValueError, OSError):
            pass

    t = threading.Thread(target=pump_err, daemon=True)
    t.start()
    out = proc.stdout.read()
    try:
        rc = proc.wait(timeout=RECOMMEND_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        proc.kill()
        out = proc.stdout.read()
        raise RuntimeError(
            f"recommend.py 超过 {RECOMMEND_TIMEOUT_SEC}s 未返回"
            f"（角数 {stops} 搜索太慢或 UEX 无响应），已放弃本次检索")
    t.join(timeout=5)
    if rc != 0:
        raise RuntimeError("\n".join(err_lines)[-1200:] or "recommend.py 失败")
    return out


def build_routes(params: dict, on_progress=None) -> dict:
    """跑搜索并把每条路线的 Markdown 落盘，返回 key；图片由 /api/png 按需生成。

    on_progress(done, total, label)：检索很慢（一次几十秒到几分钟），
    不回传进度前端就只能干等。
    """
    def prog(done, total, label):
        if on_progress:
            on_progress(done, total, label)

    prof = load_profile()
    ship = params.get("ship") or prof.get("default_ship")
    if ship not in prof.get("ships", {}):
        return {"ok": False, "error": f"未知船型 {ship}"}

    budget = int(params.get("budget") or prof.get("budget_default") or 100000)
    system = params.get("system") or "all"
    if system not in SYSTEM_CHOICES:
        system = "all"
    top = max(1, min(TOP_MAX, int(params.get("top") or 1)))
    mixed = bool(params.get("mixed"))
    auto = bool(params.get("auto"))
    sort = params.get("sort") or "hourly"
    if sort not in SORT_FIELDS:
        sort = "hourly"
    order = params.get("order") or "desc"
    rec_sort = "profit" if sort == "profit" else "hour"      # recommend 只认这两个

    raw = params.get("stops") or "3,5"
    stops_list = []
    for part in str(raw).split(","):
        part = part.strip()
        if part.isdigit() and int(part) in STOPS_CHOICES and int(part) not in stops_list:
            stops_list.append(int(part))
    if not stops_list:
        stops_list = [3, 5]

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    ordered = sorted(stops_list)
    ntot = len(ordered)
    for si, stops in enumerate(ordered, 1):
        prog(si - 1, ntot, f"搜索 {stops} 角闭环（第 {si}/{ntot} 个）")

        def sub(label, _s=stops, _i=si):
            # 子进程里的阶段进度（如「补拉库存上限 3/10」）折算成本步内的小数，
            # 否则单个角数时进度条会一直停在 0%（这一阶段能占去几十秒）。
            frac = 0.0
            m = re.search(r"(\d+)\s*/\s*(\d+)", label)
            if m and int(m.group(2)):
                frac = min(1.0, int(m.group(1)) / int(m.group(2)))
            prog(_i - 1 + frac, ntot, f"{_s} 角 · {label}")

        md = run_recommend(ship, budget, stops, system, top, mixed, auto, rec_sort,
                           on_progress=sub)
        if "未找到闭环" in md:
            results.append({"stops": stops, "idx": 0, "title": "未找到闭环", "key": None,
                            "md": md, "route": "", "profit": 0, "time": 0, "hourly": 0})
            continue
        for idx, chunk in enumerate(route_png.split_sections(md)):
            # 路线串（md 里标题下那行缩进的 A → B → C）
            route = next((l.strip() for l in chunk.split("\n")
                          if l.startswith("    ") and "→" in l), "")
            title = next((l.replace("###", "", 1).strip() for l in chunk.split("\n")
                          if l.startswith("### ")), f"{stops} 角 环 {idx + 1}")
            # rec_sort 进了 key：换排序会产生不同的候选集，不能复用同一份 md
            key = hashlib.sha1(
                f"{ship}-{budget}-{stops}-{system}-{int(mixed)}-{int(auto)}-{top}"
                f"-{rec_sort}-{idx}".encode()
            ).hexdigest()[:16]
            (CACHE_DIR / (key + ".md")).write_text(chunk, encoding="utf-8")
            metrics = metrics_of(title)
            results.append({"stops": stops, "idx": idx + 1, "title": title, "key": key,
                            "md": chunk, "route": route,
                            "profit": metrics["profit"], "time": metrics["time"],
                            "hourly": metrics["hourly"]})

    prog(ntot, ntot, "整理结果")
    # 排序：默认按时薪降序
    def sort_key(r):
        return r["stops"] if sort == "stops" else metrics_of(r["title"])[sort]

    results.sort(key=sort_key, reverse=(order != "asc"))
    prune_cache()

    return {
        "ok": True,
        "results": results,
        "params": {"ship": ship, "budget": budget, "stops": stops_list, "system": system,
                   "top": top, "mixed": mixed, "auto": auto, "sort": sort, "order": order},
    }


def make_png(key: str, scale: int) -> dict:
    """按需渲染 PNG。

    文件名带上 md 内容摘要：同一 key 的路线内容变了（UEX 数据刷新、参数微调）
    就一定会重出图，不会再拿旧图冒充新结果。
    """
    md_path = CACHE_DIR / (key + ".md")
    if not md_path.exists():
        return {"ok": False, "error": "该结果已失效，请重新检索"}
    scale = max(1, min(4, scale))
    md_text = md_path.read_text(encoding="utf-8")
    digest = hashlib.sha1(md_text.encode("utf-8")).hexdigest()[:8]
    name = f"{key}-{scale}-{digest}.png"
    png_path = CACHE_DIR / name
    if not png_path.exists():
        route_png.render(md_text, png_path, scale)
    return {"ok": True, "png": f"/img/{name}", "name": name}


def prune_cache(max_files: int | None = None) -> int:
    """按 mtime 淘汰最旧的文件，避免 .cache 无限增长。返回删除个数。

    max_files 在调用时读常量，不能写成默认参数值——那样会在 import 时绑定，
    之后改 CACHE_MAX_FILES 不生效。
    """
    keep = CACHE_MAX_FILES if max_files is None else max_files
    if not CACHE_DIR.exists():
        return 0
    files = sorted((p for p in CACHE_DIR.iterdir() if p.is_file()),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for p in files[keep:]:
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


# ── 异步检索任务（前端轮询进度，避免长时间干等）────────────────────────────
_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()


def _job_update(jid: str, **kw) -> None:
    with _JOBS_LOCK:
        if jid in _JOBS:
            _JOBS[jid].update(kw)


def job_start(params: dict) -> str:
    """起一个后台检索任务，立刻返回 job id；进度用 /api/progress 轮询。"""
    jid = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[jid] = {"state": "running", "step": 0, "total": 1, "label": "准备中",
                      "result": None, "error": None, "ts": time.time()}
        # 只留最近 JOB_MAX 个，别让任务表无限长
        for old in sorted(_JOBS, key=lambda k: _JOBS[k]["ts"])[:-JOB_MAX]:
            if old != jid:
                _JOBS.pop(old, None)

    def work():
        try:
            res = build_routes(
                params,
                on_progress=lambda d, t, l: _job_update(jid, step=d, total=t, label=l))
            _job_update(jid, state="done", label="完成", result=res)
        except Exception as e:                                  # noqa: BLE001
            _job_update(jid, state="error", label="失败",
                        error=f"{type(e).__name__}: {e}")

    threading.Thread(target=work, daemon=True).start()
    return jid


def job_snapshot(jid: str) -> dict:
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
    if not j:
        return {"ok": False, "error": "任务不存在或已过期，请重新检索"}
    total = j["total"] or 1
    return {"ok": True, "state": j["state"], "step": j["step"], "total": total,
            "pct": int(min(100, round(j["step"] / total * 100)))
                   if j["state"] != "done" else 100,
            "label": j["label"], "result": j["result"], "error": j["error"]}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            return

        if path == "/api/ships":
            self._json(ships_payload())
            return

        if path == "/api/png":
            params = {k: v[0] for k, v in qs.items()}
            key = params.get("key", "")
            if not key.replace("-", "").isalnum():
                self._json({"ok": False, "error": "bad key"}, 400)
                return
            try:
                self._json(make_png(key, int(params.get("scale") or 4)))
            except Exception as e:
                self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return

        if path == "/api/search":
            params = {k: v[0] for k, v in qs.items()}
            try:
                self._json({"ok": True, "job": job_start(params)})
            except Exception as e:                              # noqa: BLE001
                self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return

        if path == "/api/progress":
            params = {k: v[0] for k, v in qs.items()}
            self._json(job_snapshot(params.get("job", "")))
            return

        if path == "/api/routes" or path == "/api/route":
            params = {k: v[0] for k, v in qs.items()}
            try:
                self._json(build_routes(params))
            except Exception as e:                              # noqa: BLE001
                self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return

        if path.startswith("/img/"):
            fname = pathlib.Path(path).name
            # 只接受我们自己生成的 <key>-<scale>-<digest>.png，挡掉 ../ 之类的路径穿越
            if re.fullmatch(r"[A-Za-z0-9_-]+\.png", fname):
                f = CACHE_DIR / fname
                if f.exists():
                    self._send(200, f.read_bytes(), "image/png")
                    return
            self._send(404, b"not found", "text/plain")
            return

        self._send(404, b"not found", "text/plain")

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    if not (WEB_DIR / "index.html").exists():
        print(f"ERROR: 缺少 {WEB_DIR / 'index.html'}", file=sys.stderr)
        return 1

    if CACHE_DIR.exists():
        n = prune_cache()
        if n:
            print(f"已清理 {n} 个过期缓存文件（保留最新 {CACHE_MAX_FILES} 个）")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"serving on http://{args.host}:{args.port}/  (Ctrl-C 停止)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
