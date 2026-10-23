"""sc-trader 环境自检 + 缺失自动补装（幂等，可反复运行）。

用法：
    tools/uexinfo/venv/bin/python skills/sc-trader/scripts/check.py [--no-fix]

检查项：子模块 init → uexinfo venv → uexinfo 库导入 → tesseract（brew）→
playwright extra → UEX API 连通性。缺什么补什么；--no-fix 只报告不修复。
"""
from __future__ import annotations

import argparse
import importlib
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
REPO_ROOT = SKILL_DIR.parent.parent
UEXINFO_DIR = REPO_ROOT / "tools" / "uexinfo"
VENV_PY = UEXINFO_DIR / "venv" / "bin" / "python"

sys.path.insert(0, str(SCRIPT_DIR))

RESULTS: list[tuple[str, str, str]] = []  # (状态, 名称, 说明)


def record(ok: bool, name: str, note: str = "") -> None:
    RESULTS.append(("PASS" if ok else "FAIL", name, note))


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 600) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def check_submodules(fix: bool) -> bool:
    code, out = run(["git", "submodule", "status"], cwd=REPO_ROOT)
    if code != 0:
        record(False, "git 子模块", "git submodule status 失败（不在仓库内？）")
        return False
    missing = [line for line in out.splitlines() if line.startswith("-")]
    if not missing:
        record(True, "git 子模块", "全部已初始化")
        return True
    if not fix:
        record(False, "git 子模块", f"{len(missing)} 个未初始化")
        return False
    code, out = run(["git", "submodule", "update", "--init", "--recursive"],
                    cwd=REPO_ROOT, timeout=900)
    record(code == 0, "git 子模块", "已自动 init" if code == 0 else out[-200:])
    return code == 0


def check_venv(fix: bool) -> bool:
    if VENV_PY.exists():
        record(True, "uexinfo venv", str(VENV_PY))
        return True
    if not fix:
        record(False, "uexinfo venv", f"{VENV_PY} 不存在")
        return False
    py312 = shutil.which("python3.12") or shutil.which("python3")
    if not py312:
        record(False, "uexinfo venv", "找不到 python3.12，无法创建 venv")
        return False
    code, out = run([py312, "-m", "venv", "venv"], cwd=UEXINFO_DIR, timeout=300)
    if code != 0:
        record(False, "uexinfo venv", out[-200:])
        return False
    code, out = run([str(VENV_PY), "-m", "pip", "install", "--quiet", "-e", "."],
                    cwd=UEXINFO_DIR, timeout=900)
    record(code == 0, "uexinfo venv", "已自动创建并安装" if code == 0 else out[-200:])
    return code == 0


def check_uexinfo_import() -> bool:
    code, out = run([str(VENV_PY), "-c",
                     "import uexinfo.api.uex_client, uexinfo.cache.manager, "
                     "uexinfo.models.transport_network, uexinfo.cache.scan_prices; "
                     "print('ok')"], timeout=120)
    record(code == 0 and "ok" in out, "uexinfo 库导入", out[-150:] if code != 0 else "")
    return code == 0 and "ok" in out


def check_tesseract(fix: bool) -> bool:
    if shutil.which("tesseract"):
        record(True, "tesseract OCR", shutil.which("tesseract"))
        return True
    if not fix:
        record(False, "tesseract OCR", "未安装（uexinfo 终端扫描 OCR 需要）")
        return False
    brew = shutil.which("brew")
    if not brew:
        record(False, "tesseract OCR", "brew 不可用，请手动安装")
        return False
    code, out = run([brew, "install", "tesseract"], timeout=1200)
    record(shutil.which("tesseract") is not None, "tesseract OCR",
           "brew 已自动安装" if shutil.which("tesseract") else out[-200:])
    return shutil.which("tesseract") is not None


def check_playwright(fix: bool) -> bool:
    code, out = run([str(VENV_PY), "-c", "import playwright; print('ok')"], timeout=120)
    if code == 0:
        record(True, "playwright extra", "已安装")
        return True
    if not fix:
        record(False, "playwright extra", "未安装（sc-trade.tools 交叉验证可选依赖）")
        return False
    code, out = run([str(VENV_PY), "-m", "pip", "install", "--quiet", "-e", ".[playwright]"],
                    cwd=UEXINFO_DIR, timeout=900)
    record(code == 0, "playwright extra", "已自动安装" if code == 0 else out[-200:])
    return code == 0


def check_uex_api() -> bool:
    code, out = run([str(VENV_PY), "-c",
                     "import sys; sys.path.insert(0, r'%s'); "
                     "from uexinfo.api.uex_client import UEXClient; "
                     "data = UEXClient()._get('commodities_status'); "
                     "print('ok' if data else 'empty')" % SCRIPT_DIR], timeout=60)
    ok = code == 0 and "ok" in out
    record(ok, "UEX API 连通", out[-150:] if not ok else "commodities_status 正常")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-fix", action="store_true", help="只检查不修复")
    args = parser.parse_args()
    fix = not args.no_fix

    check_submodules(fix)
    venv_ok = check_venv(fix)
    if venv_ok:
        check_uexinfo_import()
        check_tesseract(fix)
        check_playwright(fix)
        check_uex_api()
    else:
        record(False, "uexinfo 库导入", "跳过（无 venv）")
        record(False, "tesseract OCR", "跳过（无 venv）")
        record(False, "playwright extra", "跳过（无 venv）")
        record(False, "UEX API 连通", "跳过（无 venv）")

    width = max(len(r[1]) for r in RESULTS) + 2
    print("\n=== sc-trader 环境自检 ===")
    for status, name, note in RESULTS:
        mark = "✅" if status == "PASS" else "❌"
        print(f"{mark} {name:<{width}} {note}")
    failed = [r for r in RESULTS if r[0] == "FAIL"]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} 项通过")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
