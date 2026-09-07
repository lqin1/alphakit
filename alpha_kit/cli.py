"""三个可执行入口：run 算 / store 查 / pnl 评（architecture.md §十二）。"""
from __future__ import annotations

import argparse
import glob
import json
import sys
import time
import warnings
from pathlib import Path

from .core import project
from .core.axes import StoreMissing as _StoreMissing
from .pnl.simulate import SimError
from .core.config import ConfigError, find_region, load_spec
from .core.store import Store, StoreError

DEFAULT_L3 = "storage/l3"


def _specs(path: str) -> list[Path]:
    """PATH 可以是节点目录、yaml、或 glob——扫描族因此能在一个进程里跑完。"""
    hits = [Path(p) for p in sorted(glob.glob(path))] or [Path(path)]
    out: list[Path] = []
    for h in hits:
        # 排除隐藏文件: macOS 在 exFAT/SMB 上会生成 `._mom.yaml` 这类 AppleDouble
        # 伴生文件, pathlib 的 glob 会把它们一并匹配进来当成 spec 喂给 load_spec。
        out.extend(sorted(q for q in h.glob("*.yaml") if not q.name.startswith("."))
                   if h.is_dir() else [h])
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
    records: list[dict] = []
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
            recs = run(spec, store, a.sd or store.axes.sessions[0], a.ed,
                       only=a.only, rebuild=a.rebuild, probe=a.probe)
            records.extend(recs)
            rc = _report_degenerate(recs) or rc
            if not a.no_pnl and a.probe is None:
                rc = _evaluate_alphas(spec, store, a, recs) or rc
        except (ConfigError, StoreError, ValueError, KeyError, TypeError) as e:
            # ConfigError 是 ValueError 的子类, 但引擎运行期抛的多数是**裸**的
            # ValueError/KeyError/TypeError: ctx._coerce 的形状不符（作者最常犯的
            # 那个错）、multi_outputs 的键不对、OpChain 的游标跳变、轴上没有这一天。
            # 之前它们一个都不在这个 except 里, 于是 _runtime_error 那 20 行"把位置
            # 指回作者自己那一行"从不执行, 而且异常直接冲出 for 循环——glob 里后面
            # 九个 yaml 连预检都跑不到, 与 cmd_run 开头写明的契约相反。
            report([_runtime_error(f, e)]); rc = 1
    if getattr(a, "record", None):
        Path(a.record).parent.mkdir(parents=True, exist_ok=True)
        Path(a.record).write_text(json.dumps(
            {"argv": sys.argv[1:], "region": a.region,
             # 存相对项目根的路径: 记录是要被提交/传阅的, 不该带上产出它的那台机器
             # 的目录布局（此前 anchor 之后这里成了 /Users/xxx/... 的绝对路径）。
             "store": _portable(a.store),
             "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "rc": rc, "nodes": records}, indent=1, ensure_ascii=False))
        print(f"  run record -> {a.record}")
    return rc


def _evaluate_alphas(spec, store, a, recs: list[dict]) -> int:
    """`run` 之后顺手评估本次跑过的每个 alpha。

    算完一个 alpha 紧接着就想看指标, 是研究期最短的那个回路; 让人再敲一条 `pnl --node
    <60 字符的 ref>` 只是把 ref 抄一遍。所以这是**缺省行为**, `--no-pnl` 关掉。

    只评 alpha: 数据节点与因子没有权重可仿真。`--probe` 下不评——那一趟本来就不落库,
    评的会是上一次的权重, 比不评更容易骗人。
    """
    from .pnl.report import evaluate
    done = {r["node"] for r in recs}
    rc = 0
    for name, node in spec.nodes.items():
        if node.kind != "alpha" or name not in done:
            continue
        for key in node.outputs:
            ref = str(node.ref(key))
            if not store.exists(ref):
                continue
            # config 可以覆盖 region 的口径（§4.1）——`booksize` / `sim.*` /
            # `return_metric` 此前被 load_spec 解析出来却没有任何人读, 于是
            # 「alpha 可覆盖其中 booksize / sim.*」这句承诺在三处文档里写着而实际不生效:
            # 写了 booksize: 50000000 的人拿到的仍是 region 的 20M, 一声不吭。
            sim = spec.sim or {}
            try:
                evaluate(store, node=ref, sd=a.sd, ed=a.ed,
                         booksize=float(spec.booksize) if spec.booksize else a.booksize,
                         rm=spec.return_metric or a.rm,
                         cost_bps=a.cost_bps,
                         participation=float(sim.get("participation", a.participation)),
                         halt_proxy=sim.get("halt_proxy", a.halt_proxy),
                         out=a.out, by=a.by, region_hash=getattr(a, "region_hash", None))
            except (StoreError, SimError) as e:
                # 评估失败不该把 run 的成果一起判负: 数已经算出来并落库了。
                print(f"  warn  {ref}: evaluation skipped -- {e}", file=sys.stderr)
                rc = rc or 0
    return rc


def _report_degenerate(recs: list[dict]) -> int:
    """空账日必须在**运行结束时**再说一次, 而不是只在第一天喊一声。

    OpChain 只对第一个 Σ|w|=0 的日子发告警, 之后只累计——那条注释写着"供 runner
    汇报", 但此前没有任何人读它。一次跑几百天、前面几十天全是空账的情形, 使用者
    只会看到一行早已滚出屏幕的 warning, 然后把一串 0.0 权重当成正常结果落进库,
    而 pnl 的 dropna 删不掉 0.0——那些天会被当作"收益恰好为零"算进 Sharpe。
    """
    bad = [r for r in recs if r.get("degenerate_days")]
    for r in bad:
        print(f"  warn  {r['node']}: {r['degenerate_days']} day(s) produced an empty book "
              f"(Sigma|w|=0), first {r['degenerate_first']} -- these are written as 0.0 and "
              f"pnl cannot drop them")
    return 0                       # 不改退出码: 空账未必是错, 但绝不能不出声


def cmd_store(a) -> int:
    store = Store(a.store, a.region)
    if a.action == "status":
        cat = store.catalog()
        if a.ref:                      # 此前这个参数被收下然后丢掉
            cat = cat[cat["ref"].str.startswith(a.ref)]
            if cat.empty:
                print(f"no ref starts with {a.ref!r}"); return 1
        if cat.empty:
            # 一定要说清是**哪个**库空的。此前只印 "store is empty" 加退出码 0——
            # 从一个碰巧也有 storage/l3/us 的目录下跑, 使用者会以为数据没 clone 下来,
            # 然后一头扎进 pipeline 去重造一份本来就在的数据。
            print(f"store is empty: {store.root / store.region}")
            return 0
        cat["first"] = cat["first_session"].map(lambda i: store.axes.date(int(i)) if i is not None else "")
        cat["last"] = cat["last_session"].map(lambda i: store.axes.date(int(i)) if i is not None else "")
        cols = ["ref", "dims", "dtype", "version", "first", "last"]
        print(cat[cols].to_string(index=False))
        print(f"\nstore: {store.root / store.region}")
        print(f"axes:  {store.axes.n_sessions} sessions x {store.axes.n_securities} securities")
    elif a.action == "ls":
        hits = [r for r in store.list_refs() if not a.ref or r.startswith(a.ref)]
        if not hits:
            print(f"no ref starts with {a.ref!r}"); return 1
        for r in hits:
            print(r)
    elif a.action == "meta":
        if not a.ref:
            print("store meta needs a ref", file=sys.stderr); return 1
        if not store.exists(a.ref):
            # 这条命令的全部工作就是"按名字查一个 ref", 打错名字正是它最该帮上忙的时候。
            # 此前它把 zarr 的 FileNotFoundError 连同五层库内栈一起漏出来, 而编辑距离
            # 建议的机器就在 preflight 里现成放着。
            import difflib
            near = difflib.get_close_matches(a.ref, store.list_refs(), 3, cutoff=0.5)
            print(f"error  no such ref: {a.ref}"
                  + (f"\n  did you mean: {', '.join(near)}" if near else
                     "\n  `ak store ls` lists everything that has landed"), file=sys.stderr)
            return 1
        print(json.dumps(store.meta(a.ref), indent=1, ensure_ascii=False, default=str))
    return 0


def cmd_pnl(a) -> int:
    from .pnl.report import run_pnl
    return run_pnl(a)


def _portable(p) -> str:
    """把路径写成相对项目根的形式——记录要能在别的机器上读懂。"""
    root = project.find_root()
    q = Path(p)
    try:
        return str(q.resolve().relative_to(root)) if root else str(q)
    except ValueError:
        return str(q)


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
    r.add_argument("--no-pnl", dest="no_pnl", action="store_true",
                   help="skip the automatic evaluation of alpha nodes")
    r.add_argument("--by", choices=["year", "month", "both", "none"], default="both",
                   help="period breakdown tables in the automatic evaluation")
    r.add_argument("--record", default=None,
                   help="write a machine-readable run record (JSON) to this path")
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("store", parents=[common], help="query tool, not an executor")
    s.add_argument("action", choices=["status", "ls", "meta"])
    s.add_argument("ref", nargs="?", help="prefix filter for status/ls; exact ref for meta")
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
    # 只钉**缺省值**, 不钉使用者亲手敲的。region 里的 `storage/l3/us` 是相对仓库说的,
    # 必须钉到根; 而 `--store ./scratch` 里的 `./` 是使用者相对**自己**说的, 钉走它就
    # 违背了 shell 的常识——同一个字符串在 tab 补全和在这里指向两个地方。
    # 三个路径选项此前三种规则（store 一律钉、out 只钉缺省、record 从不钉）, 现在一致:
    # 缺省钉根, 显式给的照 cwd 解析。
    if rfile is None:
        # 静默退回缺省是最贵的一种失败: booksize / participation / return_metric 会换成
        # 硬编码常量（今天恰好与 yaml 相等, 改了 yaml 就不等了）, halt_proxy 变 None,
        # region_hash 变 None 于是 gate 7 永远 NO-BASIS——而这一切都不会说一个字。
        print(f"warn  no regions/{a.region}.yaml found from {project.find_root() or Path.cwd()}"
              f" -- falling back to built-in defaults; booksize/participation/halt_proxy/"
              f"return_metric are NOT the ones your region file declares", file=sys.stderr)
    a.store = a.store if a.store else str(project.anchor(rdoc.get("l3_root") or DEFAULT_L3))
    if a.cmd in ("pnl", "run"):
        # `run` 缺省也要评估 alpha, 所以这套口径两条命令都要摊平——它们本来就该来自
        # 同一个 region, 让 run 走一套缺省、pnl 走另一套, 同一个 alpha 会给出两个数。
        sim = rdoc.get("sim") or {}
        for k, v in (("out", None), ("halt_proxy", None), ("participation", None),
                     ("booksize", None), ("rm", None), ("cost_bps", 10.0), ("by", "both")):
            if not hasattr(a, k):
                setattr(a, k, v)
        if a.out is None:
            a.out = str(project.anchor(rdoc.get("pnl_out") or "pnl_out"))
        if a.halt_proxy is None:
            a.halt_proxy = sim.get("halt_proxy")
        if a.participation is None:
            a.participation = float(sim.get("participation", 0.10))
        if a.booksize is None:
            a.booksize = float(rdoc.get("booksize") or 20e6)
        if a.rm is None:
            a.rm = rdoc.get("return_metric") or "g_common.field_base_px.ret_1d_1500"
        a.region_hash = rhash
    try:
        return a.fn(a)
    except (StoreError, _StoreMissing, SimError) as e:
        # 只兜"库不在/轴打不开"这一类环境失败, 让它以一句话而不是 traceback 呈现——
        # 这里的读者是"我刚 clone 下来, 它没跑起来"。
        # **不能兜整个 FileNotFoundError**: 节点自己 `open("x.csv")` 失败也是它,
        # 那样会绕过 _runtime_error 的定位、并中断 glob 里后面的 yaml, 与 cmd_run
        # 开头写明的契约相反。
        print(f"error  {e}", file=sys.stderr)
        return 1


def main_run(argv=None) -> int:
    """console script `run` —— 等价于 `alphakit run …`。"""
    return main(["run"] + list(sys.argv[1:] if argv is None else argv))


def main_pnl(argv=None) -> int:
    """console script `pnl` —— 等价于 `alphakit pnl …`。"""
    return main(["pnl"] + list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())
