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
R1 = "g_common.field_macro_cpi.field_macro_cpi-yoy"                # 秩-1
R3 = "g_common.field_taq_rv.field_taq_rv-rv_5m"                   # 秩-3
RB = "g_common.field_common_univ.field_common_univ-us_top3000"        # bool
RI = "g_common.factor_common_gics.factor_common_gics-sector"         # int8


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
        raise AssertionError(f"抛的是 {type(e).__name__}（期望 {exc.__name__}）：{e}") from None
    raise AssertionError(f"没有抛 {exc.__name__}——被放行了")


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
    check(a.sessions == SESSIONS, f"sessions 变了：{a.sessions}")
    check(a.securities == SECURITIES, f"securities 变了：{a.securities}")
    check(a.n_sessions == 10 and a.n_securities == 5, f"{a.n_sessions}×{a.n_securities}")
    cap = json.loads((Path(a.root) / "_axes" / "capacity.json").read_text())
    check(cap["n_active"] == 5 and cap["allocated"] == 12, f"capacity.json = {cap}")
    b = Axes.load(a.root)
    check(b.sessions == a.sessions and b.securities == a.securities
          and b.allocated == a.allocated, "第二次 load 与第一次不一致")
    return f"D={a.n_sessions} N={a.n_securities} allocated={a.allocated}"


def test_pos_date_inverse():
    """`pos` 与 `date` 必须互为逆。§3.2 的路径推导、§7.2 的主循环全靠这一条。"""
    a = fresh("ax_pos").axes
    for i, d in enumerate(SESSIONS):
        check(a.pos(d) == i, f"pos({d})={a.pos(d)} 期望 {i}")
        check(a.date(i) == d, f"date({i})={a.date(i)} 期望 {d}")
    e = raises(KeyError, a.pos, "2024-01-06")           # 周末不在轴上
    check("2024-01-06" in str(e), f"报错没说清是哪个日期：{e}")
    return f"{a.n_sessions} 个 session 双向逐个对上, 非交易日 pos 抛 KeyError"


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
        check(got == want, f"slice({sd}, {ed}) = {got}，期望 {want}")
    lo, hi = a.slice("2024-01-09", "2024-01-03")        # sd > ed
    check(hi - lo <= 0, f"sd 晚于 ed 却给出非空区间 [{lo}, {hi})")
    return f"{len(cases)} 组边界全中, sd>ed 宽度 {hi - lo}"


def test_ensure_sessions_appends():
    """按日期 append 是 §3.3 的 O(1) 承诺：只在末尾长, 已有位置一个都不许动。"""
    st = fresh("ax_app")
    a = st.axes
    before = {d: a.pos(d) for d in SESSIONS}
    n = a.ensure_sessions(["2024-01-15", "2024-01-16"])
    check(n == 2, f"返回新增 {n} 条，期望 2")
    check(a.n_sessions == 12, f"n_sessions={a.n_sessions}")
    check(a.sessions[-2:] == ["2024-01-15", "2024-01-16"], f"末尾是 {a.sessions[-2:]}")
    for d, i in before.items():
        check(a.pos(d) == i, f"append 之后 {d} 的位置从 {i} 变成了 {a.pos(d)}——历史 chunk 全废")
    check(a.ensure_sessions(["2024-01-16"]) == 0, "重复的末日不该再 append（日更要可重入）")
    check(Axes.load(a.root).sessions == a.sessions, "没落盘：进程重启就丢")
    return f"10 → {a.n_sessions}，旧位置 {len(before)} 个不变，重放同一天返回 0"


def test_ensure_sessions_rejects_earlier_date():
    """轴 append-only：接受一个不严格晚于末日的新日期 = 悄悄改写每个历史 chunk 的列义。"""
    a = fresh("ax_rej").axes
    # 早于首日 / 落在周末缺口里 / 缺口的另一半——三个都不在轴上, 且都不晚于末日
    for bad in ["2023-12-31", "2024-01-06", "2024-01-07"]:
        e = raises(ValueError, a.ensure_sessions, [bad])
        check("append-only" in str(e), f"{bad} 的报错没提 append-only：{e}")
        check(a.n_sessions == 10, f"{bad} 被拒了却已经写进轴：n={a.n_sessions}")
    e = raises(ValueError, a.ensure_sessions, ["2024-01-06", "2024-01-20"])
    check(a.n_sessions == 10, "一批里混了一个早日期, 整批必须原子拒绝")
    check(a.ensure_sessions(["2024-01-12"]) == 0, "末日本身重放应是 0 而不是报错")
    return f"3 个非法日期 + 1 个混批全部拒绝, n_sessions 仍 {a.n_sessions}"


def test_ensure_sessions_rejects_duplicate_in_batch():
    """同一批里出现两次同一天。轴单调、位置即列义, 重复即两个位置指向同一天。

    触发方式很平常：上游给的日期列表没去重（`--ed today` 与日历合并时最容易），
    而轴是 append-only, 写进去就再也拿不掉——`pos(d)` 与 `date(i)` 从此不再互逆。
    """
    a = fresh("ax_dup").axes
    n = a.ensure_sessions(["2024-01-15", "2024-01-15"])
    check(a.n_sessions == 11,
          f"['2024-01-15', '2024-01-15'] 追加后 n_sessions={a.n_sessions}（期望 11），"
          f"轴末尾 = {a.sessions[-3:]}，返回值 {n}——同一天占了两个位置")
    check(a.date(a.pos("2024-01-15")) == "2024-01-15", "pos/date 不再互逆")
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
          f"在已有轴上重放 create 并把 999 插到首位被静默接受：securities 由 {SECURITIES} "
          f"变成 {Axes.load(root).securities}——每个历史 chunk 的第 0 列现在指向另一只票")
    return type(e).__name__


def test_capacity_reserve():
    """预留列容量：§3.3「实际 + 500」。按标的 resize 要重写全部 chunk, 故一年一次。"""
    st = fresh("ax_cap")
    check(st.axes.allocated == len(SECURITIES) + RESERVE,
          f"allocated={st.axes.allocated}")
    st.write(R2, panel(SESSIONS, SECURITIES, 1.0))
    z = zarr.open_array(str(st.path(R2)), mode="r")
    check(z.shape[1] == st.axes.allocated,
          f"数组按 n_securities({st.axes.n_securities}) 而非 allocated 开宽度：{z.shape}")

    # 在预留额度内加一只新票（ingestion 的日常）：旧 chunk 必须仍可读, 新列是 NaN
    a = Path(st.axes.root) / "_axes"
    (a / "securities.json").write_text(json.dumps(SECURITIES + [106]))
    (a / "capacity.json").write_text(json.dumps({"n_active": 6, "allocated": st.axes.allocated}))
    st2 = Store(st.root, REGION)
    df = st2.read(R2)
    check(list(df.columns) == SECURITIES + [106], f"扩容后列轴 {list(df.columns)}")
    check(df[106].isna().all(), "新票的历史应当整列 NaN")
    check((df[SECURITIES] == 1.0).all().all(), "扩容后旧数据变了——旧 chunk 没能原样读出")
    return f"allocated={st.axes.allocated} 数组宽度={z.shape[1]} 扩容后旧值不变、新列全 NaN"


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
    check(isinstance(b1, pd.Series), f"秩-1 返回 {type(b1).__name__}，期望 Series")
    check(isinstance(b2, pd.DataFrame), f"秩-2 返回 {type(b2).__name__}，期望 DataFrame")
    check(isinstance(b3, np.ndarray), f"秩-3 返回 {type(b3).__name__}，期望 ndarray")
    check(b1.shape == (10,) and list(b1.index) == SESSIONS, f"秩-1 {b1.shape}")
    check(b2.shape == (10, 5) and list(b2.columns) == SECURITIES, f"秩-2 {b2.shape}")
    check(b3.shape == (10, 5, 4), f"秩-3 {b3.shape}")
    check(np.allclose(b1.to_numpy(), v1.to_numpy()), "秩-1 值变了")
    check(np.allclose(b2.to_numpy(), v2.to_numpy()), "秩-2 值变了")
    check(np.allclose(b3, v3), "秩-3 值变了")
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
        check(got[r] == w, f"{r} 分块 {got[r]}，期望 {w}")
    for r in (R1, R2, R3):
        z = zarr.open_array(str(st.path(r)), mode="r")
        check(np.isnan(z.fill_value), f"{r} 的 fill_value 是 {z.fill_value}，f4 必须是 NaN")
    return f"秩1 {got[R1]} 秩2 {got[R2]} 秩3 {got[R3]}"


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
    check(gf.dtypes.unique().tolist() == [np.dtype("f4")], f"f4 读回 {gf.dtypes.unique()}")
    check(gb.dtypes.unique().tolist() == [np.dtype("bool")], f"bool 读回 {gb.dtypes.unique()}")
    check(gi.dtypes.unique().tolist() == [np.dtype("i1")], f"i1 读回 {gi.dtypes.unique()}")
    check(np.array_equal(gf.to_numpy(), f.to_numpy()), "f4 值变了")
    check(np.array_equal(gb.to_numpy(), b.to_numpy()), "bool 值变了")
    check(np.array_equal(gi.to_numpy(), i.to_numpy()), "i1 值变了")
    return "f4 / bool / i1 均原样往返"


def test_read_aligns_to_global_axis():
    """§3.3 的核心承诺, 也是 L3 相对 L2 的核心价值：读回来的列**永远**是全局轴。

    调用方零对齐负担意味着 handle 里可以直接 `a / b`——只要有一处返回「只有写过的
    列」的窄表, 那个除法就变成静默的按标签对齐, 少掉的票无声消失。
    """
    st = fresh("st_align")
    st.write(R2, panel(SESSIONS, [101, 103, 105], 2.0))          # 只覆盖 5 列中的 3 列
    df = st.read(R2)
    check(list(df.columns) == SECURITIES, f"列轴 {list(df.columns)}，期望全局 5 列")
    check(list(df.index) == SESSIONS, f"行轴 {df.index[:3].tolist()}...")
    check((df[[101, 103, 105]] == 2.0).all().all(), "写过的列值不对")
    missing = [c for c in SECURITIES if df[c].isna().all()]
    check(missing == [102, 104], f"未覆盖的列应整列 NaN，实得 {missing}")
    return f"写 3/5 列 → 读回 {df.shape[1]} 列，未写列 {missing} 全 NaN"


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
        check(list(got.index) == list(want.index), f"[{sd},{ed}] 行轴 {list(got.index)}")
        check(np.allclose(got.to_numpy(), want.to_numpy(), equal_nan=True),
              f"[{sd},{ed}] 区间读与全量切片不一致")
        g1 = st.read(R1, sd, ed)
        check(np.allclose(g1.to_numpy(), full1.iloc[i0:i1].to_numpy(), equal_nan=True),
              f"[{sd},{ed}] 秩-1 区间读不一致")
    t = st.tail(R2, 3)
    check(list(t.index) == SESSIONS[-3:], f"tail(3) 给出 {list(t.index)}")
    check(np.allclose(t.to_numpy(), full.iloc[-3:].to_numpy(), equal_nan=True), "tail 与切片不符")
    return "4 组区间 + tail(3) 与全量切片逐位相同"


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
    for nm, r in [("秩1", R1), ("秩2", R2), ("秩3", R3)]:
        try:
            v = st.read(r)
            got[nm] = f"shape={v.shape}"
        except Exception as e:                               # noqa: BLE001
            got[nm] = f"{type(e).__name__}"
    try:
        got["tail"] = f"shape={st.tail(R2, 1).shape}"
    except Exception as e:                                   # noqa: BLE001
        got["tail"] = f"{type(e).__name__}"
    check(got == {"秩1": "shape=(11,)", "秩2": "shape=(11, 5)",
                  "秩3": "shape=(11, 5, 2)", "tail": "shape=(1, 5)"},
          f"轴 +1 天后 read/tail 没有对齐到全局轴：{got}"
          f"（期望三种秩都补出末行 NaN，tail(1) 给出那一行）")
    check(st.read(R2).iloc[-1].isna().all(), "补出来的末行应当是 NaN")
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
    check(df.shape == (10, 5), f"一行交付之后数组变成了 {df.shape}")
    check((df.iloc[:9] == 1.0).all().all(), "一行交付覆盖了历史——`write` 不是区间 upsert")
    check((df.iloc[-1] == 9.0).all(), "末行没写进去")

    st.write(R2, panel(SESSIONS[5:], SECURITIES, 7.0))           # run --sd 2024-01-08
    df = st.read(R2)
    check((df.iloc[:5] == 1.0).all().all(), "短区间回填截断了它之前的历史")
    check((df.iloc[5:] == 7.0).all().all(), "短区间没写全")
    m = st.meta(R2)
    check(m["version"] == v0, f"upsert 不该 bump version：{v0} → {m['version']}")
    check(m["first_session"] == 0 and m["last_session"] == 9,
          f"watermark 被区间写缩窄了：{m['first_session']}..{m['last_session']}")
    return f"1 行 / 5 行交付后仍是 {df.shape}，version 恒 {m['version']}"


def test_write_rejects_broken_date_index():
    """交付的日期必须是轴上一段连续、升序的区间——错序会让整块数据平移到别的日子。"""
    st = fresh("st_dates")
    e = raises(StoreError, st.write, R2,
               panel([SESSIONS[0], SESSIONS[4]], SECURITIES))     # 跳着的两天
    check("不连续" in str(e), f"报错没说清是日期不连续：{e}")
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
          f"错序索引 {idx} 被放行且按给定顺序落库：col101 = {got}，期望 [0.0, 1.0, 2.0, 3.0]"
          f"——第 2、3 天的值互换了, 且没有任何报错")
    return f"不连续/非交易日已拒；错序 → {type(err).__name__ if err else got}"


def test_version_bumps_only_on_rebuild():
    """§4.11.5：名字是契约, version 是构建。只有显式 --rebuild 才是新版本。"""
    st = fresh("st_ver")
    one = panel(SESSIONS[:1], SECURITIES)
    st.write(R2, one);                    v1 = st.meta(R2)["version"]
    st.write(R2, one);                    v2 = st.meta(R2)["version"]
    st.write(R2, one, rebuild=True);      v3 = st.meta(R2)["version"]
    st.write(R2, one);                    v4 = st.meta(R2)["version"]
    st.write(R2, one, rebuild=True);      v5 = st.meta(R2)["version"]
    check(v1 == 1, f"首次写入 version={v1}，期望 1")
    check(v2 == v1, f"普通写入 bump 了 version：{v1} → {v2}（version 会退化成天数计数器）")
    check(v3 == v1 + 1, f"rebuild 没 bump：{v1} → {v3}")
    check(v4 == v3, f"rebuild 之后的普通写入又 bump 了：{v3} → {v4}")
    check(v5 == v3 + 1, f"第二次 rebuild 没 bump：{v3} → {v5}")
    return f"1,1,2,2,3 → 实得 {v1},{v2},{v3},{v4},{v5}"


def test_check_fingerprint():
    """§3.3 / §十三 治理断言：改一行公式再跑日更必须被拦下, 而不是静默 upsert。

    upsert 不 bump version, 所以同一个数组里改动日之前是定义 A、之后是定义 B,
    meta 与 catalog 都看不出来, 事后也无法判断断点在哪天。
    """
    st = fresh("st_fp")
    check(st.check_fingerprint(R2, "sha256:aaaa") is None, "节点还不存在时应当静默放行")
    st.write(R2, panel(SESSIONS, SECURITIES), meta={"fingerprint": "sha256:aaaa"})
    check(st.check_fingerprint(R2, "sha256:aaaa") is None, "同一个指纹不该报错")
    e = raises(StoreError, st.check_fingerprint, R2, "sha256:bbbb")
    check("指纹" in str(e) and "sha256:aaaa" in str(e) and "sha256:bbbb" in str(e),
          f"报错没同时给出两边的指纹：{e}")
    check("rebuild" in str(e), f"报错没给出路（--rebuild / 换 identity）：{e}")
    st.write(R1, pd.Series(np.ones(10, dtype="f4"), index=SESSIONS), dims=["di"])
    check(st.check_fingerprint(R1, "sha256:cccc") is None,
          "store 里没记指纹的老节点应当放行（向后兼容）")
    return "首写静默 / 同指纹静默 / 改指纹抛 StoreError 且两边指纹都在消息里"


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
          f"展开结果 {hits}")
    check("g_yliu.liq.factor_yliu_liq2-x" not in hits, "前缀相近的另一个节点被卷了进来")
    e = raises(StoreError, st.expand, "g_yliu.liq.factor_yliu_none-*")
    check("空" in str(e), f"展开为空的报错文不对题：{e}")
    check(st.expand(R2) == [R2], "非通配名应原样返回")
    return f"3 个输出被展开、相邻节点未误入、空展开抛 {type(e).__name__}"


def test_path_ref_roundtrip():
    """§4.11.6 检查 ⑨：ref 拆解后能拼回原路径。两者一一对应、不需要索引。"""
    st = fresh("st_path")
    refs = [R1, R2, R3, RB, RI, "g_yliu.beta_decomp.factor_yliu_beta_decomp-mkt_beta_w250"]
    for ref in refs:
        p = st.path(ref)
        back = f"{p.parent.parent.name}.{p.parent.name}.{p.name}"
        check(back == ref, f"path→ref 回不去：{ref} → {p} → {back}")
        check(p.parent.parent.parent == st.root / REGION, f"region 层不对：{p}")
        r = parse_ref(ref)
        check(str(r) == ref, f"str(Ref) 不等于原串：{r}")
        check(st.path(r) == p, "传 Ref 与传字符串给出不同路径")
        check(f"{r.node_name}-{r.output}" == p.name, f"叶子名不是 node-output：{p.name}")
    st.write(R2, panel(SESSIONS[:1], SECURITIES))
    check(st.list_refs() == [R2], f"list_refs = {st.list_refs()}")
    cat = st.catalog()
    check(len(cat) == 1 and cat["ref"].iloc[0] == R2, f"catalog：\n{cat}")
    never = [c for c in cat.columns if cat[c].isna().all()]
    return f"{len(refs)} 个 ref 往返一致；catalog 中恒为空的列 {never}"


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
          f"列稀疏（500/{N}）占 {cols[0]/1e6:.2f} MB，稠密 {dense[0]/1e6:.2f} MB，"
          f"覆盖率 {cover:.1%} 却没省下相应的空间")
    check(both[0] < cols[0] / 5, f"双稀疏 {both[0]/1e6:.3f} MB 没比列稀疏 {cols[0]/1e6:.2f} MB 小")
    check(both[1] < dense[1], f"时间稀疏应当少写 chunk 文件：{both[1]} vs {dense[1]}")
    check(empty[1] <= 1, f"空节点应当只剩 zarr.json，实得 {empty[1]} 个文件")
    return (f"稠密 {dense[0]/1e6:.2f}MB/{dense[1]}文件 · 列稀疏 {cols[0]/1e6:.2f}MB/{cols[1]} "
            f"({cols[0]/dense[0]:.1%}, 覆盖率 {cover:.1%}) · 双稀疏 {both[0]/1e6:.3f}MB/{both[1]} "
            f"· 空 {empty[0]}B/{empty[1]}")


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
          f"bool 面板只交付了 101/103，未覆盖列 {uncovered} 读回 "
          f"{row[uncovered].tolist()}（NaN 转 bool = True）——池外的票被标成了池内")
    return (f"bool 未覆盖列 {row[uncovered].tolist()} · i1 未覆盖列 "
            f"{[irow[SECURITIES.index(c)] for c in uncovered]}"
            f"（{len(caught)} 条 cast 告警）")


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
          "写入含轴外列 999 的面板：既没报错、读回来也没有这一列——那一列的数据无声消失了")
    return f"{type(err).__name__ if err else 'kept'}"


def test_same_day_rerun_is_idempotent():
    """§十三 4：同日重跑不重复写。日更任务必须可重入（超时重试是常态）。"""
    st = fresh("st_idem")
    st.write(R2, panel(SESSIONS, SECURITIES, 1.0))
    one = panel(SESSIONS[-1:], SECURITIES, 3.0)
    st.write(R2, one); a = st.read(R2).to_numpy().copy(); n_a = st.axes.n_sessions
    st.write(R2, one); b = st.read(R2).to_numpy()
    check(np.array_equal(a, b, equal_nan=True), "同一天写两遍结果不同")
    check(st.axes.n_sessions == n_a, f"重跑把轴推长了：{n_a} → {st.axes.n_sessions}")
    check(st.meta(R2)["last_session"] == 9, f"last_session={st.meta(R2)['last_session']}")
    return f"重放同一日逐位相同，n_sessions 恒 {n_a}"


def test_read_missing_ref_says_where():
    """依赖不存在是最常见的一类失败, 报错必须带上它去哪儿找过（§7.1 唯一的兜底）。"""
    st = fresh("st_missing")
    e = raises(StoreError, st.read, R2)
    check(R2 in str(e) and str(st.path(R2)) in str(e), f"报错没给出期望路径：{e}")
    check(not st.exists(R2), "exists 对不存在的节点返回了 True")
    return "报错含 ref 与期望路径"


# ============================================================ 命名与配置 §4.11
def test_parse_ref_accepts_canonical():
    """`{repo}.{node_dir}.{node_name}-{output}`：kind / ns 从名字里解析, 不另外声明。"""
    r = parse_ref("g_yliu.liq.factor_yliu_liq-adv20")
    check(isinstance(r, Ref), f"返回的不是 Ref：{type(r).__name__}")
    check((r.repo, r.node_dir, r.node_name, r.output)
          == ("g_yliu", "liq", "factor_yliu_liq", "adv20"), f"拆错了：{r}")
    check(r.kind == "factor" and r.ns == "yliu", f"kind/ns = {r.kind}/{r.ns}")
    check(str(r) == "g_yliu.liq.factor_yliu_liq-adv20", f"拼不回去：{r}")
    r2 = parse_ref("g_yliu.rev.alpha_yliu_rev_w005-weight")
    check(r2.kind == "alpha" and r2.output == "weight", f"{r2.kind}/{r2.output}")
    check(parse_ref("g_common.field_base_px.field_base_px-adj_close_1500").ns == "base",
          "g_common 的 ns 段解析错")
    return f"kind={r.kind} ns={r.ns} output={r.output}，str() 往返一致"


def test_parse_ref_rejects_legacy_and_malformed():
    """老的三段式 `field.base.x` 与叶子无 `-` 必须被拒——它们与新形式看着一样长。"""
    bad = {
        "field.base.adj_close": "老三段式（kind/ns/name），叶子里没有 `-`",
        "g_yliu.liq.factor_yliu_liq": "缺 output",
        "g_yliu.liq.foo_yliu_liq-x": "首段不是 field/factor/alpha",
        "g_yliu.factor_yliu_liq-adv20": "只有两段",
        "a.b.c.d-e": "四段",
        "factor_yliu_liq-adv20": "裸名（neutralize 最容易这么写）",
    }
    for ref, why in bad.items():
        e = raises(ConfigError, parse_ref, ref)
        check(ref in str(e), f"{why}：报错里没有原串 {ref}")
    return f"{len(bad)} 种非法引用名全部拒绝"


def test_parse_ref_rejects_broken_leaf():
    """§4.11.1 的四条硬约束落在 ref 上：节点名必须是三段, output 非空、无连字符、无大写。

    只查首段的话, `factor-adv20` 一路通到 `Ref.ns`, 那里 `split("_")[1]` 抛 IndexError
    ——报错发生在离原因很远的地方, 且不是 ConfigError, 上层的 friendly 分支接不住。
    """
    bad = {
        "g_yliu.liq.factor-adv20": "节点名不是 {kind}_{ns}_{name}（Ref.ns 随后 IndexError）",
        "g_yliu.liq.factor_yliu_liq-": "output 为空 → 目录名以裸连字符结尾",
        "G_YLIU.LIQ.factor_YLIU_liq-ADV20": "大写（§4.11.1 第 4 条：APFS 上会撞名）",
        "g_yliu.liq.factor_yliu_liq-a-b": "output 里有连字符（§4.11.1 第 3 条）",
    }
    passed = {}
    for ref, why in bad.items():
        try:
            parse_ref(ref); passed[ref] = why
        except ConfigError:
            pass
    check(not passed, f"以下非法引用名被放行：{passed}")
    return f"{len(bad)} 种畸形叶子全部拒绝"


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
    check(syn is not None, "前提没了：5dr_250d= 居然是合法语法")
    e = raises(ConfigError, check_name, "5dr_250d", "输出名")
    check("5dr_250d" in str(e), f"报错没带上名字：{e}")
    check(check_name("dr5_250d", "输出名") is None, "数字在中间是合法的")
    return f"5dr_250d 被拒；直接编译得 SyntaxError: {syn.msg}"


def test_check_name_rejects_reserved_and_unsafe():
    """§4.11.6 保留字表 + §4.11.1 语法：每一条都对着一个具体的坏结果。"""
    bad = {
        "class": "Python 关键字 → 关键字参数是 SyntaxError",
        "return": "同上（§4.11.1 点名的那个）",
        "Adv20": "大写 → 大小写不敏感文件系统上两台机器不一致",
        "adj.close": "点号 → 与引用名的分段符冲突",
        "adj-close": "连字符 → 与 {node_name}-{output} 的接缝冲突",
        "adj_close_tc": "_tc 结尾 → 那只是源码形态的模板标记",
        "_axes": "下划线开头 → 与轴目录撞名",
        "dims": "schema 键", "outputs": "schema 键", "all": "缺省 universe",
        "region": "schema 键", "sim": "schema 键",
        "a" * 41: "超过 40 字符",
        "": "空名",
    }
    for s, why in bad.items():
        raises(ConfigError, check_name, s, "输出名")
    for good in ("adv20", "adj_close_1500", "mkt_beta_w250", "weight", "rv_5m"):
        check(check_name(good, "输出名") is None, f"合法名字被拒：{good}")
    return f"{len(bad)} 个非法名全拒、5 个合法名全过"


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
    check("lqin" in str(e) and "yliu" in str(e), f"报错没点出两边：{e}")
    s = spec_from("nodes:\n  factor_yliu_x: {}\n")
    check(list(s.nodes) == ["factor_yliu_x"], f"自己的 ns 被误拒：{list(s.nodes)}")
    s = spec_from("nodes:\n  field_base_px: {}\n", repo="g_common", node_dir="base_px",
                  stem="base_px")
    check(list(s.nodes) == ["field_base_px"], "g_common 应当能写 base 等共享 ns")
    e = raises(ConfigError, spec_from, "nodes:\n  factor_yliu: {}\n")
    check("{kind}_{ns}_{name}" in str(e) or "三段" in str(e) or "name" in str(e),
          f"两段名字的报错文不对题：{e}")
    return "跨 ns 拒绝 / 自己的 ns 放行 / g_common 豁免 / 两段名拒绝"


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
          f"`factor__x` 被接受：ns/outputs = {got}——ns 段没有做语法检查")
    return type(e).__name__


def test_single_output_default_name():
    """§4.11.2 / 检查 ③：单输出的名字由节点名推导, 不许另起一个。

    单输出 alpha 恒为 `weight`——`…-weight` 是「这是个可评估的 alpha」的可 grep 标志,
    改掉它, pnl 与 §15 的 alpha 池就得靠约定而不是靠名字找权重。
    """
    s = spec_from("nodes:\n  factor_yliu_liq:\n    params: {window: 20}\n")
    check(list(s.nodes["factor_yliu_liq"].outputs) == ["liq"],
          f"数据节点缺省输出名 {list(s.nodes['factor_yliu_liq'].outputs)}，期望去掉前缀的 liq")
    s = spec_from("nodes:\n  factor_yliu_beta_decomp: {}\n", node_dir="bd", stem="bd")
    check(list(s.nodes["factor_yliu_beta_decomp"].outputs) == ["beta_decomp"],
          "多段 name 的缺省输出名应当是整个去前缀部分")
    s = spec_from("nodes:\n  alpha_yliu_rev_w005:\n    params: {window: 5}\n"
                  "    ops: [{scale: book}]\n", node_dir="rev", stem="rev")
    check(list(s.nodes["alpha_yliu_rev_w005"].outputs) == ["weight"],
          f"单输出 alpha 的缺省名 {list(s.nodes['alpha_yliu_rev_w005'].outputs)}，期望 weight")
    e = got = None
    try:
        got = list(spec_from("nodes:\n  factor_yliu_liq:\n    outputs:\n      banana: {}\n"
                             ).nodes["factor_yliu_liq"].outputs)
    except ConfigError as exc:
        e = exc
    check(e is not None,
          f"单输出显式写成 {got} 被接受——检查 ③「单输出 key == 缺省名」没有实现，"
          f"节点 factor_yliu_liq 的缺省名是 'liq'")
    return "数据节点/多段名/alpha 三条缺省都对"


def test_alpha_must_be_rank2_and_end_with_scale():
    """§3.6 + §4.11.6 检查 ⑧：alpha 是权重, 秩必须是 di×ii, ops 链必须以 scale 收尾。

    少了 scale：上游各自 Σ|w|=1 的权重线性组合后会因抵消而缩水, 账本投不满而
    Sharpe 看着正常——一个不会报错、只会让收益凭空少一截的错误。
    """
    e = raises(ConfigError, spec_from,
               "nodes:\n  alpha_yliu_x:\n    outputs:\n"
               "      weight: {dims: [di, ii, ti], grid: m5, ops: [{scale: book}]}\n",
               node_dir="a1", stem="a1")
    check("秩-2" in str(e) or "di×ii" in str(e), f"秩报错文不对题：{e}")
    e = raises(ConfigError, spec_from, "nodes:\n  alpha_yliu_x:\n    ops: [rank]\n",
               node_dir="a2", stem="a2")
    check("scale" in str(e), f"收尾报错没提 scale：{e}")
    e = raises(ConfigError, spec_from, "nodes:\n  alpha_yliu_x: {}\n",
               node_dir="a3", stem="a3")
    check("scale" in str(e), f"空 ops 的 alpha 也必须被拒：{e}")
    e = raises(ConfigError, spec_from,
               "nodes:\n  alpha_yliu_x:\n    ops: [{scale: book}, rank]\n",
               node_dir="a4", stem="a4")
    check("scale" in str(e), f"scale 不在最后也必须被拒：{e}")
    s = spec_from("nodes:\n  alpha_yliu_x:\n    ops:\n      - rank\n      - truncate: 0.02\n"
                  "      - scale: book\n", node_dir="a5", stem="a5")
    check([o for o, _ in s.nodes["alpha_yliu_x"].outputs["weight"].ops][-1] == "scale",
          "合法链被改了")
    return "秩-3 / 无 scale / 空链 / scale 不在末尾 四种全拒"


def test_cs_ops_only_on_rank2():
    """§3.6：CS 类作用在 ii 上, 仅秩-2 合法；秩-1 没有 ii 轴, 秩-3 的轴不明确。"""
    for dims, extra, tag in [("[di]", "", "秩-1"),
                             ("[di, ii, ti]", ", grid: m5", "秩-3")]:
        for op in sorted(CS_OPS):
            arg = {"rank": "", "truncate": ": 0.02", "scale": ": book",
                   "neutralize": ": g_common.factor_common_gics.factor_common_gics-sector"}[op]
            e = raises(ConfigError, spec_from,
                       f"nodes:\n  factor_yliu_m:\n    outputs:\n"
                       f"      m: {{dims: {dims}{extra}, ops: [{{{op}{arg}}}]}}\n"
                       if arg else
                       f"nodes:\n  factor_yliu_m:\n    outputs:\n"
                       f"      m: {{dims: {dims}{extra}, ops: [{op}]}}\n",
                       node_dir="cs", stem="cs")
            check(op in str(e) and ("CS" in str(e) or "秩-2" in str(e)),
                  f"{tag} 上的 {op} 报错文不对题：{e}")
    for op in sorted(TS_OPS):                       # TS 类三种秩皆合法
        arg = {"linear_decay": 3, "exp_decay": 5, "delay": 1}[op]
        s = spec_from(f"nodes:\n  factor_yliu_m:\n    outputs:\n"
                      f"      m: {{dims: [di], ops: [{{{op}: {arg}}}]}}\n",
                      node_dir="ts", stem="ts")
        check(s.nodes["factor_yliu_m"].outputs["m"].ops == [(op, arg)], f"{op} 在秩-1 被拒")
    e = raises(ConfigError, spec_from,
               "nodes:\n  factor_yliu_m:\n    outputs:\n      m: {dims: [di, ii, ti]}\n",
               node_dir="g", stem="g")
    check("grid" in str(e), f"秩-3 缺 grid 的报错文不对题：{e}")
    return f"秩-1/秩-3 × {len(CS_OPS)} 个 CS 算子全拒；{len(TS_OPS)} 个 TS 算子在秩-1 放行"


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
          f"逗号案的报错没点出收到的是字符串：{e}")
    bad = [
        ("truncate", "'0.02'", "带引号的数"), ("truncate", "true", "bool 不是数"),
        ("linear_decay", "3.5", "小数不是正整数"), ("linear_decay", "0", "0 不是正整数"),
        ("linear_decay", "-3", "负数"), ("delay", "'2'", "字符串"),
        ("exp_decay", "true", "bool"), ("neutralize", "sector", "裸名不是全 ref"),
        ("neutralize", "3", "不是名字"), ("rank", "3", "rank 不接受参数"),
    ]
    for op, arg, why in bad:
        raises(ConfigError, spec_from,
               f"nodes:\n  factor_yliu_m:\n    ops: [{{{op}: {arg}}}]\n",
               node_dir="o2", stem="o2")
    e = raises(ConfigError, spec_from,
               "nodes:\n  factor_yliu_m:\n    ops: [zscore]\n", node_dir="o3", stem="o3")
    check("zscore" in str(e) and "可用" in str(e), f"未知算子的报错没列出可用算子：{e}")
    s = spec_from("nodes:\n  factor_yliu_m:\n    ops:\n      - rank\n"
                  "      - truncate: 0.02\n      - linear_decay: 3\n"
                  "      - neutralize: g_common.factor_common_gics.factor_common_gics-sector\n",
                  node_dir="o4", stem="o4")
    ops = s.nodes["factor_yliu_m"].outputs["m"].ops
    check(ops[1] == ("truncate", 0.02) and ops[2] == ("linear_decay", 3),
          f"合法参数被改了：{ops}")
    check(set(OP_TYPES) == CS_OPS | TS_OPS, f"算子分类漂移：{set(OP_TYPES) ^ (CS_OPS | TS_OPS)}")
    return f"逗号案 + {len(bad)} 种错类型 + 未知算子全拒；合法链原样保留"


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
          f"节点级 ops [rank, truncate] 配 2 个输出 → 实得 {got}，ops 链被静默丢弃")
    return type(e).__name__ if e else str(got)


def test_node_ops_and_output_ops_conflict():
    """两处都写 ops 时哪一处生效只能靠猜——所以编译期直接拒绝。"""
    e = raises(ConfigError, spec_from,
               "nodes:\n  factor_yliu_two:\n    ops: [rank]\n    outputs:\n"
               "      a: {ops: [rank]}\n      b: {}\n", node_dir="tw2", stem="tw2")
    check("同时" in str(e), f"报错文不对题：{e}")
    s = spec_from("nodes:\n  factor_yliu_one:\n    ops:\n      - truncate: 0.02\n"
                  "    outputs:\n      one: {}\n", node_dir="tw3", stem="tw3")
    check(s.nodes["factor_yliu_one"].outputs["one"].ops == [("truncate", 0.02)],
          "单输出时节点级 ops 应当落到那唯一的输出上")
    return "并存拒绝；单输出时节点级 ops 生效"


def test_param_tag_consistency():
    """§4.11.4：params 是真相, 名字是标签, 编译期校验二者一致。

    抓的是「复制了一个变体却只改了 params 忘了改名」——改完名字仍写着 w005、
    params 已是 20, 两个变体在 dump 与 pnl 里就成了同名的两条不同曲线。
    """
    s = spec_from("nodes:\n  factor_yliu_adv20:\n    params: {window: 20}\n",
                  node_dir="t1", stem="t1")
    check(list(s.nodes) == ["factor_yliu_adv20"],
          "单个从未被扫描的粘连名（adv20）应当放行——§4.11.4 的行业惯用语豁免")
    two = ("nodes:\n  alpha_yliu_rev_w005:\n    params: {window: %s}\n"
           "    ops: [{scale: book}]\n"
           "  alpha_yliu_rev_w020:\n    params: {window: 20}\n    ops: [{scale: book}]\n")
    s = spec_from(two % 5, node_dir="t2", stem="t2")
    check(len(s.nodes) == 2, "对得上的一族被误拒")
    e = raises(ConfigError, spec_from, two % 20, node_dir="t3", stem="t3")
    check("w005" in str(e) or "005" in str(e), f"报错没指出是哪个成员：{e}")
    e = raises(ConfigError, spec_from,
               "nodes:\n  alpha_yliu_rev_w005:\n    params: {window: 5}\n"
               "    ops: [{scale: book}]\n"
               "  alpha_yliu_rev_slow:\n    params: {window: 20}\n    ops: [{scale: book}]\n",
               node_dir="t4", stem="t4")
    check("标签" in str(e), f"一族里缺标签的报错文不对题：{e}")
    e = raises(ConfigError, spec_from,
               "nodes:\n  factor_yliu_x_h010:\n    params: {halflife: 20}\n"
               "  factor_yliu_x_h020:\n    params: {halflife: 20}\n",
               node_dir="t5", stem="t5")
    check("h" in str(e), f"halflife 标签没被校验：{e}")
    check(set(TAGS) >= {"window", "halflife", "lag", "quantile", "count"},
          f"标签字典缩水：{TAGS}")
    return f"孤例豁免 / 一族对上放行 / 值不符与缺标签均拒（{len(TAGS)} 个标签）"


def test_fingerprint_covers_the_definition():
    """§3.3：指纹 = yaml 子树 + code 字节 + deps identity + params。

    它是「改了定义却没换名字」的唯一防线, 所以凡是能改变数值的输入都必须进指纹。
    `universe:` 与 `lookback:` 写在 yaml 顶层而非节点子树里, 但它们**逐值改变输出**
    （池外整列 NaN、预热长度决定 TS 算子的初值），改了却指纹不变, check_fingerprint
    就会放行, 同一个数组里改动日前后是两个定义。
    """
    body = "nodes:\n  factor_yliu_f:\n    params: {window: 20}\n    deps: [%s]\n"
    dep = "g_common.field_base_px.field_base_px-adj_close_1500"
    base = spec_from(body % dep, node_dir="fp", stem="fp").nodes["factor_yliu_f"].fingerprint()
    p = spec_from((body % dep).replace("window: 20", "window: 21"),
                  node_dir="fp", stem="fp").nodes["factor_yliu_f"].fingerprint()
    check(p != base, "改 params 指纹没变")
    d = spec_from(body % "g_common.field_base_px.field_base_px-volume_1500",
                  node_dir="fp", stem="fp").nodes["factor_yliu_f"].fingerprint()
    check(d != base, "换 deps 指纹没变")
    c = spec_from(body % dep, node_dir="fp", stem="fp",
                  code="def handle(ctx):\n    return 1.0\n"
                  ).nodes["factor_yliu_f"].fingerprint()
    check(c != base, "改 code 指纹没变")
    u = spec_from("universe: g_common.field_common_univ.field_common_univ-us_top400\n" + (body % dep),
                  node_dir="fp", stem="fp").nodes["factor_yliu_f"].fingerprint()
    u2 = spec_from("universe: g_common.field_common_univ.field_common_univ-us_top3000\n" + (body % dep),
                   node_dir="fp", stem="fp").nodes["factor_yliu_f"].fingerprint()
    lb = spec_from("lookback: 250\n" + (body % dep),
                   node_dir="fp", stem="fp").nodes["factor_yliu_f"].fingerprint()
    check(u != u2 and lb != base,
          f"改 universe（top400 → top3000）指纹相同={u == u2}、改 lookback 指纹相同="
          f"{lb == base}——这两项逐值改变输出却不进指纹，日更会静默放行")
    return f"params/deps/code 三项均改变指纹（{base[:14]}…）"


def test_op_contract_matches_runner():
    """编译期放行的算子参数, 执行期必须也认——两边不一致时用户被挡在合法写法之外。"""
    try:
        from alpha_kit.runner.ops import OPS, OpChain
    except Exception as e:                          # noqa: BLE001
        return f"跳过（runner.ops 不可导入：{e}）"
    check(set(OPS) == set(OP_TYPES), f"算子集合漂移：{set(OPS) ^ set(OP_TYPES)}")
    runtime_ok = True
    try:
        OpChain([("rank", None), ("scale", None)], None)     # 执行期允许裸 scale
    except Exception:                               # noqa: BLE001
        runtime_ok = False
    cfg_ok = True
    try:
        spec_from("nodes:\n  alpha_yliu_x:\n    ops: [rank, scale]\n",
                  node_dir="sc", stem="sc")
    except ConfigError:
        cfg_ok = False
    check(runtime_ok == cfg_ok,
          f"`ops: [rank, scale]`（不带参数的 scale）执行期接受={runtime_ok}、"
          f"编译期接受={cfg_ok}——OP_TYPES['scale'] 是 str，None 过不去，"
          f"而 OpChain 明确把 None 当作 book")
    return f"{len(OPS)} 个算子名一致，裸 scale 两侧一致={runtime_ok == cfg_ok}"


def test_real_repo_specs_load():
    """仓库里现成的 yaml 必须都能加载——它们是 §4.10 那条研究链的实物。"""
    root = Path(__file__).resolve().parents[1] / "repos"
    if not root.exists():
        return "跳过（没有 repos/）"
    files = sorted(root.glob("g_*/nodes/*/*.yaml"))
    check(files, f"没找到任何节点 yaml：{root}")
    n_nodes = n_alpha = 0
    for f in files:
        s = load_spec(f)
        n_nodes += len(s.nodes)
        for node in s.nodes.values():
            check(node.repo == f.parents[2].name, f"{f}: repo 推导错 {node.repo}")
            check(node.node_dir == f.parent.name, f"{f}: node_dir 推导错 {node.node_dir}")
            check(node.kind in KINDS, f"{f}: kind={node.kind}")
            for k, o in node.outputs.items():
                check(str(node.ref(k)).endswith(f"{node.name}-{k}"), f"{f}: ref 拼错")
            if node.kind == "alpha":
                n_alpha += 1
                check(all(str(node.ref(k)).endswith("-weight") for k in node.outputs)
                      or len(node.outputs) > 1, f"{f}: 单输出 alpha 不叫 weight")
    return f"{len(files)} 个 yaml / {n_nodes} 个节点（{n_alpha} 个 alpha）全部加载通过"


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
    test_fingerprint_covers_the_definition,
    test_op_contract_matches_runner,
    test_real_repo_specs_load,
]

if __name__ == "__main__":
    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    print(f"core 自检  ({len(TESTS)} 项)  zarr {zarr.__version__} / "
          f"pandas {pd.__version__} / numpy {np.__version__}")
    print(f"临时 store: {TMP}\n")
    for t in TESTS:
        run(t)
    print(f"\n{len(TESTS) - len(FAILS)}/{len(TESTS)} 通过")
    if FAILS:
        print("\n红的这几条不是测试环境问题, 每条都对着 architecture.md 里明写的一条承诺：")
        for i, (name, msg) in enumerate(FAILS, 1):
            print(f"  {i}. {name}\n     {msg.splitlines()[0]}")
    sys.exit(1 if FAILS else 0)
