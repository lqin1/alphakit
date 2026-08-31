#!/usr/bin/env python
"""pnl 仿真器验收（architecture.md §十三.1 + §8.2 三处易错点 + §九 三分类）。

普通脚本跑：`.venv/bin/python tests/test_simulate.py`，任一条红了非零退出。

系统可信度不靠 review 靠断言——corporate action 的正确性由**会计恒等式**保证，
而不是人眼看那几行推进式（§十三）。
"""
from __future__ import annotations

import sys
import time
import traceback
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # tests/ 的上一层就是仓库根
from alpha_kit.pnl.metrics import format_report, metrics          # noqa: E402
from alpha_kit.pnl.simulate import SimError, simulate             # noqa: E402

FAILURES: list[str] = []
LOG: list[str] = []


def say(msg=""):
    print(msg)
    LOG.append(str(msg))


def check(cond, what, detail=""):
    tag = "ok  " if cond else "FAIL"
    say(f"    [{tag}] {what}{('  — ' + detail) if detail else ''}")
    if not cond:
        FAILURES.append(what)
    return bool(cond)


def close(a, b, tol, what, scale=""):
    d = float(np.max(np.abs(np.asarray(a) - np.asarray(b))))
    return check(d <= tol, what, f"max|diff| = {d:.3e} ≤ {tol:.1e}{scale}")


def panel(rows, dates, cols):
    return pd.DataFrame(np.asarray(rows, dtype=float), index=dates, columns=cols)


def sessions(n, start="2024-01-02"):
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, periods=n)]


# =====================================================================
# 1. 会计恒等式（§十三.1）
# =====================================================================
def test_identity():
    """`pos_t ≡ pos_{t−1} + pnl_t + 净流入`，逐日逐位，且带成本、停牌、退市、cap。

    净流入 = delta + settle + cost：
      cost 必须**加回来**，因为它记进了 pnl 却从未从价值账本里扣（它是现金腿的事）。
      这个怪项正是把 §8.3 的 cash 只当"未投出去的额度"、另立真实现金腿的理由——
      NAV 形式 `Σpos + cash_account` 才是无怪项的那一个。
    """
    rng = np.random.default_rng(7)
    T, N, B = 120, 40, 20e6
    dates, cols = sessions(T), list(range(1, N + 1))
    sig = rng.standard_normal((T, N))
    W = sig / np.abs(sig).sum(axis=1, keepdims=True)          # Σ|w| = 1
    R = rng.standard_normal((T, N)) * 0.02
    R[30:33, 5] = np.nan                                      # 停牌 3 天
    R[40:44, 6] = np.nan                                      # 停牌 4 天（多空另一侧）
    R[70, 9] = np.nan                                         # 一日 NaN → 幽灵持仓
    R[81:, 11] = np.nan                                       # 退市之后
    R[80, 11] = -0.30                                         # 退市末日 = 最终对价
    delist = pd.Series({c: (dates[80] if c == 12 else pd.NaT) for c in cols})
    adv = pd.DataFrame(rng.uniform(2e6, 3e7, (T, N)), index=dates, columns=cols)
    adv.iloc[:5] = np.nan                                     # ADV 预热期缺失
    res = simulate(panel(W, dates, cols), panel(R, dates, cols), booksize=B,
                   adv_dollar=adv, cost_bps=3.0, delist_date=delist,
                   halt_proxy=2, participation=0.10, keep_flows=True)

    hv, pl = res.holding_value.to_numpy(), res.pnl.to_numpy()
    fl = res.flows
    inflow = fl["trade"].to_numpy() + fl["settle"].to_numpy() + fl["cost"].to_numpy()
    prev = np.vstack([np.zeros((1, N)), hv[:-1]])
    resid = hv - (prev + pl + inflow)
    tol = 1e-9 * B                                            # f64 相对精度 ~2e-16，账本 2e7 美元
    close(resid, 0.0, tol, "逐股逐日恒等式 pos_t = pos_{t-1} + pnl_t + (δ+settle+cost)",
          f"（tol = 1e-9 × booksize = ${tol:.3g}）")

    d = res.daily
    nav = d["nav"].to_numpy()
    nav_prev = np.concatenate([[0.0], nav[:-1]])
    close(nav - (nav_prev + d["pnl"].to_numpy()), 0.0, 1e-6,
          "NAV 恒等式 Σpos_t + cash_t = Σpos_{t-1} + cash_{t-1} + Σpnl_t")
    close(nav[-1], d["pnl"].sum(), 1e-6, "NAV 终值 = 累计损益（现金腿初值 0）")
    close(d["pnl"].to_numpy(), (d["holding_pnl"] - d["cost"]).to_numpy(), 1e-9,
          "daily.pnl = holding_pnl + trading_pnl − cost")
    close(pl.sum(axis=1), d["pnl"].to_numpy(), 1e-8, "逐股 pnl 横向求和 = daily.pnl")
    check(np.isfinite(hv).all() and np.isfinite(pl).all(), "全程无 NaN/Inf 持仓与损益")

    # 无成本时同一条恒等式退化为逐位精确（唯一的舍入来源就是 ±cost 的分组）
    res0 = simulate(panel(W, dates, cols), panel(R, dates, cols), booksize=B,
                    adv_dollar=adv, cost_bps=0.0, delist_date=delist, halt_proxy=2,
                    keep_flows=True)
    hv0, pl0 = res0.holding_value.to_numpy(), res0.pnl.to_numpy()
    fl0 = res0.flows["trade"].to_numpy() + res0.flows["settle"].to_numpy()
    prev0 = np.vstack([np.zeros((1, N)), hv0[:-1]])
    r0 = float(np.max(np.abs(hv0 - (prev0 + pl0 + fl0))))
    check(r0 == 0.0, "cost≡0 时恒等式**逐位精确**（残差恰为 0.0）", f"residual = {r0!r}")
    say(f"    ghost_days={res.audit['ghost_days']} ghost_cells={res.audit['ghost_cells']} "
        f"delist_events={res.audit['delist_events']} halt_cells={res.audit['halt_cells']}")


# =====================================================================
# 2. 公司行动：拆股 / 分红
# =====================================================================
def _hold_one(px_raw, tot_ret, **kw):
    """单票满仓账本：权重恒为 1，故无漂移时目标 = 现仓、当日不成交，
    可以干净地观察'价值账本 × 复权 ret'这一条推进式本身。"""
    T = len(tot_ret)
    dates, cols = sessions(T), [1]
    res = simulate(panel(np.ones((T, 1)), dates, cols),
                   panel(np.asarray(tot_ret).reshape(T, 1), dates, cols),
                   booksize=1e6, halt_proxy=2, **kw)
    v = res.holding_value.iloc[:, 0].to_numpy()
    return res, v, v / np.asarray(px_raw)          # 隐含股数 = 价值 / 原始价


def test_split():
    """2:1 拆股：复权 ret 当日为 0 → 价值不动、隐含股数翻倍。账本无需 split_factor。"""
    px = [100.0, 100.0, 50.0]                      # 第 3 日 1 股拆 2 股
    tot = [0.0, 0.0, (50.0 * 2) / 100.0 - 1.0]     # 复权总收益：拆股不是收益
    res, v, sh = _hold_one(px, tot)
    check(abs(v[2] - v[1]) < 1e-9, "拆股日持仓价值连续", f"{v[1]:,.2f} → {v[2]:,.2f}")
    check(abs(sh[2] / sh[1] - 2.0) < 1e-12, "隐含股数恰好翻倍",
          f"{sh[1]:,.2f} → {sh[2]:,.2f} 股")
    check(abs(res.daily['pnl'].iloc[2]) < 1e-9, "拆股日 pnl = 0（不是 −50%）",
          f"pnl = {res.daily['pnl'].iloc[2]:.6f}")
    # 反面：若误喂原始价收益（−50%），账本当场亏掉一半——split_factor 账本的经典事故。
    # 比的是 pnl 而不是收盘持仓：账本每天重新瞄准 booksize，收盘价值总会被买回来。
    bad, _, _ = _hold_one(px, [0.0, 0.0, 50.0 / 100.0 - 1.0])
    check(abs(bad.daily["pnl"].iloc[2] + 500_000) < 1e-6, "对照：喂原始价收益则当日亏 50%",
          f"pnl = {bad.daily['pnl'].iloc[2]:,.0f}")


def test_dividend():
    """现金分红：复权 ret 含 div/px_prev → 分红直接落进 pnl，无需现金账户改写。"""
    px, div = [100.0, 100.0, 100.0], 1.0
    tot = [0.0, 0.0, (100.0 - 100.0 + div) / 100.0]        # 除息日价格未动 + 1 美元分红
    res, v, sh = _hold_one(px, tot)
    pnl = res.daily["pnl"].iloc[2]
    check(abs(pnl - sh[1] * div) < 1e-6, "分红日 pnl = 昨日股数 × 每股分红",
          f"pnl = {pnl:,.2f}  股数 {sh[1]:,.2f} × ${div}")
    # 分红先按总收益推进进价值账本（= 再投资），当日再平衡时被原样卖回目标——
    # 故当日 delta 恰等于 −分红额，账本无需任何现金账户改写。
    res2, _, _ = _hold_one(px, tot, keep_flows=True)
    trim = res2.flows["trade"].iloc[2, 0]
    check(abs(trim + pnl) < 1e-6, "分红先入价值账本、当日再平衡按 −分红额修剪回目标",
          f"delta = {trim:,.2f} vs pnl = {pnl:,.2f}")
    check(abs(v[2] - 1e6) < 1e-6, "收盘回到满仓 1M（每日重新瞄准 booksize）",
          f"{v[1]:,.2f} → {v[2]:,.2f}")


# =====================================================================
# 3. 停牌：冻结、不 NaN、跳空落在复牌日
# =====================================================================
def test_halt():
    T, B = 8, 20e6
    dates, cols = sessions(T), [1, 2]
    W = np.tile([0.5, -0.5], (T, 1))
    R = np.zeros((T, 2))
    R[3:5, 0] = np.nan                              # 第 4、5 日停牌
    R[5, 0] = 0.10                                  # 复牌日 = 跨停牌期累计收益（跳空 +10%）
    halted = pd.DataFrame(False, index=dates, columns=cols)
    halted.iloc[3:5, 0] = True                      # 正向判据
    res = simulate(panel(W, dates, cols), panel(R, dates, cols), booksize=B,
                   is_halted=halted, cost_bps=0.0)
    hv, d = res.holding_value, res.daily
    a = hv.iloc[:, 0].to_numpy()
    check(res.audit["ghost_detection"] == "field", "检测口径 = field",
          res.audit["ghost_detection"])
    check(np.isfinite(a).all(), "停牌日持仓不是 NaN（§8.2 注 1）", f"{a[3]:,.0f}")
    check(abs(a[3] - a[2]) < 1e-9 and abs(a[4] - a[3]) < 1e-9, "停牌两日持仓原地冻结",
          f"{a[2]:,.0f} / {a[3]:,.0f} / {a[4]:,.0f}")
    check(np.isfinite(d["pnl"].to_numpy()).all(), "停牌日 daily.pnl 是有限数")
    check(abs(res.pnl.iloc[3, 0]) < 1e-12 and abs(res.pnl.iloc[4, 0]) < 1e-12,
          "停牌日该票 pnl = 0")
    gap = res.pnl.iloc[5, 0]
    check(abs(gap - a[4] * 0.10) < 1e-6, "跳空损益一次性落在复牌日",
          f"{gap:,.2f} = {a[4]:,.0f} × 10%")
    check(abs(d['frozen_value'].iloc[3] - abs(a[3])) < 1e-9, "frozen_value = 冻结票 gross",
          f"{d['frozen_value'].iloc[3]:,.0f}")
    check(abs(d['avail'].iloc[3] - (B - abs(a[3]))) < 1e-9, "avail = booksize − frozen_value",
          f"{d['avail'].iloc[3]:,.0f}")
    check(abs(d['frozen_reprice_pnl'].iloc[5] - gap) < 1e-6,
          "frozen_reprice_pnl 单列出复牌重估损益", f"{d['frozen_reprice_pnl'].iloc[5]:,.2f}")
    check(res.audit["ghost_days"] == 0, "有正向 is_halted 时无幽灵持仓")


# =====================================================================
# 4. 退市：资金真的回收
# =====================================================================
def test_delist():
    T, B = 8, 20e6
    dates, cols = sessions(T), [1, 2]
    W = np.tile([0.5, 0.5], (T, 1))
    R = np.zeros((T, 2))
    R[4, 0] = -0.30                                  # 退市末日 = 破产保守估计
    R[5:, 0] = np.nan                                # 之后没有报价
    delist = pd.Series({1: dates[4], 2: pd.NaT})
    res = simulate(panel(W, dates, cols), panel(R, dates, cols), booksize=B,
                   delist_date=delist, halt_proxy=2, cost_bps=0.0)
    hv, d = res.holding_value, res.daily
    a, b = hv.iloc[:, 0].to_numpy(), hv.iloc[:, 1].to_numpy()
    check(abs(res.pnl.iloc[4, 0] - a[3] * -0.30) < 1e-6, "退市日按最终对价计损益",
          f"{res.pnl.iloc[4, 0]:,.2f} = {a[3]:,.0f} × −30%")
    check(a[4] == 0.0, "退市日收盘该票已平仓", f"pos = {a[4]}")
    check(d["delist_close_value"].iloc[4] > 0 and res.audit["delist_events"] == 1,
          "delist_events 计到 1（退市路径不是死代码）",
          f"释放 {d['delist_close_value'].iloc[4]:,.0f}")
    check(d["frozen_value"].iloc[5:].max() == 0.0, "退市后不再占用 frozen_value（资金回收）",
          f"max = {d['frozen_value'].iloc[5:].max():,.0f}")
    check(abs(b[3] - B / 2) < 1e-6 and abs(b[4] - B) < 1e-6,
          "释放的资金当日就回到活票上（退市 = 资金回收，不是占用）",
          f"另一票 {b[3]:,.0f} → {b[4]:,.0f}")
    # [偏离 4]：退市当日不许买进一只将死的票，只许卖出
    r2 = simulate(panel(W, dates, cols), panel(R, dates, cols), booksize=B,
                  delist_date=delist, halt_proxy=2, cost_bps=0.0, keep_flows=True)
    dlt = r2.flows["trade"].iloc[4, 0]
    check(dlt <= 0, "退市当日只减仓不加仓（否则凭空抬高 trade_dollar 与成本）",
          f"delta = {dlt:,.2f}")


# =====================================================================
# 5. 多空两侧同时停牌 —— 带符号求和的陷阱（§十三.1 用例 ①）
# =====================================================================
def test_both_sides_frozen():
    T, B = 5, 20e6
    dates, cols = sessions(T), [1, 2, 3, 4]
    W = np.tile([0.25, -0.25, 0.25, -0.25], (T, 1))
    # 停牌当日 alpha 想加仓那只停牌的多头（0.25 → 0.50）：它动不了，那份钱只能落到
    # 可交易的两只身上，且落法与信号本身的意图不同 —— 这正是 realloc_turnover 要
    # 单列的非信号换手（"冻结重分配引起的摩擦"，不许混进 alpha 换手）。
    W[2:] = [0.50, -0.25, 0.20, -0.05]
    R = np.zeros((T, 4))
    R[2:, 0] = np.nan                                 # 多头一只停牌
    R[2:, 1] = np.nan                                 # 空头一只停牌
    halted = pd.DataFrame(False, index=dates, columns=cols)
    halted.iloc[2:, [0, 1]] = True
    res = simulate(panel(W, dates, cols), panel(R, dates, cols), booksize=B,
                   is_halted=halted, cost_bps=0.0)
    hv, d = res.holding_value, res.daily
    pos = hv.iloc[2].to_numpy()
    gross = abs(pos[0]) + abs(pos[1])
    signed = pos[0] + pos[1]
    check(abs(d["frozen_value"].iloc[2] - gross) < 1e-6,
          "frozen_value = 两侧 gross 之和（而非带符号和）",
          f"frozen_value = {d['frozen_value'].iloc[2]:,.0f}，两侧 gross = {gross:,.0f}")
    check(abs(signed) < 1e-6,
          "对照：带符号和 ≈ 0 —— 写错就会让 avail ≈ booksize、冻结重分配等于没做",
          f"带符号和 = {signed:,.2f}")
    check(abs(d["avail"].iloc[2] - (B - gross)) < 1e-6, "avail 扣掉了 10M 而不是 0",
          f"avail = {d['avail'].iloc[2]:,.0f}（若用带符号和会是 {B:,.0f}）")
    live = abs(hv.iloc[2, 2]) + abs(hv.iloc[2, 3])
    check(abs(live - (B - gross)) < 1e-6, "可交易的两只把 avail 投满（可交易部分始终满仓）",
          f"{live:,.0f}")
    check(d["realloc_turnover"].iloc[2] > 0 and d["gap_realloc"].iloc[2] > 0,
          "冻结引起的摩擦换手单列在 realloc_turnover，不混进 alpha 换手",
          f"realloc {d['realloc_turnover'].iloc[2]:.3%} / alpha {d['alpha_turnover'].iloc[2]:.3%}"
          f" / gap_realloc {d['gap_realloc'].iloc[2]:,.0f}")
    close((d["alpha_turnover"] + d["realloc_turnover"]).to_numpy(),
          (d["trade_dollar"] / B).to_numpy(), 1e-15,
          "两块换手恰好把当日成交额分完（alpha + realloc ≡ turnover）")


# =====================================================================
# 6. 全员冻结（§十三.1 用例 ③）
# =====================================================================
def test_all_frozen():
    T, B = 4, 20e6
    dates, cols = sessions(T), [1, 2]
    W = np.tile([0.5, -0.5], (T, 1))
    R = np.zeros((T, 2))
    R[2:, :] = np.nan
    halted = pd.DataFrame(False, index=dates, columns=cols)
    halted.iloc[2:, :] = True
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)   # 除零/无效值会以 RuntimeWarning 现身
        res = simulate(panel(W, dates, cols), panel(R, dates, cols), booksize=B,
                       is_halted=halted, cost_bps=1.0)
    d, hv = res.daily, res.holding_value
    check(np.isfinite(hv.to_numpy()).all() and np.isfinite(res.pnl.to_numpy()).all(),
          "全员冻结不产生 NaN/Inf（不除零）")
    check(abs(d["avail"].iloc[2]) < 1e-9, "avail = 0", f"{d['avail'].iloc[2]}")
    check(d["trade_dollar"].iloc[2:].sum() == 0.0, "avail=0 时不产生任何交易",
          f"trade_dollar = {d['trade_dollar'].iloc[2:].sum()}")
    check(d["cost"].iloc[2:].sum() == 0.0, "也不产生任何成本")
    check(abs(d["frozen_value"].iloc[2] - B) < 1e-6, "整本 booksize 全被冻结",
          f"{d['frozen_value'].iloc[2]:,.0f}")


# =====================================================================
# 7. ghost 三分类与降级口径（§九）
# =====================================================================
def test_ghost_detection():
    T, B = 12, 20e6
    dates, cols = sessions(T), [1, 2]
    W = np.tile([0.5, -0.5], (T, 1))
    R = np.zeros((T, 2))
    R[4, 0] = np.nan                                  # 一日 NaN，无 delist → 幽灵
    R[7:10, 1] = np.nan                               # 三日 NaN → proxy(2) 判为停牌
    w_, r_ = panel(W, dates, cols), panel(R, dates, cols)

    try:
        simulate(w_, r_, booksize=B)
        check(False, "无 is_halted 且无 halt_proxy 时必须拒绝运行")
    except SimError as e:
        check("拒绝运行" in str(e), "无正向停牌信号 → 拒绝运行（不静默降级）",
              str(e).splitlines()[0])
    try:
        simulate(w_, r_, booksize=B, halt_proxy=1)
        check(False, "halt_proxy=1 必须被拒")
    except SimError as e:
        check("K=1" in str(e), "halt_proxy=1 被拒（否则第三类恒空、告警永不触发）",
              str(e).splitlines()[0])

    with warnings.catch_warnings(record=True) as W_:
        warnings.simplefilter("always")
        # 12 天 × 2 票的玩具面板上 1 个幽灵就占 4.8%，故这里把阈值放宽；
        # 默认阈值会不会响，由本函数末尾那条断言单独证明。
        res = simulate(w_, r_, booksize=B, halt_proxy=2, cost_bps=0.0, ghost_tolerance=0.10)
    check(res.audit["ghost_detection"] == "proxy(2)", "ghost_detection = proxy(2)",
          res.audit["ghost_detection"])
    check(res.audit["ghost_cells"] == 1 and res.audit["ghost_days"] == 1,
          "一日 NaN 记为幽灵持仓（降级口径会漏掉真正的一日停牌）",
          f"ghost_cells = {res.audit['ghost_cells']}")
    check(any("幽灵持仓" in str(x.message) for x in W_), "少量幽灵 → warning 而非静默")
    check(res.holding_value.iloc[4, 0] == 0.0, "幽灵持仓按最后可得价当日平仓",
          f"pos = {res.holding_value.iloc[4, 0]}")
    check(res.audit["halt_cells"] == 3 and res.daily["frozen_value"].iloc[7] > 0,
          "≥K 的连续 NaN 判为停牌 → 冻结而非平仓",
          f"halt_cells = {res.audit['halt_cells']}")

    res_d = simulate(w_, r_, booksize=B, halt_proxy=0, cost_bps=0.0)   # 显式关闭
    check(res_d.audit["ghost_detection"] == "disabled" and res_d.audit["ghost_days"] == 0,
          "halt_proxy=0 → disabled，且报表能区分'没有幽灵'与'根本没查'",
          f"{res_d.audit['ghost_detection']} / ghost_days={res_d.audit['ghost_days']}")
    try:
        simulate(w_, r_, booksize=B, halt_proxy=2, ghost_tolerance=0.0)
        check(False, "幽灵超阈值必须报错")
    except SimError as e:
        check("幽灵持仓超阈值" in str(e), "幽灵超阈值 → 报错（不是调阈值继续跑）",
              str(e).splitlines()[0])
    check(simulate(w_, r_, booksize=B, halt_proxy=2, cost_bps=0.0, ghost_tolerance=0.10
                   ).audit["delist_source"] == "none",
          "无 delist_date → delist_source=none（退市路径是死代码，必须可见）")


# =====================================================================
# 8. 内核的三条不变量：别名 / 无冻结分解 / NaN ADV
# =====================================================================
def test_kernel_invariants():
    rng = np.random.default_rng(3)
    T, N, B = 60, 25, 20e6
    dates, cols = sessions(T), list(range(1, N + 1))
    sig = rng.standard_normal((T, N))
    Wm = sig / np.abs(sig).sum(axis=1, keepdims=True)
    w = panel(Wm, dates, cols)
    r = panel(rng.standard_normal((T, N)) * 0.015, dates, cols)
    snapshot = w.copy(deep=True)

    res = simulate(w, r, booksize=B, halt_proxy=2, cost_bps=0.0)
    check(w.equals(snapshot), "[偏离 1] 权重面板未被就地改写（容量扫描要重复喂同一份）")
    r2 = simulate(w, r, booksize=2 * B, halt_proxy=2, cost_bps=0.0)
    close(r2.pnl.to_numpy(), 2 * res.pnl.to_numpy(), 1e-6,
          "容量扫描：无 cap 时 booksize 翻倍 → 损益逐位翻倍")

    d = res.daily
    check(float(d["realloc_turnover"].abs().max()) == 0.0,
          "无冻结时 realloc_turnover 恒为 0", f"max = {d['realloc_turnover'].abs().max()}")
    close(d["alpha_turnover"].to_numpy(), (d["trade_dollar"] / B).to_numpy(), 1e-12,
          "无冻结时 alpha_turnover ≡ trade_dollar / booksize")
    check(float(d[["gap_participation", "gap_realloc", "gap_reprice"]].abs().max().max()) < 1e-6,
          "无冻结、无 cap 时三个 gap 全为 0",
          f"max = {float(d[['gap_participation', 'gap_realloc', 'gap_reprice']].abs().max().max()):.3e}")
    check(float(d["cash"].abs().max()) < 1e-6, "满仓：cash ≈ 0",
          f"max|cash| = {d['cash'].abs().max():.3e}")

    # [偏离 3] ADV 缺失：cap 取 +inf 而不是 NaN，否则 np.clip(x, nan, nan) 把持仓变 NaN
    adv = pd.DataFrame(1e9, index=dates, columns=cols)
    adv.iloc[:19] = np.nan                            # 20 日 ADV 的预热期
    adv.iloc[:, 3] = np.nan                           # 中途上市的票
    res3 = simulate(w, r, booksize=B, adv_dollar=adv, halt_proxy=2, cost_bps=0.0)
    check(np.isfinite(res3.holding_value.to_numpy()).all(),
          "[偏离 3] ADV=NaN 不把持仓变成 NaN")
    check(res3.audit["adv_uncapped_cells"] > 0, "ADV 缺失处未设约束，且计数可见",
          f"adv_uncapped_cells = {res3.audit['adv_uncapped_cells']}")

    # 参与率约束：cap 咬住时缺口进 gap_participation，且每日重试
    advs = pd.DataFrame(2e6, index=dates, columns=cols)
    res4 = simulate(w, r, booksize=B, adv_dollar=advs, participation=0.10,
                    halt_proxy=2, cost_bps=0.0)
    check(res4.daily["gap_participation"].iloc[0] > 0, "cap 咬住 → gap_participation > 0",
          f"{res4.daily['gap_participation'].iloc[0]:,.0f}")
    check(res4.daily["cash"].iloc[0] > 0, "投不满的部分留在 cash（容量警报）",
          f"cash = {res4.daily['cash'].iloc[0]:,.0f}")
    check(float(res4.daily["trade_dollar"].max()) <= N * 0.10 * 2e6 + 1e-6,
          "单日成交额不超过 Σ cap")


# =====================================================================
# 9. 指标与七道闸门
# =====================================================================
def test_metrics_and_gates():
    rng = np.random.default_rng(11)
    T, N, B = 520, 60, 20e6
    dates, cols = sessions(T, "2023-01-02"), list(range(1, N + 1))
    mkt = rng.standard_normal(T) * 0.01
    sig = rng.standard_normal((T, N))
    Wm = sig / np.abs(sig).sum(axis=1, keepdims=True)
    R = rng.standard_normal((T, N)) * 0.02 + mkt[:, None] * 0.5
    R += np.roll(Wm, 1, axis=0) * 0.012                     # 让 alpha 真有一点（而非离谱的）收益
    R[100:103, 4] = np.nan
    uni = pd.DataFrame(True, index=dates, columns=cols)
    res = simulate(panel(Wm, dates, cols), panel(R, dates, cols), booksize=B,
                   cost_bps=2.0, halt_proxy=2, universe=uni,
                   adv_dollar=pd.DataFrame(5e8, index=dates, columns=cols))
    m = metrics(res, meta={"region_hash": "a3f91c", "region_hash_canonical": "a3f91c",
                           "deps_tc": {"g_common.field_base_px.field_base_px-adj_close_tc":
                                       "g_common.field_base_px.field_base_px-adj_close_1500"},
                           "weight_hash": "sha256:deadbeef"})
    say("")
    for line in format_report(m).splitlines():
        say("    " + line)
    say("")
    g = m["gates"]
    check(len(g) == 7, "七道闸门齐全", f"{len(g)} 道")
    check(all(x["numbers"] for x in g), "每道闸门都带数字（通过也打印）")
    check(all(x["state"] in ("PASS", "FAIL", "NO-BASIS") for x in g), "每道闸门都有状态")
    check(m["summary"].startswith("submission readiness:"), "末行是 readiness", m["summary"])
    check(m["audit"]["ghost_detection"] == "proxy(2)", "metrics.json 恒含 ghost_detection",
          m["audit"]["ghost_detection"])
    check(m["audit"]["delist_source"] == "none", "metrics.json 恒含 delist_source")
    check(len(m["by_year"]) >= 2, "分年度表", str(sorted(m["by_year"])))
    s = m["scalar"]
    for k in ("sharpe", "ann_return", "turnover", "margin_bps", "fitness", "max_drawdown",
              "avg_long_value", "avg_short_value", "avg_long_count", "avg_short_count"):
        if not check(s.get(k) is not None, f"标量指标 {k} 已算出"):
            break
    fit = s["sharpe"] * np.sqrt(abs(s["ann_return"]) / max(s["turnover"], 0.125))
    check(abs(fit - s["fitness"]) < 1e-9, "Fitness = Sharpe×√(|Ret|/max(TO,0.125))",
          f"{s['fitness']:.4f}")
    import json
    js = json.dumps(m, allow_nan=False)                       # 严格 JSON：NaN/Inf 已转 None
    check(len(js) > 0, "metrics 可直接 json.dump（严格模式）", f"{len(js)} bytes")

    # NO-BASIS 必须真的会出现：不给 universe、不给 meta、不计成本
    res2 = simulate(panel(Wm, dates, cols), panel(R, dates, cols), booksize=B,
                    cost_bps=0.0, halt_proxy=2)
    m2 = metrics(res2)
    nb = [x["gate"] for x in m2["gates"] if x["state"] == "NO-BASIS"]
    check("成本临界倍数" in nb and "池子卫生" in nb,
          "没有判据的闸门报 NO-BASIS 而不是悄悄 PASS", f"NO-BASIS: {nb}")


# =====================================================================
# 10. 真实 store 端到端
# =====================================================================
def test_real_store():
    root = Path(__file__).resolve().parents[1] / "storage" / "l3"
    from alpha_kit.core.store import Store
    # 让 Store 自己去定位轴, 不在这里硬编码 `_axes` 的层级——轴挪过一次家
    # （l3/_axes → l3/{region}/_axes, 见 Store.__init__ 的理由）, 而硬编码的那版守卫
    # 挪家后并不报错, 只是从此静静地跳过这 4 条断言。跳过必须有理由, 不能是路径写错。
    try:
        s = Store(str(root), "us")
    except FileNotFoundError:
        say(f"    [skip] {root} 下没有可用的轴（先跑 pipeline/build_l3_base.py），跳过端到端")
        return
    ret = s.read("g_common.field_base_px.field_base_px-ret_1d_1500")
    adv = s.read("g_common.field_base_px.field_base_px-adv_dollar")
    uni = s.read("g_common.field_common_univ.field_common_univ-us_top400").astype(bool)
    mom = s.read("g_yliu.factor_yliu_mom.factor_yliu_mom-mom")
    # 现成的 alpha 权重面板此刻还是全 0，故就地用 mom 因子造一份真权重：
    # 池内去均值再 scale（Σ|w|=1），池外恰为 0——正是 §3.5 两端夹住的掩码。
    x = mom.where(uni)
    x = x.sub(x.mean(axis=1), axis=0)
    w = x.div(x.abs().sum(axis=1), axis=0).fillna(0.0)
    keep = w.abs().sum(axis=1) > 0
    w, ret_, adv_, uni_ = w[keep], ret.loc[keep], adv.loc[keep], uni.loc[keep]
    with warnings.catch_warnings(record=True) as WW:
        warnings.simplefilter("always")
        res = simulate(w, ret_, booksize=20e6, adv_dollar=adv_, cost_bps=2.0,
                       halt_proxy=2, participation=0.10, universe=uni_, keep_flows=True)
    hv, fl = res.holding_value.to_numpy(), res.flows
    prev = np.vstack([np.zeros((1, hv.shape[1])), hv[:-1]])
    resid = hv - (prev + res.pnl.to_numpy() + fl["trade"].to_numpy()
                  + fl["settle"].to_numpy() + fl["cost"].to_numpy())
    close(resid, 0.0, 1e-9 * 20e6, "真实面板上会计恒等式成立")
    m = metrics(res, market_ret=s.read("g_common.field_base_px.field_base_px-market_ret"))
    say("")
    for line in format_report(m).splitlines():
        say("    " + line)
    say("")
    check(float(res.daily["oop_weight"].max()) == 0.0, "池外权重恰为 0（闸门六有判据）")
    check(np.isfinite(hv).all(), "真实数据上无 NaN 持仓")
    say(f"    真实 warning: {[str(x.message).splitlines()[0] for x in WW][:3]}")

    # K 的取值直接决定这道防线会不会响 —— 本面板实测的 NaN 连续段长度分布：
    nanm = ret_.isna().to_numpy()
    dist: dict[int, int] = {}
    for j in range(nanm.shape[1]):
        run = 0
        for v in nanm[:, j]:
            if v:
                run += 1
            elif run:
                dist[run] = dist.get(run, 0) + 1
                run = 0
        if run:
            dist[run] = dist.get(run, 0) + 1
    say(f"    NaN 连续段长度分布 {dict(sorted(dist.items()))}（首行 ret 无定义的那一列不计）")
    ks = {}
    for k in (2, 3):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ks[k] = simulate(w, ret_, booksize=20e6, adv_dollar=adv_, cost_bps=2.0,
                             halt_proxy=k, universe=uni_, ghost_tolerance=1.0).audit
        say(f"    halt_proxy={k}: ghost_days={ks[k]['ghost_days']} "
            f"ghost_cells={ks[k]['ghost_cells']} halt_cells={ks[k]['halt_cells']}")
    check(ks[2]["ghost_days"] == 0 and ks[3]["ghost_days"] > 0,
          "K 的取值是有后果的：本面板真实缺口都是 2 个 session，K=2 会把它们判成停牌、"
          "ghost_days 恒为 0（§九 要堵的那个失效模式的软化版）；K=3 才让告警响",
          f"K=2 → {ks[2]['ghost_days']} 天；K=3 → {ks[3]['ghost_days']} 天 "
          f"{ks[3]['ghost_examples'][:1]}")


# =====================================================================
# 10b. 四交付物落盘（§8.3）
# =====================================================================
def test_deliverables():
    """holding / pnl / daily / metrics.json 四件都要能真的写出来再读回去。"""
    import json
    import tempfile
    rng = np.random.default_rng(5)
    T, N, B = 40, 8, 20e6
    dates = pd.to_datetime(sessions(T))                 # 顺带验一遍 DatetimeIndex 轴
    cols = list(range(1, N + 1))
    sig = rng.standard_normal((T, N))
    Wm = sig / np.abs(sig).sum(axis=1, keepdims=True)
    res = simulate(panel(Wm, dates, cols),
                   panel(rng.standard_normal((T, N)) * 0.02, dates, cols),
                   booksize=B, cost_bps=1.0, halt_proxy=2,
                   delist_date=pd.Series({c: pd.NaT for c in cols}))
    h = res.holding
    check(list(h.columns.levels[0]) == ["holding_value", "holding_weight"],
          "holding 同时带 value 与 weight 两块", str(h.shape))
    close(res.holding_weight.to_numpy(), res.holding_value.to_numpy() / B, 0,
          "holding_weight = holding_value / booksize（与目标权重同尺度）")
    with tempfile.TemporaryDirectory() as td:
        paths = res.write(td)
        (Path(td) / "metrics.json").write_text(
            json.dumps(metrics(res), ensure_ascii=False, indent=1, allow_nan=False))
        back = pd.read_feather(paths["daily"])
        check(len(back) == T and "return" in back.columns, "daily.feather 可读回",
              f"{len(back)} 行 × {len(back.columns)} 列")
        check(pd.read_feather(paths["holding"]).shape[1] == 2 * N + 1,
              "holding.feather 拍平成 {块}|{security_id}")
        m = json.loads((Path(td) / "metrics.json").read_text())
        check(set(m) >= {"scalar", "by_year", "audit", "gates", "summary", "snapshot"},
              "metrics.json 结构齐全", str(sorted(m)))

    # 全零权重（引擎预热期）不许把仿真器打崩
    z = pd.DataFrame(0.0, index=dates, columns=cols)
    r0 = simulate(z, panel(rng.standard_normal((T, N)) * 0.02, dates, cols),
                  booksize=B, halt_proxy=2)
    m0 = metrics(r0)
    check(float(r0.daily["trade_dollar"].sum()) == 0.0 and m0["scalar"]["sharpe"] is None,
          "权重全为 0 的预热期：不交易、指标为 None 而不是崩溃或假数字")
    check(m0["gates"][5]["state"] == "NO-BASIS", "此时池子卫生闸门报 NO-BASIS")


# =====================================================================
# 11. 5000 天 × 6000 票计时（§8.2 的"秒级"）
# =====================================================================
def test_timing():
    T, N = 5000, 6000
    rng = np.random.default_rng(0)
    dates = sessions(T, "2005-01-03")
    cols = list(range(1, N + 1))
    sig = rng.standard_normal((T, N)).astype(np.float32)
    W = sig / np.abs(sig).sum(axis=1, keepdims=True)
    R = (rng.standard_normal((T, N)) * 0.02).astype(np.float32)
    R[rng.random((T, N)) < 0.001] = np.nan             # 0.1% 的 NaN（停牌/缺口）
    w, r = pd.DataFrame(W, index=dates, columns=cols), pd.DataFrame(R, index=dates, columns=cols)
    del sig, W, R
    t0 = time.perf_counter()
    res = simulate(w, r, booksize=20e6, cost_bps=2.0, halt_proxy=2, ghost_tolerance=1.0)
    dt = time.perf_counter() - t0
    check(dt < 60, f"{T}×{N} 仿真耗时 {dt:.2f}s（§8.2 称'秒级'）",
          f"{dt / T * 1e3:.3f} ms/日")
    t1 = time.perf_counter()
    m = metrics(res)
    say(f"    metrics + 七道闸门 {time.perf_counter() - t1:.2f}s（§15.9 要求 < 200ms 的是闸门部分）")
    say(f"    Sharpe {m['scalar']['sharpe']:.3f}  ghost_cells {res.audit['ghost_cells']}  "
        f"halt_cells {res.audit['halt_cells']}")


# =====================================================================
def main() -> int:
    tests = [test_identity, test_split, test_dividend, test_halt, test_delist,
             test_both_sides_frozen, test_all_frozen, test_ghost_detection,
             test_kernel_invariants, test_metrics_and_gates, test_deliverables,
             test_real_store, test_timing]
    for fn in tests:
        head = next((l.strip() for l in (fn.__doc__ or "").splitlines() if l.strip()), "")
        say(f"\n=== {fn.__name__} — {head}")
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message="幽灵持仓")
                warnings.filterwarnings("ignore", message="无 delist_date")
                fn()
        except Exception:
            FAILURES.append(fn.__name__)
            say(traceback.format_exc())
    say("\n" + "=" * 72)
    if FAILURES:
        say(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    say("ALL GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
