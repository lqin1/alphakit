"""执行引擎主循环（architecture.md §7.2）。

三行无分支的内核：handle → mask(universe) → ops → 落库。
执行期不存在任何按 kind 的分支——差异全部退化为配置字段的取值。
"""
from __future__ import annotations

import importlib.util
import sys
import warnings
import time
from pathlib import Path

import numpy as np
import pandas as pd

from ..core.config import NodeSpec, Spec
from ..core.store import Store, StoreError
from .ctx import Ctx, PanelLoader, UniverseView


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"_node_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def effective_ed(store: Store, ed: str | None, deps: set[str]) -> str:
    """ed 的可用性由数据新鲜度决定：任一依赖最新 session 未落地则回退（§7.3）。

    绝不静默算半截数据。
    """
    last = store.axes.n_sessions - 1
    for d in deps:
        if store.exists(d):
            last = min(last, int(store.meta(d).get("last_session", last)))
    cap = store.axes.date(last)
    return cap if ed is None else min(ed, cap)


def resolve_deps(store: Store, node: NodeSpec) -> list[str]:
    """通配在编译期展开为当时该节点的全部输出，展开清单进 meta（§3.2）。"""
    out: list[str] = []
    for d in node.deps:
        out.extend(store.expand(d) if d.endswith("-*") else [d])
    return out


def run_node(store: Store, spec: Spec, node: NodeSpec, sd: str, ed: str,
             *, rebuild: bool = False, probe: int | None = None,
             verbose: bool = True) -> dict:
    t0 = time.time()
    deps = resolve_deps(store, node)
    for d in deps:
        if not store.exists(d):
            raise StoreError(
                f"{node.name}: 依赖 {d} 不在 store 里。\n"
                f"  v0 不做图分析，跨 config 的依赖必须先跑（§7.1）。`store status` 可查。")

    i_sd, i_ed = store.axes.pos(sd), store.axes.pos(ed)
    # 先建算子链：预热下限由链自己给（TS 算子串联时窗口相加, 且 n 日窗口只需 n-1 天
    # 先前历史）。这里曾经有一份独立实现, 与链的算法双向不一致——`delay:2 → decay:5`
    # 它给 5 而正确值是 6, 预热不足会让最初几天的输出来自未填满的缓冲, 数值看着合理却是错的。
    from .ops import ops_lookback
    has_ops = any(o.ops for o in node.outputs.values())
    lookback = max(spec.lookback,
                   max((ops_lookback(o.ops) for o in node.outputs.values()), default=0))
    i_start = max(0, i_sd - lookback)
    load_sd, load_ed = store.axes.date(i_start), ed
    cols = store.axes.securities

    # 惰性面板：持有 loader, 首次 ctx.f/win 才读盘并对齐一次（§7.2 第 4 条）
    panels = {d: PanelLoader(store, d, load_sd, load_ed) for d in deps}
    universe = UniverseView(store, spec.universe, load_sd, load_ed, cols, i_start)
    if spec.universe and not store.exists(spec.universe):
        raise StoreError(f"{node.name}: universe {spec.universe} 不在 store 里")

    # 自引用：节点读自己昨天的输出是合法写法, 当日产出必须能回灌（§7.2 第 1 条）
    # 自己的输出：optional=True——首次运行时数组还不存在, 自引用写法不该因此崩掉
    own = {str(node.ref(k)): PanelLoader(store, str(node.ref(k)), load_sd, load_ed,
                                         optional=True, dims=list(o.dims))
           for k, o in node.outputs.items()}
    for ref, p in own.items():
        panels.setdefault(ref, p)

    dates = store.axes.sessions[i_start:i_ed + 1]
    ctx = Ctx(store, panels, universe, node, i_start, cols, dates)
    mod = load_module(node.code)
    if hasattr(mod, "init"):
        mod.init(ctx)

    # OpChain 必须拿到池子：scale 后的池外归零与 CS 算子的 scope 都靠它（§7.2 第 3 条）。
    # 上面为算预热已经建过一次链, 这里带上池子重建——链持有 op-state, 不能复用那一份。
    if has_ops:
        from .ops import OpChain
        chains = {k: OpChain(o.ops, universe) for k, o in node.outputs.items()}
    else:
        chains = {k: (lambda v, t: v) for k in node.outputs}

    rows: dict[str, dict[int, object]] = {k: {} for k in node.outputs}
    last = None
    for t in range(i_start, i_ed + 1):
        # 预热段的产出只喂状态、不落库, 其告警也就没有收件人。首日窗口必然是空的,
        # 若照发不误, 每次运行都会响一条结构性告警——那会训练人忽略告警,
        # 正是 §九 那条教训的反面。故只对真正落库的日子放行告警。
        with warnings.catch_warnings():
            if t < i_sd:
                warnings.simplefilter("ignore", RuntimeWarning)
            ctx._advance(t)
            out = mod.handle(ctx)
            out = last if out is None else out          # None → 沿用昨日
            last = out
            for name, v in _normalize(out, node, ctx).items():
                o = node.outputs[name]
                if o.dims == ("di", "ii"):
                    v = v.where(universe.mask(t), np.nan)   # 闸门 1: ops 前池外 NaN
                    v = chains[name](v, t)                  # 闸门 2 在 scale 内
                elif o.dims == ("di", "ii", "ti"):
                    # 秩-3 的掩码沿 ti 广播：池外标的整个 (ti) 切片置 NaN（§3.6）。
                    # CS 类算子对秩-3 非法, 故这里只有闸门 1。
                    m = universe.mask(t).to_numpy()
                    v = np.where(m[:, None], np.asarray(v), np.nan)
                rows[name][t] = v
                panels[str(node.ref(name))].publish(t, _to_row(v, o, cols))

    written = {}
    for name, r in rows.items():
        o = node.outputs[name]
        keep = {t: v for t, v in r.items() if t >= i_sd}     # 预热段只喂状态, 不落库
        if probe is not None or not keep:
            continue
        df = _assemble(keep, o, store, cols)
        ref = str(node.ref(name))
        store.write(ref, df, dims=list(o.dims), dtype=o.dtype,
                    fingerprint=node.fingerprint(),
                    grid_len=(df.shape[2] if o.dims == ("di", "ii", "ti") else None),
                    rebuild=rebuild,
                    meta={"deps": deps,
                          # §4.11.5：`write` 会 bump version 但**原地重写字节**, 所以
                          # "修数发新版本、无覆盖"对节点数据并不成立。补法在产物侧——
                          # 记下每个依赖当时的 version 与 last_session, 否则半年后
                          # 这份数据说不出它当时看到的是哪一版上游。
                          "deps_versions": {d: store.meta(d).get("version")
                                            for d in deps if store.exists(d)},
                          "region": spec.region, "region_hash": spec.region_hash,
                          "cutoff": spec.cutoff, "universe": spec.universe,
                          "node": node.name, "node_dir": node.node_dir, "repo": node.repo,
                          "params": node.params, "sibling_outputs": sorted(node.outputs),
                          "code_ref": {"path": str(node.code.relative_to(Path.cwd()))
                                       if node.code.is_relative_to(Path.cwd()) else str(node.code)},
                          "lookback": lookback})
        written[ref] = len(keep)
    if verbose:
        tag = f"probe({probe})" if probe is not None else "写入"
        print(f"  {node.name:<34} {len(dates)} 日 (预热 {i_sd - i_start}) "
              f"{tag} {len(written)} 输出  {time.time()-t0:.2f}s")
    return {"node": node.name, "written": written, "deps": deps,
            "seconds": round(time.time() - t0, 3),
            "rows": {k: len(v) for k, v in rows.items()}}


def _normalize(out, node: NodeSpec, ctx) -> dict:
    """裸值 → 单键；多输出已由 ctx.multi_outputs 的构造器保证齐全（§4.2）。"""
    if isinstance(out, dict):
        if len(node.outputs) < 2:
            raise ValueError(f"{node.name}: 单输出请直接 return 值，不要用 multi_outputs")
        return out
    if len(node.outputs) >= 2:
        raise ValueError(
            f"{node.name}: 声明了 {len(node.outputs)} 个输出，必须用 ctx.multi_outputs(...)")
    (key, o), = node.outputs.items()
    return {key: ctx._coerce(out, o)}


def _to_row(v, o, cols):
    if o.dims == ("di",):
        return float(v)
    if o.dims == ("di", "ii"):
        return np.asarray(v.reindex(cols), dtype="f4")
    return np.asarray(v, dtype="f4")


def _assemble(keep: dict[int, object], o, store: Store, cols):
    ts = sorted(keep)
    idx = [store.axes.date(t) for t in ts]
    if o.dims == ("di",):
        return pd.Series([float(keep[t]) for t in ts], index=idx)
    if o.dims == ("di", "ii"):
        return pd.DataFrame(np.vstack([np.asarray(keep[t].reindex(cols)) for t in ts]),
                            index=idx, columns=cols)
    arr = np.stack([np.asarray(keep[t]) for t in ts])
    return arr


def run(spec: Spec, store: Store, sd: str, ed: str | None, *, only: str | None = None,
        rebuild: bool = False, probe: int | None = None) -> list[dict]:
    todo = [spec.nodes[only]] if only else list(spec.nodes.values())
    alldeps = {d for n in todo for d in n.deps if not d.endswith("-*")}
    if spec.return_metric:
        alldeps.add(spec.return_metric)
    ed = effective_ed(store, ed, alldeps)
    if probe is not None:
        i = store.axes.pos(ed)
        sd = store.axes.date(max(0, i - probe))
    print(f"run {spec.path.parent.name}/{spec.path.name}  {sd}..{ed}"
          f"{'  [probe 不写 store]' if probe is not None else ''}")
    return [run_node(store, spec, n, sd, ed, rebuild=rebuild, probe=probe) for n in todo]
