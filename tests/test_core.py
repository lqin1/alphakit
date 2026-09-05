"""core 层的自检脚本（architecture.md §3.2 / §3.3 / §3.6 / §4.11 / §十三 3–4）。

    .venv/bin/python tests/test_core.py        # 失败则退出码非 0

不依赖 pytest：§十三 要的是「跑一条命令就知道对不对」，多一个依赖就多一个装不上的
理由。格式与 tests/test_ops.py 一致，`ok`/`FAIL` 各占一行，run_all.py 按行计数。

core 是 runner 与 pnl 共同的地基：轴错一格，历史每一列的含义都跟着错；命名放行一个
非法名字，模块加载期就是 SyntaxError。**故这里的每条断言都对着文档里明写的一条承诺**，
而不是对着当前实现的行为——两者不一致时红的是实现，红行末尾标了 §节号，照着查即可。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import zarr

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alpha_kit.core.axes import Axes                              # noqa: E402
from alpha_kit.core.store import Store, StoreError                # noqa: E402
from alpha_kit.core.config import (                               # noqa: E402
    CS_OPS, OP_TYPES, TAGS, TS_OPS, load_spec)
try:                                    # 命名层已从 config 拆出（core/naming.py）
    from alpha_kit.core.naming import (                           # noqa: E402
        KINDS, ConfigError, Ref, check_name, parse_ref)
except ImportError:                     # 拆分前它住在 config 里, 两种布局都要能跑
    from alpha_kit.core.config import (                           # noqa: E402,F811
        KINDS, ConfigError, Ref, check_name, parse_ref)

# 只用临时目录：仓库里的 storage/ 是真数据, 自检绝不许碰它。
TMP = Path(os.environ.get(
    "ALPHAKIT_TEST_TMP",
    "/tmp/claude-1000/-home-ubuntu-workspace-alphakit"
    "/6ece511a-8e20-4887-ba36-f30641366d7b/scratchpad")) / "test_core"
REGION = "us"

# 带周末缺口的日历：01-05 是周一到周五, 06/07 是周末（不在轴上）, 08-12 是下一周。
# 缺口是刻意的——`slice` 的边界若只用连续日期测, 「sd 不是交易日」这条永远走不到。
SESSIONS = [f"2024-01-{d:02d}" for d in (1, 2, 3, 4, 5, 8, 9, 10, 11, 12)]
SECURITIES = [101, 102, 103, 104, 105]
RESERVE = 7

R2 = "g_yliu.liq.factor_yliu_liq-adv20"                  # 秩-2
R1 = "g_common.field_macro_cpi.yoy"                # 秩-1
R3 = "g_common.field_taq_rv.rv_5m"                   # 秩-3
RB = "g_common.field_common_univ.us_top3000"        # bool
RI = "g_common.factor_common_gics.sector"         # int8


# --------------------------------------------------------------- 测试骨架
FAILS: list[tuple[str, str]] = []


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def run(fn):
    name = fn.__name__
    try:
        note = fn()
    except Exception as e:                      # noqa: BLE001
        FAILS.append((name, f"{type(e).__name__}: {e}"))
        print(f"FAIL {name}\n     {type(e).__name__}: {e}")
    else:
        print(f"ok   {name}" + (f"   [{note}]" if note else ""))


def fresh(tag: str, sessions=None, securities=None, reserve: int = RESERVE):
    """一个空 store。轴的落点跟着 Store 走, 不写死——存储层挪过一次轴的位置了。"""
    root = TMP / tag
    shutil.rmtree(root, ignore_errors=True)
    ss = list(SESSIONS if sessions is None else sessions)
    sc = list(SECURITIES if securities is None else securities)
    Axes.create(root / REGION, ss, sc, reserve=reserve)
    try:
        st = Store(root, REGION)
    except FileNotFoundError:               # 轴曾经在 region 之上, 两种布局都兜住
        Axes.create(root, ss, sc, reserve=reserve)
        st = Store(root, REGION)
    return st


def panel(dates, cols, val=1.0):
    return pd.DataFrame(np.full((len(dates), len(cols)), val, dtype="f4"),
                        index=list(dates), columns=list(cols))


def raises(exc, fn, *a, **kw):
    """返回被抛出的异常；没抛就是断言失败（消息带上它反而放行的那个入参）。"""
    try:
        fn(*a, **kw)
    except exc as e:
        return e
    except Exception as e:                      # noqa: BLE001
        raise AssertionError(f"raised {type(e).__name__} (expected {exc.__name__}): {e}") from None
    raise AssertionError(f"did not raise {exc.__name__} -- it was let through")


def du(p: Path) -> tuple[int, int]:
    tot = files = 0
    for r, _, fs in os.walk(p):
        for f in fs:
            tot += os.path.getsize(os.path.join(r, f)); files += 1
    return tot, files


# ==================================================================== 轴 §3.3
def test_axes_create_load_roundtrip():
    """轴是唯一真相源, 写下去与读回来必须逐位相同——它错一格, 每个节点都跟着错。"""
    st = fresh("ax_rt")
    a = st.axes
    check(a.sessions == SESSIONS, f"sessions changed: {a.sessions}")
    check(a.securities == SECURITIES, f"securities changed: {a.securities}")
    check(a.n_sessions == 10 and a.n_securities == 5, f"{a.n_sessions}×{a.n_securities}")
    cap = json.loads((Path(a.root) / "_axes" / "capacity.json").read_text())
    check(cap["n_active"] == 5 and cap["allocated"] == 12, f"capacity.json = {cap}")
    b = Axes.load(a.root)
    check(b.sessions == a.sessions and b.securities == a.securities
          and b.allocated == a.allocated, "the second load disagrees with the first")
    return f"D={a.n_sessions} N={a.n_securities} allocated={a.allocated}"


def test_pos_date_inverse():
    """`pos` 与 `date` 必须互为逆。§3.2 的路径推导、§7.2 的主循环全靠这一条。"""
    a = fresh("ax_pos").axes
    for i, d in enumerate(SESSIONS):
        check(a.pos(d) == i, f"pos({d})={a.pos(d)} expected {i}")
        check(a.date(i) == d, f"date({i})={a.date(i)} expected {d}")
    e = raises(KeyError, a.pos, "2024-01-06")           # 周末不在轴上
    check("2024-01-06" in str(e), f"the error does not say which date: {e}")
    return f"{a.n_sessions} sessions map both ways one by one; pos raises KeyError on a non-trading day"


def test_slice_half_open():
    """[sd, ed] 闭区间 → [i0, i1) 半开位置区间；None 不设限；越界与非交易日不报错。"""
    a = fresh("ax_slice").axes
    cases = {
        (None, None): (0, 10),                          # 不设限 = 全轴
        ("2024-01-03", "2024-01-05"): (2, 5),           # 闭区间含 ed → 3 个 session
        ("2024-01-01", "2024-01-01"): (0, 1),           # 单日 = 宽度 1
        (None, "2024-01-05"): (0, 5),
        ("2024-01-08", None): (5, 10),
        ("2024-01-06", "2024-01-07"): (5, 5),           # 整段落在周末 → 空
        ("2024-01-06", None): (5, 10),                  # sd 不是交易日 → 向后取
        (None, "2024-01-07"): (0, 5),                   # ed 不是交易日 → 向前取
        ("2025-01-01", None): (10, 10),                 # 越过末日 → 空
        (None, "2023-01-01"): (0, 0),                   # 早于首日 → 空
        ("1900-01-01", "2100-01-01"): (0, 10),          # 两头都越界 → 全轴
    }
    for (sd, ed), want in cases.items():
        got = a.slice(sd, ed)
        check(got == want, f"slice({sd}, {ed}) = {got}, expected {want}")
    lo, hi = a.slice("2024-01-09", "2024-01-03")        # sd > ed
    check(hi - lo <= 0, f"sd is later than ed yet the range [{lo}, {hi}) is non-empty")
    return f"all {len(cases)} boundary cases hit; width with sd>ed is {hi - lo}"


def test_ensure_sessions_appends():
    """按日期 append 是 §3.3 的 O(1) 承诺：只在末尾长, 已有位置一个都不许动。"""
    st = fresh("ax_app")
    a = st.axes
    before = {d: a.pos(d) for d in SESSIONS}
    n = a.ensure_sessions(["2024-01-15", "2024-01-16"])
    check(n == 2, f"reported {n} new entries, expected 2")
    check(a.n_sessions == 12, f"n_sessions={a.n_sessions}")
    check(a.sessions[-2:] == ["2024-01-15", "2024-01-16"], f"the tail is {a.sessions[-2:]}")
    for d, i in before.items():
        check(a.pos(d) == i, f"after append the position of {d} moved from {i} to {a.pos(d)} -- every historical chunk is invalidated")
    check(a.ensure_sessions(["2024-01-16"]) == 0, "a repeated last session must not append again (the daily update has to be re-entrant)")
    check(Axes.load(a.root).sessions == a.sessions, "not persisted: it would be lost on restart")
    return f"10 -> {a.n_sessions}; {len(before)} old positions unchanged; replaying the same day returns 0"


def test_ensure_sessions_rejects_earlier_date():
    """轴 append-only：接受一个不严格晚于末日的新日期 = 悄悄改写每个历史 chunk 的列义。"""
    a = fresh("ax_rej").axes
    # 早于首日 / 落在周末缺口里 / 缺口的另一半——三个都不在轴上, 且都不晚于末日
    for bad in ["2023-12-31", "2024-01-06", "2024-01-07"]:
        e = raises(ValueError, a.ensure_sessions, [bad])
        check("append-only" in str(e), f"the error for {bad} does not mention append-only: {e}")
        check(a.n_sessions == 10, f"{bad} was rejected yet had already been written to the axis: n={a.n_sessions}")
    e = raises(ValueError, a.ensure_sessions, ["2024-01-06", "2024-01-20"])
    check(a.n_sessions == 10, "one early date in a batch must cause the whole batch to be rejected atomically")
    check(a.ensure_sessions(["2024-01-12"]) == 0, "replaying the last session itself must return 0, not raise")
    return f"3 invalid dates plus 1 mixed batch all rejected; n_sessions is still {a.n_sessions}"


def test_ensure_sessions_rejects_duplicate_in_batch():
    """同一批里出现两次同一天。轴单调、位置即列义, 重复即两个位置指向同一天。

    触发方式很平常：上游给的日期列表没去重（`--ed today` 与日历合并时最容易），
    而轴是 append-only, 写进去就再也拿不掉——`pos(d)` 与 `date(i)` 从此不再互逆。
    """
    a = fresh("ax_dup").axes
    n = a.ensure_sessions(["2024-01-15", "2024-01-15"])
    check(a.n_sessions == 11,
          f"after appending ['2024-01-15', '2024-01-15'] n_sessions={a.n_sessions} (expected 11), "
          f"axis tail = {a.sessions[-3:]}, returned {n} -- the same day took two slots")
    check(a.date(a.pos("2024-01-15")) == "2024-01-15", "pos and date are no longer inverses")
    return f"n_sessions={a.n_sessions}"


def test_axes_create_refuses_to_clobber():
    """`create` 是唯一的建轴入口, 而 pipeline 每次重建都调它（build_l3_base.py）。

    它若不检查已有轴, 一次「L2 里多了一只中间序号的票」就会让 securities 整体后移,
    历史每个 chunk 的列义全错且无人报错——正是 §3.3「只增不减、单调分配」防的那件事。
    """
    st = fresh("ax_clobber")
    root = Path(st.axes.root)
    e = None
    try:
        Axes.create(root, SESSIONS, [999, 101, 102, 103, 104, 105])
    except Exception as exc:                     # noqa: BLE001
        e = exc
    check(e is not None,
          f"replaying create on an existing axis with 999 inserted first was silently accepted: securities "
          f"went from {SECURITIES} to {Axes.load(root).securities} -- column 0 of every historical "
          f"chunk now points at a different name")
    return type(e).__name__


def test_capacity_reserve():
    """预留列容量：§3.3「实际 + 500」。按标的 resize 要重写全部 chunk, 故一年一次。"""
    st = fresh("ax_cap")
    check(st.axes.allocated == len(SECURITIES) + RESERVE,
          f"allocated={st.axes.allocated}")
    st.write(R2, panel(SESSIONS, SECURITIES, 1.0))
    z = zarr.open_array(str(st.path(R2)), mode="r")
    check(z.shape[1] == st.axes.allocated,
          f"the array was sized to n_securities({st.axes.n_securities}) rather than allocated: {z.shape}")

    # 在预留额度内加一只新票（ingestion 的日常）：旧 chunk 必须仍可读, 新列是 NaN
    a = Path(st.axes.root) / "_axes"
    (a / "securities.json").write_text(json.dumps(SECURITIES + [106]))
    (a / "capacity.json").write_text(json.dumps({"n_active": 6, "allocated": st.axes.allocated}))
    st2 = Store(st.root, REGION)
    df = st2.read(R2)
    check(list(df.columns) == SECURITIES + [106], f"column axis after growth: {list(df.columns)}")
    check(df[106].isna().all(), "the history of a new name must be an all-NaN column")
    check((df[SECURITIES] == 1.0).all().all(), "old data changed after growth -- old chunks did not read back unchanged")
    return f"allocated={st.axes.allocated} array width={z.shape[1]}; old values unchanged after growth, new column all NaN"


# ================================================================== store §3.3
def test_roundtrip_three_ranks():
    """三种秩的返回类型与形状（§3.6 / §十三 3）：Series / DataFrame / ndarray。"""
    st = fresh("st_rank")
    rng = np.random.default_rng(0)
    v1 = pd.Series(rng.standard_normal(10).astype("f4"), index=SESSIONS)
    v2 = pd.DataFrame(rng.standard_normal((10, 5)).astype("f4"),
                      index=SESSIONS, columns=SECURITIES)
    v3 = rng.standard_normal((10, 5, 4)).astype("f4")
    st.write(R1, v1, dims=["di"])
    st.write(R2, v2)
    st.write(R3, v3, dims=["di", "ii", "ti"], grid_len=4, meta={"dates": SESSIONS})

    b1, b2, b3 = st.read(R1), st.read(R2), st.read(R3)
    check(isinstance(b1, pd.Series), f"rank-1 returned {type(b1).__name__}, expected Series")
    check(isinstance(b2, pd.DataFrame), f"rank-2 returned {type(b2).__name__}, expected DataFrame")
    check(isinstance(b3, np.ndarray), f"rank-3 returned {type(b3).__name__}, expected ndarray")
    check(b1.shape == (10,) and list(b1.index) == SESSIONS, f"rank-1 {b1.shape}")
    check(b2.shape == (10, 5) and list(b2.columns) == SECURITIES, f"rank-2 {b2.shape}")
    check(b3.shape == (10, 5, 4), f"rank-3 {b3.shape}")
    check(np.allclose(b1.to_numpy(), v1.to_numpy()), "rank-1 values changed")
    check(np.allclose(b2.to_numpy(), v2.to_numpy()), "rank-2 values changed")
    check(np.allclose(b3, v3), "rank-3 values changed")
    return f"(D,)={b1.shape} (D,N)={b2.shape} (D,N,T)={b3.shape}"


def test_chunking_matches_spec():
    """§3.3 的分块定稿：秩-1 (4096,) / 秩-2 (50, N_alloc) / 秩-3 (1, N_alloc, T)。

    秩-3 一日一块不是美学：日更因此仍只写 1 个 chunk 文件（§3.3 两种扩容成本那一条）。
    """
    st = fresh("st_chunk")
    st.write(R1, pd.Series(np.ones(10, dtype="f4"), index=SESSIONS), dims=["di"])
    st.write(R2, panel(SESSIONS, SECURITIES))
    st.write(R3, np.ones((10, 5, 4), dtype="f4"), dims=["di", "ii", "ti"],
             grid_len=4, meta={"dates": SESSIONS})
    N = st.axes.allocated
    got = {r: zarr.open_array(str(st.path(r)), mode="r").chunks for r in (R1, R2, R3)}
    want = {R1: (4096,), R2: (50, N), R3: (1, N, 4)}
    for r, w in want.items():
        check(got[r] == w, f"{r} chunks {got[r]}, expected {w}")
    for r in (R1, R2, R3):
        z = zarr.open_array(str(st.path(r)), mode="r")
        check(np.isnan(z.fill_value), f"fill_value of {r} is {z.fill_value}; f4 must be NaN")
    return f"rank1 {got[R1]} rank2 {got[R2]} rank3 {got[R3]}"


def test_dtypes_survive():
    """f4 / bool / i1 三种 dtype 原样往返（§3.3：bool universe 比 f4 省 22 倍）。"""
    st = fresh("st_dtype")
    f = pd.DataFrame(np.arange(50, dtype="f4").reshape(10, 5) / 8,
                     index=SESSIONS, columns=SECURITIES)
    b = pd.DataFrame(np.array([[True, False, True, False, True]] * 10),
                     index=SESSIONS, columns=SECURITIES)
    i = pd.DataFrame(np.array([[1, 2, 3, -4, 5]] * 10, dtype="i1"),
                     index=SESSIONS, columns=SECURITIES)
    st.write(R2, f, dtype="f4"); st.write(RB, b, dtype="bool"); st.write(RI, i, dtype="i1")
    gf, gb, gi = st.read(R2), st.read(RB), st.read(RI)
    check(gf.dtypes.unique().tolist() == [np.dtype("f4")], f"f4 read back as {gf.dtypes.unique()}")
    check(gb.dtypes.unique().tolist() == [np.dtype("bool")], f"bool read back as {gb.dtypes.unique()}")
    check(gi.dtypes.unique().tolist() == [np.dtype("i1")], f"i1 read back as {gi.dtypes.unique()}")
    check(np.array_equal(gf.to_numpy(), f.to_numpy()), "f4 values changed")
    check(np.array_equal(gb.to_numpy(), b.to_numpy()), "bool values changed")
    check(np.array_equal(gi.to_numpy(), i.to_numpy()), "i1 values changed")
    return "f4 / bool / i1 all round-trip unchanged"


def test_read_aligns_to_global_axis():
    """§3.3 的核心承诺, 也是 L3 相对 L2 的核心价值：读回来的列**永远**是全局轴。

    调用方零对齐负担意味着 handle 里可以直接 `a / b`——只要有一处返回「只有写过的
    列」的窄表, 那个除法就变成静默的按标签对齐, 少掉的票无声消失。
    """
    st = fresh("st_align")
    st.write(R2, panel(SESSIONS, [101, 103, 105], 2.0))          # 只覆盖 5 列中的 3 列
    df = st.read(R2)
    check(list(df.columns) == SECURITIES, f"column axis {list(df.columns)}, expected the global 5 columns")
    check(list(df.index) == SESSIONS, f"row axis {df.index[:3].tolist()}...")
    check((df[[101, 103, 105]] == 2.0).all().all(), "the values of the written columns are wrong")
    missing = [c for c in SECURITIES if df[c].isna().all()]
    check(missing == [102, 104], f"uncovered columns must be all-NaN, got {missing}")
    return f"wrote 3 of 5 columns -> read back {df.shape[1]} columns; unwritten {missing} all NaN"


def test_range_read_equals_full_sliced():
    """区间读只是全量读的切片——不然「跑得快的那条路」与「对的那条路」就分了岔。"""
    st = fresh("st_range")
    rng = np.random.default_rng(1)
    v = pd.DataFrame(rng.standard_normal((10, 5)).astype("f4"),
                     index=SESSIONS, columns=SECURITIES)
    st.write(R2, v)
    st.write(R1, pd.Series(rng.standard_normal(10).astype("f4"), index=SESSIONS), dims=["di"])
    full, full1 = st.read(R2), st.read(R1)
    for sd, ed in [("2024-01-03", "2024-01-09"), (None, "2024-01-05"),
                   ("2024-01-08", None), ("2024-01-06", "2024-01-07")]:
        i0, i1 = st.axes.slice(sd, ed)
        got, want = st.read(R2, sd, ed), full.iloc[i0:i1]
        check(list(got.index) == list(want.index), f"[{sd},{ed}] row axis {list(got.index)}")
        check(np.allclose(got.to_numpy(), want.to_numpy(), equal_nan=True),
              f"[{sd},{ed}] ranged read disagrees with the full slice")
        g1 = st.read(R1, sd, ed)
        check(np.allclose(g1.to_numpy(), full1.iloc[i0:i1].to_numpy(), equal_nan=True),
              f"[{sd},{ed}] rank-1 ranged read disagrees")
    t = st.tail(R2, 3)
    check(list(t.index) == SESSIONS[-3:], f"tail(3) gave {list(t.index)}")
    check(np.allclose(t.to_numpy(), full.iloc[-3:].to_numpy(), equal_nan=True), "tail disagrees with the slice")
    return "4 ranges plus tail(3) match the full slice bit for bit"


def test_read_after_axis_grows():
    """轴长了一天而节点还没重跑——日更里每天都会出现的中间态（ingestion 先推轴）。

    §3.3：`read` 永远返回对齐到全局轴的完整结果, 无数据处 NaN。故这里应当拿到 11 行、
    末行全 NaN, 而不是异常、更不是一个「短一行」的数组（后者会让调用方拿着错位的
    日期用下去）。`tail` 更要命：它是 §3.3 点名的产线路径。
    """
    st = fresh("st_grow")
    st.write(R2, panel(SESSIONS, SECURITIES, 1.0))
    st.write(R1, pd.Series(np.ones(10, dtype="f4"), index=SESSIONS), dims=["di"])
    st.write(R3, np.ones((10, 5, 2), dtype="f4"), dims=["di", "ii", "ti"],
             grid_len=2, meta={"dates": SESSIONS})
    st.axes.ensure_sessions(["2024-01-15"])                  # 轴走到了 11 天
    got = {}
    for nm, r in [("rank1", R1), ("rank2", R2), ("rank3", R3)]:
        try:
            v = st.read(r)
            got[nm] = f"shape={v.shape}"
        except Exception as e:                               # noqa: BLE001
            got[nm] = f"{type(e).__name__}"
    try:
        got["tail"] = f"shape={st.tail(R2, 1).shape}"
    except Exception as e:                                   # noqa: BLE001
        got["tail"] = f"{type(e).__name__}"
    check(got == {"rank1": "shape=(11,)", "rank2": "shape=(11, 5)",
                  "rank3": "shape=(11, 5, 2)", "tail": "shape=(1, 5)"},
          f"after the axis grew by a day, read/tail did not align to the global axis: {got} "
          f"(all three ranks should pad a trailing NaN row, and tail(1) should return it)")
    check(st.read(R2).iloc[-1].isna().all(), "the padded trailing row must be NaN")
    return f"{got}"


def test_write_is_range_upsert_not_truncate():
    """§7.2 第 2 条 / §十三 治理断言：日更交付的只有一行, 它不许动区间外的历史。

    照「全量重建」的字面语义执行就是用一行覆盖整个数组, 历史全毁而 version 退化成
    天数计数器。研究员图快写 `--sd 2024-01-08` 是同一个地雷的另一种触发方式。
    """
    st = fresh("st_upsert")
    st.write(R2, panel(SESSIONS, SECURITIES, 1.0))
    v0 = st.meta(R2)["version"]

    st.write(R2, panel(SESSIONS[-1:], SECURITIES, 9.0))          # run --ed today
    df = st.read(R2)
    check(df.shape == (10, 5), f"after delivering one row the array became {df.shape}")
    check((df.iloc[:9] == 1.0).all().all(), "delivering one row overwrote history -- `write` is not a ranged upsert")
    check((df.iloc[-1] == 9.0).all(), "the last row was not written")

    st.write(R2, panel(SESSIONS[5:], SECURITIES, 7.0))           # run --sd 2024-01-08
    df = st.read(R2)
    check((df.iloc[:5] == 1.0).all().all(), "a short backfill truncated the history before it")
    check((df.iloc[5:] == 7.0).all().all(), "the short range was not fully written")
    m = st.meta(R2)
    check(m["version"] == v0, f"upsert must not bump version: {v0} -> {m['version']}")
    check(m["first_session"] == 0 and m["last_session"] == 9,
          f"the watermark was narrowed by a ranged write: {m['first_session']}..{m['last_session']}")
    return f"still {df.shape} after delivering 1 row and 5 rows; version stays {m['version']}"


def test_write_rejects_broken_date_index():
    """交付的日期必须是轴上一段连续、升序的区间——错序会让整块数据平移到别的日子。"""
    st = fresh("st_dates")
    e = raises(StoreError, st.write, R2,
               panel([SESSIONS[0], SESSIONS[4]], SECURITIES))     # 跳着的两天
    check("not contiguous" in str(e), f"the error does not say the dates are non-contiguous: {e}")
    raises(KeyError, st.write, R2, panel(["2024-01-06"], SECURITIES))   # 非交易日

    # 首尾恰好卡住区间宽度的错序：连续性检查算的是「跨几个 session」, 它数得对,
    # 中间两行却是反的。写进去不报错, 03 的值落在 02 那一行上。
    idx = [SESSIONS[0], SESSIONS[2], SESSIONS[1], SESSIONS[3]]
    df = pd.DataFrame(np.array([[0.], [2.], [1.], [3.]], dtype="f4") @ np.ones((1, 5)),
                      index=idx, columns=SECURITIES)
    err = None
    try:
        st.write(R2, df)
    except Exception as e:                                        # noqa: BLE001
        err = e
    got = None if err is not None else st.read(R2)[101].iloc[:4].tolist()
    check(err is not None or got == [0.0, 1.0, 2.0, 3.0],
          f"an out-of-order index {idx} was accepted and stored in the given order: col101 = {got}, "
          f"expected [0.0, 1.0, 2.0, 3.0] -- days 2 and 3 were swapped with no error at all")
    return f"non-contiguous and non-trading days rejected; out-of-order -> {type(err).__name__ if err else got}"


def test_version_bumps_only_on_rebuild():
    """§4.11.5：名字是契约, version 是构建。只有显式 --rebuild 才是新版本。"""
    st = fresh("st_ver")
    one = panel(SESSIONS[:1], SECURITIES)
    st.write(R2, one);                    v1 = st.meta(R2)["version"]
    st.write(R2, one);                    v2 = st.meta(R2)["version"]
    st.write(R2, one, rebuild=True);      v3 = st.meta(R2)["version"]
    st.write(R2, one);                    v4 = st.meta(R2)["version"]
    st.write(R2, one, rebuild=True);      v5 = st.meta(R2)["version"]
    check(v1 == 1, f"first write gave version={v1}, expected 1")
    check(v2 == v1, f"an ordinary write bumped version: {v1} -> {v2} (version would decay into a day counter)")
    check(v3 == v1 + 1, f"rebuild did not bump: {v1} -> {v3}")
    check(v4 == v3, f"an ordinary write after rebuild bumped again: {v3} -> {v4}")
    check(v5 == v3 + 1, f"the second rebuild did not bump: {v3} -> {v5}")
    return f"1,1,2,2,3 -> got {v1},{v2},{v3},{v4},{v5}"


def test_check_fingerprint():
    """§3.3 / §十三 治理断言：改一行公式再跑日更必须被拦下, 而不是静默 upsert。

    upsert 不 bump version, 所以同一个数组里改动日之前是定义 A、之后是定义 B,
    meta 与 catalog 都看不出来, 事后也无法判断断点在哪天。
    """
    st = fresh("st_fp")
    check(st.check_fingerprint(R2, "sha256:aaaa") is None, "must pass silently when the node does not exist yet")
    st.write(R2, panel(SESSIONS, SECURITIES), meta={"fingerprint": "sha256:aaaa"})
    check(st.check_fingerprint(R2, "sha256:aaaa") is None, "the same fingerprint must not raise")
    e = raises(StoreError, st.check_fingerprint, R2, "sha256:bbbb")
    check("fingerprint" in str(e) and "sha256:aaaa" in str(e) and "sha256:bbbb" in str(e),
          f"the error does not give both fingerprints: {e}")
    check("rebuild" in str(e), f"the error offers no way forward (--rebuild / a different identity): {e}")
    st.write(R1, pd.Series(np.ones(10, dtype="f4"), index=SESSIONS), dims=["di"])
    check(st.check_fingerprint(R1, "sha256:cccc") is None,
          "an older node with no recorded fingerprint must pass (backward compatibility)")
    return "first write silent / same fingerprint silent / changed fingerprint raises StoreError with both in the message"


def test_expand_wildcard():
    """§3.2 通配在编译期展开为该节点当时的全部输出；展开为空必须报错而不是返回 []。

    返回空列表的话, 依赖清单会静默变短——节点照跑, 只是少吃了一个输入。
    """
    st = fresh("st_expand")
    for out in ("adv20", "illiq20", "rvol20"):
        st.write(f"g_yliu.liq.factor_yliu_liq-{out}", panel(SESSIONS[:1], SECURITIES))
    st.write("g_yliu.liq.factor_yliu_liq2-x", panel(SESSIONS[:1], SECURITIES))
    hits = st.expand("g_yliu.liq.factor_yliu_liq-*")
    check(hits == [f"g_yliu.liq.factor_yliu_liq-{o}" for o in ("adv20", "illiq20", "rvol20")],
          f"expansion gave {hits}")
    check("g_yliu.liq.factor_yliu_liq2-x" not in hits, "another node with a similar prefix was swept in")
    e = raises(StoreError, st.expand, "g_yliu.liq.factor_yliu_none-*")
    check("expanded to nothing" in str(e), f"the empty-expansion error is off topic: {e}")
    check(st.expand(R2) == [R2], "a non-wildcard name must be returned unchanged")
    return f"3 outputs expanded, the neighbouring node stayed out, an empty expansion raised {type(e).__name__}"


def test_store_root_accepts_region_qualified_path():
    """region 文件里的 `l3_root` 写的是它自己那一层（storage/l3/us）。

    两种写法必须落到同一个目录, 否则 `storage/l3/us` 会被拼成 `storage/l3/us/us`——
    那不会报错, 只会安静地建出一个空 store, 然后所有 read 都说"没这个 ref"。
    """
    st1 = fresh("st_root")
    root = st1.root
    a = Store(str(root), REGION)                   # 不带 region 段
    b = Store(str(root / REGION), REGION)          # 带 region 段
    check(a.root == b.root, f"the two l3_root spellings gave different roots: {a.root} vs {b.root}")
    check(a.path(R2) == b.path(R2), f"the two spellings gave different paths: {a.path(R2)} vs {b.path(R2)}")
    check(a.path(R2).parent.parent.parent == root / REGION,
          f"the region level is wrong: {a.path(R2)}")
    return f"`{root.name}` and `{root.name}/{REGION}` land in the same place"


def test_path_ref_roundtrip():
    """§4.11.6 检查 ⑨：ref 拆解后能拼回原路径。两者一一对应、不需要索引。"""
    st = fresh("st_path")
    refs = [R1, R2, R3, RB, RI, "g_yliu.beta_decomp.factor_yliu_beta_decomp-mkt_beta_w250"]
    for ref in refs:
        p = st.path(ref)
        back = f"{p.parent.parent.name}.{p.parent.name}.{p.name}"
        check(back == ref, f"path->ref does not round-trip: {ref} -> {p} -> {back}")
        check(p.parent.parent.parent == st.root / REGION, f"the region level is wrong: {p}")
        r = parse_ref(ref)
        check(str(r) == ref, f"str(Ref) does not equal the original: {r}")
        check(st.path(r) == p, "passing a Ref and a string gave different paths")
        check(r.leaf == p.name, f"the leaf name does not follow the collapse rule: {p.name}")
    st.write(R2, panel(SESSIONS[:1], SECURITIES))
    check(st.list_refs() == [R2], f"list_refs = {st.list_refs()}")
    cat = st.catalog()
    check(len(cat) == 1 and cat["ref"].iloc[0] == R2, f"catalog：\n{cat}")
    never = [c for c in cat.columns if cat[c].isna().all()]
    return f"{len(refs)} refs round-trip; columns always empty in catalog: {never}"


def test_sparse_cost_is_proportional():
    """§3.3：稀疏不需要任何特殊设计, 成本几乎正比于实际数据量。

    这条兑现的话, universe 差异、上市前空白、只跑最近两年的实验节点都不需要额外
    机制；不兑现的话每个窄覆盖的节点都要按满仓付钱, 秩-3 立刻不可用。
    """
    D = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2023-01-02", periods=250)]
    N = 3000
    secs = list(range(1, N + 1))
    st = fresh("st_sparse", sessions=D, securities=secs, reserve=0)
    rng = np.random.default_rng(7)

    def w(ref, dates, cols):
        st.write(ref, pd.DataFrame(
            rng.standard_normal((len(dates), len(cols))).astype("f4"),
            index=dates, columns=cols))
        return du(st.path(ref))

    dense = w("g_yliu.s.factor_yliu_s-dense", D, secs)
    cols = w("g_yliu.s.factor_yliu_s-cols", D, secs[:500])            # 500/3000 列
    both = w("g_yliu.s.factor_yliu_s-both", D[-20:], secs[:500])      # 近 20 日 × 500 列
    st.write("g_yliu.s.factor_yliu_s-empty",
             pd.DataFrame(np.full((1, 1), np.nan, dtype="f4"), index=D[:1], columns=[1]))
    empty = du(st.path("g_yliu.s.factor_yliu_s-empty"))

    cover = 500 / N
    check(cols[0] < dense[0] * cover * 2.5,
          f"column-sparse (500/{N}) takes {cols[0]/1e6:.2f} MB vs dense {dense[0]/1e6:.2f} MB; "
          f"coverage is {cover:.1%} yet the space saved does not match")
    check(both[0] < cols[0] / 5, f"doubly-sparse {both[0]/1e6:.3f} MB is not smaller than column-sparse {cols[0]/1e6:.2f} MB")
    check(both[1] < dense[1], f"time sparsity should write fewer chunk files: {both[1]} vs {dense[1]}")
    check(empty[1] <= 1, f"an empty node should leave only zarr.json, got {empty[1]} files")
    return (f"dense {dense[0]/1e6:.2f}MB/{dense[1]} files - column-sparse {cols[0]/1e6:.2f}MB/{cols[1]} "
            f"({cols[0]/dense[0]:.1%}, coverage {cover:.1%}) - doubly-sparse {both[0]/1e6:.3f}MB/{both[1]} "
            f"- empty {empty[0]}B/{empty[1]}")


def test_sparse_write_of_nonfloat_dtype():
    """窄覆盖的 bool / i1 面板：未覆盖的列是「没有数据」, 不是 True、也不该是有效码。

    写入前 `reindex(columns=全局轴)` 会给未覆盖的列填 NaN, 而 `np.asarray(NaN, bool)`
    是 **True**。universe 恰恰是 bool 面板（§3.5）——一个只交付了自己那一段的池子节点，
    会把全市场其余的票**全部标成在池内**, 且 fill_value=0 的未写区反而是 False,
    于是同一个数组里「没写」与「写过但没覆盖」给出相反的答案。
    """
    st = fresh("st_sparsedtype")
    st.write(RB, pd.DataFrame(np.array([[True, False]] * 10), index=SESSIONS,
                              columns=[101, 103]), dtype="bool")
    row = st.read(RB).iloc[0]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        st.write(RI, pd.DataFrame(np.array([[7, 8]] * 10, dtype="i1"), index=SESSIONS,
                                  columns=[101, 103]), dtype="i1")
    irow = st.read(RI).iloc[0].tolist()
    uncovered = [102, 104, 105]
    check(not row[uncovered].any(),
          f"the bool panel delivered only 101/103; uncovered columns {uncovered} read back as "
          f"{row[uncovered].tolist()} (NaN cast to bool is True) -- names outside the pool were marked inside")
    return (f"bool uncovered columns {row[uncovered].tolist()} - i1 uncovered columns "
            f"{[irow[SECURITIES.index(c)] for c in uncovered]}"
            f" ({len(caught)} cast warnings)")


def test_write_never_drops_columns_silently():
    """轴是唯一真相源, 但「不在轴上的列」被无声丢掉就成了静默数据丢失。

    handle 算出一只轴上还没登记的新票时, 这一列连同它的告警一起消失——研究员看到的
    是「这只票没有值」, 而不是「你得先给它分配 security_id」。
    """
    st = fresh("st_drop")
    df = panel(SESSIONS, SECURITIES + [999], 5.0)
    err = None
    try:
        st.write(R2, df)
    except Exception as e:                                    # noqa: BLE001
        err = e
    # 注意不能急切地 read：实现若选择「在建数组之前就拒绝」, 此时数组并不存在,
    # 而 f-string 的实参先于 check 求值, 会把一次通过变成一次 StoreError。
    kept = False
    if err is None:
        try:
            kept = 999 in st.read(R2).columns
        except Exception:                                     # noqa: BLE001
            kept = False
    check(err is not None or kept,
          "writing a panel containing off-axis column 999 neither raised nor read that column back -- its data vanished silently")
    return f"{type(err).__name__ if err else 'kept'}"


def test_same_day_rerun_is_idempotent():
    """§十三 4：同日重跑不重复写。日更任务必须可重入（超时重试是常态）。"""
    st = fresh("st_idem")
    st.write(R2, panel(SESSIONS, SECURITIES, 1.0))
    one = panel(SESSIONS[-1:], SECURITIES, 3.0)
    st.write(R2, one); a = st.read(R2).to_numpy().copy(); n_a = st.axes.n_sessions
    st.write(R2, one); b = st.read(R2).to_numpy()
    check(np.array_equal(a, b, equal_nan=True), "writing the same day twice gave different results")
    check(st.axes.n_sessions == n_a, f"the re-run extended the axis: {n_a} -> {st.axes.n_sessions}")
    check(st.meta(R2)["last_session"] == 9, f"last_session={st.meta(R2)['last_session']}")
    return f"replaying the same day is bit-identical; n_sessions stays {n_a}"


def test_read_missing_ref_says_where():
    """依赖不存在是最常见的一类失败, 报错必须带上它去哪儿找过（§7.1 唯一的兜底）。"""
    st = fresh("st_missing")
    e = raises(StoreError, st.read, R2)
    check(R2 in str(e) and str(st.path(R2)) in str(e), f"the error does not give the expected path: {e}")
    check(not st.exists(R2), "exists returned True for a node that does not exist")
    return "the error carries both the ref and the expected path"


# ============================================================ 命名与配置 §4.11
def test_parse_ref_accepts_canonical():
    """`{repo}.{node_dir}.{node_name}-{output}`：kind / ns 从名字里解析, 不另外声明。"""
    r = parse_ref("g_yliu.liq.factor_yliu_liq-adv20")
    check(isinstance(r, Ref), f"did not return a Ref: {type(r).__name__}")
    check((r.repo, r.node_dir, r.node_name, r.output)
          == ("g_yliu", "liq", "factor_yliu_liq", "adv20"), f"parsed incorrectly: {r}")
    check(r.kind == "factor" and r.ns == "yliu", f"kind/ns = {r.kind}/{r.ns}")
    check(str(r) == "g_yliu.liq.factor_yliu_liq-adv20", f"does not reassemble: {r}")
    r2 = parse_ref("g_yliu.rev.alpha_yliu_rev_w005-weight")
    check(r2.kind == "alpha" and r2.output == "weight", f"{r2.kind}/{r2.output}")
    check(parse_ref("g_common.field_base_px.adj_close_1500").ns == "base",
          "the ns segment of g_common parsed incorrectly")
    return f"kind={r.kind} ns={r.ns} output={r.output}; str() round-trips"


def test_parse_ref_rejects_legacy_and_malformed():
    """老的三段式 `field.base.x` 与叶子无 `-` 必须被拒——它们与新形式看着一样长。"""
    bad = {
        "field.base.adj_close": "the old three-segment form (kind/ns/name), no `-` in the leaf",
        "g_yliu.liq.factor_yliu_liq": "missing output",
        "g_yliu.liq.foo_yliu_liq-x": "the first segment is not field/factor/alpha",
        "g_yliu.factor_yliu_liq-adv20": "only two segments",
        "a.b.c.d-e": "four segments",
        "factor_yliu_liq-adv20": "a bare name (the easiest mistake to make in neutralize)",
    }
    for ref, why in bad.items():
        e = raises(ConfigError, parse_ref, ref)
        check(ref in str(e), f"{why}: the error does not contain the original {ref}")
    return f"all {len(bad)} malformed refs were rejected"


def test_parse_ref_rejects_broken_leaf():
    """§4.11.1 的四条硬约束落在 ref 上：节点名必须是三段, output 非空、无连字符、无大写。

    只查首段的话, `factor-adv20` 一路通到 `Ref.ns`, 那里 `split("_")[1]` 抛 IndexError
    ——报错发生在离原因很远的地方, 且不是 ConfigError, 上层的 friendly 分支接不住。
    """
    bad = {
        "g_yliu.liq.factor-adv20": "the node name is not {kind}_{ns}_{name} (Ref.ns would then IndexError)",
        "g_yliu.liq.factor_yliu_liq-": "empty output -- the directory name would end in a bare hyphen",
        "G_YLIU.LIQ.factor_YLIU_liq-ADV20": "uppercase (§4.11.1 item 4: collides on APFS)",
        "g_yliu.liq.factor_yliu_liq-a-b": "a hyphen inside output (§4.11.1 item 3)",
    }
    passed = {}
    for ref, why in bad.items():
        try:
            parse_ref(ref); passed[ref] = why
        except ConfigError:
            pass
    check(not passed, f"these malformed refs were let through: {passed}")
    return f"all {len(bad)} malformed leaves were rejected"


def test_check_name_rejects_leading_digit():
    """§4.11.6 检查 ①：以数字开头的名字连模块都加载不了。

    输出名会作为 `ctx.multi_outputs(**kw)` 的**关键字参数**传递, `5dr_250d=...` 是
    `SyntaxError: invalid decimal literal`——语法错误意味着整个模块根本加载不了,
    §4.3 精心设计的「在写错那一行抛错、typo 带修复建议」压根执行不到。
    """
    syn = None
    try:
        compile("ctx.multi_outputs(5dr_250d=v)", "<yaml>", "eval")
    except SyntaxError as e:
        syn = e
    check(syn is not None, "premise gone: 5dr_250d= turns out to be valid syntax")
    e = raises(ConfigError, check_name, "5dr_250d", "output name")
    check("5dr_250d" in str(e), f"the error does not include the name: {e}")
    check(check_name("dr5_250d", "output name") is None, "a digit in the middle is legal")
    return f"5dr_250d rejected; compiling it directly gives SyntaxError: {syn.msg}"


def test_check_name_rejects_reserved_and_unsafe():
    """§4.11.6 保留字表 + §4.11.1 语法：每一条都对着一个具体的坏结果。"""
    bad = {
        "class": "a Python keyword -- as a keyword argument this is a SyntaxError",
        "return": "same as above (the one §4.11.1 names explicitly)",
        "Adv20": "uppercase -- two machines disagree on a case-insensitive filesystem",
        "adj.close": "a dot -- collides with the ref segment separator",
        "adj-close": "a hyphen -- collides with the {node_name}-{output} seam",
        "adj_close_tc": "ends in _tc -- that is only a source-level template marker",
        "_axes": "leading underscore -- collides with the axes directory",
        "dims": "a schema key", "outputs": "a schema key", "all": "the default universe",
        "region": "a schema key", "sim": "a schema key",
        "a" * 41: "longer than 40 characters",
        "": "an empty name",
    }
    for s, _why in bad.items():
        raises(ConfigError, check_name, s, "output name")
    for good in ("adv20", "adj_close_1500", "mkt_beta_w250", "weight", "rv_5m"):
        check(check_name(good, "output name") is None, f"a legal name was rejected: {good}")
    return f"all {len(bad)} illegal names rejected, all 5 legal names accepted"


# ---------------------------------------------------------------- load_spec
def spec_from(text: str, *, repo="g_yliu", node_dir="liq", stem="liq",
              code="def handle(ctx):\n    return None\n"):
    """把一段 yaml 落到 `{repo}/nodes/{node_dir}/{stem}.yaml` 再加载——repo 与 node_dir
    是从路径推出来的（§4.11.2 坍缩），所以不能拿字符串直接喂 load_spec。"""
    d = TMP / "cfg" / repo / "nodes" / node_dir
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{stem}.yaml"
    p.write_text(text)
    if code is not None:
        (d / f"{stem}.py").write_text(code)
    return load_spec(p)


def test_ns_must_match_repo():
    """§4.11.6：ns 段必须等于所在 repo 的 owner——§二 的写权限模型的名字表达。"""
    e = raises(ConfigError, spec_from, "nodes:\n  factor_lqin_x: {}\n")
    check("lqin" in str(e) and "yliu" in str(e), f"the error does not name both sides: {e}")
    s = spec_from("nodes:\n  factor_yliu_x: {}\n")
    check(list(s.nodes) == ["factor_yliu_x"], f"a node's own ns was wrongly rejected: {list(s.nodes)}")
    s = spec_from("nodes:\n  field_base_px: {}\n", repo="g_common", node_dir="base_px",
                  stem="base_px")
    check(list(s.nodes) == ["field_base_px"], "g_common must be able to write shared ns such as base")
    e = raises(ConfigError, spec_from, "nodes:\n  factor_yliu: {}\n")
    check("{kind}_{ns}_{name}" in str(e) or "name" in str(e),
          f"the two-segment-name error is off topic: {e}")
    return "cross-ns rejected / own ns allowed / g_common exempt / two-segment name rejected"


def test_ns_segment_syntax():
    """§4.11.1：`ns ::= ^[a-z][a-z0-9]*$`，单段、非空。

    `factor__x` 在 g_common 下会被拆成 ns='' 、name='x'：ns 是治理与权限的挂载点,
    空 ns 意味着这个节点不属于任何人, 而它的路径与引用名照样能拼出来。
    """
    e = got = None
    try:
        s = spec_from("nodes:\n  factor__x: {}\n", repo="g_common", node_dir="m", stem="m")
        got = (s.nodes["factor__x"].ns, list(s.nodes["factor__x"].outputs))
    except ConfigError as exc:
        e = exc
    check(e is not None,
          f"`factor__x` was accepted: ns/outputs = {got} -- the ns segment is not syntax-checked")
    return type(e).__name__


def test_single_output_default_name():
    """§4.11.2 / 检查 ③：单输出的名字由节点名推导, 不许另起一个。

    单输出 alpha 恒为 `weight`——`…-weight` 是「这是个可评估的 alpha」的可 grep 标志,
    改掉它, pnl 与 §15 的 alpha 池就得靠约定而不是靠名字找权重。
    """
    s = spec_from("nodes:\n  factor_yliu_liq:\n    params: {window: 20}\n")
    check(list(s.nodes["factor_yliu_liq"].outputs) == ["liq"],
          f"data node default output name {list(s.nodes['factor_yliu_liq'].outputs)}, expected the de-prefixed liq")
    s = spec_from("nodes:\n  factor_yliu_beta_decomp: {}\n", node_dir="bd", stem="bd")
    check(list(s.nodes["factor_yliu_beta_decomp"].outputs) == ["beta_decomp"],
          "for a multi-segment name the default output must be the whole de-prefixed part")
    s = spec_from("nodes:\n  alpha_yliu_rev_w005:\n    params: {window: 5}\n"
                  "    ops: [{scale: book}]\n", node_dir="rev", stem="rev")
    check(list(s.nodes["alpha_yliu_rev_w005"].outputs) == ["weight"],
          f"single-output alpha default name {list(s.nodes['alpha_yliu_rev_w005'].outputs)}, expected weight")
    e = got = None
    try:
        got = list(spec_from("nodes:\n  factor_yliu_liq:\n    outputs:\n      banana: {}\n"
                             ).nodes["factor_yliu_liq"].outputs)
    except ConfigError as exc:
        e = exc
    check(e is not None,
          f"a single output written explicitly as {got} was accepted -- check 3 (single-output key == "
          f"default name) is not implemented; the default for factor_yliu_liq is 'liq'")
    return "all three defaults are right: data node, multi-segment name, alpha"


def test_alpha_must_be_rank2_and_end_with_scale():
    """§3.6 + §4.11.6 检查 ⑧：alpha 是权重, 秩必须是 di×ii, ops 链必须以 scale 收尾。

    少了 scale：上游各自 Σ|w|=1 的权重线性组合后会因抵消而缩水, 账本投不满而
    Sharpe 看着正常——一个不会报错、只会让收益凭空少一截的错误。
    """
    e = raises(ConfigError, spec_from,
               "nodes:\n  alpha_yliu_x:\n    outputs:\n"
               "      weight: {dims: [di, ii, ti], grid: m5, ops: [{scale: book}]}\n",
               node_dir="a1", stem="a1")
    check("rank-2" in str(e) or "di x ii" in str(e), f"the rank error is off topic: {e}")
    e = raises(ConfigError, spec_from, "nodes:\n  alpha_yliu_x:\n    ops: [rank]\n",
               node_dir="a2", stem="a2")
    check("scale" in str(e), f"the trailing-op error does not mention scale: {e}")
    e = raises(ConfigError, spec_from, "nodes:\n  alpha_yliu_x: {}\n",
               node_dir="a3", stem="a3")
    check("scale" in str(e), f"an alpha with empty ops must also be rejected: {e}")
    e = raises(ConfigError, spec_from,
               "nodes:\n  alpha_yliu_x:\n    ops: [{scale: book}, rank]\n",
               node_dir="a4", stem="a4")
    check("scale" in str(e), f"scale not being last must also be rejected: {e}")
    s = spec_from("nodes:\n  alpha_yliu_x:\n    ops:\n      - rank\n      - truncate: 0.02\n"
                  "      - scale: book\n", node_dir="a5", stem="a5")
    check([o for o, _ in s.nodes["alpha_yliu_x"].outputs["weight"].ops][-1] == "scale",
          "a legal chain was altered")
    return "all four rejected: rank-3, no scale, empty chain, scale not last"


def test_cs_ops_only_on_rank2():
    """§3.6：CS 类作用在 ii 上, 仅秩-2 合法；秩-1 没有 ii 轴, 秩-3 的轴不明确。"""
    for dims, extra, tag in [("[di]", "", "rank-1"),
                             ("[di, ii, ti]", ", grid: m5", "rank-3")]:
        for op in sorted(CS_OPS):
            arg = {"rank": "", "truncate": ": 0.02", "scale": ": book",
                   "neutralize": ": g_common.factor_common_gics.sector"}[op]
            e = raises(ConfigError, spec_from,
                       f"nodes:\n  factor_yliu_m:\n    outputs:\n"
                       f"      m: {{dims: {dims}{extra}, ops: [{{{op}{arg}}}]}}\n"
                       if arg else
                       f"nodes:\n  factor_yliu_m:\n    outputs:\n"
                       f"      m: {{dims: {dims}{extra}, ops: [{op}]}}\n",
                       node_dir="cs", stem="cs")
            check(op in str(e) and ("CS" in str(e) or "rank-2" in str(e)),
                  f"the error for {op} on {tag} is off topic: {e}")
    for op in sorted(TS_OPS):                       # TS 类三种秩皆合法
        arg = {"linear_decay": 3, "exp_decay": 5, "delay": 1}[op]
        s = spec_from(f"nodes:\n  factor_yliu_m:\n    outputs:\n"
                      f"      m: {{dims: [di], ops: [{{{op}: {arg}}}]}}\n",
                      node_dir="ts", stem="ts")
        check(s.nodes["factor_yliu_m"].outputs["m"].ops == [(op, arg)], f"{op} was rejected on rank-1")
    e = raises(ConfigError, spec_from,
               "nodes:\n  factor_yliu_m:\n    outputs:\n      m: {dims: [di, ii, ti]}\n",
               node_dir="g", stem="g")
    check("grid" in str(e), f"the missing-grid error for rank-3 is off topic: {e}")
    return f"rank-1/rank-3 x {len(CS_OPS)} CS ops all rejected; {len(TS_OPS)} TS ops allowed on rank-1"


def test_op_arg_types():
    """§4.11.6 检查 ⑦：算子参数按签名校验。头号目标是 YAML 的静默字符串化。

    `- truncate: 0.02,` —— 行尾多一个逗号, YAML 不报错, 它把 `0.02,` 当**字符串**。
    不查类型的话这个值会一路走到 `cs_truncate`, 那里 `float("0.02,")` 才炸,
    或者更糟：被当成一个非 NaN 的真值参与比较。
    """
    e = raises(ConfigError, spec_from,
               "nodes:\n  alpha_yliu_x:\n    ops:\n      - rank\n"
               "      - truncate: 0.02,\n      - scale: book\n", node_dir="o1", stem="o1")
    check("truncate" in str(e) and ("str" in str(e) or "0.02," in str(e)),
          f"the trailing-comma error does not say a string was received: {e}")
    bad = [
        ("truncate", "'0.02'", "a quoted number"), ("truncate", "true", "a bool is not a number"),
        ("linear_decay", "3.5", "a decimal is not a positive integer"), ("linear_decay", "0", "0 is not a positive integer"),
        ("linear_decay", "-3", "a negative number"), ("delay", "'2'", "a string"),
        ("exp_decay", "true", "a bool"), ("neutralize", "sector", "a bare name is not a full ref"),
        ("neutralize", "3", "not a name"), ("rank", "3", "rank takes no argument"),
    ]
    for op, arg, _why in bad:
        raises(ConfigError, spec_from,
               f"nodes:\n  factor_yliu_m:\n    ops: [{{{op}: {arg}}}]\n",
               node_dir="o2", stem="o2")
    e = raises(ConfigError, spec_from,
               "nodes:\n  factor_yliu_m:\n    ops: [zscore]\n", node_dir="o3", stem="o3")
    check("zscore" in str(e) and "available" in str(e),
          f"the unknown-op error does not list the available ops: {e}")
    s = spec_from("nodes:\n  factor_yliu_m:\n    ops:\n      - rank\n"
                  "      - truncate: 0.02\n      - linear_decay: 3\n"
                  "      - neutralize: g_common.factor_common_gics.sector\n",
                  node_dir="o4", stem="o4")
    ops = s.nodes["factor_yliu_m"].outputs["m"].ops
    check(ops[1] == ("truncate", 0.02) and ops[2] == ("linear_decay", 3),
          f"legal arguments were altered: {ops}")
    check(set(OP_TYPES) == CS_OPS | TS_OPS, f"op classification has drifted: {set(OP_TYPES) ^ (CS_OPS | TS_OPS)}")
    return f"trailing comma + {len(bad)} wrong types + unknown op all rejected; legal chains preserved"


def test_node_level_ops_with_multiple_outputs():
    """节点级 ops 遇上多输出：要么应用到每个输出、要么报错, 唯独不能悄悄丢掉。

    丢掉的后果是静默换口径——一个 rank + truncate 的因子照常落库, 只是没做 rank
    也没做 truncate, 数值仍然「看着合理」。对 alpha 还有 scale 那条闸门兜底,
    对 field/factor 则完全无声。
    """
    s = None
    e = None
    try:
        s = spec_from("nodes:\n  factor_yliu_two:\n    ops:\n      - rank\n"
                      "      - truncate: 0.02\n    outputs:\n      a: {}\n      b: {}\n",
                      node_dir="two", stem="two")
    except ConfigError as exc:
        e = exc
    got = None if s is None else {k: v.ops for k, v in s.nodes["factor_yliu_two"].outputs.items()}
    check(e is not None or all(o for o in got.values()),
          f"node-level ops [rank, truncate] with 2 outputs gave {got} -- the ops chain was silently dropped")
    return type(e).__name__ if e else str(got)


def test_node_ops_and_output_ops_conflict():
    """两处都写 ops 时哪一处生效只能靠猜——所以编译期直接拒绝。"""
    e = raises(ConfigError, spec_from,
               "nodes:\n  factor_yliu_two:\n    ops: [rank]\n    outputs:\n"
               "      a: {ops: [rank]}\n      b: {}\n", node_dir="tw2", stem="tw2")
    check("both" in str(e), f"the error is off topic: {e}")
    s = spec_from("nodes:\n  factor_yliu_one:\n    ops:\n      - truncate: 0.02\n"
                  "    outputs:\n      one: {}\n", node_dir="tw3", stem="tw3")
    check(s.nodes["factor_yliu_one"].outputs["one"].ops == [("truncate", 0.02)],
          "with a single output, node-level ops must land on that one output")
    return "coexistence rejected; node-level ops take effect for a single output"


def test_param_tag_consistency():
    """§4.11.4：params 是真相, 名字是标签, 编译期校验二者一致。

    抓的是「复制了一个变体却只改了 params 忘了改名」——改完名字仍写着 w005、
    params 已是 20, 两个变体在 dump 与 pnl 里就成了同名的两条不同曲线。
    """
    s = spec_from("nodes:\n  factor_yliu_adv20:\n    params: {window: 20}\n",
                  node_dir="t1", stem="t1")
    check(list(s.nodes) == ["factor_yliu_adv20"],
          "a lone unscanned run-on name (adv20) must pass -- the idiom exemption of §4.11.4")
    two = ("nodes:\n  alpha_yliu_rev_w005:\n    params: {window: %s}\n"
           "    ops: [{scale: book}]\n"
           "  alpha_yliu_rev_w020:\n    params: {window: 20}\n    ops: [{scale: book}]\n")
    s = spec_from(two % 5, node_dir="t2", stem="t2")
    check(len(s.nodes) == 2, "a consistent family was wrongly rejected")
    e = raises(ConfigError, spec_from, two % 20, node_dir="t3", stem="t3")
    check("w005" in str(e) or "005" in str(e), f"the error does not say which member: {e}")
    e = raises(ConfigError, spec_from,
               "nodes:\n  alpha_yliu_rev_w005:\n    params: {window: 5}\n"
               "    ops: [{scale: book}]\n"
               "  alpha_yliu_rev_slow:\n    params: {window: 20}\n    ops: [{scale: book}]\n",
               node_dir="t4", stem="t4")
    check("tag" in str(e), f"the missing-tag-within-a-family error is off topic: {e}")
    e = raises(ConfigError, spec_from,
               "nodes:\n  factor_yliu_x_h010:\n    params: {halflife: 20}\n"
               "  factor_yliu_x_h020:\n    params: {halflife: 20}\n",
               node_dir="t5", stem="t5")
    check("h" in str(e), f"the halflife tag was not validated: {e}")
    check(set(TAGS) >= {"window", "halflife", "lag", "quantile", "count"},
          f"the tag dictionary shrank: {TAGS}")
    return f"lone case exempt / consistent family allowed / mismatched value and missing tag both rejected ({len(TAGS)} tags)"


def test_op_registry_is_the_single_source():
    """算子的声明只有一份, 且实现方在 import 时自证覆盖它（§6.2）。

    此前同一件事写了七遍——config 四处、ops 三处——靠一条测试互相盯着。用测试守住
    两份重复只能事后告诉你它们不一致了, 不能阻止你只改其中一份。现在 config 的
    CS_OPS/TS_OPS/OP_TYPES 与参数校验、ops 的分派与预热, 全部由 core.opspec 派生,
    对不上是 ImportError 而不是一条红测试。
    """
    from alpha_kit.core import opspec
    from alpha_kit.runner.ops import OPS as RUNTIME_OPS, ops_lookback

    check(set(RUNTIME_OPS) == set(opspec.OPS), f"分派表与声明表不一致：{set(RUNTIME_OPS) ^ set(opspec.OPS)}")
    check(set(CS_OPS) | set(TS_OPS) == set(opspec.OPS), "CS/TS 划分没有覆盖全表")
    check(set(OP_TYPES) == set(opspec.OPS), "OP_TYPES 没有从声明表派生")

    # 覆盖检查真的会响, 否则它只是一句装饰
    e = raises(ImportError, opspec.check_covers, [n for n in opspec.OPS if n != "rank"], "fake")
    check("rank" in str(e), f"少一个算子没被点名：{e}")
    e2 = raises(ImportError, opspec.check_covers, list(opspec.OPS) + ["bogus"], "fake")
    check("bogus" in str(e2), f"多一个算子没被点名：{e2}")

    # 预热规则也只有一份: TS 串联相加
    check(ops_lookback([("delay", 2), ("linear_decay", 5)]) == 6, "delay2+decay5 应是 6")
    check(ops_lookback([("rank", None)]) == 0, "CS 算子不吃历史")
    check(opspec.lookback([("delay", 2), ("linear_decay", 5)]) == 6, "两个入口给出不同的预热")
    return f"{len(opspec.OPS)} 个算子单一出处；缺/多一个都是 ImportError"


def test_half_created_array_does_not_exist():
    """建好数组但属性未提交 = 尚不存在（§3.3）。

    write 先建数组后写属性。若 exists() 只看 zarr.json, 死在这两步之间留下的空壳会
    通过 exists()、进 list_refs()、meta() 返回 {}——于是 effective_ed 的
    `meta.get("last_session", last)` 把它当成**完全新鲜**, 预检的 DEP_MISSING 也不响:
    那个专为"库还没准备好"而生的零数据检查, 会把一具尸体判成活的。
    """
    import zarr as _z
    st = fresh("halfmade")
    p = st.path(R2)
    p.parent.mkdir(parents=True, exist_ok=True)
    _z.create_array(store=str(p), shape=(10, 12), chunks=(50, 12), dtype="f4",
                    fill_value=float("nan"))          # 建了数组, 一个属性都没写
    check((p / "zarr.json").exists(), "前提没成立：数组没建出来")
    check(not st.exists(R2), "属性未提交的空壳被判为存在")
    check(R2 not in st.list_refs(), f"空壳进了 list_refs：{st.list_refs()}")
    st.write(R2, panel(SESSIONS[:2], SECURITIES))     # 补完属性后应当正常
    check(st.exists(R2), "属性写好之后仍判为不存在")
    check(st.meta(R2).get("dims") == ["di", "ii"], "属性没写进去")
    return "空壳不存在 / 不入 list_refs / 补完属性后恢复"


def test_session_axis_is_append_only():
    """di 轴与 ii 轴同一道闸门（§3.3）。

    此前只有 securities 有守卫, sessions 是无条件覆写。而 di 轴错位比 ii 轴更糟:
    日历里补进一个半日市或删掉一个节假日, 每个 chunk 仍在原来的行位置上, 于是全库
    所有面板整体错开一天——一次性给每个 alpha 注入一天前视, 形状没变、日期范围看着
    干净、指纹不动（定义确实没改）, 没有任何地方会喊。
    """
    root = TMP / "axguard"
    shutil.rmtree(root, ignore_errors=True)
    ss = list(SESSIONS)
    Axes.create(root / REGION, ss, list(SECURITIES), reserve=RESERVE)
    Axes.create(root / REGION, ss + ["2024-01-15"], list(SECURITIES), reserve=RESERVE)
    check(Axes.load(root / REGION).n_sessions == len(ss) + 1, "追加 session 被误拒")
    for bad, why in [(ss[:3] + ["2024-01-99"] + ss[4:], "改中间某一天"),
                     (["2024-01-15"] + ss, "在头部插入一天"),
                     (ss[:5], "截短")]:
        e = raises(ValueError, Axes.create, root / REGION, bad, list(SECURITIES))
        check("sessions" in str(e) and "append-only" in str(e).lower() or "Append-only" in str(e),
              f"{why}: 报错文不对题 {e}")
    check(Axes.load(root / REGION).n_sessions == len(ss) + 1, "被拒的改动却已落盘")
    ov = Axes.create(root / REGION, ss[:3], list(SECURITIES), overwrite=True)
    check(ov.n_sessions == 3, "overwrite=True 仍应放行")
    return "追加放行 / 3 种非扩展全拒 / overwrite 仍是逃生口"


def test_dims_must_be_a_rank():
    """dims 只能是三种秩之一（§3.6）。

    此前只做 tuple(), 无成员检查: `dims: [di, zz]` 被原样收下, 在 store/ctx/node 的
    每一处分支里落到 else, 最后以"rank-3 必须声明 grid"炸出来——报的是另一个问题。
    """
    dep = "g_common.field_base_px.adj_close_1500"
    def mk(dims):
        return spec_from(f"nodes:\n  factor_yliu_d:\n    deps: [{dep}]\n"
                         f"    outputs:\n      d:\n        dims: {dims}\n",
                         node_dir="dm", stem="dm")
    for ok in ("[di]", "[di, ii]"):
        check(mk(ok) is not None, f"合法 dims {ok} 被拒")
    for bad in ("[di, zz]", "[ii, di]", "[di, ii, ti, xx]", "[]"):
        e = raises(ConfigError, mk, bad)
        check("rank" in str(e), f"dims={bad} 的报错文不对题：{e}")
    return "2 种合法放行；4 种非法在写下它的那一行被拒"


def test_yaml_key_set_is_closed():
    """认不得的 yaml 键必须报错, 不能静默丢掉。

    `universe:` 拼错一个字母 → spec.universe 是 None → 掩码恒 True → alpha 悄悄按
    全部 503 只票交易而不是 us_top400; 而预检里每一处 universe 检查都在
    `if spec.universe:` 后面, 所以一句话都不会说。
    """
    dep = "g_common.field_base_px.adj_close_1500"
    base = "nodes:\n  factor_yliu_k:\n    deps: [%s]\n" % dep
    check(spec_from(base, node_dir="ky", stem="ky") is not None, "合法 yaml 被拒")
    for bad, frag in [("univeres: x\n" + base, "univeres"),
                      ("lookbak: 3\n" + base, "lookbak")]:
        e = raises(ConfigError, spec_from, bad, node_dir="ky", stem="ky")
        check(frag in str(e) and "did you mean" in str(e), f"缺少近似候选：{e}")
    e = raises(ConfigError, spec_from,
               "nodes:\n  factor_yliu_k:\n    deps: [%s]\n    codee: x.py\n" % dep,
               node_dir="ky", stem="ky")
    check("codee" in str(e), f"节点级未知键没报：{e}")
    return "文件级 2 个 + 节点级 1 个未知键全拒, 且都给出最接近的候选"


def test_warmup_is_additive_not_max():
    """预热 = handle 要的 + 算子链要的, 不是两者取大（§7.1）。

    链吃的是 handle 的产出: 要让链在第一个请求日就填满缓冲, handle 必须在那之前
    ops_lookback 天就已经在产出有效值, 而 handle 本身要 declared 天才有效。

    这条曾经写成 `max()`。`lookback: 5` + `win(6)` + `linear_decay: 3` 的真实需求是
    5+2=7, max 给 5——实测同一个 session 与预热充足时相比 501/503 只票全不一样、
    最大差 0.11, 而且不报任何警。取大之所以诱人是两段各自都"够", 但它们不是同一段
    时间; 这条断言就是钉住"相加"这件事本身。
    """
    from alpha_kit.runner.node import warmup
    dep = "g_common.field_base_px.adj_close_1500"
    def spec_with(ops_yaml, look):
        body = (f"lookback: {look}\nnodes:\n  factor_yliu_w:\n"
                f"    deps: [{dep}]\n{ops_yaml}")
        s = spec_from(body, node_dir="wu", stem="wu")
        return s.lookback, s.nodes["factor_yliu_w"]

    cases = [
        ("",                                        5,  0,  5),   # 无 ops
        ("    ops:\n      - linear_decay: 3\n",     5,  2,  7),   # n 日窗口要 n-1 天
        ("    ops:\n      - delay: 2\n",            5,  2,  7),
        ("    ops:\n      - delay: 2\n      - linear_decay: 5\n", 5, 6, 11),  # TS 串联相加
        ("    ops:\n      - rank\n",                5,  0,  5),   # CS 算子不吃历史
        ("    ops:\n      - linear_decay: 3\n",     0,  2,  2),
    ]
    for ops_yaml, look, want_ops, want_total in cases:
        declared, node = spec_with(ops_yaml, look)
        got = warmup(declared, node)
        check(got == want_total,
              f"lookback={look} + ops({want_ops}) 应是 {want_total}, 实得 {got}"
              f"（max 会给 {max(look, want_ops)}）")
    # 相加与取大必须真的分得开, 否则这条断言测了个寂寞
    d, n = spec_with("    ops:\n      - linear_decay: 5\n", 3)
    check(warmup(d, n) == 7 and max(3, 4) == 4, "本例中相加与取大不可区分, 测试无效")
    return f"{len(cases)} 组: 相加而非取大; 3+4 给 7 而非 4"


def test_params_spellings_are_one_definition():
    """params 的两种写法必须归一到同一个定义（含指纹）。

        params:            params:              params:
          window: 5          - window: 5          - window: 5
          halflife: 7          halflife: 7        - halflife: 7

    归一若发生在指纹之后, 同一份定义会 hash 出两个指纹、指向同一个数组——正是
    引用名折叠规则要防的那种分叉, 只是换了个地方发生。没写 params 的节点也不能
    因为归一而凭空多出一个 `params: {}`：那会让定义一字未动的节点指纹改变。
    """
    dep = "g_common.field_base_px.adj_close_1500"
    forms = {
        "flow":      "nodes:\n  factor_yliu_f:\n    params: {window: 5, halflife: 7}\n    deps: [%s]\n",
        "block":     "nodes:\n  factor_yliu_f:\n    params:\n      window: 5\n      halflife: 7\n    deps: [%s]\n",
        "list_one":  "nodes:\n  factor_yliu_f:\n    params:\n      - window: 5\n        halflife: 7\n    deps: [%s]\n",
        "list_each": "nodes:\n  factor_yliu_f:\n    params:\n      - window: 5\n      - halflife: 7\n    deps: [%s]\n",
    }
    seen = {}
    for tag, body in forms.items():
        n = spec_from(body % dep, node_dir="pf", stem="pf").nodes["factor_yliu_f"]
        check(n.params == {"window": 5, "halflife": 7}, f"{tag}: params = {n.params}")
        seen[tag] = n.fingerprint()
    check(len(set(seen.values())) == 1,
          f"the same definition hashed to several fingerprints: {seen}")

    # 没写 params 的节点: 归一不得让它凭空多出一个键
    bare = "nodes:\n  factor_yliu_f:\n    deps: [%s]\n" % dep
    f1 = spec_from(bare, node_dir="pf", stem="pf").nodes["factor_yliu_f"]
    check(f1.params == {}, f"a node with no params got {f1.params}")
    check("params" not in f1.src, f"an empty params key leaked into src: {f1.src!r}")

    e = raises(ConfigError, spec_from,
               "nodes:\n  factor_yliu_f:\n    params:\n      - window: 5\n      - window: 7\n"
               "    deps: [%s]\n" % dep, node_dir="pf", stem="pf")
    check("window" in str(e), f"a duplicate params key was not named: {e}")
    return f"4 spellings -> one fingerprint {next(iter(seen.values()))[:14]}...; duplicate key rejected"


def test_fingerprint_covers_the_definition():
    """§3.3：指纹 = yaml 子树 + code 字节 + deps identity + params。

    它是「改了定义却没换名字」的唯一防线, 所以凡是能改变数值的输入都必须进指纹。
    `universe:` 与 `lookback:` 写在 yaml 顶层而非节点子树里, 但它们**逐值改变输出**
    （池外整列 NaN、预热长度决定 TS 算子的初值），改了却指纹不变, check_fingerprint
    就会放行, 同一个数组里改动日前后是两个定义。
    """
    body = "nodes:\n  factor_yliu_f:\n    params: {window: 20}\n    deps: [%s]\n"
    dep = "g_common.field_base_px.adj_close_1500"
    base = spec_from(body % dep, node_dir="fp", stem="fp").nodes["factor_yliu_f"].fingerprint()
    p = spec_from((body % dep).replace("window: 20", "window: 21"),
                  node_dir="fp", stem="fp").nodes["factor_yliu_f"].fingerprint()
    check(p != base, "changing params did not change the fingerprint")
    d = spec_from(body % "g_common.field_base_px.volume_1500",
                  node_dir="fp", stem="fp").nodes["factor_yliu_f"].fingerprint()
    check(d != base, "changing deps did not change the fingerprint")
    c = spec_from(body % dep, node_dir="fp", stem="fp",
                  code="def handle(ctx):\n    return 1.0\n"
                  ).nodes["factor_yliu_f"].fingerprint()
    check(c != base, "changing code did not change the fingerprint")
    u = spec_from("universe: g_common.field_common_univ.us_top400\n" + (body % dep),
                  node_dir="fp", stem="fp").nodes["factor_yliu_f"].fingerprint()
    u2 = spec_from("universe: g_common.field_common_univ.us_top3000\n" + (body % dep),
                   node_dir="fp", stem="fp").nodes["factor_yliu_f"].fingerprint()
    lb = spec_from("lookback: 250\n" + (body % dep),
                   node_dir="fp", stem="fp").nodes["factor_yliu_f"].fingerprint()
    check(u != u2 and lb != base,
          f"changing universe (top400 -> top3000) same fingerprint={u == u2}, changing lookback same="
          f"{lb == base} -- both change the output value by value yet do not enter the fingerprint, "
          f"so the daily update would pass silently")
    return f"params/deps/code all change the fingerprint ({base[:14]}...)"


def test_op_contract_matches_runner():
    """编译期放行的算子参数, 执行期必须也认——两边不一致时用户被挡在合法写法之外。"""
    try:
        from alpha_kit.runner.ops import OPS, OpChain
    except Exception as e:                          # noqa: BLE001
        return f"skipped (runner.ops could not be imported: {e})"
    check(set(OPS) == set(OP_TYPES), f"op sets have drifted: {set(OPS) ^ set(OP_TYPES)}")
    runtime_ok = True
    try:
        OpChain([("rank", None), ("scale", None)], None)     # a bare scale is allowed at runtime
    except Exception:                               # noqa: BLE001
        runtime_ok = False
    cfg_ok = True
    try:
        spec_from("nodes:\n  alpha_yliu_x:\n    ops: [rank, scale]\n",
                  node_dir="sc", stem="sc")
    except ConfigError:
        cfg_ok = False
    check(runtime_ok == cfg_ok,
          f"`ops: [rank, scale]` (scale with no argument) accepted at runtime={runtime_ok}, at compile "
          f"time={cfg_ok} -- OP_TYPES['scale'] is str so None does not pass, while OpChain "
          f"explicitly treats None as book")
    return f"{len(OPS)} op names agree; bare scale agrees on both sides={runtime_ok == cfg_ok}"


def test_real_repo_specs_load():
    """仓库里现成的 yaml 必须都能加载——它们是 §4.10 那条研究链的实物。"""
    root = Path(__file__).resolve().parents[1] / "repos"
    if not root.exists():
        return "skipped (no repos/)"
    files = sorted(root.glob("g_*/nodes/*/*.yaml"))
    check(files, f"found no node yaml at all: {root}")
    n_nodes = n_alpha = 0
    for f in files:
        s = load_spec(f)
        n_nodes += len(s.nodes)
        for node in s.nodes.values():
            check(node.repo == f.parents[2].name, f"{f}: repo inferred incorrectly as {node.repo}")
            check(node.node_dir == f.parent.name, f"{f}: node_dir inferred incorrectly as {node.node_dir}")
            check(node.kind in KINDS, f"{f}: kind={node.kind}")
            for k, _o in node.outputs.items():
                # 折叠规则（§4.11）：node_name 与 node_dir 同名时中间那段省略
                want = k if node.name == node.node_dir else f"{node.name}-{k}"
                check(str(node.ref(k)).endswith(want), f"{f}: ref assembled incorrectly")
            if node.kind == "alpha":
                n_alpha += 1
                check(all(node.ref(k).output == "weight" for k in node.outputs)
                      or len(node.outputs) > 1, f"{f}: the single output of an alpha is not called weight")
    return f"{len(files)} yaml files / {n_nodes} nodes ({n_alpha} alphas) all loaded"


TESTS = [
    # ---- 轴 §3.3
    test_axes_create_load_roundtrip,
    test_pos_date_inverse,
    test_slice_half_open,
    test_ensure_sessions_appends,
    test_ensure_sessions_rejects_earlier_date,
    test_ensure_sessions_rejects_duplicate_in_batch,
    test_axes_create_refuses_to_clobber,
    test_capacity_reserve,
    # ---- store §3.3 / §3.6
    test_roundtrip_three_ranks,
    test_chunking_matches_spec,
    test_dtypes_survive,
    test_read_aligns_to_global_axis,
    test_range_read_equals_full_sliced,
    test_read_after_axis_grows,
    test_write_is_range_upsert_not_truncate,
    test_write_rejects_broken_date_index,
    test_version_bumps_only_on_rebuild,
    test_check_fingerprint,
    test_expand_wildcard,
    test_store_root_accepts_region_qualified_path,
    test_path_ref_roundtrip,
    test_sparse_cost_is_proportional,
    test_sparse_write_of_nonfloat_dtype,
    test_write_never_drops_columns_silently,
    test_same_day_rerun_is_idempotent,
    test_read_missing_ref_says_where,
    # ---- 命名与配置 §4.11
    test_parse_ref_accepts_canonical,
    test_parse_ref_rejects_legacy_and_malformed,
    test_parse_ref_rejects_broken_leaf,
    test_check_name_rejects_leading_digit,
    test_check_name_rejects_reserved_and_unsafe,
    test_ns_must_match_repo,
    test_ns_segment_syntax,
    test_single_output_default_name,
    test_alpha_must_be_rank2_and_end_with_scale,
    test_cs_ops_only_on_rank2,
    test_op_arg_types,
    test_node_level_ops_with_multiple_outputs,
    test_node_ops_and_output_ops_conflict,
    test_param_tag_consistency,
    test_op_registry_is_the_single_source,
    test_half_created_array_does_not_exist,
    test_session_axis_is_append_only,
    test_dims_must_be_a_rank,
    test_yaml_key_set_is_closed,
    test_warmup_is_additive_not_max,
    test_params_spellings_are_one_definition,
    test_fingerprint_covers_the_definition,
    test_op_contract_matches_runner,
    test_real_repo_specs_load,
]

if __name__ == "__main__":
    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    print(f"core self-check  ({len(TESTS)} tests)  zarr {zarr.__version__} / "
          f"pandas {pd.__version__} / numpy {np.__version__}")
    print(f"temp store: {TMP}\n")
    for t in TESTS:
        run(t)
    print(f"\n{len(TESTS) - len(FAILS)}/{len(TESTS)} passed")
    if FAILS:
        print("\nThese failures are not environment problems -- each maps to a promise written in architecture.md:")
        for i, (name, msg) in enumerate(FAILS, 1):
            print(f"  {i}. {name}\n     {msg.splitlines()[0]}")
    sys.exit(1 if FAILS else 0)
