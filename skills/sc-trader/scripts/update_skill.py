"""sc-trader skill 自更新：上游组件更新 + 依赖刷新 + 重新部署 + 自检。

用法：
    tools/uexinfo/venv/bin/python skills/sc-trader/scripts/update_skill.py [--apply]

默认安全模式：只**报告**上游可用更新，并刷新 pip 安装、重建部署 symlink、跑自检。
--apply：真正快进子模块到上游最新（uexinfo/kioskprobe/scdb-mcp），重装依赖，全量自检。
注意：--apply 会移动子模块指针，更新后仓库会出现待提交的子模块变更，确认功能正常后提交。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
REPO_ROOT = SKILL_DIR.parent.parent
UEXINFO_DIR = REPO_ROOT / "tools" / "uexinfo"

sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402

VENV_PY = common.venv_python()
CHECK = SCRIPT_DIR / "check.py"

SUBMODULES = ["mcp/scdb-mcp", "tools/kioskprobe", "tools/uexinfo"]


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 900) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def submodule_report() -> list[tuple[str, str, str]]:
    """(名称, 本地 commit, 远端最新 commit)——不移动任何指针。"""
    code, out = run(["git", "submodule", "status", "--recursive"], cwd=REPO_ROOT)
    local = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            local[parts[1]] = parts[0].lstrip("+-U ")
    code, out = run(["git", "submodule", "status", "--recursive", "--remote"], cwd=REPO_ROOT)
    remote = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            remote[parts[1]] = parts[0].lstrip("+-U ")
    report = []
    for name in SUBMODULES:
        cur, new = local.get(name, "?"), remote.get(name, "?")
        report.append((name, cur[:8], new[:8]))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="真正快进子模块到上游最新并重装依赖（默认只报告）")
    args = parser.parse_args()

    print("=== 上游组件更新检查 ===")
    updates = []
    for name, cur, new in submodule_report():
        mark = "可更新" if cur != new and new != "?" else "已是最新"
        print(f"  {name:<18} {cur} -> {new}  [{mark}]")
        if cur != new and new != "?":
            updates.append(name)

    if args.apply:
        print("\n=== 快进子模块（--apply）===")
        code, out = run(["git", "submodule", "update", "--remote", "--recursive"],
                        cwd=REPO_ROOT, timeout=1200)
        print("  " + ("完成" if code == 0 else out[-300:]))
        if code == 0:
            print("=== 重装 uexinfo（依赖可能随上游变化）===")
            code2, out2 = run([str(VENV_PY), "-m", "pip", "install", "--quiet", "-e", "."],
                              cwd=UEXINFO_DIR, timeout=900)
            print("  " + ("完成" if code2 == 0 else out2[-300:]))
    else:
        if updates:
            print(f"\n（{len(updates)} 个组件有上游更新：{', '.join(updates)}；"
                  f"加 --apply 执行快进）")

    print("\n=== 刷新部署与自检 ===")
    code, out = run([str(VENV_PY), str(CHECK)], timeout=900)
    print(out)
    if updates and not args.apply:
        print("提示：存在未应用的更新（--apply 快进后建议重跑推荐/更新链路回归验证，"
              "并提交子模块指针变更）")
    return 0 if code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
