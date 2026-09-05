"""三个可执行入口：run 算 / store 查 / pnl 评（architecture.md §十二）。"""
from __future__ import annotations

import argparse
import glob
import sys
import time
import warnings
from pathlib import Path

from .core.config import ConfigError, find_region, load_spec
from .core.store import Store, StoreError

DEFAULT_L3 = "storage/l3"


def _specs(path: str) -> list[Path]:
    """PATH 可以是节点目录、yaml、或 glob——扫描族因此能在一个进程里跑完。"""
    hits = [Path(p) for p in sorted(glob.glob(path))] or [Path(path)]
    out: list[Path] = []
    for h in hits:
        out.extend(sorted(h.glob("*.yaml")) if h.is_dir() else [h])
    if not out:
        raise SystemExit(f"no config matched: {path}")
    return out


def _preflight_summary(f: Path, spec, diags: list, ms: float) -> None:
    """恒印一行。没有诊断时它是「预检确实跑过了」的唯一证据，也是 §15.7 那句
    「catalog 查询，< 50 ms」的现场读数——承诺要能被当场读出来，否则它会悄悄失效。"""
    from .runner.preflight import n_errors
    e = n_errors(diags); w = len(diags) - e
    n_out = sum(len(n.outputs) for n in spec.nodes.values())
    n_dep = sum(len(n.deps) for n in spec.nodes.values())
    print(f"preflight {'FAIL' if e else ('WARN' if w else 'OK  ')}  {f}  "
          f"{len(spec.nodes)} nodes / {n_out} outputs / {n_dep} deps  "
          f"{e} error {w} warn  {ms:.1f} ms")


def _runtime_error(path, exc: BaseException, code: str = "RUNTIME"):
    """把一个异常折成一条诊断，位置指向**写错的那一行**。

    §4.2/§4.3 都把「错误发生在写错的那一行」当成契约的一部分，那么呈现也该落到那一行。
    但最深的那一帧常常在 pandas 里（形状不符是 pandas 先喊的），报它等于把人送去
    读一个与自己无关的库。故按优先级挑：**先挑节点自己目录下的帧**，退一步挑任何
    非第三方库的帧，实在没有才用最深的那一帧。
    """
    import traceback
    from .runner.preflight import ERROR, Diagnostic, _rel
    frames = traceback.extract_tb(exc.__traceback__)
    here = str(Path(path).parent.resolve())
    pick = None
    for want in (lambda fn: str(Path(fn).resolve()).startswith(here),
                 lambda fn: "site-packages" not in fn and "/lib/python" not in fn):
        pick = next((fr for fr in reversed(frames) if want(fr.filename)), None)
        if pick is not None:
            break
    pick = pick or (frames[-1] if frames else None)
    where = f"{_rel(pick.filename)}:{pick.lineno}" if pick else "-"
    return Diagnostic(ERROR, code, _rel(path), "-", where,
                      f"{type(exc).__name__}: {' '.join(str(exc).split())}")


def cmd_run(a) -> int:
    """§15.7：预检在**读任何数据之前**跑, 每一次 run 都跑。

    一个 config 出错不中断其余的——PATH 可以是 glob，一次能带十个 yaml（§十二），
    第一个里的一个 typo 不该让后面九个连检查都跑不到。
    """
    from .runner.node import run
    from .runner.preflight import config_error, n_errors, preflight, report
    store = Store(a.store, a.region)
    rc = 0
    for f in _specs(a.path):
        try:
            spec = load_spec(f)
        except (ConfigError, StoreError) as e:
            # 加载期就报的那几项（§4.4 的 ops 以 scale 收尾、§3.6 的 CS 算子仅秩-2）
            # 不在预检里重复实现, 只把出口并过来: 使用者要的是一个诊断面, 不是两套风格。
            report([config_error(f, e)]); rc = 1; continue

        t0 = time.perf_counter()
        diags = preflight(spec, store, sd=a.sd, ed=a.ed)
        ms = (time.perf_counter() - t0) * 1e3
        report(diags)
        _preflight_summary(f, spec, diags, ms)
        if n_errors(diags):
            rc = 1; continue        # 有 error 就不进引擎——不让人等到第 12 秒

        try:
            run(spec, store, a.sd or store.axes.sessions[0], a.ed,
                only=a.only, rebuild=a.rebuild, probe=a.probe)
        except (ConfigError, StoreError) as e:
            report([_runtime_error(f, e)]); rc = 1
    return rc


def cmd_store(a) -> int:
    store = Store(a.store, a.region)
    if a.action == "status":
        cat = store.catalog()
        if cat.empty:
            print("store is empty"); return 0
        cat["first"] = cat["first_session"].map(lambda i: store.axes.date(int(i)) if i is not None else "")
        cat["last"] = cat["last_session"].map(lambda i: store.axes.date(int(i)) if i is not None else "")
        cols = ["ref", "dims", "dtype", "version", "first", "last"]
        print(cat[cols].to_string(index=False))
        print(f"\naxes: {store.axes.n_sessions} sessions x {store.axes.n_securities} securities")
    elif a.action == "ls":
        for r in store.list_refs():
            print(r)
    elif a.action == "meta":
        import json
        print(json.dumps(store.meta(a.ref), indent=1, ensure_ascii=False, default=str))
    return 0


def cmd_pnl(a) -> int:
    from .pnl.report import run_pnl
    return run_pnl(a)


def _terse_warning(message, category, filename, lineno, line=None):  # noqa: ARG001
    """警告只留正文。

    默认格式会连源码那一行一起吐出来, 于是 `pnl` 的报表里夹着 `warnings.warn(`
    这种对使用者毫无意义的残片。警告本身是要留的（§九 的降级必须被看见）,
    但它该读起来像一句话。
    """
    return f"  warn  {' '.join(str(message).split())}\n"


def main(argv=None) -> int:
    warnings.formatwarning = _terse_warning
    # --store / --region 在顶层和子命令上都认。子命令那一份用 SUPPRESS 作缺省,
    # 不给时就不会往 namespace 里写, 顶层的值因而不被 None 覆盖——这是 argparse
    # 里最容易踩空的一处: 两处同名选项、后解析的那个会无条件盖掉先前的值。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store", default=argparse.SUPPRESS,
                        help="L3 root directory; defaults to the region's l3_root")
    common.add_argument("--region", default=argparse.SUPPRESS)

    ap = argparse.ArgumentParser(prog="alphakit")
    ap.add_argument("--store", default=None)
    ap.add_argument("--region", default="us")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", parents=[common],
                       help="the one execution entry point: data nodes and alphas take the same command")
    r.add_argument("path", help="node directory / yaml / glob")
    r.add_argument("--sd"); r.add_argument("--ed")
    r.add_argument("--only", help="run only the named node")
    r.add_argument("--probe", nargs="?", type=int, const=20, default=None,
                   help="trial-run the warmed tail; does not write the store")
    r.add_argument("--rebuild", action="store_true", help="full rebuild, bumping version")
    r.add_argument("--pnl", action="store_true")
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("store", parents=[common], help="query tool, not an executor")
    s.add_argument("action", choices=["status", "ls", "meta"])
    s.add_argument("ref", nargs="?")
    s.set_defaults(fn=cmd_store)

    p = sub.add_parser("pnl", parents=[common], help="weights -> metrics")
    p.add_argument("--node", default=None)
    p.add_argument("--sd"); p.add_argument("--ed")
    p.add_argument("--booksize", type=float, default=None)
    p.add_argument("--rm", default=None)
    p.add_argument("--cost-bps", dest="cost_bps", type=float, default=10.0,
                   help="flat bps cost model; §4.9.3 makes the cost model a versioned L3 field, "
                        "so a constant is only v0's honest approximation. Recorded in metrics.json")
    p.add_argument("--participation", type=float, default=None)
    p.add_argument("--halt-proxy", dest="halt_proxy", type=int, default=None,
                   help="explicit fallback when there is no is_halted field: K consecutive NaN return days count as halted (§9)")
    p.add_argument("--by", choices=["year", "month", "both", "none"], default="both",
                   help="period breakdown tables printed to the console; "
                        "metrics.json always carries both")
    p.add_argument("--weight", default=None, help="entry point for externally supplied weights")
    p.add_argument("--out", default=None, help="where the four deliverables land; defaults to the region's pnl_out")
    p.set_defaults(fn=cmd_pnl)

    a = ap.parse_args(argv)

    # 口径全部落在 region 上（§二）: L3 根、pnl 落地处、仿真参数。命令行显式给的优先,
    # region 只提供缺省。此前 pnl 完全不读 region, 于是 region 里写着 halt_proxy: 3
    # 却仍要在命令行上重敲一遍——同一个口径存在两处, 迟早对不上。
    repo = a.node.split(".")[0] if getattr(a, "node", None) else None
    try:
        rdoc, rhash, rfile = find_region(a.region, repo=repo)
    except ConfigError as e:
        print(f"error  {e}", file=sys.stderr)
        return 1
    a.store = a.store or rdoc.get("l3_root") or DEFAULT_L3
    if a.cmd == "pnl":
        sim = rdoc.get("sim") or {}
        if a.out is None:
            a.out = rdoc.get("pnl_out") or "pnl_out"
        if a.halt_proxy is None:
            a.halt_proxy = sim.get("halt_proxy")
        if a.participation is None:
            a.participation = float(sim.get("participation", 0.10))
        if a.booksize is None:
            a.booksize = float(rdoc.get("booksize") or 20e6)
        if a.rm is None:
            a.rm = rdoc.get("return_metric") or "g_common.field_base_px.ret_1d_1500"
        a.region_file = str(rfile) if rfile else None
        a.region_hash = rhash
    return a.fn(a)


def main_run(argv=None) -> int:
    """console script `run` —— 等价于 `alphakit run …`。"""
    return main(["run"] + list(sys.argv[1:] if argv is None else argv))


def main_pnl(argv=None) -> int:
    """console script `pnl` —— 等价于 `alphakit pnl …`。"""
    return main(["pnl"] + list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())
