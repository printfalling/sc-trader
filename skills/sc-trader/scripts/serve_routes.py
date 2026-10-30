#!/usr/bin/env python3
"""serve_routes.py — 本地 Web 服务：主页选参数，查不同船的多角路线图。

不依赖任何 Web 框架（venv 里没装 Flask/FastAPI），只用标准库 http.server。
后端复用 recommend.py（搜环 + Markdown）与 route_png.py（PIL 渲染）。

启动：
  tools/uexinfo/venv/bin/python skills/sc-trader/scripts/serve_routes.py [--port 8765]
然后浏览器打开 http://127.0.0.1:8765/

接口：
  GET /api/ships                     → 船型清单（含货舱/箱型/默认船/默认预算）
  GET /api/route?ship=&budget=&stops=&system=&mixed=1&auto=1&scale=4
                                    → {ok, png, md}
  GET /img/<file>.png               → 生成的图片
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
WEB_DIR = SCRIPT_DIR.parent / "web"
CACHE_DIR = WEB_DIR / ".cache"
RECOMMEND = SCRIPT_DIR / "recommend.py"

sys.path.insert(0, str(SCRIPT_DIR))
import route_png  # noqa: E402  （复用它的 render，避免重复实现绘制）

DEFAULT_PORT = 8765
# 星系：UEX id（Stanton=68, Pyro=64），all = 不限
SYSTEM_CHOICES = {"68": "Stanton", "64": "Pyro", "all": "不限（跨星系）"}


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
    }


def build_route(params: dict) -> dict:
    """跑 recommend.py --detail 拿 Markdown，再渲染 PNG（结果按参数哈希缓存）。"""
    prof = load_profile()
    ship = params.get("ship") or prof.get("default_ship")
    if ship not in prof.get("ships", {}):
        return {"ok": False, "error": f"未知船型 {ship}"}

    budget = int(params.get("budget") or prof.get("budget_default") or 100000)
    stops = int(params.get("stops") or 5)
    stops = max(2, min(6, stops))
    system = params.get("system") or "all"
    if system not in SYSTEM_CHOICES:
        system = "all"
    scale = int(params.get("scale") or 4)
    scale = max(1, min(4, scale))

    key = f"{ship}-{budget}-{stops}-{system}-{int(bool(params.get('mixed')))}-{int(bool(params.get('auto')))}-{scale}"
    name = hashlib.sha1(key.encode()).hexdigest()[:16] + ".png"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    png_path = CACHE_DIR / name

    if not png_path.exists():
        cmd = [
            sys.executable, str(RECOMMEND),
            "--ship", ship, "--budget", str(budget),
            "--max-stops", str(stops), "--system", system,
            "--sort", "hour", "--top", "1", "--detail",
        ]
        if params.get("mixed"):
            cmd.append("--mixed")
        if params.get("auto"):
            cmd.append("--auto-load-only")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            return {"ok": False, "error": proc.stderr[-1500:]}
        md = proc.stdout
        if "未找到闭环" in md:
            return {"ok": False, "error": "该条件下未找到闭环，试试放宽角数或去掉 --auto-load-only"}
        route_png.render(md, png_path, scale)
        (CACHE_DIR / (name[:-4] + ".md")).write_text(md, encoding="utf-8")
    else:
        # 命中缓存时重跑一次 markdown 只为回显（成本与搜索相同，故也缓存 md）
        md_path = CACHE_DIR / (name[:-4] + ".md")
        md = md_path.read_text(encoding="utf-8") if md_path.exists() else ""

    return {
        "ok": True,
        "png": f"/img/{name}",
        "md": md,
        "params": {"ship": ship, "budget": budget, "stops": stops,
                   "system": system, "mixed": bool(params.get("mixed")),
                   "auto": bool(params.get("auto")), "scale": scale},
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            html = (WEB_DIR / "index.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
            return

        if path == "/api/ships":
            self._json(ships_payload())
            return

        if path == "/api/route":
            params = {k: v[0] for k, v in qs.items()}
            try:
                self._json(build_route(params))
            except Exception as e:  # 出错也返回 JSON，便于前端提示
                self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return

        if path.startswith("/img/"):
            f = CACHE_DIR / pathlib.Path(path).name
            if f.exists():
                self._send(200, f.read_bytes(), "image/png")
                return
            self._send(404, b"not found", "text/plain")
            return

        self._send(404, b"not found", "text/plain")

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    if not (WEB_DIR / "index.html").exists():
        print(f"ERROR: 缺少 {WEB_DIR / 'index.html'}", file=sys.stderr)
        return 1

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
