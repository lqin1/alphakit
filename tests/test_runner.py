"""执行层自检：`run_node` / `Ctx` / 预热 / 掩码 / 自引用。

这一套在 `core.panels.Panels` 这道接缝出现之前**写不出来**。`run_node` 拿的是一个
具体的 `Store`, 而 `Store` 只会往磁盘上的 zarr 写, 于是 node.py / ctx.py /
preflight.py / report.py / cli.py 合计约 1490 行的唯一触碰方式, 是 smoke 起子进程
打 CLI、对着仓库里那份真实的 storage/l3 断言 returncode == 0——happy path、
非 hermetic、且要先有数据。

现在生产是 zarr on disk、测试是内存 dict, 两个适配器, 于是"预热够不够""自引用回灌
有没有生效""池外是不是真的被夹成 0"这些第一次能被直接断言。
"""
from __future__ import annotations

import shutil
import sys
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_kit.core import rank as rk                      # noqa: E402
from alpha_kit.core.config import load_spec                # noqa: E402
from alpha_kit.core.panels import Panels                   # noqa: E402
from alpha_kit.core.store import Store                     # noqa: E402
from alpha_kit.runner.node import run_node, warmup         # noqa: E402
from fakes import FakePanels                               # noqa: E402

TMP = Path("/tmp/alphakit-test-runner")
SESSIONS = [f"2024-01-{d:02d}" for d in range(1, 26)]
SECURITIES = [101, 102, 103, 104]
PX = "g_common.field_base_px.adj_close_1500"
UNIV = "g_common.field_common_univ.pool"

FAILS: list[tuple[str, str]] = []
N_OK = 0


def check(cond, msg: str) -> bool:
    global N_OK
    if not cond:
        raise AssertionError(msg)
    N_OK += 1
    return True


def spec_from(text: str, *, node_dir="rn", stem="rn", code=None):
    d = TMP / "cfg" / "g_yliu" / "nodes" / node_dir
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}.yaml").write_text(text)
    (d / f"{stem}.py").write_text(code or "def handle(ctx):\n    return None\n")
    return load_spec(d / f"{stem}.yaml")


def fresh() -> FakePanels:
    """一个铺好 px 与池子的内存库。px 逐日递增, 便于手算。"""
    s = FakePanels(SESSIONS, SECURITIES)
    vals = np.arange(len(SESSIONS) * len(SECURITIES), dtype="f4").reshape(
        len(SESSIONS), len(SECURITIES)) + 100.0
    s.seed(PX, vals)
    pool = np.ones((len(SESSIONS), len(SECURITIES)), dtype=bool)
    pool[:, 3] = False                                   # 104 恒在池外
    s.seed(UNIV, pool, dtype="bool")
    return s


# ------------------------------------------------------------------ 接缝本身
def test_both_adapters_satisfy_panels():
    """两个适配器才算一道真接缝, 不是事后贴上去的一层注解。"""
    check(isinstance(fresh(), Panels), "FakePanels 不满足 Panels")
    real = Store("storage/l3/us", "us")
    check(isinstance(real, Panels), "core.store.Store 不满足 Panels")
    for m in ("axes", "exists", "meta", "read", "write", "expand", "list_refs"):
        check(hasattr(real, m) and hasattr(fresh(), m), f"两个适配器都要有 {m}")
    return "Store 与 FakePanels 同时满足 Panels"


# ------------------------------------------------------------------ 预热
def test_run_node_warms_the_ops_chain():
    """一个 session 的值不该取决于从哪天起跑（§7.1）。

    这正是 max/相加那个 bug 的端到端形态: `lookback: 3` + `win(4)` + `linear_decay: 3`
    真实需求是 3+2=5, 取大只给 3, 于是起跑后头两天的输出来自未填满的衰减缓冲。
    在此之前这条断言写不出来——run_node 只肯往磁盘 zarr 写。
    """
    yaml = ("lookback: 3\nnodes:\n  factor_yliu_w:\n    code: rn.py\n"
            f"    deps: [{PX}]\n    ops:\n      - linear_decay: 3\n")
    code = (f'PX = "{PX}"\n'
            "def handle(ctx):\n"
            "    w = ctx.win(PX, 4)\n"
            "    return w.loc[0] / w.loc[-3] - 1.0\n")
    spec = spec_from(yaml, node_dir="factor_yliu_w", stem="rn", code=code)
    node = spec.nodes["factor_yliu_w"]
    check(warmup(spec.lookback, node) == 5,
          f"预热应是 3+2=5, 实得 {warmup(spec.lookback, node)}（取大会给 3）")

    late, early = fresh(), fresh()
    run_node(late, spec, node, "2024-01-15", "2024-01-20")
    run_node(early, spec, node, "2024-01-08", "2024-01-20")
    a = late.read("g_yliu.factor_yliu_w.w").loc["2024-01-15"]
    b = early.read("g_yliu.factor_yliu_w.w").loc["2024-01-15"]
    d = (a - b).abs().to_numpy()
    check(np.nanmax(d) < 1e-9,
          f"同一天因起跑点不同而不同: 最大差 {np.nanmax(d):.3e}, {int((d>1e-9).sum())} 只票")
    return "起跑点无关；预热 3+2=5 而非 max 的 3"


# ------------------------------------------------------------------ 掩码
def test_pool_is_masked_on_both_ends():
    """§3.5 两道闸门：ops 之前池外整列 NaN, scale 之后池外恰为 0。"""
    yaml = (f"lookback: 0\nuniverse: {UNIV}\nnodes:\n  alpha_yliu_m:\n    code: rn.py\n"
            f"    deps: [{PX}]\n    ops:\n      - rank\n      - scale: book\n")
    code = f'PX = "{PX}"\ndef handle(ctx):\n    return ctx.f(PX)\n'
    spec = spec_from(yaml, node_dir="alpha_yliu_m", stem="rn", code=code)
    s = fresh()
    run_node(s, spec, spec.nodes["alpha_yliu_m"], "2024-01-10", "2024-01-12")
    w = s.read("g_yliu.alpha_yliu_m.weight", "2024-01-10", "2024-01-12")
    check(float(np.nanmax(np.abs(w[104].to_numpy()))) == 0.0,
          f"池外的 104 权重不为 0：{w[104].tolist()}")
    gross = np.abs(w.to_numpy()).sum(axis=1)
    check(np.allclose(gross, 1.0, atol=1e-6), f"Σ|w| 不是 1：{gross}")
    check(int(w.notna().sum(axis=1).min()) == len(SECURITIES), "权重里出现了 NaN")
    return "池外恰为 0；Σ|w|=1；无 NaN"


# ------------------------------------------------------------------ 自引用
def test_self_reference_sees_its_own_yesterday():
    """节点读自己昨天的输出是合法写法, 当日产出必须能回灌（§7.2 第 1 条）。

    冷启动时它自己还不存在, 所以这条路径同时是"optional 面板"的验证。
    """
    yaml = ("lookback: 0\nnodes:\n  factor_yliu_s:\n    code: rn.py\n"
            f"    deps: [{PX}, g_yliu.factor_yliu_s.s]\n")
    code = (f'PX = "{PX}"\nSELF = "g_yliu.factor_yliu_s.s"\n'
            "def handle(ctx):\n"
            "    prev = ctx.win(SELF, 2).loc[-1]\n"
            "    return ctx.f(PX) * 0 + prev.fillna(0.0) + 1.0\n")
    spec = spec_from(yaml, node_dir="factor_yliu_s", stem="rn", code=code)
    s = fresh()
    run_node(s, spec, spec.nodes["factor_yliu_s"], "2024-01-10", "2024-01-14")
    v = s.read("g_yliu.factor_yliu_s.s").loc["2024-01-10":"2024-01-14"]
    got = v[101].tolist()
    check(got == [1.0, 2.0, 3.0, 4.0, 5.0],
          f"当日产出没有回灌给下一天: {got}（应是 1..5 逐日累加）")
    return f"自引用逐日累加 {got}"


# ------------------------------------------------------------------ probe
def test_probe_writes_nothing():
    yaml = ("lookback: 0\nnodes:\n  factor_yliu_p:\n    code: rn.py\n"
            f"    deps: [{PX}]\n")
    code = f'PX = "{PX}"\ndef handle(ctx):\n    return ctx.f(PX)\n'
    spec = spec_from(yaml, node_dir="factor_yliu_p", stem="rn", code=code)
    s = fresh()
    before = set(s.list_refs())
    run_node(s, spec, spec.nodes["factor_yliu_p"], "2024-01-10", "2024-01-12", probe=2)
    check(set(s.list_refs()) == before, f"probe 写了库：{set(s.list_refs()) - before}")
    return "probe 不落库"


TESTS = [test_both_adapters_satisfy_panels,
         test_run_node_warms_the_ops_chain,
         test_pool_is_masked_on_both_ends,
         test_self_reference_sees_its_own_yesterday,
         test_probe_writes_nothing]

if __name__ == "__main__":
    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    print(f"runner self-check  ({len(TESTS)} tests)\n")
    for t in TESTS:
        try:
            print(f"ok   {t.__name__}   [{t()}]")
        except Exception as e:                            # noqa: BLE001
            FAILS.append((t.__name__, str(e)))
            print(f"FAIL {t.__name__}\n     {traceback.format_exc().splitlines()[-1]}")
    print(f"\n{N_OK}/{N_OK + len(FAILS)} passed")
    sys.exit(1 if FAILS else 0)
