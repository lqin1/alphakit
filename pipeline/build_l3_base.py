#!/usr/bin/env python
"""从 L2 一次性生成 base 数据集的 L3（architecture.md §3.2 / §3.6）。

这一步属于 ingestion 管道而非引擎——v0 引擎只吃 L3、只吐 L3（§七 范围声明），
L2 → L3 的文件格式、路径模板与 vendor 容错三副担子都留在这里。

产出（region = us）：
  g_common.field_base_px.adj_close_1500   秩-2 f4   复权收盘
  g_common.field_base_px.volume_1500      秩-2 f4   原始成交股数
  g_common.field_base_px.ret_1d_1500      秩-2 f4   复权日收益
  g_common.field_base_px.adv_dollar       秩-2 f4   20 日平均成交额
  g_common.field_base_px.market_ret       秩-1 f4   等权市场收益
  g_common.factor_common_gics.sector       秩-2 i1   GICS sector 码
  g_common.field_common_univ.us_top400       秩-2 bool 按 ADV 排名的池子
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from alpha_kit.core.axes import Axes          # noqa: E402
from alpha_kit.core.store import Store        # noqa: E402

REPO = Path(__file__).resolve().parent.parent
L2 = REPO / "storage" / "data" / "base" / "l2" / "us"
L3 = REPO / "storage" / "l3"
REGION = "us"


def read_pipe(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, sep="|", dtype={"security_id": "int64"})


def load_l2():
    cal = pd.concat([read_pipe(Path(p)) for p in sorted(glob.glob(f"{L2}/calendar/*/calendar.*"))])
    cal = cal.sort_values("session").reset_index(drop=True)
    sm = read_pipe(Path(sorted(glob.glob(f"{L2}/sec_master/*/*/sec_master.*"))[-1]))
    # industry 是逐 session 的 PIT 表, 必须全读——只读最后一份会让此前所有日期空掉
    ind = pd.concat([read_pipe(Path(p)) for p in
                     sorted(glob.glob(f"{L2}/industry/*/*/industry.*"))], ignore_index=True)
    bars = pd.concat([read_pipe(Path(p)) for p in sorted(glob.glob(f"{L2}/pv/*/*/pv.*"))],
                     ignore_index=True)
    return cal, sm, ind, bars


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=L3)
    ap.add_argument("--top", type=int, default=400, help="universe = top N names by ADV")
    args = ap.parse_args()

    cal, sm, ind, bars = load_l2()
    dates = list(cal["date"])
    secs = sorted(sm["security_id"].unique().tolist())
    print(f"L2: {len(dates)} sessions {dates[0]}..{dates[-1]}, {len(secs)} securities, "
          f"{len(bars):,} bar rows")

    # ---- 全局轴：唯一真相源, 所有节点共享同一坐标系
    (args.out / REGION).mkdir(parents=True, exist_ok=True)
    Axes.create(args.out / REGION, dates, secs)   # 轴按 region 存
    store = Store(args.out, REGION)
    print(f"axes: di={store.axes.n_sessions} ii={store.axes.n_securities} "
          f"(reserved to {store.axes.allocated})")

    # ---- 面板化：一律铺在全局轴上, 无数据处 NaN (§3.3 稀疏免费)
    def panel(col, dtype="f4"):
        p = bars.pivot(index="date", columns="security_id", values=col)
        return p.reindex(index=dates, columns=secs).astype(dtype)

    adj_close = (panel("close") * panel("adj_factor")).astype("f4")
    volume = panel("volume")
    ret_1d = adj_close.pct_change().astype("f4")
    dollar = (panel("close") * volume).astype("f4")
    adv = dollar.rolling(20, min_periods=5).mean().astype("f4")
    # 等权市场收益：没有基准序列, 也没有股本做市值加权 —— 口径写进 meta, 不假装是指数
    market_ret = ret_1d.mean(axis=1, skipna=True).astype("f4")

    sector = (ind.pivot(index="date", columns="security_id", values="gics_sector_code")
                 .reindex(index=dates, columns=secs).ffill().fillna(0).astype("i1"))

    # universe: 按 ADV 排名取前 N, 缺 ADV 的不进池
    rank = adv.rank(axis=1, ascending=False, na_option="keep")
    univ = (rank <= args.top).fillna(False)
    univ = univ.where(adv.notna(), False).astype(bool)

    # ingestion 也要有指纹, 而且它比引擎更需要：这里改一行公式没有任何 yaml 会变,
    # 而 store 里的名字照旧——正是"同名不同定义"最容易发生的地方。
    # 指纹 = 本脚本字节 + L2 的 asof（adj_factor 是向后复权的, 换一次 L2 快照值就变）
    l2_meta = json.loads((L2 / "_meta.json").read_text())
    fp = "sha256:" + hashlib.sha256(
        Path(__file__).read_bytes() + l2_meta["asof"].encode()).hexdigest()[:16]
    common = dict(region=REGION, source="l2 base us", cutoff="1500",
                  l2_asof=l2_meta["asof"], builder="pipeline/build_l3_base.py")
    jobs = [
        ("g_common.field_base_px.adj_close_1500", adj_close, ["di", "ii"], "f4",
         {**common, "desc": "close × adj_factor"}),
        ("g_common.field_base_px.volume_1500", volume, ["di", "ii"], "f4",
         {**common, "desc": "raw share volume, not split-adjusted"}),
        ("g_common.field_base_px.ret_1d_1500", ret_1d, ["di", "ii"], "f4",
         {**common, "desc": "adjusted daily return"}),
        ("g_common.field_base_px.adv_dollar", adv, ["di", "ii"], "f4",
         {**common, "desc": "20-day average dollar volume"}),
        ("g_common.field_base_px.market_ret", market_ret, ["di"], "f4",
         {**common, "desc": "equal-weighted market return; no benchmark series or share count, so not cap-weighted",
          "caveat": "equal_weighted"}),
        ("g_common.factor_common_gics.sector", sector, ["di", "ii"], "i1",
         {**common, "desc": "official GICS sector codes 10..60; 0 = unknown",
          "caveat": "back-filled from the current snapshot; no historical sector changes"}),
        (f"g_common.field_common_univ.us_top{args.top}", univ, ["di", "ii"], "bool",
         {**common, "desc": f"top {args.top} by 20-day ADV"}),
    ]
    for ref, data, dims, dtype, meta in jobs:
        store.write(ref, data, dims=dims, dtype=dtype, meta=meta,
                    fingerprint=fp, rebuild=True)
        cov = (float(np.isfinite(data.to_numpy(dtype='f8')).mean()) if dims == ["di", "ii"]
               else float(np.isfinite(np.asarray(data, dtype='f8')).mean()))
        print(f"  {ref:<58} dims={dims} non-null {cov:6.1%}")

    print(f"\nL3 -> {args.out}/{REGION}/  {len(store.list_refs())} nodes total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
