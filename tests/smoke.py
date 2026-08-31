#!/usr/bin/env python
"""端到端自检：新机器装完之后跑这一条, 确认整条链是通的。

    python tests/smoke.py                      # 失败则退出码非 0

不替代 validate_l2.py（那是数据的验收闸门）——这里查的是**引擎**：
轴 / store 读写 / 配置与命名检查 / 逐日主循环 / 预热 / 自引用回灌 / 落库路径。
任一条不过即非零退出。

住在 tests/ 而不是 pipeline/：pipeline 的职责是 L1→L2→L3 摄入, 本文件一行摄入代码都没有,
查的全是引擎。放错目录的测试没人会想起来跑。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
# 子进程用「跑本文件的那个解释器」, 而不是写死 .venv/bin/python:
# 装没装 alpha_kit 都能跑, 且 run_all.py 转发下来的解释器与这里一致。
PY_ = sys.executable

ok = fail = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}" + (f"  {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  [FAIL] {name}  {detail}")


def main() -> int:
    from alpha_kit.core.store import Store
    from alpha_kit.core.config import load_spec, ConfigError, parse_ref

    print("=== 1. 依赖 ===")
    for m in ("pandas", "numpy", "zarr", "yaml"):
        try:
            __import__(m); check(f"import {m}", True)
        except ImportError as e:
            check(f"import {m}", False, str(e))

    print("=== 2. L3 store 与全局轴 ===")
    s = Store(REPO / "storage" / "l3", "us")
    check("轴已建立", s.axes.n_sessions > 0 and s.axes.n_securities > 0,
          f"{s.axes.n_sessions} sessions × {s.axes.n_securities} securities")
    base = [r for r in s.list_refs() if r.startswith("g_common.")]
    check("base 节点齐全", len(base) >= 7, f"{len(base)} 个")
    px = s.read("g_common.field_base_px.field_base_px-adj_close_1500")
    check("秩-2 读回对齐到全局轴", px.shape == (s.axes.n_sessions, s.axes.n_securities),
          f"{px.shape}")
    mr = s.read("g_common.field_base_px.field_base_px-market_ret")
    check("秩-1 读回是 Series", mr.ndim == 1, f"{mr.shape}")
    u = s.read("g_common.field_common_univ.field_common_univ-us_top400")
    check("bool universe", u.dtypes.iloc[0] == bool, f"日均成员 {int(u.sum(axis=1).mean())}")
    check("区间读", s.read("g_common.field_base_px.field_base_px-ret_1d_1500",
                          s.axes.date(-6), s.axes.date(-1)).shape[0] == 6)
    check("通配展开", len(s.expand("g_common.field_base_px.field_base_px-*")) == 5)

    print("=== 3. 命名与编译期检查 ===")
    r = parse_ref("g_yliu.alpha_yliu_rev.alpha_yliu_rev_w005-weight")
    check("引用名往返", str(r) == "g_yliu.alpha_yliu_rev.alpha_yliu_rev_w005-weight" and
          r.kind == "alpha" and r.ns == "yliu")
    for bad, why in [("field.base.x", "旧三段式"), ("g_yliu.rev.rev_w005-weight", "缺 kind 前缀")]:
        try:
            parse_ref(bad); check(f"拒绝 {why}", False, f"却接受了 {bad}")
        except ConfigError:
            check(f"拒绝 {why}", True)

    print("=== 4. 三个例子 ===")
    steps = [("例5 因子", "repos/g_yliu/nodes/factor_yliu_liq/", "2025-10-01"),
             ("他人 alpha", "repos/g_lqin/nodes/alpha_lqin_senti/", "2025-12-01"),
             ("例6 alpha", "repos/g_yliu/nodes/alpha_yliu_rev/rev.yaml", "2025-12-01"),
             ("例7 combo", "repos/g_yliu/nodes/alpha_yliu_rev/rev_mix.yaml", "2025-12-01")]
    for name, path, sd in steps:
        p = subprocess.run([PY_, "-m", "alpha_kit.cli", "run", path, "--sd", sd],
                           cwd=REPO, capture_output=True, text=True)
        # 诊断走 stdout（linter 惯例, 便于 grep）, 异常走 stderr——两边都要看,
        # 只取 stderr 会在诊断路径上拿到空列表并 IndexError, 把一次失败变成一次崩溃。
        out = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
        check(name, p.returncode == 0, (out or [""])[-1][:130])

    print("=== 5. 产出正确性 ===")
    refs = s.list_refs()
    for want in ["g_yliu.factor_yliu_liq.factor_yliu_liq-adv20",
                 "g_yliu.alpha_yliu_rev.alpha_yliu_rev_w005-weight",
                 "g_yliu.alpha_yliu_rev.alpha_yliu_rev_mix-weight"]:
        check(f"落库 {want.split('.')[-1]}", want in refs)
    if "g_yliu.factor_yliu_liq.factor_yliu_liq-adv20" in refs:
        vol = s.read("g_common.field_base_px.field_base_px-volume_1500")
        adv = s.read("g_yliu.factor_yliu_liq.factor_yliu_liq-adv20")
        d = adv.dropna(how="all").index[-1]; i = list(px.index).index(d)
        ref_ = (px.iloc[i - 19:i + 1] * vol.iloc[i - 19:i + 1]).mean()
        got = adv.loc[d]; both = ref_.notna() & got.notna()
        err = float(((got[both] - ref_[both]).abs() / ref_[both].abs()).max())
        check("adv20 与独立重算一致", err < 1e-5, f"最大相对误差 {err:.1e}")
    w = "g_yliu.alpha_yliu_rev.alpha_yliu_rev_mix-weight"
    if w in refs:
        a = s.read(w); row = a.dropna(how="all").iloc[-1]
        gross = float(row.abs().sum())
        check("combo Σ|w| = 1（scale 生效）", abs(gross - 1.0) < 1e-3, f"Σ|w| = {gross:.6f}")
        pool = s.read("g_common.field_common_univ.field_common_univ-us_top400").loc[row.name]
        outside = float(row[~pool].abs().sum())
        check("池外权重恰为 0（掩码第二道闸门）", outside == 0.0, f"池外 Σ|w| = {outside:g}")

    print("=== 6. pnl 四交付物与七道闸门 ===")
    r = subprocess.run([PY_, "-m", "alpha_kit.cli", "pnl",
                        "--node", "g_yliu.alpha_yliu_rev.alpha_yliu_rev_mix-weight",
                        "--halt-proxy", "3"], cwd=REPO, capture_output=True, text=True)
    check("pnl 跑通", r.returncode == 0,
          "" if r.returncode == 0 else r.stderr.strip().splitlines()[-1][:110])
    d = REPO / "pnl_out" / "g_yliu.alpha_yliu_rev.alpha_yliu_rev_mix-weight"
    for f in ("holding.feather", "pnl.feather", "daily.feather", "metrics.json"):
        check(f"交付物 {f}", (d / f).exists())
    if (d / "metrics.json").exists():
        import json
        m = json.loads((d / "metrics.json").read_text())
        check("七道闸门都出结果", len(m.get("gates", [])) == 7,
              f"{m.get('n_pass')}/{m.get('n_total')} PASS")
        au = m.get("audit", {})
        # §九 的教训：防线的实际状态必须在报表上可见, 否则读的人分不清
        # "没有幽灵持仓" 与 "根本没在查"
        check("防线状态可见", au.get("ghost_detection") is not None
              and au.get("delist_source") is not None,
              f"ghost_detection={au.get('ghost_detection')} delist_source={au.get('delist_source')}")
        gross_dev = next((g["numbers"].get("weight_gross_dev_max")
                          for g in m["gates"] if "numbers" in g
                          and "weight_gross_dev_max" in g["numbers"]), None)
        if gross_dev is not None:
            check("权重 Σ|w| 恒为 1", gross_dev < 1e-6, f"最大偏差 {gross_dev:.1e}")

    print(f"\n{'='*54}\n通过 {ok} / 失败 {fail}\n{'='*54}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
