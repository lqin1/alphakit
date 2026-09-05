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

from ..core.config import NodeSpec, Spec, is_wildcard
from ..core.freshness import effective_ed
from ..core.store import Store, StoreError
from .ctx import Ctx, PanelLoader, UniverseView


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"_node_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def warmup(declared: int, node: NodeSpec) -> int:
    """这个节点要预热多少天（§7.1）。

    两段**相加**, 不是取大的那个:

        declared      handle 自己要多少天先前历史（`ctx.win(ref, w)` 要 w-1 天）
        ops_lookback  算子链要多少天**有效的 handle 输出**（TS 串联时窗口相加）

    链吃的是 handle 的产出。要让链在第一个**请求日**就已填满缓冲, handle 必须在那
    之前 ops_lookback 天就在产出有效值——而 handle 本身又要 declared 天才有效。

    这里曾经写的是 `max()`: `lookback: 5` + `win(6)` + `linear_decay: 3` 的真实需求是
    5+2=7, max 给 5, 于是起跑后前 2 天的输出来自未填满的衰减缓冲。实测同一个 session
    与预热充足时相比 **501/503 只票全不一样, 最大差 0.11**, 且不报任何警——数值看着
    完全合理。取大的那个之所以诱人, 是因为两段各自都"够"; 但它们不是同一段时间。

    单独成函数而不是内联: 预热错了产出的是**看着合理的错数**, 这类东西必须能被
    一条不需要 store 的断言钉住。
    """
    from .ops import ops_lookback
    return declared + max((ops_lookback(o.ops) for o in node.outputs.values()), default=0)


def resolve_deps(store: Store, node: NodeSpec) -> list[str]:
    """通配在编译期展开为当时该节点的全部输出，展开清单进 meta（§3.2）。"""
    out: list[str] = []
    for d in node.deps:
        out.extend(store.expand(d) if is_wildcard(d) else [d])
    return out


def run_node(store: Store, spec: Spec, node: NodeSpec, sd: str, ed: str,
             *, rebuild: bool = False, probe: int | None = None,
             verbose: bool = True) -> dict:
    t0 = time.time()
    deps = resolve_deps(store, node)
    # 自引用节点必须把自己列进 deps（§7.2 第 1 条, 预检也是这么建议的）, 而冷启动时
    # 它自己当然还不存在——照着建议写反而会在这里被拒, 于是那条"合法写法"永远跑不起来。
    # 它由 PanelLoader 的 optional 分支承接: 缺数组时退化成只有当日回灌的空面板。
    own = {str(node.ref(k)) for k in node.outputs}
    for d in deps:
        if d in own:
            continue
        if not store.exists(d):
            raise StoreError(
                f"{node.name}: dependency {d} is not in the store.\n"
                f"  v0 does no graph analysis, so cross-config deps must be run first (§7.1). "
                f"`store status` lists what has landed.")

    i_sd, i_ed = store.axes.pos(sd), store.axes.pos(ed)
    has_ops = any(o.ops for o in node.outputs.values())
    lookback = warmup(spec.lookback, node)
    i_start = max(0, i_sd - lookback)
    load_sd, load_ed = store.axes.date(i_start), ed
    cols = store.axes.securities

    # 惰性面板：持有 loader, 首次 ctx.f/win 才读盘并对齐一次（§7.2 第 4 条）
    panels = {d: PanelLoader(store, d, load_sd, load_ed) for d in deps}
    universe = UniverseView(store, spec.universe, load_sd, load_ed, cols, i_start)
    if spec.universe and not store.exists(spec.universe):
        raise StoreError(f"{node.name}: universe {spec.universe} is not in the store")

    # 自引用：节点读自己昨天的输出是合法写法, 当日产出必须能回灌（§7.2 第 1 条）
    # 自己的输出：optional=True——首次运行时数组还不存在, 自引用写法不该因此崩掉
    own = {str(node.ref(k)): PanelLoader(store, str(node.ref(k)), load_sd, load_ed,
                                         optional=True, dims=list(o.dims))
           for k, o in node.outputs.items()}
    # **覆盖而不是 setdefault**: 自引用节点按 §7.2 与预检的建议会把自己写进 deps,
    # 于是 panels 里已经有一个**非 optional** 的 loader, setdefault 便什么也不做——
    # 那个带 optional 与当日回灌的 loader 被静默丢弃, 冷启动时首次触碰就是
    # KeyError('dims')。照着建议写反而跑不起来, 而建议本身是对的。
    panels.update(own)

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
        tag = f"probe({probe})" if probe is not None else "wrote"
        print(f"  {node.name:<34} {len(dates)} days (warmup {i_sd - i_start}) "
              f"{tag} {len(written)} outputs  {time.time()-t0:.2f}s")
    # 空账日必须随记录一起走。OpChain 把它们攒在 degenerate_scale 里, 注释写着
    # "供 runner 汇报", 但此前没有任何人读——一个只在第一天出声、之后只累计的告警,
    # 如果最终没人汇总, 等于整段区间只喊了一次。Σ|w|=0 的日子会被原样写进库,
    # 而 pnl 的 dropna 删不掉 0.0, 它们会被当成"收益恰好为零"算进 Sharpe。
    # 无 ops 的节点这里是个恒等 lambda, 没有 degenerate_scale
    # **只算落库区间内的**。预热段也会走完整条链, 那一段 Σ|w|=0 是正常的（缓冲还没
    # 填满、上游还没有值）, 而且它根本不落库——把它一起报出来, 那句"这些以 0.0 写进
    # 库、pnl 删不掉"就是假的, 而一个会喊狼来了的告警很快就没人看了。
    degenerate = sorted({t for c in chains.values()
                         for t in getattr(c, "degenerate_scale", ())
                         if t >= i_sd})
    return {"node": node.name, "written": written, "deps": deps,
            "seconds": round(time.time() - t0, 3),
            "warmup": lookback, "sd": sd, "ed": ed,
            "degenerate_days": len(degenerate),
            "degenerate_first": (store.axes.date(degenerate[0]) if degenerate else None),
            "rows": {k: len(v) for k, v in rows.items()}}


def _normalize(out, node: NodeSpec, ctx) -> dict:
    """裸值 → 单键；多输出已由 ctx.multi_outputs 的构造器保证齐全（§4.2）。"""
    if isinstance(out, dict):
        if len(node.outputs) < 2:
            raise ValueError(f"{node.name}: single-output node must return the value directly, not multi_outputs")
        return out
    if len(node.outputs) >= 2:
        raise ValueError(
            f"{node.name}: declares {len(node.outputs)} outputs, so it must use ctx.multi_outputs(...)")
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
    # 新鲜度上限要覆盖**所有真正会被读到的** L3, 否则「绝不静默算半截数据」就有洞:
    #   · 通配 dep 此前被整个丢掉（`if not is_wildcard(d)`）, 而 template 的缺省
    #     依赖恰恰就是通配形——最常见的那一类反而不设防;
    #   · universe 从来不是 dep（UniverseView 单独构造）, 也就从不参与封顶。
    #     它落后时新的几行是 fill_value=False, 掩码全 False, scale 记一笔退化,
    #     于是整天的权重是一排 0.0 写进库——pnl 的 dropna 删不掉 0.0, 那几天
    #     被当成"收益恰好为零"算进 Sharpe。
    alldeps: set[str] = set()
    for n in todo:
        for d in n.deps:
            alldeps.update(store.expand(d) if is_wildcard(d) else [d])
    if spec.return_metric:
        alldeps.add(spec.return_metric)
    if spec.universe:
        alldeps.add(spec.universe)
    ed = effective_ed(store, ed, {d for d in alldeps if store.exists(d)})
    if probe is not None:
        i = store.axes.pos(ed)
        sd = store.axes.date(max(0, i - probe))
    print(f"run {spec.path.parent.name}/{spec.path.name}  {sd}..{ed}"
          f"{'  [probe: store not written]' if probe is not None else ''}")
    return [run_node(store, spec, n, sd, ed, rebuild=rebuild, probe=probe) for n in todo]
