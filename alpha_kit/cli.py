"""三个可执行入口：run 算 / store 查 / pnl 评（architecture.md §十二）。"""
from __future__ import annotations

import argparse
import glob
import sys
import time
from pathlib import Path

from .core.config import ConfigError, load_spec
from .core.store import Store, StoreError

DEFAULT_L3 = "storage/l3"


def _specs(path: str) -> list[Path]:
    """PATH 可以是节点目录、yaml、或 glob——扫描族因此能在一个进程里跑完。"""
    hits = [Path(p) for p in sorted(glob.glob(path))] or [Path(path)]
    out: list[Path] = []
    for h in hits:
        out.extend(sorted(h.glob("*.yaml")) if h.is_dir() else [h])
    if not out:
        raise SystemExit(f"没有找到 config：{path}")
    return out


def _preflight_summary(f: Path, spec, diags: list, ms: float) -> None:
    """恒印一行。没有诊断时它是「预检确实跑过了」的唯一证据，也是 §15.7 那句
    「catalog 查询，< 50 ms」的现场读数——承诺要能被当场读出来，否则它会悄悄失效。"""
    from .runner.preflight import n_errors
    e = n_errors(diags); w = len(diags) - e
    n_out = sum(len(n.outputs) for n in spec.nodes.values())
    n_dep = sum(len(n.deps) for n in spec.nodes.values())
    print(f"preflight {'FAIL' if e else ('WARN' if w else 'OK  ')}  {f}  "
          f"{len(spec.nodes)} 节点 / {n_out} 输出 / {n_dep} 依赖  "
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


def _dry_run(spec, store, a) -> list:
    """§15.7 的 `--dry-run`：预检之后，在**一个** session 上执行 handle，不预热。

    元数据答不了的那一半问题在这里露头——handle 当日返回的形状、单/多输出写法、
    ops 链能不能真的吃下那一天的截面（§4.2 的那张表全是运行期才知道的事）。

    实现上不另写一个单日执行器，而是把引擎既有的两个旋钮拧到位：`lookback=0` 拿掉
    yaml 声明的预热（ops 自己推导的那点下限留着——它是算子正确性的一部分，
    拿掉反而假）；`probe=0` 让 `run()` 把 sd 收成 ed 那一天，且 probe 分支
    **从构造上**一个字节都不写 store（§15.7「把快速路径做成构造上不落盘」）。
    复用引擎而不是复制引擎：一份平行的单日执行器迟早与主循环漂移，
    而它漂移的那天，正是预检开始说谎的那天。
    """
    import warnings
    from dataclasses import replace
    from .runner.node import run
    from .runner.preflight import WARN, Diagnostic, _rel

    out: list = []
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            run(replace(spec, lookback=0), store, a.sd or store.axes.sessions[0],
                a.ed, only=a.only, probe=0)
        except Exception as e:                      # noqa: BLE001
            # 这里**故意**兜住一切：dry-run 存在的理由就是把 ValueError / KeyError
            # 一类的运行期错误变成一条诊断, 而不是一屏 traceback。
            out.append(_runtime_error(spec.path, e, code="DRYRUN"))
    for w in caught:
        out.append(Diagnostic(
            WARN, "DRYRUN_RUNTIME", _rel(spec.path), "-", "handle",
            " ".join(str(w.message).split())[:200],
            "--dry-run 不预热, 单日窗口几乎全是 NaN, 故 Σ|w|=0 一类的告警"
            "在这里是预期的; 数值对不对请用 --probe 看"))
    return out


def cmd_run(a) -> int:
    """§15.7：预检在**读任何数据之前**跑，而不只在 `--dry-run` 下跑。

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
            if a.dry_run:
                d2 = _dry_run(spec, store, a)
                report(d2)
                rc = 1 if n_errors(d2) else rc
            else:
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
            print("store 为空"); return 0
        cat["first"] = cat["first_session"].map(lambda i: store.axes.date(int(i)) if i is not None else "")
        cat["last"] = cat["last_session"].map(lambda i: store.axes.date(int(i)) if i is not None else "")
        cols = ["ref", "dims", "dtype", "version", "first", "last"]
        print(cat[cols].to_string(index=False))
        print(f"\n轴: {store.axes.n_sessions} sessions × {store.axes.n_securities} securities")
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="alphakit")
    ap.add_argument("--store", default=DEFAULT_L3)
    ap.add_argument("--region", default="us")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="唯一执行入口: 数据节点与 alpha 同一条命令")
    r.add_argument("path", help="节点目录 / yaml / glob")
    r.add_argument("--sd"); r.add_argument("--ed")
    r.add_argument("--only", help="只跑指定节点")
    r.add_argument("--probe", nargs="?", type=int, const=20, default=None,
                   help="暖机尾段试跑, 不写 store")
    r.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="编译检查 + 单个 session 上执行 handle; 不预热、不写 store "
                        "(§15.7)。与 --probe 同时给时以本项为准")
    r.add_argument("--rebuild", action="store_true", help="全量重建并 bump version")
    r.add_argument("--pnl", action="store_true")
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("store", help="查询工具, 不是执行器")
    s.add_argument("action", choices=["status", "ls", "meta"])
    s.add_argument("ref", nargs="?")
    s.set_defaults(fn=cmd_store)

    p = sub.add_parser("pnl", help="权重 → 指标")
    p.add_argument("--node", default=None)
    p.add_argument("--sd"); p.add_argument("--ed")
    p.add_argument("--booksize", type=float, default=20e6)
    p.add_argument("--rm", default="g_common.field_base_px.field_base_px-ret_1d_1500")
    p.add_argument("--cost-bps", dest="cost_bps", type=float, default=10.0,
                   help="常数 bps 成本模型; §4.9.3 说成本模型是有版本的 L3 field, "
                        "常数只是 v0 的诚实近似, 会写进 metrics.json")
    p.add_argument("--participation", type=float, default=0.10)
    p.add_argument("--halt-proxy", dest="halt_proxy", type=int, default=None,
                   help="无 is_halted field 时的显式降级: 连续 K 日 ret=NaN 视作停牌 (§九)")
    p.add_argument("--weight", default=None, help="外来权重文件入口")
    p.add_argument("--out", default="pnl_out")
    p.set_defaults(fn=cmd_pnl)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
