"""pnl 的入口层：把 L3 store 接到仿真器，落 §8.3 的四交付物。

权重文件是引擎与评估的正式接口（§八），故这里支持两个入口：
`--node REF` 直接读 store，`--weight FILE` 吃外来权重——两侧独立演进。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..core.store import Store, StoreError
from .metrics import metrics as compute_metrics
from .simulate import simulate

RET = "g_common.field_base_px.ret_1d_1500"
ADV = "g_common.field_base_px.adv_dollar"
MKT = "g_common.field_base_px.market_ret"


def _load_weights(store: Store, node: str | None, weight_file: str | None,
                  sd, ed) -> pd.DataFrame:
    if weight_file:
        # 外来权重按扩展名认格式: 交付物现在落 CSV, 但别人递过来的仍可能是 feather/parquet
        suf = Path(weight_file).suffix.lower()
        rd = {".csv": pd.read_csv, ".feather": pd.read_feather,
              ".parquet": pd.read_parquet}.get(suf)
        if rd is None:
            raise StoreError(f"unrecognised weight file format `{suf}`: {weight_file} (supported: .csv/.feather/.parquet)")
        w = rd(weight_file)
        return w.set_index(w.columns[0])
    return store.read(node, sd, ed)


def run_pnl(a) -> int:
    store = Store(a.store, a.region)
    node = getattr(a, "node", None)
    w = _load_weights(store, node, getattr(a, "weight", None), a.sd, a.ed)
    w = w.dropna(how="all")
    if w.empty:
        raise StoreError(f"{node}: weights are empty -- run that node first")
    sd, ed = w.index[0], w.index[-1]

    ret = store.read(a.rm, sd, ed)
    adv = store.read(ADV, sd, ed) if store.exists(ADV) else None
    mkt = store.read(MKT, sd, ed) if store.exists(MKT) else None

    # 本数据集没有 is_halted / delist_date（l2_schema §0.1）。§九 规定此时必须**显式降级
    # 或拒绝运行**，不允许把 ghost_days 记 0 继续跑——故这里把 halt_proxy 一路传下去，
    # 由 simulate 决定是降级还是拒绝。
    res = simulate(w, ret, booksize=a.booksize, adv_dollar=adv,
                   cost_bps=a.cost_bps, participation=a.participation,
                   halt_proxy=a.halt_proxy)

    name = node or Path(a.weight).stem
    out = Path(a.out) / name
    out.mkdir(parents=True, exist_ok=True)
    # 用 SimResult 自己的写法：手工 to_feather 会让整型 security_id 列名被
    # pyarrow 静默强转成字符串（只发一条 warning），读回来就对不上了
    res.write(out)

    m = compute_metrics(res, market_ret=mkt,
                        meta={"node": name, "return_metric": a.rm,
                              "booksize": a.booksize, "sd": str(sd), "ed": str(ed),
                              "cost_bps": a.cost_bps,
                              "participation": a.participation,
                              # 数据集的已知缺陷必须随指标一起走, 否则读报表的人
                              # 无从知道这些数字是在什么样的数据上算出来的
                              "known_defects": ["survivorship_bias_no_delisted",
                                                "no_vwap", "no_shares_outstanding",
                                                "equal_weighted_market_proxy"]})
    (out / "metrics.json").write_text(json.dumps(m, indent=1, ensure_ascii=False))

    _print(name, m, out)
    return 0


def _print(name: str, m: dict, out: Path) -> None:
    """控制台是这条命令的**主要**输出面。

    metrics.json 有 60 多个字段, 但决定"这个 alpha 还要不要继续做"的就那十来个。
    全打出来等于没打——真正的信息会淹在里面。所以这里挑三组: 收益 / 成本 / 风险,
    外加七道闸门的逐条判定。闸门通过也印数字（§15.9）: 空白绝不能在"干净"与
    "没查"之间有歧义。
    """
    sc, snap, au = m.get("scalar", {}), m.get("snapshot", {}), m.get("audit", {})
    W = 78
    print("\n" + "=" * W)
    print(f" {name}")
    print(f" {snap.get('sd','')} .. {snap.get('ed','')}   "
          f"{snap.get('n_sessions','?')} sessions × {snap.get('n_securities','?')} names   "
          f"book {_money(snap.get('booksize'))}")
    print("=" * W)

    rows = [
        ("Return", [("Sharpe", _num(sc.get("sharpe"))), ("AnnRet", _pct(sc.get("ann_return"))),
                  ("AnnRet$", _money(sc.get("ann_return_dollar")))]),
        ("",     [("Fitness", _num(sc.get("fitness"))), ("HitRate", _pct(sc.get("hit_rate"))),
                  ("DailyVol", _pct(sc.get("return_std_daily")))]),
        ("Cost", [("Turnover", _pct(sc.get("turnover"))), ("Margin", f"{_num(sc.get('margin_bps'))} bps"),
                  ("CostTotal", _money(sc.get("cost_total")))]),
        ("",     [("Cost/Gross", _pct(sc.get("cost_share_of_gross"))),
                  ("Gross", _money(sc.get("holding_pnl_total"))),
                  ("Net", _money(sc.get("pnl_total")))]),
        ("Risk", [("MaxDD", _pct(sc.get("max_drawdown"))),
                  ("MaxDD$", _money(sc.get("max_drawdown_dollar"))),
                  ("DDWindow", f"{sc.get('max_drawdown_from','?')}→{sc.get('max_drawdown_to','?')}")]),
        ("Book", [("Long", f"{_money(sc.get('avg_long_value'))} / {_num(sc.get('avg_long_count'))} names"),
                  ("Short", f"{_money(sc.get('avg_short_value'))} / {_num(sc.get('avg_short_count'))} names"),
                  ("L/S", _num(sc.get("long_short_ratio")))]),
    ]
    for head, cells in rows:
        print(" " + _pad(head, 7) + "".join(_pad(f"{k}={v}", 26) for k, v in cells))

    print("-" * W)
    print(f" Gates     {m.get('n_pass','?')}/{m.get('n_total','?')} passed")
    for g in m.get("gates", []):
        nums = "  ".join(f"{k}={_fmt(v)}" for k, v in list((g.get("numbers") or {}).items())[:3])
        print(f"   [{g.get('state','?'):<8}] {g.get('gate',''):<18} {nums}")
    print("-" * W)
    print(f" Audit   ghost_detection={au.get('ghost_detection')}  ghost_days={au.get('ghost_days')}  "
          f"delist_source={au.get('delist_source')}")
    kd = snap.get("known_defects") or []
    if kd:
        print(f" Defects {', '.join(kd)}")
    print(f" Verdict {m.get('summary','')}")
    print(f" Output  {out}/  →  daily.csv  pnl.csv  holding.csv  metrics.json")
    print("=" * W)


def _dw(s: str) -> int:
    """显示宽度：CJK 占两列。终端按列对齐, 按字符数 ljust 会歪掉一半。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, n: int) -> str:
    # 至少留一个空格: 内容超宽时列会挤在一起, 两个字段黏成一个词
    return s + " " * max(1, n - _dw(s))


def _num(v):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "n/a"
    return f"{v:,.4g}" if isinstance(v, float) else str(v)


def _pct(v):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "n/a"
    return f"{v * 100:.2f}%"


def _money(v):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "n/a"
    a = abs(v)
    for lim, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= lim:
            return f"${v / lim:,.2f}{suf}"
    return f"${v:,.0f}"


def _fmt(v):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "n/a"
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)
