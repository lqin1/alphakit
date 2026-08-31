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

RET = "g_common.field_base_px.field_base_px-ret_1d_1500"
ADV = "g_common.field_base_px.field_base_px-adv_dollar"
MKT = "g_common.field_base_px.field_base_px-market_ret"


def _load_weights(store: Store, node: str | None, weight_file: str | None,
                  sd, ed) -> pd.DataFrame:
    if weight_file:
        w = pd.read_feather(weight_file)
        return w.set_index(w.columns[0])
    return store.read(node, sd, ed)


def run_pnl(a) -> int:
    store = Store(a.store, a.region)
    node = getattr(a, "node", None)
    w = _load_weights(store, node, getattr(a, "weight", None), a.sd, a.ed)
    w = w.dropna(how="all")
    if w.empty:
        raise StoreError(f"{node}: 权重为空——先 run 该节点")
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
    sc, snap = m.get("scalar", {}), m.get("snapshot", {})
    print(f"\n{name}   {snap.get('sd','')}..{snap.get('ed','')}  "
          f"({snap.get('n_sessions','?')} sessions × {snap.get('n_securities','?')} names)")
    row = [("Sharpe", "sharpe"), ("Ann.Ret", "ann_return"), ("Turnover", "turnover"),
           ("Margin(bps)", "margin_bps"), ("Fitness", "fitness"), ("MaxDD", "max_drawdown")]
    print("  " + "  ".join(f"{lbl}={_fmt(sc.get(k))}" for lbl, k in row))

    au = m.get("audit", {})
    print(f"  ghost_detection={au.get('ghost_detection')}  ghost_days={au.get('ghost_days')}  "
          f"delist_source={au.get('delist_source')}")

    # 七道闸门：通过也打印数字。空白绝不能在"干净"与"没查"之间有歧义（§15.9）
    for g in m.get("gates", []):
        nums = "  ".join(f"{k}={_fmt(v)}" for k, v in list((g.get("numbers") or {}).items())[:3])
        print(f"  [{g.get('state','?'):<8}] {g.get('gate',''):<14} {nums}")
    print(f"  {m.get('summary','')}")
    print(f"  四交付物 → {out}/")


def _fmt(v):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "n/a"
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)
