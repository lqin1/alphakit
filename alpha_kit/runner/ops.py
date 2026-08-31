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

# exp_decay 的预热下限取几个半衰期（§7.1：引擎取 max(config 声明值, 这个下限)）。
# 递推形式自带按有效权重归一，第一天就是一个合法的加权平均，故这里要的不是「算得出」
# 而是「与更长的历史算得一样」——4 个半衰期覆盖 93.75% 的稳态权重。
EXP_DECAY_WARMUP_HALFLIVES = 4


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
                f"{type(self).__name__}: 列轴宽度从 {self._width} 变成 {x.size}——"
                f"TS 缓冲按位置对齐，宽度一变就是按位置错配。ctx 应交付对齐到全局"
                f"列轴的截面（§十）；确实换了区间请先 reset()。")

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
            raise ValueError(f"linear_decay 需要正整数窗口，收到 {n!r}")
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
            raise ValueError(f"exp_decay 需要正的半衰期，收到 {h!r}")
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
            raise ValueError(f"delay 需要正整数滞后，收到 {k!r}")
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
_CS_OPS = ("rank", "neutralize", "truncate", "scale")
OPS = tuple(_CS_OPS) + tuple(_TS_FACTORY)        # 与 config.OP_TYPES 的键必须一致


# --------------------------------------------------------------------- 链
def ops_lookback(ops: list[tuple[str, object]]) -> int:
    """ops 可推导的预热下限（§7.1）。

    **TS 算子串联时窗口相加**：delay 2 接 linear_decay 5 要 2 + 4 = 6 天先前历史,
    因为 n 日窗口只需 n-1 天**先前**数据（当日自己算第 n 天）。
    做成模块级函数而非只挂在链上：runner 要在建链**之前**就知道该预热多久,
    而建链需要池子（neutralize 要取分组字段）——先有鸡还是先有蛋。
    两处各写一份的代价已经付过一次：曾经 runner 那份对 `delay:2 → decay:5` 给 5,
    预热不足会让最初几天的输出来自未填满的缓冲, 数值看着合理却是错的。
    """
    need = 0
    for op, arg in ops:
        if op == "linear_decay":
            need += int(arg) - 1
        elif op == "delay":
            need += int(arg)
        elif op == "exp_decay":
            need += EXP_DECAY_WARMUP_HALFLIVES * int(arg)
    return need


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
                        f"neutralize: {arg} 需要 universe 视图来取分组字段，但链拿到的是 None。"
                        f"（§7.2 第 3 条：OpChain 必须拿到池子。）")
                self._plan.append(self._neutralize_step(str(arg)))
            elif op == "truncate":
                if not isinstance(arg, (int, float)) or isinstance(arg, bool):
                    raise ValueError(f"truncate 需要一个数，收到 {arg!r}")
                x = float(arg)
                if x <= 0.0:
                    raise ValueError(
                        f"truncate: {arg!r} ——上限 ≤ 0 会把整本账夹成 0。"
                        f"要的多半是 0.02 这样的比例。")
                self._plan.append(lambda v, t, m, x=x: cs_truncate(v, x))
            elif op == "scale":
                if arg not in (None, "book"):
                    raise ValueError(
                        f"scale: {arg!r} 未知（可用：book）。静默忽略枚举值等于换了口径不报错。")
                self._plan.append(self._scale_step)
            else:
                raise ValueError(f"未知算子 {op}（可用：{sorted(OPS)}）")

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
                    f"scale: t={t} 日 Σ|w|=0（全抵消或全 NaN），跳过归一、当日空仓。"
                    f"后续同类日只累计不再重复告警，见 OpChain.degenerate_scale。",
                    RuntimeWarning, stacklevel=3)
        return cs_scale(v, gross=g)             # mask 已落地, 不再重复夹

    # ---- 执行
    def __call__(self, v: pd.Series, t: int) -> pd.Series:
        if not isinstance(v, pd.Series):
            raise TypeError(
                f"OpChain 收截面 Series（秩-2 的一天），收到 {type(v).__name__}。"
                f"秩-1/秩-3 只有 TS 算子合法（§3.6），其接入由 runner 负责。")
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
                f"OpChain 游标跳变：上次 t={self._last_t}，这次 t={t}。"
                f"decay/delay 的缓冲按调用次数推进，跳日会让滞后与衰减权重整体错位而"
                f"不报错。预热段也要照常调用（§7.1）；换区间重跑请先 reset()。")
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
