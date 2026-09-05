"""alpha 的出口算子链（architecture.md §6.2 / §3.5 / §3.6 / 附录 B）。

一条链就是「顺序即语义」的算子列表。引擎逐日调 `chain(v, t)`：`v` 是当日截面，
`t` 是全局 session 序号——**含预热段、每次 +1**，因为 TS 算子的缓冲按调用次数推进，
预热那几天的输出虽然被丢弃，状态却必须照常前进（§7.1「预热段照常执行、只喂状态」）。

**两端夹住（§3.5）**：链首池外压成 NaN、`scale` 之后池外压成 0，中间自由。
中间之所以能自由，正是因为第二道闸门在：TS 算子会把昨天的值搬到今天，而那只票今天
可能已经出池——只做第一道的实现里，它就带着非零权重进了 dump 与 pnl，同时
`rank` / `neutralize` 的 scope 悄悄退化成全集。两者都只改变口径、都不报错（§7.2 第 3 条）。

**op-state 归链、不归 ctx（§十）**：decay/delay 的缓冲是引擎的世界，ctx 只装 handle
的世界。放进 ctx 则 handle 能摸到自己的 decay 缓冲，语义即脏。

**CS 类仅秩-2（§3.6）**：`config._norm_ops` 已在编译期挡住秩-1/秩-3 用 CS 算子，
故这里只按截面 Series 实现，不再重复判秩。TS 核按 ndarray 写（`_TsOp`），秩-1/秩-3
接进来时是同一份实现，只是缓冲形状不同。

算子函数（`cs_*`）都是无状态纯函数，`ctx.to_weight(x, **overrides)` 复用同一份实现
（§6.2「ops 算子与普通算子共用实现」）。
"""
from __future__ import annotations

import warnings
from typing import Protocol, runtime_checkable

import numpy as np
import pandas as pd

from ..core import opspec

# exp_decay 的预热下限取几个半衰期（§7.1：引擎取 max(config 声明值, 这个下限)）。
# 唯一出处在 core.opspec（预热规则与算子声明同属一处）; 这里转出来给旧调用点。
EXP_DECAY_WARMUP_HALFLIVES = opspec.EXP_DECAY_WARMUP_HALFLIVES


# ------------------------------------------------------------------ universe
@runtime_checkable
class UniverseLike(Protocol):
    """ops 链看得见的 universe 切面（§3.5 三个角色中的后两个：CS scope 与权重掩码）。

    只有两个方法：当日池子、分组字段。链不持 store 句柄、也不关心池子怎么生产
    （PIT / 含退市 / ADV 门槛 / 缓冲带 / 月度重构都是该 field 生产者的内部事务）。
    """

    def mask(self, t: int) -> pd.Series:
        """第 t 个 session 的池子成员：index=security_id 的 bool Series。"""
        ...

    def group(self, ref: str, t: int) -> pd.Series:
        """`neutralize` 的分组字段（一个 int field 的全 ref）在第 t 日的取值。"""
        ...


def _mask_array(uni: "UniverseLike | None", t: int, index: pd.Index) -> np.ndarray | None:
    """把当日池子对齐到当日列轴。不在池子面板里的名字算池外——缺席即不在池内，
    比 KeyError 更贴近语义（新股在旧的池子面板里本来就没有行）。"""
    if uni is None:
        return None                     # universe 缺省 all（§4.4），掩码是恒等
    m = uni.mask(t)
    if m is None:
        return None
    if not m.index.equals(index):
        m = m.reindex(index)
    return np.asarray(m.fillna(False), dtype=bool)


# ----------------------------------------------------------------- CS 算子
def cs_rank(v: pd.Series) -> pd.Series:
    """scope 内映射到 [-0.5, 0.5]，NaN 保持（§6.2 / 附录 B）。

    NaN 既不参与排名也不占名次（skipna），输出位仍是 NaN——不是填 0、更不是排在末位：
    「没有观测」和「观测值最小」是两件事，混起来会让覆盖率下降悄悄变成一个空头因子。
    scope 无需另算：链首已把池外压成 NaN，skipna 天然就把 scope 限在池内。
    端点取到 ±0.5（闭区间），且平均名次法下 rank 之和恒定，故输出截面均值恒为 0——
    rank 之后的截面天然是美元中性的。
    """
    r = v.rank(method="average")
    m = int(r.notna().sum())
    if m <= 1:
        # 独苗没有截面可言：给 0 而不是 ±0.5，免得单票在 scale 后独吞整本账
        return v.where(v.isna(), 0.0)
    return (r - 1.0) / (m - 1.0) - 0.5


def cs_neutralize(v: pd.Series, groups: pd.Series) -> pd.Series:
    """分组 demean（§6.2）。分组字段缺失的名字自成一组，值为 NaN 的位保持 NaN。

    `dropna=False` 是要害：默认的 `dropna=True` 会把分组缺失的名字整组丢掉，它们的
    组均值变 NaN，于是原本有值的票在这里静默变成 NaN——覆盖率掉一块而不报错。
    组内均值 skipna，所以 NaN 值不拉偏组均值；单元素组 demean 后恒为 0，这是对的：
    一个组里只有一只票时，它不携带任何截面信息。
    """
    g = groups if groups.index.equals(v.index) else groups.reindex(v.index)
    return v - v.groupby(g, dropna=False).transform("mean")


def cs_truncate(v: pd.Series, x: float) -> pd.Series:
    """单票 |w| ≤ x × gross（§6.2）。gross 取**截断前**的 Σ|w|，单次夹紧、不迭代。

    迭代到不动点（每夹一次 gross 变小、cap 跟着变小）在 x < 1/有效票数 时根本没有
    非退化解，会把整本账迭代成 0。单次夹紧的代价是：紧随其后的 `scale` 会按缩水后的
    gross 归一，把被夹的票顶回 x 之上一点点（比例是 gross_前/gross_后）。只要被夹的
    是少数票（truncate 本来就是干这个的），这个偏离可以忽略。
    """
    gross = float(np.nansum(np.abs(v.to_numpy(dtype=float))))
    if not np.isfinite(gross) or gross <= 0.0:
        return v                        # 无仓可截；cap=0 会把全票夹成 0
    cap = x * gross
    return v.clip(-cap, cap)            # clip 保持 NaN


def cs_gross(v: pd.Series) -> float:
    """Σ|w|，skipna。0 意味着「今天没有仓位」而不是「除数待定」。"""
    return float(np.nansum(np.abs(v.to_numpy(dtype=float))))


def as_weights(v: pd.Series, mask: np.ndarray | None = None) -> pd.Series:
    """权重收口：池外 → 0、NaN → 0（§3.5 闸门二 + 附录 B「scale 时 NaN → 权重 0」）。

    强制的是 0 而不是 NaN：NaN 的权重不是权重，它会在 pnl 的推进式里
    `pos_value * (1 + NaN)` 摧毁持仓并向后传染（附录 B 最后两行）。
    """
    w = v if mask is None else v.where(mask)
    return w.fillna(0.0)


def cs_scale(v: pd.Series, mask: np.ndarray | None = None, *,
             gross: float | None = None) -> pd.Series:
    """Σ|w| = 1，并落下 §3.5 的第二道闸门（§6.2）。

    **顺序要紧：池外先出局，再算 gross。** 反过来（先归一、再把池外抹成 0）时两条
    承诺不能同时成立——TS 算子会把昨天的值搬到今天，那只票今天若已出池，它的那份
    权重先进了分母、再被抹掉，Σ|w| 就悄悄小于 1，正是 §4.10 例 7 说的「账本有一半
    没投出去而 Sharpe 看着正常」。掩码进分母之前落地，两条才同时为真。

    gross 为 0（全抵消 / 全 NaN）时**不做除法**：0/0 会把整本账变成 NaN 或 inf，
    而那是一整段静默错误的开始。此时不缩放、只收口——非 NaN 值本就都是 0，
    收口后是一本诚实的空账。
    """
    v = v if mask is None else v.where(mask)
    g = cs_gross(v) if gross is None else gross
    if not np.isfinite(g) or g <= 0.0:
        return as_weights(v)                    # 不缩放，只收口：非 NaN 值本就都是 0
    return as_weights(v / g)


# ------------------------------------------------------------------ TS 算子
class _TsOp:
    """TS 算子的共同形状：每次调用推进一格，`reset()` 回到零状态。

    缓冲存 float64 ndarray 而不是 Series：§十 保证列轴全局共享、逐日恒定，存标签
    等于每天重复存一份 6000 元素的 index；反过来，列轴一旦真的变了必须吵闹地失败，
    否则就是按位置错配——每一天都算得出数、每一个数都对错了票。
    也统一升到 float64：面板落库是 f4，用 f4 累加 decay 会一路掉精度。
    """

    def reset(self) -> None:
        raise NotImplementedError

    def _check_width(self, x: np.ndarray) -> None:
        if getattr(self, "_width", None) is None:
            self._width = x.size
        elif self._width != x.size:
            raise ValueError(
                f"{type(self).__name__}: column-axis width changed from {self._width} to {x.size} -- "
                f"TS buffers align by position, so a width change is a positional mismatch. "
                f"ctx should deliver cross-sections aligned to the global column axis (§10); "
                f"if the period really changed, call reset() first.")

    def __call__(self, x: np.ndarray) -> np.ndarray:
        raise NotImplementedError


class LinearDecay(_TsOp):
    """线性衰减：今日权重 n、昨日 n-1、……、n-1 天前 1，按**有效**权重归一（§6.2）。

    分母是有效权重之和而非常数 sum(w)，于是缓冲里的 NaN 只是「该票该日按权重 0 参与」
    而不传染整条缓冲（附录 B）。预热期走的是同一条路：少一天历史 = 那天缺一个观测，
    所以 warmup 不需要任何特判，跑不跑预热只影响精度、不改变代码路径。
    """

    def __init__(self, n: int):
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ValueError(f"linear_decay needs a positive integer window, got {n!r}")
        self.n = int(n)
        self.w = np.arange(self.n, 0, -1, dtype=float)   # 按 age：[n, n-1, …, 1]
        self.reset()

    def reset(self) -> None:
        self._buf: np.ndarray | None = None
        self._width: int | None = None
        self._pos = 0                   # 今日写在哪一行

    def __call__(self, x: np.ndarray) -> np.ndarray:
        self._check_width(x)
        if self._buf is None:
            self._buf = np.full((self.n, x.size), np.nan)  # 未填满处即 NaN 即权重 0
        self._pos = (self._pos - 1) % self.n
        self._buf[self._pos] = x
        # 行 r 的 age = (r - pos) % n；np.roll 正是这个映射，O(n) 而非 O(n·N) 的搬缓冲
        w = np.roll(self.w, self._pos)[:, None]
        num = np.nansum(self._buf * w, axis=0)
        den = np.where(np.isnan(self._buf), 0.0, w).sum(axis=0)
        # np.where 会把两个分支都算出来（含 0/0 的告警），用 where= 从源头跳过
        return np.divide(num, den, out=np.full(x.size, np.nan), where=den > 0.0)


class ExpDecay(_TsOp):
    """指数衰减：lag j 的权重 0.5**(j/h)，按有效权重归一（§6.2）。

    用递推的 (num, den) 而不是定长环形缓冲。§4.10 里 `alpha_yliu_rev_w005_h250`
    的 h=250 是真实用法，要把权重截到可忽略需要约 10 个半衰期 = 2500 天缓冲，在
    N=6000 上是 100 MB 级常驻——而递推是同一个加权平均的**精确**形式（窗口无限长），
    只占 O(N)。两个累加器同步衰减，故 num/den 与整体缩放无关；den 上界为 1。
    NaN 那天两个累加器都不加，等价于权重 0（附录 B），不传染。

    一个已知后果：某票长期 NaN 时 num/den 冻结不动（无限窗口不会让它自然过期），
    §3.5 的第二道闸门是收口处——退市/出池的票 universe 必为 False，scale 后归 0。
    """

    def __init__(self, h: int):
        if not isinstance(h, int) or isinstance(h, bool) or h < 1:
            raise ValueError(f"exp_decay needs a positive half-life, got {h!r}")
        self.h = int(h)
        self.a = 0.5 ** (1.0 / float(self.h))
        self.reset()

    def reset(self) -> None:
        self._num: np.ndarray | None = None
        self._den: np.ndarray | None = None
        self._width: int | None = None

    def __call__(self, x: np.ndarray) -> np.ndarray:
        self._check_width(x)
        if self._num is None:
            self._num = np.zeros(x.size)
            self._den = np.zeros(x.size)
        self._num *= self.a
        self._den *= self.a
        ok = ~np.isnan(x)
        w0 = 1.0 - self.a               # 今日权重；只是缩放，num/den 不受影响
        self._num[ok] += w0 * x[ok]
        self._den[ok] += w0
        return np.divide(self._num, self._den, out=np.full(x.size, np.nan),
                         where=self._den > 0.0)


class Delay(_TsOp):
    """显式滞后 k 个 session（§6.2）。前 k 次调用无历史，输出 NaN。

    §6.2「delay 双重身份」：执行滞后由撮合边界全局施加一次，这里只服务**故意**做的
    滞后版本。别惯性再加一次——那是白白多丢一天的信息且不会有任何报错。
    """

    def __init__(self, k: int):
        if not isinstance(k, int) or isinstance(k, bool) or k < 1:
            raise ValueError(f"delay needs a positive integer lag, got {k!r}")
        self.k = int(k)
        self.reset()

    def reset(self) -> None:
        self._buf: np.ndarray | None = None
        self._width: int | None = None
        self._pos = 0

    def __call__(self, x: np.ndarray) -> np.ndarray:
        self._check_width(x)
        if self._buf is None:
            self._buf = np.full((self.k, x.size), np.nan)
        out = self._buf[self._pos].copy()    # 此格现在装的正是 k 次之前的那天
        self._buf[self._pos] = x
        self._pos = (self._pos + 1) % self.k
        return out


_TS_FACTORY = {"linear_decay": LinearDecay, "exp_decay": ExpDecay, "delay": Delay}
_CS_OPS = tuple(sorted(opspec.CS_OPS))
OPS = tuple(_CS_OPS) + tuple(_TS_FACTORY)
# 接缝从测试搬进代码: 分派表与声明表对不上就是 ImportError, 不必等测试跑。
opspec.check_covers(OPS, "runner.ops.OPS")
opspec.check_covers(tuple(_TS_FACTORY) + _CS_OPS, "runner.ops dispatch")


def ops_lookback(ops: list[tuple[str, object]]) -> int:
    """一条 ops 链的预热下限——转调 `core.opspec.lookback`, 不另写一份阶梯。

    runner 要在**建链之前**就知道预热多久, 而建链需要池子（neutralize 要取分组字段）,
    所以它必须是个不依赖链实例的函数。
    """
    return opspec.lookback(ops)


# --------------------------------------------------------------------- 链
class OpChain:
    """一个输出的 ops 链 + 它的 op-state（§6.2 / §7.2）。

    每个输出一条链、每条链独占自己的缓冲。构造期把算子编译成一列闭包，执行期只是
    顺序调用——与 §四「执行期不存在任何按 kind 的分支」同一个取向。
    """

    def __init__(self, ops: list[tuple[str, object]], universe: "UniverseLike | None") -> None:
        self.ops = [(str(op), arg) for op, arg in (ops or [])]
        self.universe = universe
        self.degenerate_scale: list[int] = []   # gross=0 的日子，供 runner 汇报
        self._state: dict[int, _TsOp] = {}      # 按位置存：同一个算子可以出现两次
        self._plan: list = []
        self._last_t: int | None = None

        for i, (op, arg) in enumerate(self.ops):
            if op in _TS_FACTORY:
                # 缓冲按位置归属：`[delay: 1, delay: 1]` 是两个独立的滞后器
                self._state[i] = _TS_FACTORY[op](arg)
                self._plan.append(self._ts_step(self._state[i]))
            elif op == "rank":
                self._plan.append(lambda v, t, m: cs_rank(v))
            elif op == "neutralize":
                if universe is None:
                    raise ValueError(
                        f"neutralize: {arg} needs the universe view to fetch the grouping field, but the "
                        f"chain was given None. (§7.2 item 3: OpChain must receive the pool.)")
                self._plan.append(self._neutralize_step(str(arg)))
            elif op == "truncate":
                if not isinstance(arg, (int, float)) or isinstance(arg, bool):
                    raise ValueError(f"truncate needs a number, got {arg!r}")
                x = float(arg)
                if x <= 0.0:
                    raise ValueError(
                        f"truncate: {arg!r} -- a cap <= 0 would clamp the entire book to 0. "
                        f"You probably want a fraction such as 0.02.")
                self._plan.append(lambda v, t, m, x=x: cs_truncate(v, x))
            elif op == "scale":
                if arg not in (None, "book"):
                    raise ValueError(
                        f"scale: {arg!r} is unknown (available: book). Silently ignoring an enum value would "
                        f"change the convention without reporting it.")
                self._plan.append(self._scale_step)
            else:
                raise ValueError(f"unknown op {op} (available: {sorted(OPS)})")

    # ---- 编译期生成的步骤（闭包捕获参数，执行期零查表）
    def _ts_step(self, state: _TsOp):
        def step(v: pd.Series, t: int, m) -> pd.Series:
            x = state(np.asarray(v.to_numpy(), dtype=float))
            return pd.Series(x, index=v.index, name=v.name)
        return step

    def _neutralize_step(self, ref: str):
        def step(v: pd.Series, t: int, m) -> pd.Series:
            return cs_neutralize(v, self.universe.group(ref, t))
        return step

    def _scale_step(self, v: pd.Series, t: int, m) -> pd.Series:
        v = v if m is None else v.where(m)      # 闸门二先落地, 池外不进分母（见 cs_scale）
        g = cs_gross(v)
        if not np.isfinite(g) or g <= 0.0:      # 判一次退化, 归一让 cs_scale 去做
            # §4.10 例 7 的极端版：抵消到一分不剩。除法免了，但绝不能不出声——
            # 少投出去的那部分收益和风险同比例缩水，Sharpe 看着还挺正常。
            self.degenerate_scale.append(int(t))
            if len(self.degenerate_scale) == 1:
                warnings.warn(
                    f"scale: on t={t}, Sigma|w|=0 (everything cancelled or all NaN); normalisation skipped "
                    f"and the book is empty for the day. Later days of the same kind are only "
                    f"counted, not re-warned -- see OpChain.degenerate_scale.",
                    RuntimeWarning, stacklevel=3)
        return cs_scale(v, gross=g)             # mask 已落地, 不再重复夹

    # ---- 执行
    def __call__(self, v: pd.Series, t: int) -> pd.Series:
        if not isinstance(v, pd.Series):
            raise TypeError(
                f"OpChain takes a cross-section Series (one day of a rank-2 panel), got {type(v).__name__}. "
                f"For rank-1/rank-3 only TS ops are legal (§3.6); wiring those up is the runner's job.")
        self._advance(t)
        # astype 顺带复制：链内所有算子都可以就地写而不会写穿 ctx 交付的那份（§十）
        v = v.astype(float)
        m = _mask_array(self.universe, t, v.index)
        if m is not None:
            v = v.where(m)              # 闸门一：幂等的二次确认（§3.5）
        for step in self._plan:
            v = step(v, t, m)
        return v

    def _advance(self, t: int) -> None:
        """游标必须逐日 +1——TS 缓冲按调用次数推进，跳一天就是全链静默错位。"""
        t = int(t)
        if self._last_t is not None and t != self._last_t + 1:
            raise ValueError(
                f"OpChain cursor jumped: last t={self._last_t}, now t={t}. decay/delay buffers advance "
                f"per call, so skipping a day shifts every lag and decay weight without raising. "
                f"The warmup segment must be called through as well (§7.1); to re-run a "
                f"different period, call reset() first.")
        self._last_t = t

    def reset(self) -> None:
        """清 op-state，供预热后复用 / 换区间重跑。"""
        for s in self._state.values():
            s.reset()
        self._last_t = None
        self.degenerate_scale.clear()

    # ---- 供 runner 推导预热下限（§7.1：引擎取 max(config 声明值, 这个)）
    @property
    def lookback(self) -> int:
        """见模块级的 ops_lookback——链只是转发, 保证两处永不漂移。"""
        return ops_lookback(self.ops)

    def __repr__(self) -> str:
        body = ", ".join(op if arg is None else f"{op}:{arg}" for op, arg in self.ops)
        return f"OpChain([{body}], universe={'set' if self.universe else None})"
