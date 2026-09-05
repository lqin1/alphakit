#!/usr/bin/env python
"""一条命令证明这台机器装对了：四套自检串起来跑, 任一套红了即非零退出。

    .venv/bin/python tests/run_all.py          # 只打每套的汇总表
    .venv/bin/python tests/run_all.py -v       # 连子进程的原始输出一起打

四套各查一层, 谁也替不了谁：

    tests/test_ops.py         算子链（纯内存, 不碰 store）
    tests/test_simulate.py    pnl 仿真器 + 会计恒等式 + 七道闸门
    tests/smoke.py            引擎端到端（轴 / store / 命名 / 主循环 / 落库 / pnl 交付物）
    pipeline/validate_l2.py   数据的验收闸门（L2 契约 V1–V8 + X 系列）

只用标准库：这个脚本存在的意义就是「依赖装坏了也要能跑起来把话说清楚」,
自己 import 一行 pandas 就先炸在它本该诊断的那件事上了。
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------ 各套的计数解析
# 四套的输出格式各不相同, 而汇总表要给出「跑了多少条断言」——所以一套一个解析器。
# 每个解析器返回 (通过数, 失败数, 备注)。解析不出来返回 (0, 0, ...), 只报退出码。

def _p_ops(out: str) -> tuple[int, int, str]:
    m = re.search(r"(\d+)\s*/\s*(\d+)\s*passed", out)
    if not m:
        return 0, 0, ""
    ok, tot = int(m.group(1)), int(m.group(2))
    return ok, tot - ok, ""


def _p_simulate(out: str) -> tuple[int, int, str]:
    return out.count("[ok  ]"), out.count("[FAIL]"), ""


def _p_smoke(out: str) -> tuple[int, int, str]:
    m = re.search(r"passed\s*(\d+)\s*/\s*failed\s*(\d+)", out)
    return (int(m.group(1)), int(m.group(2)), "") if m else (0, 0, "")


def _p_validate(out: str) -> tuple[int, int, str]:
    """W 系列是 advisory——它不影响退出码, 所以也不能混进 pass/total 里当分母。

    只认带 `checked=` 的那些行：它们是 SUMMARY 段, 一个检查一行；后面的 DETAIL 段会把
    带 notes 的检查再打一遍, 不筛就会重复计数。
    """
    ok = len(re.findall(r"^\[PASS\] [VXW]\d.*checked=", out, re.M))
    bad = len(re.findall(r"^\[FAIL\] [VXW]\d.*checked=", out, re.M))
    warn = re.findall(r"^\[WARN\] ([VXW]\d+).*checked=", out, re.M)
    return ok, bad, (f"+{len(warn)} WARN({','.join(warn)}) warnings only" if warn else "")


def _p_generic(out: str) -> tuple[int, int, str]:
    """给后来新增的自检兜底：认几种本仓库已在用的标记, 认不出就只报退出码。"""
    ok = out.count("[ok  ]") + out.count("[PASS]") + len(re.findall(r"^ok\s+test_", out, re.M))
    bad = out.count("[FAIL]") + len(re.findall(r"^FAIL\s+test_", out, re.M))
    return ok, bad, ""


# 顺序是从便宜到贵：算子链几百毫秒, validate_l2 要读一整个 L2 数据集。
# 先跑快的, 装环境时最常犯的错（依赖没装全）在第一套就会现形。
SUITES: list[tuple[str, list[str], object]] = [
    ("tests/test_ops.py", ["tests/test_ops.py"], _p_ops),
    ("tests/test_simulate.py", ["tests/test_simulate.py"], _p_simulate),
    ("tests/smoke.py", ["tests/smoke.py"], _p_smoke),
    ("pipeline/validate_l2.py", ["pipeline/validate_l2.py"], _p_validate),
]


def _discovered() -> list[tuple[str, list[str], object]]:
    """tests/ 下后来新增的 test_*.py 自动入列——放进来却没人跑的测试等于没写。"""
    known = {s[0] for s in SUITES}
    return [(f"tests/{p.name}", [f"tests/{p.name}"], _p_generic)
            for p in sorted((REPO / "tests").glob("test_*.py"))
            if f"tests/{p.name}" not in known]


# ------------------------------------------------------------------ 排版
def _w(s: str) -> int:
    """中文在终端占两列, str.ljust 只会数字符——不补这一下表格就是歪的。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, n: int) -> str:
    return s + " " * max(0, n - _w(s))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    verbose = "-v" in argv or "--verbose" in argv

    suites = SUITES + _discovered()
    rows: list[tuple[str, str, float, str, str]] = []
    t_all = time.perf_counter()

    for name, args, parse in suites:
        target = REPO / args[0]
        if not target.exists():
            rows.append((name, "-", 0.0, "MISSING", f"no such file: {target}"))
            print(f"▸ {name}  ——  MISSING")
            continue
        print(f"▸ {name}  ...", flush=True)
        t0 = time.perf_counter()
        # 用「跑本文件的这个解释器」跑子进程, 免得 run_all 在 venv 里而子套件在系统 python 上
        p = subprocess.run([sys.executable, *[str(REPO / a) for a in args]],
                           cwd=REPO, capture_output=True, text=True)
        dt = time.perf_counter() - t0
        out = p.stdout + p.stderr
        ok, bad, note = parse(out)
        status = "PASS" if p.returncode == 0 else "FAIL"
        count = f"{ok}/{ok + bad}" if (ok or bad) else "-"
        rows.append((name, count, dt, status, note))
        print(f"  {status}  {count} assertions  {dt:.1f}s" + (f"  {note}" if note else ""))
        if verbose:
            print(out)
        elif p.returncode != 0:
            # 失败时必须留下线索, 否则用户只拿到一个 FAIL 还得自己再跑一遍
            tail = out.strip().splitlines()[-25:]
            print("  --- last 25 lines " + "-" * 44)
            print("\n".join("  " + l for l in tail))
            print("  " + "-" * 57)

    w_name = max(_w(r[0]) for r in rows) + 2
    w_cnt = max(max(_w(r[1]) for r in rows), _w("assertions")) + 2
    bar = "=" * (w_name + w_cnt + 22)
    print("\n" + bar)
    print(_pad("suite", w_name) + _pad("assertions", w_cnt) + _pad("time", 9) + "result")
    print("-" * len(bar))
    n_pass = n_ok = n_tot = 0
    for name, count, dt, status, note in rows:
        print(_pad(name, w_name) + _pad(count, w_cnt) + _pad(f"{dt:.1f}s", 9)
              + _pad(status, 8) + note)
        n_pass += status == "PASS"
        if "/" in count:
            a, b = count.split("/")
            n_ok += int(a); n_tot += int(b)
    print("-" * len(bar))
    print(f"{n_pass}/{len(rows)} suites passed    assertions {n_ok}/{n_tot}    "
          f"total {time.perf_counter() - t_all:.1f}s")
    print(bar)
    if n_pass != len(rows):
        print("\nfailing suites: " + ", ".join(r[0] for r in rows if r[3] != "PASS"))
    return 0 if n_pass == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
