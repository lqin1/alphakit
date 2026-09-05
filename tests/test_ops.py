"""ops 链的自检脚本（architecture.md §6.2 / §3.5 / §4.10 例 7 / 附录 B）。

    python tests/test_ops.py                   # 失败则退出码非 0

不依赖 pytest：§十三 要的是「跑一条命令就知道对不对」，多一个依赖就多一个装不上的
理由。每条断言都对着文档里记过的一个失败模式。
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # tests/ 的上一层就是仓库根
from alpha_kit.runner.ops import (       # noqa: E402
    OpChain, cs_gross, cs_rank, cs_neutralize, cs_truncate, OPS)

IDX = pd.Index([101, 102, 103, 104, 105], name="security_id")


class Univ:
    """测试用 UniverseView：固定池子 + 固定分组，不随 t 变（除非给了逐日表）。"""

    def __init__(self, members, groups=None, index=IDX, by_t=None):
        self._m = pd.Series([s in set(members) for s in index], index=index)
        self._g = None if groups is None else pd.Series(groups, index=index)
        self._by_t = by_t                      # {t: [members]}，用于测试出池

    def mask(self, t):
        if self._by_t is not None and t in self._by_t:
            return pd.Series([s in set(self._by_t[t]) for s in self._m.index],
                             index=self._m.index)
        return self._m

    def group(self, ref, t):
        assert self._g is not None, "this test did not wire up a grouping field"
        return self._g


# --------------------------------------------------------------- 测试骨架
FAILS: list[str] = []


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def near(a, b, tol=1e-9):
    return abs(float(a) - float(b)) <= tol


def run(fn):
    name = fn.__name__
    try:
        note = fn()
    except Exception as e:                      # noqa: BLE001
        FAILS.append(name)
        print(f"FAIL {name}\n     {type(e).__name__}: {e}")
    else:
        print(f"ok   {name}" + (f"   [{note}]" if note else ""))


# ------------------------------------------------------------------ rank
def test_rank_range_and_nan():
    v = pd.Series([3.0, 1.0, np.nan, 2.0, 5.0], index=IDX)
    r = cs_rank(v)
    check(r.isna().tolist() == [False, False, True, False, False],
          f"NaN positions were not preserved: {r.tolist()}")
    fin = r.dropna()
    check(near(fin.min(), -0.5) and near(fin.max(), 0.5),
          f"not mapped into the closed interval [-0.5, 0.5]: min={fin.min()} max={fin.max()}")
    check(((fin >= -0.5 - 1e-12) & (fin <= 0.5 + 1e-12)).all(), f"out of range: {fin.tolist()}")
    # 4 个有效值 → (rank-1)/3 - 0.5 = [-0.5+2/3, -0.5, nan, -0.5+1/3, 0.5]
    check(near(fin.loc[102], -0.5) and near(fin.loc[105], 0.5), f"wrong ordering: {fin.to_dict()}")
    check(near(fin.sum(), 0.0), f"the cross-sectional mean after rank must be 0, got {fin.sum()}")
    return f"min={fin.min():.3f} max={fin.max():.3f} mean={fin.mean():.1e}"


def test_rank_ties_and_singleton():
    r = cs_rank(pd.Series([1.0, 1.0, 1.0, 2.0, 2.0], index=IDX))
    check(near(r.loc[101], r.loc[102]) and near(r.loc[101], r.loc[103]),
          f"ties did not take the average rank: {r.tolist()}")
    check(near(r.sum(), 0.0), f"the mean must still be 0 under ties: {r.sum()}")
    one = cs_rank(pd.Series([np.nan, np.nan, 7.0, np.nan, np.nan], index=IDX))
    check(near(one.loc[103], 0.0), f"a lone name must map to 0, not +/-0.5: {one.loc[103]}")
    return "ties take the average rank; a lone name maps to 0"


# ------------------------------------------------------------- neutralize
def test_neutralize_group_means():
    v = pd.Series([1.0, 3.0, 10.0, 20.0, 30.0], index=IDX)
    g = pd.Series([1, 1, 2, 2, 2], index=IDX)
    out = cs_neutralize(v, g)
    for k in (1, 2):
        m = out[g == k].mean()
        check(near(m, 0.0, 1e-12), f"group {k} has mean {m} != 0 after demean")
    check(near(out.loc[101], -1.0) and near(out.loc[103], -10.0), f"wrong values: {out.tolist()}")
    return "every group mean is approximately 0"


def test_neutralize_nan_group_and_nan_value():
    v = pd.Series([1.0, 3.0, np.nan, 20.0, 30.0], index=IDX)
    g = pd.Series([1.0, 1.0, 2.0, np.nan, np.nan], index=IDX)
    out = cs_neutralize(v, g)
    check(np.isnan(out.loc[103]), "a NaN value did not stay NaN")
    # 分组缺失的 104/105 自成一组：dropna=True 会把它们静默变成 NaN（覆盖率无声下降）
    check(out.loc[104:105].notna().all(), f"the NaN group was dropped: {out.tolist()}")
    check(near(out.loc[104], -5.0) and near(out.loc[105], 5.0), f"wrong values: {out.tolist()}")
    return "the NaN group forms its own group; NaN values are preserved"


# ---------------------------------------------------------------- 衰减 / 滞后
def test_linear_decay_hand_computed():
    """n=3，权重 [今日 3, 昨日 2, 前日 1]，5 天玩具序列 1..5，手算逐日：

        t0  3·1                 / 3       = 1.0
        t1  (3·2 + 2·1)         / (3+2)   = 1.6
        t2  (3·3 + 2·2 + 1·1)   / 6       = 14/6
        t3  (3·4 + 2·3 + 1·2)   / 6       = 20/6
        t4  (3·5 + 2·4 + 1·3)   / 6       = 26/6
    """
    want = [1.0, 1.6, 14 / 6, 20 / 6, 26 / 6]
    ch = OpChain([("linear_decay", 3)], None)
    got = [float(ch(pd.Series([x], index=[101], dtype=float), t)[101])
           for t, x in enumerate([1.0, 2.0, 3.0, 4.0, 5.0])]
    for t, (a, b) in enumerate(zip(got, want)):
        check(near(a, b, 1e-12), f"t={t}: got {a}, hand-computed {b}")
    check(ch.lookback == 2, f"the warmup floor for linear_decay:3 must be 2, got {ch.lookback}")
    return "matches the hand computation day by day: " + ", ".join(f"{x:.4f}" for x in got)


def test_linear_decay_nan_is_weight_zero():
    """附录 B：缓冲含 NaN → 该票该日按权重 0 参与，不传染整条缓冲。"""
    ch = OpChain([("linear_decay", 3)], None)
    got = [float(ch(pd.Series([x], index=[101], dtype=float), t)[101])
           for t, x in enumerate([1.0, np.nan, 3.0])]
    # t1: 缓冲 [nan(今), 1(昨)] → 2·1/2 = 1.0（若 NaN 传染则整条是 NaN）
    # t2: 缓冲 [3, nan, 1]      → (3·3 + 1·1)/(3+1) = 2.5
    check(near(got[1], 1.0), f"t=1 must be 1.0 (NaN carries weight 0), got {got[1]}")
    check(near(got[2], 2.5), f"t=2 must be 2.5, got {got[2]}")
    allnan = OpChain([("linear_decay", 2)], None)
    check(np.isnan(allnan(pd.Series([np.nan], index=[101]), 0)[101]),
          "an all-NaN buffer must output NaN, not 0")
    return "NaN participates with weight 0 and does not spread; all-NaN yields NaN"


def test_exp_decay_matches_closed_form():
    """递推形式必须等于直接按 0.5**(j/h) 加权的定义式（h=2，序列 1..4）。"""
    h, xs = 2, [1.0, 2.0, 3.0, 4.0]
    ch = OpChain([("exp_decay", h)], None)
    got = [float(ch(pd.Series([x], index=[101], dtype=float), t)[101])
           for t, x in enumerate(xs)]
    for t in range(len(xs)):
        w = np.array([0.5 ** (j / h) for j in range(t + 1)])       # j=0 是今日
        want = float((w * np.array(xs[t::-1])).sum() / w.sum())
        check(near(got[t], want, 1e-12), f"t={t}: recurrence {got[t]} != closed form {want}")
    return "recurrence equals the closed form: " + ", ".join(f"{x:.4f}" for x in got)


def test_delay_lags():
    ch = OpChain([("delay", 2)], None)
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    got = [float(ch(pd.Series([x], index=[101], dtype=float), t)[101])
           for t, x in enumerate(xs)]
    check(np.isnan(got[0]) and np.isnan(got[1]), f"the first k days have no history and must be NaN: {got[:2]}")
    check([got[2], got[3], got[4]] == [1.0, 2.0, 3.0], f"not lagged by 2 days: {got}")
    check(OpChain([("delay", 2)], None).lookback == 2, "the warmup floor for delay must be k")
    return f"k=2 → {got}"


# ------------------------------------------------------------ truncate
def test_truncate_binds():
    v = pd.Series([0.90, 0.04, 0.03, 0.02, 0.01], index=IDX)   # gross = 1.0
    out = cs_truncate(v, 0.10)
    check(near(out.loc[101], 0.10), f"the cap did not clamp the largest name: {out.loc[101]}")
    check(out.loc[102:].tolist() == v.loc[102:].tolist(), "a name that should not have moved was changed")
    neg = cs_truncate(pd.Series([-0.90, 0.10], index=[1, 2]), 0.25)
    check(near(neg.loc[1], -0.25), f"the short side was not clamped: {neg.loc[1]}")
    keep = cs_truncate(v, 0.95)
    check(near(keep.loc[101], 0.90), "nothing should change when the cap exceeds the largest name")
    nanv = cs_truncate(pd.Series([np.nan, 1.0], index=[1, 2]), 0.5)
    check(np.isnan(nanv.loc[1]), "truncate swallowed a NaN")
    return "0.90 -> 0.10 (x=0.10, gross=1.0); shorts symmetric; NaN preserved"


# --------------------------------------------------------------- scale
def test_scale_normalizes_and_zeroes_out_of_pool():
    """§3.5 闸门二：scale 之后池外必须是**精确的 0**（NaN 的权重不是权重）。

    这里故意模拟一个忘了做闸门一的 caller：池外的 104/105 带着非零值进链。
    """
    uni = Univ(members=[101, 102, 103])
    v = pd.Series([2.0, -1.0, 1.0, 99.0, -99.0], index=IDX)
    w = OpChain([("scale", "book")], uni)(v, 0)
    check(w.notna().all(), f"NaN appeared in the weights: {w.tolist()}")
    check(float(w.loc[104]) == 0.0 and float(w.loc[105]) == 0.0,
          f"outside-pool weights were not forced to zero: {w.loc[104]}, {w.loc[105]}")
    check(near(w.abs().sum(), 1.0), f"Σ|w| = {w.abs().sum()} ≠ 1")
    check(near(w.loc[101], 0.5), f"in-pool proportions were contaminated by outside-pool values: {w.loc[101]}")
    # 池内的 NaN 也变 0（附录 B：scale 时 NaN → 权重 0）
    v2 = pd.Series([2.0, np.nan, 2.0, 0.0, 0.0], index=IDX)
    w2 = OpChain([("scale", "book")], uni)(v2, 0)
    check(float(w2.loc[102]) == 0.0 and near(w2.abs().sum(), 1.0),
          f"an in-pool NaN did not become 0: {w2.tolist()}")
    return "Sigma|w|=1, outside-pool == 0.0, in-pool NaN becomes 0"


def test_scale_cancellation_0484():
    """§4.10 例 7：三条各自 Σ|w|=1 的 alpha 按 0.4/0.3/0.3 混合，Σ|w| 只剩 0.484。"""
    rng = np.random.default_rng(846)
    idx = pd.Index(range(12))
    a, b, c = (pd.Series(rng.standard_normal(12), index=idx) for _ in range(3))
    a, b, c = (x / x.abs().sum() for x in (a, b, c))
    for x in (a, b, c):
        check(near(x.abs().sum(), 1.0), "the upstream alpha does not itself satisfy Sigma|w|=1")
    mix = 0.4 * a + 0.3 * b + 0.3 * c
    gross = cs_gross(mix)
    check(0.40 < gross < 0.60, f"cancellation was not reproduced (gross={gross:.4f})")
    check(gross < 0.99, "Sigma|w| is still 1 after mixing -- this test is not testing anything")
    w = OpChain([("scale", "book")], None)(mix, 0)
    check(near(w.abs().sum(), 1.0, 1e-12), f"after scale Sigma|w| = {w.abs().sum()} != 1")
    check(near((w / mix).dropna().std(), 0.0, 1e-9), "scale altered the relative shape of the weights")
    return f"Sigma|w| = {gross:.4f} after mixing (docs measured 0.484) -> {w.abs().sum():.12f} after scale"


def test_scale_degenerate_gross_zero():
    """gross=0 时不做除法（0/0 会把整本账变 NaN/inf），且必须出声。"""
    ch = OpChain([("scale", "book")], None)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        w = ch(pd.Series([0.0, 0.0, np.nan, 0.0, 0.0], index=IDX), 0)
        w2 = ch(pd.Series([np.nan] * 5, index=IDX), 1)
    check(w.notna().all() and w2.notna().all(), "a degenerate day returned NaN weights")
    check(float(w.abs().sum()) == 0.0, f"a degenerate day must be an honestly empty book: {w.tolist()}")
    check(ch.degenerate_scale == [0, 1], f"the degenerate day was not recorded: {ch.degenerate_scale}")
    check(len(caught) == 1 and issubclass(caught[0].category, RuntimeWarning),
          f"must warn exactly once (the rest are only counted): {[str(x.message)[:40] for x in caught]}")
    return f"recorded {ch.degenerate_scale}, warned {len(caught)} time(s)"


# ------------------------------------------------------------- 两道闸门
def test_gate1_scope_of_cs_ops():
    """闸门一：CS 算子的 scope 是池子。少了它，rank 的 scope 静默退化成全集。"""
    v = pd.Series([1.0, 2.0, 3.0, 400.0, 500.0], index=IDX)
    inside = OpChain([("rank", None)], Univ(members=[101, 102, 103]))(v, 0)
    check(inside.loc[104:].isna().all(), f"outside-pool was not forced to NaN: {inside.tolist()}")
    check(near(inside.loc[101], -0.5) and near(inside.loc[103], 0.5),
          f"scope is not the three in-pool names: {inside.tolist()}")
    wide = OpChain([("rank", None)], None)(v, 0)
    check(not near(wide.loc[103], 0.5), "with no pool the scope should be the full set -- this test does not distinguish them")
    return "3 in-pool names rank to +/-0.5; with no pool the scope widens to 5 (different convention, neither errors)"


def test_gate2_catches_ts_carry_across_pool_exit():
    """「两端夹住、中间自由」：delay 会把昨天的值搬到今天，而那只票今天可能已出池。

    中间不夹（TS 算子照常搬运），出口夹（scale 后归 0）——这正是闸门二存在的理由。
    """
    uni = Univ(members=[101, 102], by_t={0: [101, 102, 103], 1: [101, 102]})
    ch = OpChain([("delay", 1), ("scale", "book")], uni)
    with warnings.catch_warnings():
        # 带 delay 的链第一天必然空仓（无历史），那声退化告警是对的，不是本例的被测项
        warnings.simplefilter("ignore", RuntimeWarning)
        ch(pd.Series([1.0, 1.0, 1.0, np.nan, np.nan], index=IDX), 0)
    w = ch(pd.Series([1.0, 1.0, np.nan, np.nan, np.nan], index=IDX), 1)
    check(float(w.loc[103]) == 0.0, f"a name that left the pool carried its old weight into the dump: {w.loc[103]}")
    # 两条承诺必须同时成立：先归一再抹 0 的写法这里会得到 Σ|w| = 2/3
    check(near(w.abs().sum(), 1.0), f"Sigma|w| = {w.abs().sum()} != 1 (did the mask land in the denominator?)")
    check(near(w.loc[101], 0.5) and near(w.loc[102], 0.5),
          f"the share released by the departing name was not redistributed: {w.tolist()}")
    return "103, in the pool at t=0 and out at t=1, goes to 0 while Sigma|w| stays 1"


# ------------------------------------------------------- 预热 / 状态隔离
def test_warmup_calls_prime_state():
    """§7.1：预热段照常调用、只喂状态不进输出。状态推进与输出是否被采用无关。"""
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ch = OpChain([("linear_decay", 3)], None)
    out = [float(ch(pd.Series([x], index=[101], dtype=float), t)[101])
           for t, x in enumerate(xs)]
    warmed = out[-1]                       # 预热 t=0..2，取用 t=3..4
    cold = OpChain([("linear_decay", 3)], None)
    cold_last = float(cold(pd.Series([xs[-1]], index=[101], dtype=float), 0)[101])
    check(near(warmed, 26 / 6), f"after warmup t=4 must be 26/6, got {warmed}")
    check(near(cold_last, 5.0) and not near(cold_last, warmed),
          "skipping warmup gave the same result -- then warmup is not feeding any state")
    return f"warmed {warmed:.4f} vs cold {cold_last:.4f}"


def test_reset_gives_identical_results():
    """同一条链跑两遍、中间 reset()，必须逐位一致（op-state 归零干净）。"""
    uni = Univ(members=[101, 102, 103, 104], groups=[1, 1, 2, 2, 2])
    ops = [("rank", None), ("neutralize", "g_common.field_common_ref.sector"),
           ("delay", 1), ("linear_decay", 3), ("exp_decay", 2),
           ("truncate", 0.4), ("scale", "book")]
    ch = OpChain(ops, uni)
    rng = np.random.default_rng(7)
    days = [pd.Series(rng.standard_normal(5), index=IDX) for _ in range(8)]
    days[3].iloc[1] = np.nan                     # 中途带个洞，确保状态真的分叉
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)      # see above: the first day of delay
        first = [ch(d.copy(), t) for t, d in enumerate(days)]
        ch.reset()
        second = [ch(d.copy(), t) for t, d in enumerate(days)]
    for t, (a, b) in enumerate(zip(first, second)):
        check(np.array_equal(a.to_numpy(), b.to_numpy()),
              f"t={t} differs between the two passes:\n  {a.to_numpy()}\n  {b.to_numpy()}")
    dirty = OpChain(ops, uni)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for t, d in enumerate(days):
            dirty(d.copy(), t)
    dirty._last_t = None                          # 不 reset，只放行游标
    polluted = dirty(days[0].copy(), 0)
    check(not np.array_equal(polluted.to_numpy(), first[0].to_numpy()),
          "the same result without reset -- this chain has no op-state, so the test is vacuous")
    check(near(first[-1].abs().sum(), 1.0), "Sigma|w| != 1 at the end of the seven-op chain")
    return "7 ops x 8 days agree bit for bit; without reset they differ from day one"


def test_cursor_must_advance_by_one():
    ch = OpChain([("delay", 1)], None)
    ch(pd.Series([1.0], index=[101]), 10)
    try:
        ch(pd.Series([1.0], index=[101]), 12)      # 漏掉 t=11
    except ValueError as e:
        check("cursor jumped" in str(e), f"the error message is not explicit enough: {e}")
    else:
        raise AssertionError("skipping a day did not raise -- TS buffers would shift silently")
    ch.reset()
    ch(pd.Series([1.0], index=[101]), 99)          # reset 之后任意起点合法
    return "skipping a day fails loudly; after reset a different period is allowed"


# ---------------------------------------------------------------- 一致性
def test_op_names_match_config():
    """执行器认识的算子集合必须与 config 的编译期校验表一致——两边漂移的后果是
    config 放行了一个执行期不认识的算子（或反之），只在跑到那一行时才炸。"""
    try:
        from alpha_kit.core.config import CS_OPS, OP_TYPES, TS_OPS
    except Exception as e:                          # noqa: BLE001
        return f"skipped (core.config could not be imported: {e})"
    check(set(OPS) == set(OP_TYPES), f"op sets have drifted: {set(OPS) ^ set(OP_TYPES)}")
    check(set(CS_OPS) | set(TS_OPS) == set(OPS), "the CS/TS classification disagrees with the executor")
    return f"{len(OPS)} ops agree with config.OP_TYPES"


def test_empty_chain_is_gate1_only():
    """`ops: []` 是合法的（§4.4 缺省）：不改值，但闸门一照旧。"""
    v = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0], index=IDX)
    out = OpChain([], Univ(members=[101, 102]))(v, 0)
    check(out.loc[101:102].tolist() == [1.0, 2.0], f"the empty chain changed values: {out.tolist()}")
    check(out.loc[103:].isna().all(), f"the empty chain skipped gate 1: {out.tolist()}")
    check(np.array_equal(OpChain([], None)(v, 0).to_numpy(), v.to_numpy()),
          "an empty chain with no pool must be the identity")
    return "empty chain = identity plus gate 1"


def test_input_series_not_mutated():
    """§十「永远返回副本」：链不能写穿 ctx 交付的那一份。"""
    v = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0], index=IDX)
    before = v.to_numpy().copy()
    OpChain([("rank", None), ("scale", "book")], Univ(members=[101, 102]))(v, 0)
    check(np.array_equal(v.to_numpy(), before), f"the input was mutated in place: {v.tolist()}")
    return "the input is unchanged bit for bit"


TESTS = [
    test_rank_range_and_nan,
    test_rank_ties_and_singleton,
    test_neutralize_group_means,
    test_neutralize_nan_group_and_nan_value,
    test_linear_decay_hand_computed,
    test_linear_decay_nan_is_weight_zero,
    test_exp_decay_matches_closed_form,
    test_delay_lags,
    test_truncate_binds,
    test_scale_normalizes_and_zeroes_out_of_pool,
    test_scale_cancellation_0484,
    test_scale_degenerate_gross_zero,
    test_gate1_scope_of_cs_ops,
    test_gate2_catches_ts_carry_across_pool_exit,
    test_warmup_calls_prime_state,
    test_reset_gives_identical_results,
    test_cursor_must_advance_by_one,
    test_op_names_match_config,
    test_empty_chain_is_gate1_only,
    test_input_series_not_mutated,
]

if __name__ == "__main__":
    print(f"ops self-check  ({len(TESTS)} tests)  pandas {pd.__version__} / numpy {np.__version__}\n")
    for t in TESTS:
        run(t)
    print(f"\n{len(TESTS) - len(FAILS)}/{len(TESTS)} passed" +
          (f"; failed: {', '.join(FAILS)}" if FAILS else ""))
    sys.exit(1 if FAILS else 0)
