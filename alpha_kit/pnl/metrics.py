"""指标与闸门：§8.4 的指标集 + §15.9 的七道闸门。全部从 §8.3 的四交付物派生。

两条规矩贯穿本模块，都是 §九 那个教训（`ghost_days` 恒为 0）的直接推论：

1. **每道闸门都打印自己的状态和数字，即使通过也打印。** 空白绝不能在"干净"与
   "没查"之间有歧义——所以没有判据的闸门不报 PASS，它报 `NO-BASIS` 并说明缺什么。
2. **防线的实际状态进 metrics.json**：`ghost_detection` ∈ {field, proxy(K), disabled}、
   `delist_source` ∈ {field, none}，恒在、恒打印。

指标口径对齐 BRAIN：Sharpe 基于 `return` 列（分母恒定为 booksize，§8.3），
账本不复利，故 Ann.Return 用算术年化 `mean × 252`、MaxDD 在累计**美元**曲线上取。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

ANN_DAYS = 252

# 阈值集中在这里：闸门是研究期的 warn、CI 的 `--gate strict`、提交路径的硬阻断，
# 三处共用同一套数字（§15.9 严重度 policy）。
DEFAULT_THRESHOLDS = {
    "beta_r2_max": 0.25,          # 闸门一：市场解释掉的方差超过 1/4 就是 beta 押注
    "conc_top1_max": 0.20,        # 闸门二：单票占 Σ|pnl| 的上限
    "conc_top5_max": 0.50,
    "conc_day1_max": 0.20,        # 单日占 Σ|日度 pnl| 的上限（"就是一天"）
    "stab_half_ratio_min": 0.25,  # 闸门三：下半场 Sharpe 不得低于上半场的 1/4
    "stab_roll1y_sharpe_min": 0.0,
    "breakeven_min": 2.0,         # 闸门四：成本模型至少要错 2 倍才亏光
    "ls_ratio_lo": 0.67,          # 闸门五：多空市值比
    "ls_ratio_hi": 1.50,
    "pool_gross_tol": 1e-6,       # 闸门六：scale 之后 Σ|w| 与 1 的容差
}

PASS, FAIL, NOBASIS = "PASS", "FAIL", "NO-BASIS"


# ------------------------------------------------------------------ 小工具
def _f(x):
    """JSON 安全：numpy 标量 → python 标量；NaN/Inf → None（严格 JSON 无这两个字面量）。"""
    if isinstance(x, (np.generic,)):
        x = x.item()
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


def _jsonable(o):
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    return _f(o)


def _sharpe(x: np.ndarray, ann: int = ANN_DAYS) -> float:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if x.size < 2:
        return float("nan")
    sd = x.std(ddof=1)
    return float("nan") if sd == 0 else float(x.mean() / sd * math.sqrt(ann))


def _maxdd(pnl: np.ndarray) -> tuple[float, int, int]:
    """账本不复利，故回撤在累计**美元**曲线上取（复利口径会被 booksize 的选择污染）。"""
    cum = np.cumsum(np.nan_to_num(pnl))
    peak = np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:]
    dd = cum - peak
    if dd.size == 0:
        return 0.0, -1, -1
    i = int(np.argmin(dd))
    j = int(np.argmax(cum[:i + 1])) if i > 0 else 0
    return float(-dd[i]), j, i


def _fmt(v, spec=".4g"):
    return "n/a" if v is None or (isinstance(v, float) and not math.isfinite(v)) else format(v, spec)


# ------------------------------------------------------------------ 指标主体
def _scalars(daily: pd.DataFrame, booksize: float, ann: int) -> dict:
    ret, pnl = daily["return"].to_numpy(), daily["pnl"].to_numpy()
    trade = daily["trade_dollar"].to_numpy()
    sharpe = _sharpe(ret, ann)
    ann_ret = float(np.nanmean(ret) * ann)                  # 算术年化：分母恒定、不复利
    turnover = float(np.nanmean(trade) / booksize)          # 日均换手 = trade_dollar / booksize
    tot_trade, tot_pnl, tot_cost = float(trade.sum()), float(pnl.sum()), float(daily["cost"].sum())
    tot_hold = float(daily["holding_pnl"].sum())
    dd, i0, i1 = _maxdd(pnl)
    # Fitness = Sharpe × √(|Ret| / max(TO, 0.125))：TO 下限 0.125 是 BRAIN 的口径，
    # 防止低换手 alpha 靠除以一个极小数把 Fitness 顶上天。
    fitness = sharpe * math.sqrt(abs(ann_ret) / max(turnover, 0.125)) if np.isfinite(sharpe) else float("nan")
    lv, sv = float(daily["long_value"].mean()), float(daily["short_value"].mean())
    return {
        "n_days": int(len(daily)),
        "sharpe": sharpe,
        "ann_return": ann_ret,                              # booksize 口径（权威）
        "ann_return_dollar": ann_ret * booksize,
        "ann_return_long_side": (ann_ret * booksize / lv) if lv else float("nan"),  # BRAIN 单边展示换算
        "turnover": turnover,
        "margin_bps": (tot_pnl / tot_trade * 1e4) if tot_trade else float("nan"),
        "fitness": fitness,
        "max_drawdown": dd / booksize,
        "max_drawdown_dollar": dd,
        "max_drawdown_from": str(daily.index[i0]) if i0 >= 0 else None,
        "max_drawdown_to": str(daily.index[i1]) if i1 >= 0 else None,
        "avg_long_value": lv,
        "avg_short_value": sv,                              # 负部，带符号
        "avg_long_count": float(daily["long_count"].mean()),
        "avg_short_count": float(daily["short_count"].mean()),
        "long_short_ratio": (lv / -sv) if sv else float("inf"),
        "pnl_total": tot_pnl,
        "holding_pnl_total": tot_hold,
        "trading_pnl_total": float(daily["trading_pnl"].sum()),
        "cost_total": tot_cost,
        "trade_dollar_total": tot_trade,
        # 收益里多少被成本吃掉——高换手 alpha 的关键读数（§8.4 自增项）
        "cost_share_of_gross": (tot_cost / tot_hold) if tot_hold else float("nan"),
        "net_share_of_gross": (tot_pnl / tot_hold) if tot_hold else float("nan"),
        "return_std_daily": float(np.nanstd(ret, ddof=1)) if len(ret) > 1 else float("nan"),
        "hit_rate": float(np.mean(pnl > 0)),
    }


def _by_year(daily: pd.DataFrame, booksize: float, ann: int) -> dict:
    out = {}
    for y, g in daily.groupby(pd.to_datetime(pd.Index(daily.index)).year):
        pnl = g["pnl"].to_numpy()
        tr = float(g["trade_dollar"].mean() / booksize)
        out[str(int(y))] = {
            "days": int(len(g)),
            "sharpe": _sharpe(g["return"].to_numpy(), ann),
            "ann_return": float(g["return"].mean() * ann),
            "pnl": float(pnl.sum()),
            "turnover": tr,
            "max_drawdown": _maxdd(pnl)[0] / booksize,
            "margin_bps": (float(pnl.sum()) / float(g["trade_dollar"].sum()) * 1e4)
                          if g["trade_dollar"].sum() else float("nan"),
            "avg_long_value": float(g["long_value"].mean()),
            "avg_short_value": float(g["short_value"].mean()),
        }
    return out


def _audit(daily: pd.DataFrame, sim_audit: dict) -> dict:
    """§8.4 审计类指标。source 类字段一律透传，使防线状态在报表上可见。"""
    return {
        "ghost_detection": sim_audit["ghost_detection"],
        "ghost_detection_lookahead": sim_audit["ghost_detection_lookahead"],
        "delist_source": sim_audit["delist_source"],
        "ghost_days": sim_audit["ghost_days"],
        "ghost_cells": sim_audit["ghost_cells"],
        "ghost_rate": sim_audit["ghost_rate"],
        "ghost_examples": sim_audit["ghost_examples"],
        "delist_events": sim_audit["delist_events"],
        "halt_cells": sim_audit["halt_cells"],
        "frozen_value_avg": float(daily["frozen_value"].mean()),
        "frozen_value_max": float(daily["frozen_value"].max()),
        "frozen_count_avg": float(daily["frozen_count"].mean()),
        "frozen_reprice_pnl": float(daily["frozen_reprice_pnl"].sum()),
        "realloc_turnover_avg": float(daily["realloc_turnover"].mean()),
        "alpha_turnover_avg": float(daily["alpha_turnover"].mean()),
        "cash_avg": float(daily["cash"].mean()),
        "cash_max": float(daily["cash"].max()),
        "gap_participation_avg": float(daily["gap_participation"].mean()),
        "gap_realloc_avg": float(daily["gap_realloc"].mean()),
        "gap_reprice_avg": float(daily["gap_reprice"].mean()),
        "weight_nan_cells": sim_audit["weight_nan_cells"],
        "adv_uncapped_cells": sim_audit["adv_uncapped_cells"],
        "nav_final": float(daily["nav"].iloc[-1]),
    }


# -------------------------------------------------------------------- 闸门
def _g(name, state, numbers: dict, note: str = "") -> dict:
    return {"gate": name, "state": state, "numbers": numbers, "note": note}


def _gate_beta(daily, thr, market_ret, ann):
    y = daily["return"].to_numpy(dtype=float)
    x = (daily["market_ret"] if market_ret is None
         else pd.Series(market_ret).reindex(daily.index)).to_numpy(dtype=float)
    ok = np.isfinite(x) & np.isfinite(y)
    nums = {"beta": None, "r2": None, "sharpe_hedged": None, "sharpe_raw": _sharpe(y, ann),
            "n_obs": int(ok.sum()), "market_source": "panel_equal_weight" if market_ret is None else "given"}
    # 两边都要查方差：账本恒为空仓时 y 是常数序列，corrcoef 会除零并吐 RuntimeWarning
    if ok.sum() < 30 or np.std(x[ok]) == 0 or np.std(y[ok]) == 0:
        return _g("market beta", NOBASIS, nums,
                  "市场序列或组合收益不足 30 个有效观测、或方差为 0——无从回归")
    xs, ys = x[ok], y[ok]
    beta = float(np.cov(xs, ys, ddof=1)[0, 1] / np.var(xs, ddof=1))
    r2 = float(np.corrcoef(xs, ys)[0, 1] ** 2)
    nums.update(beta=beta, r2=r2, sharpe_hedged=_sharpe(ys - beta * xs, ann))
    state = PASS if r2 <= thr["beta_r2_max"] else FAIL
    return _g("market beta", state, nums, f"R² 阈值 {thr['beta_r2_max']}")


def _gate_concentration(res, thr):
    by_name = res.pnl.sum(axis=0)
    absn = by_name.abs()
    tot = float(absn.sum())
    daily_abs = res.daily["pnl"].abs()
    day_tot = float(daily_abs.sum())
    nums = {"total_abs_pnl": tot, "n_names_with_pnl": int((absn > 0).sum())}
    if tot == 0:
        return _g("集中度", NOBASIS, nums, "全区间 Σ|pnl| = 0，无从判断集中度")
    order = absn.sort_values(ascending=False)
    top1 = float(order.iloc[:1].sum() / tot)
    top5 = float(order.iloc[:5].sum() / tot)
    top20 = float(order.iloc[:20].sum() / tot)
    day1 = float(daily_abs.max() / day_tot) if day_tot else float("nan")
    nums.update(top1_share=top1, top5_share=top5, top20_share=top20,
                top1_day_share=day1,
                top_names=[[str(k), float(by_name[k])] for k in order.index[:5]],
                top_day=str(daily_abs.idxmax()) if day_tot else None)
    state = PASS if (top1 <= thr["conc_top1_max"] and top5 <= thr["conc_top5_max"]
                     and (not np.isfinite(day1) or day1 <= thr["conc_day1_max"])) else FAIL
    return _g("集中度", state, nums,
              f"阈值 top1≤{thr['conc_top1_max']} top5≤{thr['conc_top5_max']} 单日≤{thr['conc_day1_max']}")


def _gate_stability(daily, by_year, thr, ann):
    ret = daily["return"].to_numpy(dtype=float)
    h = len(ret) // 2
    s1, s2 = _sharpe(ret[:h], ann), _sharpe(ret[h:], ann)
    yearly = {y: v["sharpe"] for y, v in by_year.items()}
    roll = float("nan")
    if len(ret) >= ANN_DAYS + 1:
        s = pd.Series(ret)
        m, sd = s.rolling(ANN_DAYS).mean(), s.rolling(ANN_DAYS).std(ddof=1)
        roll = float((m / sd * math.sqrt(ann)).min())
    ratio = (s2 / s1) if (np.isfinite(s1) and s1 > 0) else float("nan")
    nums = {"sharpe_by_year": yearly, "sharpe_first_half": s1, "sharpe_second_half": s2,
            "half_ratio": ratio, "worst_rolling_1y_sharpe": roll,
            "worst_year": (min(yearly, key=lambda k: (yearly[k] if yearly[k] is not None
                           and np.isfinite(yearly[k]) else 9e9)) if yearly else None)}
    if not np.isfinite(roll):
        # 样本不足一年：不许因此报 PASS——那正是"空白在干净与没查之间有歧义"
        state = NOBASIS if not np.isfinite(ratio) else (
            PASS if ratio >= thr["stab_half_ratio_min"] else FAIL)
        return _g("区间稳定性", state, nums,
                  f"样本 {len(ret)} 天 < {ANN_DAYS + 1}，滚动 1 年 Sharpe 无从计算，只用上下半场比")
    state = PASS if (roll > thr["stab_roll1y_sharpe_min"] and
                     (not np.isfinite(ratio) or ratio >= thr["stab_half_ratio_min"])) else FAIL
    return _g("区间稳定性", state, nums,
              f"阈值 滚动1年 Sharpe>{thr['stab_roll1y_sharpe_min']}、下半场/上半场≥{thr['stab_half_ratio_min']}")


def _gate_breakeven(daily, thr):
    cost = float(daily["cost"].sum())
    net = float(daily["pnl"].sum())
    gross = net + cost
    nums = {"cost_total": cost, "pnl_gross": gross, "pnl_net": net, "breakeven_cost": None}
    if cost == 0:
        return _g("成本临界倍数", NOBASIS, nums,
                  "cost ≡ 0（未接成本模型）——本闸门此时无判据，不报 PASS")
    be = gross / cost
    nums["breakeven_cost"] = be
    return _g("成本临界倍数", PASS if be >= thr["breakeven_min"] else FAIL, nums,
              f"阈值 ≥{thr['breakeven_min']}x（量纲 = 成本模型可以错多少倍）")


def _gate_ls_balance(daily, thr):
    lv = float(daily["long_value"].mean())
    sv = -float(daily["short_value"].mean())
    nums = {"avg_long_value": lv, "avg_short_value": -sv,
            "avg_long_count": float(daily["long_count"].mean()),
            "avg_short_count": float(daily["short_count"].mean()),
            "long_short_ratio": (lv / sv) if sv else float("inf"),
            "net_exposure_frac_of_gross": ((lv - sv) / (lv + sv)) if (lv + sv) else float("nan")}
    if lv + sv == 0:
        return _g("多空平衡", NOBASIS, nums, "账本恒为空仓")
    if sv == 0:
        return _g("多空平衡", FAIL, nums, "纯多头账本（空头市值恒为 0）")
    r = lv / sv
    return _g("多空平衡", PASS if thr["ls_ratio_lo"] <= r <= thr["ls_ratio_hi"] else FAIL, nums,
              f"阈值 L/S ∈ [{thr['ls_ratio_lo']}, {thr['ls_ratio_hi']}]")


def _gate_pool(daily, sim_audit, thr):
    live = daily[daily["weight_gross"] > 0]
    gross_dev = float((live["weight_gross"] - 1.0).abs().max()) if len(live) else float("nan")
    oop = daily["oop_weight"]
    nums = {"empty_weight_days": int((daily["weight_gross"] == 0).sum()),
            "weight_gross_dev_max": gross_dev,
            "coverage_min": int(daily["target_count"].min()),
            "coverage_avg": float(daily["target_count"].mean()),
            "coverage_max": int(daily["target_count"].max()),
            "out_of_pool_weight_max": (float(oop.max()) if sim_audit["universe_supplied"] else None)}
    if not sim_audit["universe_supplied"]:
        return _g("池子卫生", NOBASIS, nums,
                  "未提供 universe 面板——§3.5 那道两端夹住的掩码是否生效**没有查过**")
    ok = (float(oop.max()) == 0.0) and np.isfinite(gross_dev) and gross_dev <= thr["pool_gross_tol"]
    return _g("池子卫生", PASS if ok else FAIL, nums,
              f"池外权重必须恰为 0；Σ|w| 与 1 的容差 {thr['pool_gross_tol']}")


def _gate_lookahead(sim_audit, meta):
    meta = meta or {}
    nums = {"ghost_detection": sim_audit["ghost_detection"],
            "ghost_days": sim_audit["ghost_days"],
            "delist_source": sim_audit["delist_source"],
            "delist_events": sim_audit["delist_events"],
            "deps_tc_resolved": meta.get("deps_tc"),
            "region_hash": meta.get("region_hash"),
            "region_hash_canonical": meta.get("region_hash_canonical"),
            "region_hash_match": (None if not (meta.get("region_hash") and
                                               meta.get("region_hash_canonical"))
                                  else meta["region_hash"] == meta["region_hash_canonical"])}
    bad = []
    if sim_audit["ghost_detection"] == "disabled":
        bad.append("ghost 检测被显式关闭")
    if sim_audit["delist_source"] == "none":
        bad.append("无 delist_date field：退市路径是死代码，delist_events 恒为 0")
    if nums["region_hash_match"] is False:
        bad.append("region_hash 不等于模板标准值（口径不可比）")
    unknown = [k for k in ("deps_tc_resolved", "region_hash") if nums[k] is None]
    if bad:
        return _g("前视状态", FAIL, nums, "；".join(bad))
    if unknown:
        return _g("前视状态", NOBASIS, nums, f"权重 meta 未提供 {unknown}，这几项没有查过")
    return _g("前视状态", PASS, nums, "")


def gates(res, *, thresholds: dict | None = None, market_ret=None,
          meta: dict | None = None, ann: int = ANN_DAYS) -> dict:
    """§15.9 的七道闸门。每道都返回 state + numbers，即使 PASS 也带数字。

    `NO-BASIS` 是与 PASS/FAIL 并列的第三态，专治"一道永远不会触发的告警"：
    没有判据时它必须显式说自己没查，绝不能悄悄算作通过。
    """
    thr = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    d, a = res.daily, res.audit
    by_year = _by_year(d, float(a["booksize"]), ann)
    gs = [
        _gate_beta(d, thr, market_ret, ann),
        _gate_concentration(res, thr),
        _gate_stability(d, by_year, thr, ann),
        _gate_breakeven(d, thr),
        _gate_ls_balance(d, thr),
        _gate_pool(d, a, thr),
        _gate_lookahead(a, meta),
    ]
    n_pass = sum(g["state"] == PASS for g in gs)
    return {"gates": gs, "n_pass": n_pass, "n_total": len(gs),
            "all_pass": n_pass == len(gs),
            "summary": f"submission readiness: {n_pass}/{len(gs)} gates pass"}


# ---------------------------------------------------------------- 对外主入口
def metrics(res, *, market_ret=None, meta: dict | None = None,
            thresholds: dict | None = None, ann: int = ANN_DAYS) -> dict:
    """四交付物 → metrics.json 的内容（§8.4 + §15.9）。返回值可直接 json.dump。"""
    d, a = res.daily, res.audit
    booksize = float(a["booksize"])
    out = {
        "scalar": _scalars(d, booksize, ann),
        "by_year": _by_year(d, booksize, ann),
        "audit": _audit(d, a),
        **gates(res, thresholds=thresholds, market_ret=market_ret, meta=meta, ann=ann),
        # 口径快照（§8.3）：换了任何一项，两份 metrics.json 就不可比
        "snapshot": {"booksize": booksize, "participation": a["participation"],
                     "sd": a["sd"], "ed": a["ed"], "n_sessions": a["n_sessions"],
                     "n_securities": a["n_securities"], "ann_days": ann,
                     "cost_model": a["cost_model"], "cost_bps_avg": a["cost_bps_avg"],
                     "adv_constrained": a["adv_constrained"],
                     "ghost_detection": a["ghost_detection"], "delist_source": a["delist_source"],
                     **{k: v for k, v in (meta or {}).items()}},
    }
    return _jsonable(out)


def format_report(m: dict) -> str:
    """`--pnl` 末尾那一屏。七道闸门逐条打印状态与数字，末行是 readiness。"""
    s, au = m["scalar"], m["audit"]
    L = []
    L.append(f"{'区间':<10}{m['snapshot']['sd']} → {m['snapshot']['ed']}  "
             f"{m['snapshot']['n_sessions']} sessions × {m['snapshot']['n_securities']} names  "
             f"booksize={m['snapshot']['booksize']:,.0f}")
    L.append(f"{'指标':<10}Sharpe {_fmt(s['sharpe'], '.3f')}   Ret {_fmt((s['ann_return'] or 0) * 100, '.2f')}%   "
             f"TO {_fmt((s['turnover'] or 0) * 100, '.2f')}%   Margin {_fmt(s['margin_bps'], '.2f')}bps   "
             f"Fitness {_fmt(s['fitness'], '.3f')}   MaxDD {_fmt((s['max_drawdown'] or 0) * 100, '.2f')}%")
    L.append(f"{'账本':<10}long {_fmt(s['avg_long_value'], ',.0f')}({_fmt(s['avg_long_count'], '.0f')})  "
             f"short {_fmt(s['avg_short_value'], ',.0f')}({_fmt(s['avg_short_count'], '.0f')})  "
             f"L/S {_fmt(s['long_short_ratio'], '.3f')}  成本吃掉毛利 {_fmt((s['cost_share_of_gross'] or 0) * 100, '.1f')}%")
    L.append(f"{'审计':<10}ghost_detection={au['ghost_detection']}  ghost_days={au['ghost_days']}  "
             f"delist_source={au['delist_source']}  delist_events={au['delist_events']}  "
             f"frozen_value_avg={_fmt(au['frozen_value_avg'], ',.0f')}  "
             f"realloc_TO={_fmt((au['realloc_turnover_avg'] or 0) * 100, '.3f')}%  "
             f"cash_avg={_fmt(au['cash_avg'], ',.0f')}")
    if m["by_year"]:
        L.append("分年度   " + "  ".join(
            f"{y}: Sh {_fmt(v['sharpe'], '.2f')} Ret {_fmt((v['ann_return'] or 0) * 100, '.1f')}% "
            f"({v['days']}d)" for y, v in sorted(m["by_year"].items())))
    L.append("")
    for i, g in enumerate(m["gates"], 1):
        nums = "  ".join(
            f"{k}={v if isinstance(v, (bool, str)) else _fmt(v, '.4g')}"
            for k, v in g["numbers"].items()
            if not isinstance(v, (list, dict)) and v is not None)
        L.append(f"[闸门 {i}/7] {g['gate']:<12} {g['state']:<8} {nums}")
        if g["note"]:
            L.append(f"{'':>13} └ {g['note']}")
    L.append("")
    L.append(m["summary"])
    return "\n".join(L)
