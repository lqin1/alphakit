"""precise 仿真器：单一价值账本（architecture.md §8.1–§8.3 / §九 / 附录 B）。

pnl 不是"算指标的评估器"而是**仿真器**：维护逐票美元价值账本 `pos_value`，
按复权 ret 推进——拆股/分红/退市对价/复牌累计天然安全，corporate action
问题在此模型下不存在（§8.1）。指标只是账本的汇总视图（§8.4，见 metrics.py）。

§8.2 点名的三处"写错就静默算错"逐条钉在代码注释里：
  ① 推进用 `r.fillna(0)`、可交易性判据用**原始** `r`——两者必须分开；
  ② `prev` 在推进前捕获（`pnl_t = 昨仓 × 今收益`，§4.9.6）；
  ③ `frozen_value` 取 **gross**（`.abs().sum()`）而非带符号和。

**本实现对 §8.2 伪码有六处有意偏离**，每处都在落点处标了 `[偏离 n]`，理由见注释：
  1 `w` 必须先拷贝再改（同一份权重面板要被容量扫描重复喂进来）；
  2 归零掩码是 `~tradable` 而非 `frozen`（否则停牌的**空仓**票照样占 avail 的份额，
    却被 `delta[~tradable]=0` 挡住不成交，那部分资金静默不投——违反"可交易部分始终满仓"）；
  3 ADV 缺失时 cap 取 `+inf` 而非 NaN（`np.clip(x, nan, nan)` → NaN，
    与 §8.2 注 1 要防的 NaN 传染是同一种病，只是换了扇门进来）；
  4 退市当日把目标权重清零（§九 正文说"退市日**平仓**"，而伪码会先按目标买进
    再被末行删掉，凭空抬高 trade_dollar / 成本 / breakeven 闸门）；
  5 `cost[t]` 依赖当日 `delta`，故排在执行段之后算（口径 `cost = |delta|·bps·1e-4`）；
  6 账本以 float64 运行（f4 下 2000 万美元的仓位每天每票漂 ~$0.125），并另立
    真实现金腿 `cash_account`，使 NAV 恒等式无怪项可闭（见下）。

**会计恒等式（§十三.1）**。逐位形式含一个反直觉的项：成本记进了 pnl 却从未
从账本里扣，故必须加回来——
    `pos_t − pos_{t−1} = pnl_t + (delta_t + settle_t + cost_t)`，即 净流入 ≡ δ+结算+成本。
故本实现另记真实现金腿，NAV 形式无怪项：
    `Σpos_t + cash_account_t ≡ Σpos_{t−1} + cash_account_{t−1} + Σpnl_t`，
    `cash_account_t = cash_account_{t−1} − Σδ_t − Σcost_t − Σsettle_t`（初值 0，故 NAV = 累计损益）。
两式都在 test_simulate.py 里逐日断言。
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

BPS = 1e-4

# §8.3 钉死的 daily 列（顺序照抄文档），其后是审计所需的补充列——
# §8.4 的 ghost_days / frozen_value_avg / frozen_reprice_pnl 等必须从 daily 派生，
# 文档的列清单是下限而非上限。
DAILY_COLS = [
    "long_value", "short_value", "long_count", "short_count",
    "trade_dollar", "holding_pnl", "trading_pnl", "cost", "cash",
    "pnl", "return", "alpha_turnover", "realloc_turnover",
    "gap_participation", "gap_realloc", "gap_reprice",
    # ---- 审计补充列（§8.4 的 audit 指标与 §15.9 的闸门都要从这里派生）----
    "cash_account", "nav",
    "frozen_value", "frozen_count", "avail", "frozen_reprice_pnl",
    "ghost_count", "ghost_value", "delist_close_value",
    "market_ret", "weight_gross", "target_count", "oop_weight",
]


class SimError(RuntimeError):
    pass


# ---------------------------------------------------------------------- 结果
@dataclass
class SimResult:
    """§8.3 四交付物中的前三件 + 审计字典；metrics.json 由 metrics.py 从此派生。

    `holding_value` 是账本本体，`holding_weight = value / booksize` 只是它的
    定标视图（与目标权重同尺度、可逐股对比），故按 property 现算而不落第二份
    (T,N) 数组——5000×6000 一份就是 240 MB。
    """

    holding_value: pd.DataFrame        # date × security_id，美元价值
    pnl: pd.DataFrame                  # date × security_id，逐股逐日损益
    daily: pd.DataFrame                # 日度汇总，列见 DAILY_COLS
    audit: dict = field(default_factory=dict)
    flows: dict | None = None          # keep_flows=True 时的逐股现金流（会计恒等式用）

    @property
    def booksize(self) -> float:
        return float(self.audit["booksize"])

    @property
    def holding_weight(self) -> pd.DataFrame:
        return self.holding_value / self.booksize

    @property
    def holding(self) -> pd.DataFrame:
        """§8.3 的 holding.feather：value 与 weight 两块拼成 MultiIndex 列。"""
        return pd.concat({"holding_value": self.holding_value,
                          "holding_weight": self.holding_weight}, axis=1)

    def write(self, outdir) -> dict:
        """落 §8.3 四交付物之三（metrics.json 归 metrics.py）。

        feather 要求列名是字符串，故 MultiIndex 列拍平成 `{块}|{security_id}`。
        """
        from pathlib import Path
        out = Path(outdir)
        out.mkdir(parents=True, exist_ok=True)
        h = self.holding
        h.columns = [f"{a}|{b}" for a, b in h.columns]
        paths = {}
        for name, df in (("holding", h), ("pnl", self.pnl), ("daily", self.daily)):
            d = df.copy()
            d.columns = [str(c) for c in d.columns]
            d.reset_index(names="date").to_feather(out / f"{name}.feather")
            paths[name] = str(out / f"{name}.feather")
        return paths


# ------------------------------------------------------------------ 输入对齐
def _panel(x, index, columns, what: str, default: float) -> np.ndarray:
    """标量 / DataFrame → 对齐好的 (T,N) f8。缺 session 直接报错而不是补 NaN：
    reindex 出来的 NaN 会被后续 fillna 吞掉，变成"那几天没有成本/没有 ADV"的静默口径。"""
    if x is None:
        return np.full((len(index), len(columns)), default, dtype=np.float64)
    if np.isscalar(x):
        return np.full((len(index), len(columns)), float(x), dtype=np.float64)
    if not isinstance(x, pd.DataFrame):
        raise SimError(f"{what} 必须是标量或 DataFrame，收到 {type(x).__name__}")
    miss = [d for d in index if d not in x.index]
    if miss:
        raise SimError(f"{what} 缺 {len(miss)} 个 weights 里有的 session（首个 {miss[0]}）")
    return np.asarray(x.reindex(index=index, columns=columns), dtype=np.float64)


def _ordinals(index) -> np.ndarray:
    """日期轴 → int64 序数。store 的轴是 ISO 字符串、delist_date 也是——统一转
    datetime 再比较，避免字符串与 Timestamp 混比时静默得到 False。"""
    return pd.to_datetime(pd.Index(index)).to_numpy("datetime64[ns]").astype("int64")


def _halt_by_proxy(nan_eff: np.ndarray, k: int) -> np.ndarray:
    """`--halt-proxy consecutive:K`：整段连续 NaN 长度 ≥ K 即视作停牌（§九）。

    两遍扫：前向拿"截至今日的连续长度"，后向把"本段里出现过 ≥K"回灌整段。
    **这是降级口径且带前视**——今天无从知道这段 NaN 会不会长到 K。文档明说它是
    降级（"会漏掉真正的一日停牌"），故 ghost_detection 必须把 K 打在报表上。
    """
    T, N = nan_eff.shape
    mark = np.zeros((T, N), dtype=bool)
    run = np.zeros(N, dtype=np.int32)
    for t in range(T):                      # 前向：run = 截至 t 的连续 NaN 长度
        run = np.where(nan_eff[t], run + 1, 0)
        mark[t] = run >= k
    carry = np.zeros(N, dtype=bool)
    for t in range(T - 1, -1, -1):          # 后向：段内任一处达标 → 整段是停牌
        carry = nan_eff[t] & (mark[t] | carry)
        mark[t] = carry
    return mark


# ---------------------------------------------------------------------- 内核
def simulate(weights: pd.DataFrame,
             ret: pd.DataFrame,
             *,
             booksize: float,
             adv_dollar: pd.DataFrame | None = None,
             cost_bps: pd.DataFrame | float = 0.0,
             delist_date: pd.Series | None = None,
             is_halted: pd.DataFrame | None = None,
             participation: float = 0.10,
             halt_proxy: int | None = None,
             universe: pd.DataFrame | None = None,
             ghost_tolerance: float = 0.005,
             keep_flows: bool = False) -> SimResult:
    """precise 仿真（§8.2）。weights 是**已 scale 的目标权重**（Σ|w| = 1）。

    参数
    ----
    weights       date × security_id，纯意图、不做停牌处理（§九 总原则）。
    ret           return_metric，同轴；第 t 行 = 昨执行价 → 今执行价（§4.9.6）；
                  停牌日 NaN（附录 B），退市末日 = 最终对价收益。**不在此处 shift**。
    booksize      恒定账本规模，也是 `return` 列的分母（§8.3）；账本不复利。
    adv_dollar    日均成交额；cap = participation × adv。None 或 NaN = 该处不设约束。
    cost_bps      单边成本，单位 bps，标量或面板：`cost = |delta| × bps × 1e-4`。
    delist_date   security_id → ISO date / NaT。`date > delist_date` 才算已退市，
                  故退市**当日仍可交易**——当日目标清零、当日平仓、资金即刻回收。
    is_halted     停牌的**正向判据**（§九）。缺失时必须显式降级或拒绝运行。
    halt_proxy    K ≥ 2：连续 ≥ K 个 session 的 NaN 视作停牌（降级口径）；
                  0 = 显式关闭 ghost 检测；None 且无 is_halted = **拒绝运行**。
    universe      可选 bool 面板，只喂闸门六（scale 后池外权重必须恰为 0）。
    keep_flows    额外留下逐股 delta / settle / cost 三张表，供会计恒等式逐位对账。
    """
    # ---- 0. 轴与形状 -----------------------------------------------------
    if not isinstance(weights, pd.DataFrame) or not isinstance(ret, pd.DataFrame):
        raise SimError("weights 与 ret 必须是 date × security_id 的 DataFrame")
    if weights.empty:
        raise SimError("weights 为空——没有可仿真的区间")
    if not weights.index.is_monotonic_increasing or weights.index.has_duplicates:
        raise SimError("weights 的日期轴必须严格递增且无重复")
    booksize = float(booksize)
    if not np.isfinite(booksize) or booksize <= 0:
        raise SimError(f"booksize 必须是正有限数，收到 {booksize!r}")
    if not (0 < participation <= 1):
        raise SimError(f"participation 应在 (0, 1]，收到 {participation!r}")

    index, cols = weights.index, weights.columns
    T, N = len(index), len(cols)

    # [偏离 6] 账本一律 float64：L3 面板是 f4，2000 万美元的仓位在 f32 下每票每天
    # 漂 ~$0.125，累计 5000 天足以把会计恒等式和 Margin(bps) 淹掉。
    # copy=True 是 [偏离 1] 的第一道保险：weights 本就是 f8 时 asarray 会给出**视图**，
    # 之后任何就地写都会穿透到调用方的面板上。
    W = np.array(weights, dtype=np.float64, copy=True)
    _wnan = np.isnan(W)
    w_nan = int(_wnan.sum())
    W[_wnan] = 0.0                                 # 附录 B：scale 之后的 NaN 即权重 0
    del _wnan

    R = _panel(ret, index, cols, "ret", np.nan)
    nan_ret = np.isnan(R)
    # 有权重却整列无收益 = 口径错配（多半是列轴/区间没对上）。不拦的话这些票永远
    # 不可交易、账本静默投不满，而 Sharpe 看着完全正常。
    dead = ((np.abs(W) > 0).any(axis=0) & nan_ret.all(axis=0)).nonzero()[0]
    if dead.size:
        raise SimError(
            f"{dead.size} 只有非零权重的标的在 ret 面板里整列 NaN"
            f"（如 security_id={cols[dead[0]]}）——多半是列轴/区间没对上。")

    ADV = None if adv_dollar is None else _panel(adv_dollar, index, cols, "adv_dollar", np.nan)
    # 标量成本不铺成 (T,N)：5000×6000 铺出来就是 240 MB 的常数
    cb_flat = float(cost_bps) if np.isscalar(cost_bps) else None
    CB = None if cb_flat is not None else _panel(cost_bps, index, cols, "cost_bps", 0.0)
    if (cb_flat is not None and not np.isfinite(cb_flat)) or (CB is not None and np.isnan(CB).any()):
        raise SimError("cost_bps 含 NaN——成本缺失须显式补 0 或补模型值，不由仿真器猜")
    UNI = None if universe is None else _panel(universe, index, cols, "universe", 0.0) > 0

    # ---- 1. 退市轴：缺 field 时整条退市路径是死代码，必须可见（§九） --------
    date_ord = _ordinals(index)
    if delist_date is None:
        dl_ord = np.full(N, np.iinfo(np.int64).max, dtype=np.int64)
        delist_source = "none"
        warnings.warn(
            "无 delist_date：`delisted` 恒为 False，退市路径是死代码、delist_events 恒为 0。"
            "真实退市会退化成一只永久冻结的票，把 frozen_value 越棚越高地漏掉资金——"
            "本次结果按'区间内无退市'解读，metrics 记 delist_source=none。")
    else:
        d = pd.to_datetime(pd.Series(delist_date).reindex(cols))
        raw = d.to_numpy("datetime64[ns]").astype("int64")     # NaT → int64 最小值
        dl_ord = np.where(d.isna().to_numpy(), np.iinfo(np.int64).max, raw).astype(np.int64)
        delist_source = "field"

    # ---- 2. 停牌判据：三分类必须有正向信号（§九） -------------------------
    if is_halted is not None:
        if halt_proxy is not None:
            warnings.warn("is_halted 与 halt_proxy 同时给出——以 is_halted 为准（正向判据优先）")
        HALT = _panel(is_halted, index, cols, "is_halted", 0.0) > 0
        detection = "field"
    elif halt_proxy is None:
        raise SimError(
            "拒绝运行：既没有 is_halted 面板，也没有 halt_proxy（§九）。\n"
            "  NaN 三分类需要一个**正向**的停牌信号；用'非退市即停牌'兜底会让\n"
            "  幽灵持仓那一类恒为空、ghost_days 恒为 0——一道永远不会触发的告警\n"
            "  比没有更危险，它给出的是虚假的安全感。\n"
            "  三选一：① 传 is_halted；② 传 halt_proxy=K（K≥2，降级口径，会漏掉\n"
            "  真正的一日停牌）；③ 传 halt_proxy=0 显式关闭（metrics 记 disabled）。")
    elif int(halt_proxy) == 0:
        # 显式关闭：不做三分类。此时**不许**把 NaN 记成幽灵（那是"检测"的产物），
        # 一律按停牌冻结——即 §九 之前那套口径。代价写在 ghost_detection=disabled 上。
        HALT = np.zeros((T, N), dtype=bool)
        detection = "disabled"
    else:
        k = int(halt_proxy)
        if k < 2:
            raise SimError(
                f"halt_proxy={k} 不合法。K=1 等于'任何 NaN 都算停牌'，第三类恒空、"
                f"ghost 检测永不触发——正是 §九 要堵的那个失效模式。K 请取 ≥2；"
                f"确要关闭检测请显式传 halt_proxy=0。")
        # 退市后的永久 NaN 不参与连续段计数，否则"停牌接退市"会被当成一段超长停牌
        nan_eff = nan_ret & (date_ord[:, None] <= dl_ord[None, :])
        HALT = _halt_by_proxy(nan_eff, k)
        detection = f"proxy({k})"

    # ---- 3. 逐日推进 -----------------------------------------------------
    HV = np.zeros((T, N), dtype=np.float64)        # holding_value
    PL = np.zeros((T, N), dtype=np.float64)        # 逐股逐日损益
    FL = {k: np.zeros((T, N)) for k in ("trade", "settle", "cost")} if keep_flows else None
    pos = np.zeros(N, dtype=np.float64)
    frozen_prev = np.zeros(N, dtype=bool)
    delist_seen = np.zeros(N, dtype=bool)          # 退市只算一次事件，不按天重复计
    cash_account = 0.0                             # 真实现金腿，初值 0 → NAV = 累计损益
    rows: list[dict] = []
    ghost_cells = ghost_days = held_cells = delist_events = adv_free_cells = 0
    ghost_examples: list[tuple] = []

    ghost_on = detection != "disabled"
    for t in range(T):
        r_nan = nan_ret[t]
        r = np.where(r_nan, 0.0, R[t])             # ① 推进用 fillna(0)……
        prev = pos                                 # ② prev 在推进前捕获（pos 只重绑不原地改）
        held = prev != 0
        held_cells += int(held.sum())

        hp = prev * r                              # holding_pnl = 昨仓 × 今收益（§4.9.6）
        pos = prev + hp                            # ≡ prev*(1+r)；加法写法让恒等式共享中间量

        delisted = date_ord[t] > dl_ord            # ……① 判据用**原始** NaN；严格大于 → 退市当日仍可交易
        due = date_ord[t] >= dl_ord                # 退市当日（含日历外落点的首个 session）
        tradable = (~r_nan) & (~delisted)
        # NaN 三分类（§九 / 附录 B）：退市后 → 停牌 → 皆否即幽灵持仓
        ghost = (r_nan & (~delisted) & (~HALT[t]) & held) if ghost_on else np.zeros(N, bool)
        frozen = (~tradable) & (pos != 0) & (~ghost) & (~due)   # disabled 时 NaN 一律落这里

        # ③ gross 口径。写成 pos[frozen].sum() 的话，多空两侧都有停牌票时带符号和
        #   ≈ 0 → avail ≈ booksize → "停牌 = 资金占用"被静默抹掉。
        frozen_value = float(np.abs(pos[frozen]).sum())
        avail = max(booksize - frozen_value, 0.0)

        w = W[t].copy()                            # [偏离 1] 必须拷贝：同一份权重面板会被
        w[~tradable] = 0.0                         #   容量扫描重复喂进来。[偏离 2] 掩码取 ~tradable
        w[due] = 0.0                               # [偏离 4] 退市当日目标清零 → 当日卖出而非买进
        gross = float(np.abs(w).sum())
        w = w / gross if gross > 0 else w          # 全员冻结时不做无意义的归一（不除零）
        target_value = w * avail

        # 无冻结时的"纯意图"目标：只用来把换手与 gap 拆成 alpha / realloc 两块。
        # 与 target_value 同一套表达式，故无冻结时两者逐位相等 → realloc 恒为 0。
        gross_free = float(np.abs(W[t]).sum())
        target_free = (W[t] / gross_free * booksize) if gross_free > 0 else np.zeros(N)

        pos_adv = pos
        wanted = target_value - pos_adv
        wanted[~tradable] = 0.0                    # 停牌：价值原地推进（= 股数不变）
        if ADV is None:
            delta = wanted.copy()
        else:
            # [偏离 3] ADV 缺失 → cap = +inf。np.clip(x, nan, nan) 得 NaN，会把持仓
            #   变成 NaN 并向后传染——与 §8.2 注 1 要防的是同一种病。
            cap = np.maximum(participation * ADV[t], 0.0)   # 负 ADV 是脏数据；
            bad = np.isnan(cap)                              # 若直接 clip(-cap, cap) 会上下界颠倒
            if bad.any():
                adv_free_cells += int((bad & (wanted != 0)).sum())
                cap = np.where(bad, np.inf, cap)
            delta = np.clip(wanted, -cap, cap)     # 顺序：先重分配、后 clip（§九 细节 3）
        pos = pos + delta                          # 滞留缺口每日重试

        # [偏离 5] 成本依赖当日 delta，故排在执行段之后；口径 cost = |delta|·bps·1e-4
        cost = np.abs(delta) * (cb_flat if CB is None else CB[t]) * BPS
        PL[t] = hp - cost                          # trading_pnl 在 v0 恒为 0

        # 结算流：退市对价已由 ret 兑现，平仓只是把资金收回；ghost 按最后可得价平掉。
        settle_mask = (due & (pos != 0)) | ghost
        settle = np.where(settle_mask, -pos, 0.0)
        pos = pos + settle
        HV[t] = pos
        if FL is not None:
            FL["trade"][t], FL["settle"][t], FL["cost"][t] = delta, settle, cost

        n_ghost = int(ghost.sum())
        if n_ghost:
            ghost_cells += n_ghost
            ghost_days += 1
            for j in np.flatnonzero(ghost)[:max(0, 10 - len(ghost_examples))]:
                ghost_examples.append((str(index[t]), cols[j], float(prev[j])))
        # 退市事件按"持仓遇到退市"计数，而不是按 settle≠0 计——[偏离 4] 之后当日
        # 目标已清零，仓位多半是被 delta 正常卖掉的，settle 只兜住被 cap 卡住的尾巴。
        hit_delist = due & (~delist_seen) & held
        delist_events += int(hit_delist.sum())
        delist_closed = float(np.abs(pos_adv[hit_delist]).sum())
        delist_seen |= due

        # ---- 日度汇总 ----
        d_alpha = target_free - pos_adv            # 无冻结时这就是全部想交易的量
        d_realloc = target_value - target_free     # 冻结重分配把瞄准点挪走的部分
        # 两者在可交易票上恰好相加等于 wanted（= target_value − pos_adv）。
        # 把**实际成交额**按这两个成因的绝对大小成比例分摊，使
        # alpha_turnover + realloc_turnover ≡ trade_dollar/booksize 恒成立（无冻结时
        # d_realloc 逐位为 0 → 全部归 alpha）。直接取 |d_alpha|、|d_realloc| 的话，
        # 两者反号时和会大于真实换手，"换手拆不平"就成了报表上的常态噪声。
        den = np.abs(d_alpha) + np.abs(d_realloc)
        share_a = np.divide(np.abs(d_alpha), den, out=np.ones(N), where=den > 0)
        traded = np.abs(delta) * tradable
        cash_account += -float(delta.sum()) - float(cost.sum()) - float(settle.sum())
        n_live = int(N - r_nan.sum())
        rows.append({
            "long_value": float(pos[pos > 0].sum()),
            "short_value": float(pos[pos < 0].sum()),            # 负部，带符号
            "long_count": int((pos > 0).sum()),
            "short_count": int((pos < 0).sum()),
            "trade_dollar": float(np.abs(delta).sum()),          # 结算流不算成交额
            "holding_pnl": float(hp.sum()),
            "trading_pnl": 0.0,                                  # v0 留列
            "cost": float(cost.sum()),
            # §8.3 的 cash：账本里没投出去的部分（"仅参与率滞留产生，偏大即容量警报"）
            "cash": float(booksize - np.abs(pos).sum()),
            "pnl": float(PL[t].sum()),
            "return": float(PL[t].sum()) / booksize,             # 分母恒定（§8.3）
            "alpha_turnover": float((traded * share_a).sum()) / booksize,
            "realloc_turnover": float((traded * (1.0 - share_a)).sum()) / booksize,
            "gap_participation": float(np.abs(wanted - delta)[tradable].sum()),
            "gap_realloc": float(np.abs(d_realloc)[tradable].sum()),
            "gap_reprice": float(np.abs(target_free - pos)[~tradable].sum()),
            # 真实现金腿与 NAV：与上面的 cash 是两回事，见模块 docstring 的恒等式
            "cash_account": cash_account,
            "nav": float(pos.sum()) + cash_account,
            "frozen_value": frozen_value,
            "frozen_count": int(frozen.sum()),
            "avail": avail,
            "frozen_reprice_pnl": float(hp[frozen_prev & tradable].sum()),
            "ghost_count": n_ghost,
            "ghost_value": float(np.abs(prev[ghost]).sum()),
            "delist_close_value": delist_closed,   # 当日因退市而必须清掉的仓位价值
            "market_ret": (float(r.sum() / n_live) if n_live else np.nan),
            "weight_gross": gross_free,
            "target_count": int((W[t] != 0).sum()),
            "oop_weight": (float(np.abs(W[t])[~UNI[t]].sum()) if UNI is not None else np.nan),
        })
        frozen_prev = frozen

    # ---- 4. 幽灵持仓的处置（§九：少量 warning + 当日平仓，超阈值报错） ----
    ghost_rate = ghost_cells / held_cells if held_cells else 0.0
    if detection != "disabled" and ghost_rate > ghost_tolerance:
        raise SimError(
            f"幽灵持仓超阈值：{ghost_cells} 个持仓日的 ret=NaN 归不进退市/停牌任一"
            f"已知原因（占持仓日 {ghost_rate:.2%} > {ghost_tolerance:.2%}）。\n"
            f"  头几例 (date, security_id, 昨仓): {ghost_examples[:5]}\n"
            f"  skipna 会让它们无声蒸发——零成本退出，是生存者偏差的后门。\n"
            f"  先查 ret 面板与 delist_date / is_halted 的对齐，别调阈值。")
    if ghost_cells:
        warnings.warn(
            f"幽灵持仓 {ghost_cells} 个持仓日（{ghost_days} 个 session，占持仓日 "
            f"{ghost_rate:.3%}），已按最后可得价当日平仓。检测口径 {detection}。")

    daily = pd.DataFrame(rows, index=index)
    assert list(daily.columns) == DAILY_COLS, "daily 列清单与 §8.3 不符"

    audit = {
        "booksize": booksize,
        "participation": participation,
        "n_sessions": T,
        "n_securities": N,
        "sd": str(index[0]),
        "ed": str(index[-1]),
        # 这两个 source 字段恒在，且恒打印：报表上必须能区分"没查到"与"根本没查"（§九）
        "ghost_detection": detection,            # field / proxy(K) / disabled
        "ghost_detection_lookahead": detection.startswith("proxy"),
        "delist_source": delist_source,          # field / none
        "ghost_days": ghost_days,                # 有幽灵持仓的 session 数
        "ghost_cells": ghost_cells,              # 幽灵持仓日（票×日）
        "ghost_rate": ghost_rate,
        "ghost_examples": ghost_examples[:10],
        "ghost_tolerance": ghost_tolerance,
        "delist_events": delist_events,
        "halt_cells": int(HALT.sum()),
        "weight_nan_cells": w_nan,
        "adv_uncapped_cells": adv_free_cells,    # ADV 缺失 → 该处未设参与率约束
        "adv_constrained": ADV is not None,
        "universe_supplied": UNI is not None,
        "cost_model": "flat" if CB is None else "panel",
        "cost_bps_avg": cb_flat if CB is None else float(CB.mean()),
    }
    flows = None if FL is None else {
        k: pd.DataFrame(v, index=index, columns=cols, copy=False) for k, v in FL.items()}
    return SimResult(holding_value=pd.DataFrame(HV, index=index, columns=cols, copy=False),
                     pnl=pd.DataFrame(PL, index=index, columns=cols, copy=False),
                     daily=daily, audit=audit, flows=flows)
