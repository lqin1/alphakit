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
        assert self._g is not None, "该测试没有配分组字段"
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
          f"NaN 位没有保持：{r.tolist()}")
    fin = r.dropna()
    check(near(fin.min(), -0.5) and near(fin.max(), 0.5),
          f"没有映射到闭区间 [-0.5, 0.5]：min={fin.min()} max={fin.max()}")
    check(((fin >= -0.5 - 1e-12) & (fin <= 0.5 + 1e-12)).all(), f"越界：{fin.tolist()}")
    # 4 个有效值 → (rank-1)/3 - 0.5 = [-0.5+2/3, -0.5, nan, -0.5+1/3, 0.5]
    check(near(fin.loc[102], -0.5) and near(fin.loc[105], 0.5), f"次序错：{fin.to_dict()}")
    check(near(fin.sum(), 0.0), f"rank 后截面均值应恒为 0，实得 {fin.sum()}")
    return f"min={fin.min():.3f} max={fin.max():.3f} mean={fin.mean():.1e}"


def test_rank_ties_and_singleton():
    r = cs_rank(pd.Series([1.0, 1.0, 1.0, 2.0, 2.0], index=IDX))
    check(near(r.loc[101], r.loc[102]) and near(r.loc[101], r.loc[103]),
          f"并列没有取平均名次：{r.tolist()}")
    check(near(r.sum(), 0.0), f"并列下均值也应为 0：{r.sum()}")
    one = cs_rank(pd.Series([np.nan, np.nan, 7.0, np.nan, np.nan], index=IDX))
    check(near(one.loc[103], 0.0), f"独苗应给 0 而非 ±0.5：{one.loc[103]}")
    return "并列取平均名次；独苗 → 0"


# ------------------------------------------------------------- neutralize
def test_neutralize_group_means():
    v = pd.Series([1.0, 3.0, 10.0, 20.0, 30.0], index=IDX)
    g = pd.Series([1, 1, 2, 2, 2], index=IDX)
    out = cs_neutralize(v, g)
    for k in (1, 2):
        m = out[g == k].mean()
        check(near(m, 0.0, 1e-12), f"组 {k} demean 后均值 {m} ≠ 0")
    check(near(out.loc[101], -1.0) and near(out.loc[103], -10.0), f"值错：{out.tolist()}")
    return "每组均值 ≈ 0"


def test_neutralize_nan_group_and_nan_value():
    v = pd.Series([1.0, 3.0, np.nan, 20.0, 30.0], index=IDX)
    g = pd.Series([1.0, 1.0, 2.0, np.nan, np.nan], index=IDX)
    out = cs_neutralize(v, g)
    check(np.isnan(out.loc[103]), "NaN 值没有保持 NaN")
    # 分组缺失的 104/105 自成一组：dropna=True 会把它们静默变成 NaN（覆盖率无声下降）
    check(out.loc[104:105].notna().all(), f"NaN 分组被丢掉了：{out.tolist()}")
    check(near(out.loc[104], -5.0) and near(out.loc[105], 5.0), f"值错：{out.tolist()}")
    return "NaN 分组自成一组；NaN 值保持"


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
        check(near(a, b, 1e-12), f"t={t}: 实得 {a}，手算 {b}")
    check(ch.lookback == 2, f"linear_decay:3 的预热下限应是 2，实得 {ch.lookback}")
    return "逐日与手算一致：" + ", ".join(f"{x:.4f}" for x in got)


def test_linear_decay_nan_is_weight_zero():
    """附录 B：缓冲含 NaN → 该票该日按权重 0 参与，不传染整条缓冲。"""
    ch = OpChain([("linear_decay", 3)], None)
    got = [float(ch(pd.Series([x], index=[101], dtype=float), t)[101])
           for t, x in enumerate([1.0, np.nan, 3.0])]
    # t1: 缓冲 [nan(今), 1(昨)] → 2·1/2 = 1.0（若 NaN 传染则整条是 NaN）
    # t2: 缓冲 [3, nan, 1]      → (3·3 + 1·1)/(3+1) = 2.5
    check(near(got[1], 1.0), f"t=1 应为 1.0（NaN 权重 0），实得 {got[1]}")
    check(near(got[2], 2.5), f"t=2 应为 2.5，实得 {got[2]}")
    allnan = OpChain([("linear_decay", 2)], None)
    check(np.isnan(allnan(pd.Series([np.nan], index=[101]), 0)[101]),
          "全 NaN 缓冲应输出 NaN，而不是 0")
    return "NaN 按权重 0 参与，不传染；全 NaN → NaN"


def test_exp_decay_matches_closed_form():
    """递推形式必须等于直接按 0.5**(j/h) 加权的定义式（h=2，序列 1..4）。"""
    h, xs = 2, [1.0, 2.0, 3.0, 4.0]
    ch = OpChain([("exp_decay", h)], None)
    got = [float(ch(pd.Series([x], index=[101], dtype=float), t)[101])
           for t, x in enumerate(xs)]
    for t in range(len(xs)):
        w = np.array([0.5 ** (j / h) for j in range(t + 1)])       # j=0 是今日
        want = float((w * np.array(xs[t::-1])).sum() / w.sum())
        check(near(got[t], want, 1e-12), f"t={t}: 递推 {got[t]} ≠ 定义式 {want}")
    return "递推 = 定义式：" + ", ".join(f"{x:.4f}" for x in got)


def test_delay_lags():
    ch = OpChain([("delay", 2)], None)
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    got = [float(ch(pd.Series([x], index=[101], dtype=float), t)[101])
           for t, x in enumerate(xs)]
    check(np.isnan(got[0]) and np.isnan(got[1]), f"前 k 天应无历史 → NaN：{got[:2]}")
    check([got[2], got[3], got[4]] == [1.0, 2.0, 3.0], f"没有滞后 2 天：{got}")
    check(OpChain([("delay", 2)], None).lookback == 2, "delay 的预热下限应是 k")
    return f"k=2 → {got}"


# ------------------------------------------------------------ truncate
def test_truncate_binds():
    v = pd.Series([0.90, 0.04, 0.03, 0.02, 0.01], index=IDX)   # gross = 1.0
    out = cs_truncate(v, 0.10)
    check(near(out.loc[101], 0.10), f"上限没有夹住龙头：{out.loc[101]}")
    check(out.loc[102:].tolist() == v.loc[102:].tolist(), "不该动的票被动了")
    neg = cs_truncate(pd.Series([-0.90, 0.10], index=[1, 2]), 0.25)
    check(near(neg.loc[1], -0.25), f"空头侧没有夹住：{neg.loc[1]}")
    keep = cs_truncate(v, 0.95)
    check(near(keep.loc[101], 0.90), "上限宽于最大票时不该动手")
    nanv = cs_truncate(pd.Series([np.nan, 1.0], index=[1, 2]), 0.5)
    check(np.isnan(nanv.loc[1]), "truncate 吃掉了 NaN")
    return "0.90 → 0.10 (x=0.10, gross=1.0)；空头对称；NaN 保持"


# --------------------------------------------------------------- scale
def test_scale_normalizes_and_zeroes_out_of_pool():
    """§3.5 闸门二：scale 之后池外必须是**精确的 0**（NaN 的权重不是权重）。

    这里故意模拟一个忘了做闸门一的 caller：池外的 104/105 带着非零值进链。
    """
    uni = Univ(members=[101, 102, 103])
    v = pd.Series([2.0, -1.0, 1.0, 99.0, -99.0], index=IDX)
    w = OpChain([("scale", "book")], uni)(v, 0)
    check(w.notna().all(), f"权重里出现了 NaN：{w.tolist()}")
    check(float(w.loc[104]) == 0.0 and float(w.loc[105]) == 0.0,
          f"池外没有被强制归零：{w.loc[104]}, {w.loc[105]}")
    check(near(w.abs().sum(), 1.0), f"Σ|w| = {w.abs().sum()} ≠ 1")
    check(near(w.loc[101], 0.5), f"池内比例被池外的值污染了：{w.loc[101]}")
    # 池内的 NaN 也变 0（附录 B：scale 时 NaN → 权重 0）
    v2 = pd.Series([2.0, np.nan, 2.0, 0.0, 0.0], index=IDX)
    w2 = OpChain([("scale", "book")], uni)(v2, 0)
    check(float(w2.loc[102]) == 0.0 and near(w2.abs().sum(), 1.0),
          f"池内 NaN 没有变 0：{w2.tolist()}")
    return "Σ|w|=1，池外 == 0.0，池内 NaN → 0"


def test_scale_cancellation_0484():
    """§4.10 例 7：三条各自 Σ|w|=1 的 alpha 按 0.4/0.3/0.3 混合，Σ|w| 只剩 0.484。"""
    rng = np.random.default_rng(846)
    idx = pd.Index(range(12))
    a, b, c = (pd.Series(rng.standard_normal(12), index=idx) for _ in range(3))
    a, b, c = (x / x.abs().sum() for x in (a, b, c))
    for x in (a, b, c):
        check(near(x.abs().sum(), 1.0), "上游 alpha 自己就不是 Σ|w|=1")
    mix = 0.4 * a + 0.3 * b + 0.3 * c
    gross = cs_gross(mix)
    check(0.40 < gross < 0.60, f"没有复现出抵消（gross={gross:.4f}）")
    check(gross < 0.99, "混合后 Σ|w| 居然还是 1——这个测试就没在测东西")
    w = OpChain([("scale", "book")], None)(mix, 0)
    check(near(w.abs().sum(), 1.0, 1e-12), f"scale 之后 Σ|w| = {w.abs().sum()} ≠ 1")
    check(near((w / mix).dropna().std(), 0.0, 1e-9), "scale 改变了权重的相对形状")
    return f"混合后 Σ|w| = {gross:.4f}（文档实测 0.484）→ scale 后 {w.abs().sum():.12f}"


def test_scale_degenerate_gross_zero():
    """gross=0 时不做除法（0/0 会把整本账变 NaN/inf），且必须出声。"""
    ch = OpChain([("scale", "book")], None)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        w = ch(pd.Series([0.0, 0.0, np.nan, 0.0, 0.0], index=IDX), 0)
        w2 = ch(pd.Series([np.nan] * 5, index=IDX), 1)
    check(w.notna().all() and w2.notna().all(), "退化日返回了 NaN 权重")
    check(float(w.abs().sum()) == 0.0, f"退化日应是诚实的空账：{w.tolist()}")
    check(ch.degenerate_scale == [0, 1], f"退化日没有被记录：{ch.degenerate_scale}")
    check(len(caught) == 1 and issubclass(caught[0].category, RuntimeWarning),
          f"应恰好告警一次（其余只累计）：{[str(x.message)[:40] for x in caught]}")
    return f"记录 {ch.degenerate_scale}，告警 {len(caught)} 次"


# ------------------------------------------------------------- 两道闸门
def test_gate1_scope_of_cs_ops():
    """闸门一：CS 算子的 scope 是池子。少了它，rank 的 scope 静默退化成全集。"""
    v = pd.Series([1.0, 2.0, 3.0, 400.0, 500.0], index=IDX)
    inside = OpChain([("rank", None)], Univ(members=[101, 102, 103]))(v, 0)
    check(inside.loc[104:].isna().all(), f"池外没有被压成 NaN：{inside.tolist()}")
    check(near(inside.loc[101], -0.5) and near(inside.loc[103], 0.5),
          f"scope 不是池内三只：{inside.tolist()}")
    wide = OpChain([("rank", None)], None)(v, 0)
    check(not near(wide.loc[103], 0.5), "无池子时 scope 本就该是全集——测试没区分开")
    return "池内 3 只重排为 ±0.5；无池子时 scope 退化成 5 只（口径不同、都不报错）"


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
    check(float(w.loc[103]) == 0.0, f"出池票带着搬过来的权重进了 dump：{w.loc[103]}")
    # 两条承诺必须同时成立：先归一再抹 0 的写法这里会得到 Σ|w| = 2/3
    check(near(w.abs().sum(), 1.0), f"Σ|w| = {w.abs().sum()} ≠ 1（掩码进了分母之后？）")
    check(near(w.loc[101], 0.5) and near(w.loc[102], 0.5),
          f"出池票让出的那份没有重新分配：{w.tolist()}")
    return "t=0 在池、t=1 出池的 103 归 0，且 Σ|w| 仍是 1"


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
    check(near(warmed, 26 / 6), f"预热后 t=4 应是 26/6，实得 {warmed}")
    check(near(cold_last, 5.0) and not near(cold_last, warmed),
          "不预热却得到相同结果——那预热就没在喂状态")
    return f"预热 {warmed:.4f} vs 不预热 {cold_last:.4f}"


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
        warnings.simplefilter("ignore", RuntimeWarning)      # 见上：delay 的第一天
        first = [ch(d.copy(), t) for t, d in enumerate(days)]
        ch.reset()
        second = [ch(d.copy(), t) for t, d in enumerate(days)]
    for t, (a, b) in enumerate(zip(first, second)):
        check(np.array_equal(a.to_numpy(), b.to_numpy()),
              f"t={t} 两遍不一致：\n  {a.to_numpy()}\n  {b.to_numpy()}")
    dirty = OpChain(ops, uni)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for t, d in enumerate(days):
            dirty(d.copy(), t)
    dirty._last_t = None                          # 不 reset，只放行游标
    polluted = dirty(days[0].copy(), 0)
    check(not np.array_equal(polluted.to_numpy(), first[0].to_numpy()),
          "不 reset 也拿到同样的结果——说明这条链根本没有 op-state，测试是空的")
    check(near(first[-1].abs().sum(), 1.0), "七件套链尾 Σ|w| ≠ 1")
    return f"7 个算子 × 8 天逐位一致；不 reset 则第一天就不同"


def test_cursor_must_advance_by_one():
    ch = OpChain([("delay", 1)], None)
    ch(pd.Series([1.0], index=[101]), 10)
    try:
        ch(pd.Series([1.0], index=[101]), 12)      # 漏掉 t=11
    except ValueError as e:
        check("游标跳变" in str(e), f"报错信息没说清楚：{e}")
    else:
        raise AssertionError("跳日没有报错——TS 缓冲会整体错位而不出声")
    ch.reset()
    ch(pd.Series([1.0], index=[101]), 99)          # reset 之后任意起点合法
    return "跳日吵闹地失败；reset 后可换区间"


# ---------------------------------------------------------------- 一致性
def test_op_names_match_config():
    """执行器认识的算子集合必须与 config 的编译期校验表一致——两边漂移的后果是
    config 放行了一个执行期不认识的算子（或反之），只在跑到那一行时才炸。"""
    try:
        from alpha_kit.core.config import CS_OPS, OP_TYPES, TS_OPS
    except Exception as e:                          # noqa: BLE001
        return f"跳过（core.config 不可导入：{e}）"
    check(set(OPS) == set(OP_TYPES), f"算子集合漂移：{set(OPS) ^ set(OP_TYPES)}")
    check(set(CS_OPS) | set(TS_OPS) == set(OPS), "CS/TS 分类与执行器不一致")
    return f"{len(OPS)} 个算子与 config.OP_TYPES 一致"


def test_empty_chain_is_gate1_only():
    """`ops: []` 是合法的（§4.4 缺省）：不改值，但闸门一照旧。"""
    v = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0], index=IDX)
    out = OpChain([], Univ(members=[101, 102]))(v, 0)
    check(out.loc[101:102].tolist() == [1.0, 2.0], f"空链改了值：{out.tolist()}")
    check(out.loc[103:].isna().all(), f"空链漏了闸门一：{out.tolist()}")
    check(np.array_equal(OpChain([], None)(v, 0).to_numpy(), v.to_numpy()),
          "无池子的空链应是恒等")
    return "空链 = 恒等 + 闸门一"


def test_input_series_not_mutated():
    """§十「永远返回副本」：链不能写穿 ctx 交付的那一份。"""
    v = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0], index=IDX)
    before = v.to_numpy().copy()
    OpChain([("rank", None), ("scale", "book")], Univ(members=[101, 102]))(v, 0)
    check(np.array_equal(v.to_numpy(), before), f"入参被就地改了：{v.tolist()}")
    return "入参逐位不变"


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
    print(f"ops 自检  ({len(TESTS)} 项)  pandas {pd.__version__} / numpy {np.__version__}\n")
    for t in TESTS:
        run(t)
    print(f"\n{len(TESTS) - len(FAILS)}/{len(TESTS)} 通过" +
          (f"；失败：{', '.join(FAILS)}" if FAILS else ""))
    sys.exit(1 if FAILS else 0)
