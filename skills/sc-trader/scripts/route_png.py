#!/usr/bin/env python3
"""route_png.py — 一条命令把闭环搜索结果画成 PNG。

内部调用 recommend.py --detail 拿 Markdown，再用 PIL 渲染成图片（不依赖浏览器，
不需要 playwright/chromium）。

用法：
  tools/uexinfo/venv/bin/python skills/sc-trader/scripts/route_png.py \\
      --ship raft --budget 3000000 --max-stops 5 --system 68 \\
      --mixed --auto-load-only --out docs/routes_raft_stanton.png

参数与 recommend.py 的闭环搜索模式一致（--ship/--budget/--max-stops/--system/
--mixed/--auto-load-only/--include-pyro/--sort/--min-supply-ratio）。
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
RECOMMEND = SCRIPT_DIR / "recommend.py"

# 配色（浅底，便于分享与打印）
BG = (250, 251, 253)
FG = (33, 41, 54)
HEAD_BG = (226, 232, 240)
HEAD_FG = (30, 41, 59)
GRID = (190, 200, 212)
ACCENT = (180, 95, 6)
GOOD = (22, 128, 74)
MUTED = (110, 122, 138)

TITLE_SIZE = 30
CELL_SIZE = 20
ROUTE_SIZE = 18
PAD = 12
MAX_CELL_W = 560


def load_font(size: int):
    for p in ("/System/Library/Fonts/STHeiti Medium.ttc",
              "/System/Library/Fonts/PingFang.ttc",
              "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def wrap(text: str, font, max_w: int) -> list:
    """按像素宽度折行（逐字符，中英混排都可用）。"""
    lines, cur = [], ""
    for ch in text:
        if cur and font.getlength(cur + ch) > max_w:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines or [""]


def parse_tables(md: str) -> list:
    tables, cur = [], []
    for line in md.split("\n"):
        s = line.strip()
        if s.startswith("|"):
            cur.append(s)
        elif cur:
            tables.append(cur)
            cur = []
    if cur:
        tables.append(cur)
    out = []
    for t in tables:
        rows = []
        for i, line in enumerate(t):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if i == 1 and all(set(c) <= set("-: ") for c in cells):
                continue          # 分隔行
            rows.append(cells)
        if rows:
            out.append(rows)
    return out


def draw_table(draw, rows, x, y, font, bold_font) -> tuple:
    ncol = len(rows[0])
    wrapped, col_w = [], [0] * ncol
    for r in rows:
        wrow = []
        for i, cell in enumerate(r):
            strong = "**" in cell
            lines = wrap(cell.replace("**", ""), font, MAX_CELL_W)
            wrow.append((lines, strong))
            col_w[i] = max(col_w[i], max(font.getlength(s) for s in lines))
        wrapped.append(wrow)
    col_w = [int(w) + PAD * 2 for w in col_w]

    lh = int(CELL_SIZE * 1.45)
    row_h = [max(len(lines) for lines, _ in wrow) * lh + PAD for wrow in wrapped]

    total_w, total_h = sum(col_w), sum(row_h)

    draw.rectangle([x, y, x + total_w, y + row_h[0]], fill=HEAD_BG)      # 表头底
    cy = y
    for ri, (wrow, h) in enumerate(zip(wrapped, row_h)):
        draw.line([(x, cy), (x + total_w, cy)], fill=GRID, width=1)
        cx = x
        for (lines, strong), cw in zip(wrow, col_w):
            draw.line([(cx, cy), (cx, cy + h)], fill=GRID, width=1)
            f = bold_font if (strong or ri == 0) else font
            col = GOOD if strong else (HEAD_FG if ri == 0 else FG)
            ty = cy + PAD // 2
            for line in lines:
                draw.text((cx + PAD, ty), line, font=f, fill=col)
                ty += lh
            cx += cw
        cy += h
    draw.line([(x, cy), (x + total_w, cy)], fill=GRID, width=1)
    cx = x
    for cw in col_w:
        draw.line([(cx, y), (cx, cy)], fill=GRID, width=1)
        cx += cw
    draw.line([(cx, y), (cx, cy)], fill=GRID, width=1)
    return total_w, total_h


def render(md: str, out_path: pathlib.Path) -> None:
    lines = md.split("\n")
    title = next((l.replace("###", "", 1).strip() for l in lines if l.startswith("### ")), "")
    route = next((l.strip() for l in lines if l.strip().startswith("Admin -")), "")
    note = next((l.strip() for l in lines if "口径" in l), "")

    f_title, f_cell = load_font(TITLE_SIZE), load_font(CELL_SIZE)
    f_bold, f_route = load_font(CELL_SIZE), load_font(ROUTE_SIZE)

    tables = parse_tables(md)

    probe = Image.new("RGB", (10, 10))
    pd = ImageDraw.Draw(probe)
    widths, heights = [], []
    for t in tables:
        w, h = draw_table(pd, t, 0, 0, f_cell, f_bold)
        widths.append(w)
        heights.append(h)

    content_w = max([f_title.getlength(title) if title else 0,
                     f_route.getlength(route) if route else 0] + widths) if (title or route or widths) else 600
    W = int(content_w) + 80
    H = 60 + (TITLE_SIZE + 24 if title else 0) + (ROUTE_SIZE + 28 if route else 0) + sum(h + 34 for h in heights)
    if note:
        H += 60

    img = Image.new("RGB", (max(W, 600), H), BG)
    d = ImageDraw.Draw(img)
    y = 34
    if title:
        d.text((40, y), title, font=f_title, fill=ACCENT)
        y += TITLE_SIZE + 20
    if route:
        d.text((40, y), route, font=f_route, fill=MUTED)
        y += ROUTE_SIZE + 24
    for t in tables:
        _, h = draw_table(d, t, 40, y, f_cell, f_bold)
        y += h + 34
    if note:
        d.text((40, y), note, font=f_cell, fill=MUTED)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    print(f"saved {out_path} ({img.width}x{img.height})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ship", default=None)
    ap.add_argument("--budget", type=int, default=None)
    ap.add_argument("--max-stops", type=int, default=5)
    ap.add_argument("--system", default="all", help="星系 id，或 all 表示不限")
    ap.add_argument("--mixed", action="store_true", help="混装填满货舱")
    ap.add_argument("--auto-load-only", action="store_true", help="买点须支持自动装载")
    ap.add_argument("--include-pyro", action="store_true")
    ap.add_argument("--sort", choices=["profit", "hour"], default="hour")
    ap.add_argument("--top", type=int, default=1)
    ap.add_argument("--out", required=True, help="输出 PNG 路径")

    # 其余透传给 recommend.py
    args, extra = ap.parse_known_args()

    cmd = [sys.executable, str(RECOMMEND)]
    if args.ship:
        cmd += ["--ship", args.ship]
    if args.budget:
        cmd += ["--budget", str(args.budget)]
    cmd += ["--max-stops", str(args.max_stops), "--system", str(args.system)]
    cmd += ["--sort", args.sort, "--top", str(args.top), "--detail"]
    if args.mixed:
        cmd.append("--mixed")
    if args.auto_load_only:
        cmd.append("--auto-load-only")
    if args.include_pyro:
        cmd.append("--include-pyro")
    cmd += extra

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-2000:])
        return proc.returncode
    if "未找到闭环" in proc.stdout:
        print("未找到闭环，跳过出图")
        return 1

    render(proc.stdout, pathlib.Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
