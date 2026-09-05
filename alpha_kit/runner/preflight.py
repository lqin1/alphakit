"""零数据预检：一次完整运行不该在第 12 秒才死于一个 typo（architecture.md §15.7）。

**它不是 `--dry-run` 的一部分，`--dry-run` 是它的一部分。** §15.7 的原话是"这些应当在
每一次调用读取任何数据之前就跑，而不只在 `--dry-run` 下跑"——所以 `preflight()` 是
`run` 路径的第一步，`--dry-run` 只是"跑完检查、再在一个 session 上执行 handle、然后停"。
两者共用同一份检查、同一个诊断模型、同一套输出格式。

**全部检查都是元数据级**：catalog 目录扫描（`list_refs`）、zarr 的 attrs（`meta`）、
yaml 已解析出的 `Spec`、以及 handle 源码的 AST。**一个 panel 都不读**——这既是
§15.7 承诺的 < 50 ms 的来源，也是它敢放在每一次 run 之前的前提。

**为什么不把这些检查散进 `load_spec` 就算了**：`load_spec` 只看得见 yaml，看不见
store。"这个名字存不存在"、"它是几秩"、"它的 last_session 到哪天"这三类问题必须有
store 才能答，而它们恰好是最费时间的一类错误——写错一个 60 字符的引用名，在没有预检
的世界里要等预热跑完才知道。故分成两层：语法/自洽 归 `load_spec`（加载即报错），
存在性/秩/新鲜度/源码一致性 归这里。`cli` 把两层的输出走同一个 `report()`，
使用者看到的是一个诊断面，而不是两套错误风格。
"""
from __future__ import annotations

import ast
import difflib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from ..core import rank as rk
from ..core.config import CS_OPS, KINDS, ConfigError, Spec, is_wildcard, parse_ref

ERROR = "error"
WARN = "warn"

# 固定列宽：诊断是拿来 grep 和 awk 的，不是拿来读散文的。
# 位置列按本批次的最长值对齐并封顶，免得一个深路径把所有行推到屏幕外。
SEV_W, CODE_W, LOC_MAX = 5, 22, 62

# universe 是 bool field（§3.5）。放宽到整数只是承认 0/1 的 i1 掩码是个常见写法，
# 但浮点绝不放行：`UniverseView.mask` 的判据是 `> 0`，把一个收益率面板当池子用
# 会静默变成"只留上涨的票"——一个不报错、只改口径的灾难。
_BOOL_DTYPES = {"bool", "b1", "?"}
_INT_PREFIX = ("i", "u")


# ------------------------------------------------------------------- 诊断模型
@dataclass(frozen=True)
class Diagnostic:
    """一条诊断 = 严重度 + 短码 + 出错位置 + 一句话 + （能给就给的）修法。

    位置恒为三段 `config路径:节点:字段`，缺哪段填 `-`——**宁可占一列也不省**，
    因为省了以后 `awk -F:` 就不能按列取值，而扫描族一次能吐几十条诊断。
    """

    severity: str
    code: str
    path: str
    node: str
    field: str
    message: str
    suggestion: str | None = None

    @property
    def is_error(self) -> bool:
        return self.severity == ERROR

    @property
    def loc(self) -> str:
        return f"{self.path}:{self.node or '-'}:{self.field or '-'}"

    def lines(self, width: int = LOC_MAX) -> list[str]:
        """建议单独占一行，且**同样带全套前缀**——grep 一个节点名要能捞到它的修法。"""
        head = f"{self.severity:<{SEV_W}} {self.code:<{CODE_W}} {self.loc:<{width}}  "
        out = [head + self.message]
        if self.suggestion:
            out.append(head + "↳ " + self.suggestion)
        return out


def n_errors(diags: list[Diagnostic]) -> int:
    return sum(1 for d in diags if d.is_error)


def report(diags: list[Diagnostic], *, stream=None) -> int:
    """打印并返回 error 条数。走 stdout：预检输出是产物，要能直接 `| grep`。"""
    stream = stream or sys.stdout
    if not diags:
        return 0
    w = min(LOC_MAX, max(len(d.loc) for d in diags))
    # error 先于 warn：真正拦住这次运行的东西必须在滚屏的顶上，而不是混在告警里。
    for d in sorted(diags, key=lambda x: (not x.is_error,)):
        for line in d.lines(w):
            print(line, file=stream)
    return n_errors(diags)


def config_error(path: Path | str, exc: Exception) -> Diagnostic:
    """把 `load_spec` 的 ConfigError 折成一条诊断（§4.4 的 scale 检查等走的就是这条）。

    §15.7 列的九项里有几项 `load_spec` 已经在加载期做了（alpha 的 ops 必须以 scale
    收尾、CS 算子仅秩-2、秩-3 必须有 grid）。**不重复实现、只统一出口**：两处逻辑
    并存迟早漂移，而使用者要的从来不是"谁报的"，是"同一种格式的一条诊断"。
    """
    body = str(exc).replace("\n", " ").strip()
    # load_spec 的消息以 `{path}:{node}:` 起头，去掉它免得位置列印两遍
    head = f"{path}:"
    if body.startswith(head):
        body = body[len(head):].strip()
    # load_spec 的消息形如 `{node}: …` 或 `{node}.{output}: …`——把节点名抬进位置列,
    # 否则 CONFIG 这一类诊断就成了唯一没有节点名的行, grep 一个节点会漏掉它。
    node = "-"
    m = re.match(r"^([a-z]+_[a-z0-9_]+?)(?:\.[a-z0-9_]+)?:\s*(.+)$", body, re.S)
    if m and m.group(1).split("_", 1)[0] in KINDS:
        node, body = m.group(1), m.group(2).strip()
    kind = "CONFIG" if isinstance(exc, ConfigError) else "LOAD"
    return Diagnostic(ERROR, kind, _rel(path), node, "-", body)


# ------------------------------------------------------------------ typo 建议
def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _suggest(ref: str, known: list[str]) -> str | None:
    """一个 60 字符的引用名里的 typo，靠肉眼比对是件苦差事，所以这里必须给建议。

    两级：**先按结构**——节点在、只是输出名不对，那就直接列出它真有哪些输出
    （§4.9.5「报错时列出可用的 cutoff」是同一个动作）；**再按编辑距离**。
    difflib 先做廉价筛选（内部用 real_quick_ratio 剪枝，不必对全 catalog 做 DP），
    再在少数候选里算真正的编辑距离挑最近的一个，并**把距离印出来**——距离 1 值得
    照抄，距离 11 就该自己再看一眼。
    """
    if not known:
        return "the store is empty -- `ak store status` lists what has landed"
    try:
        r = parse_ref(ref)
    except ConfigError:
        r = None
    if r is not None:
        stem = f"{r.repo}.{r.node_dir}.{r.node_name}-"
        sib = sorted(x[len(stem):] for x in known if x.startswith(stem))
        if sib:
            # 节点对了、只是输出名不对——这是最常见的一类 typo。把候选缩到该节点的
            # 输出集里再比编辑距离, 命中率远高于在整个 catalog 上比。
            best = min(sib, key=lambda c: _levenshtein(r.output, c))
            dist = _levenshtein(r.output, best)
            near = (f"closest is `{best}` (edit distance {dist}); "
                    if dist <= max(3, len(r.output) // 3) else "")
            return f"that node is in the store but has no output `{r.output}`; {near}its outputs are {sib}"
    cand = difflib.get_close_matches(ref, known, n=5, cutoff=0.5)
    if cand:
        best = min(cand, key=lambda c: _levenshtein(ref, c))
        dist = _levenshtein(ref, best)
        if dist <= max(3, len(ref) // 4):
            return f"did you mean {best}? (edit distance {dist})"
    return "no similar name in the store -- cross-config deps must be run first (§7.1); `store ls` lists them"


# --------------------------------------------------------------- catalog 缓存
class _Cat:
    """一次目录扫描 + 按 ref 记忆化的 attrs。

    一个 yaml 里几个节点常常依赖同一批 field（§4.10 例 5/6 就是），不缓存的话
    同一个 `zarr.json` 会被打开好几遍——预检的 50 ms 预算经不起这种浪费。
    """

    def __init__(self, store) -> None:
        self.store = store
        self.known: list[str] = store.list_refs()
        self._known = set(self.known)
        self._meta: dict[str, dict] = {}

    def exists(self, ref: str) -> bool:
        return ref in self._known

    def meta(self, ref: str) -> dict:
        if ref not in self._meta:
            self._meta[ref] = self.store.meta(ref) if self.exists(ref) else {}
        return self._meta[ref]

    def dims(self, ref: str) -> list[str]:
        return list(self.meta(ref).get("dims") or [])

    def dtype(self, ref: str) -> str:
        return str(self.meta(ref).get("dtype") or "")

    def expand(self, pattern: str) -> list[str]:
        stem = pattern[:-1]                       # 含末尾的 '-' 或 '.'
        return sorted(r for r in self.known if r.startswith(stem))


# ------------------------------------------------------------------- 源码 AST
@dataclass(frozen=True)
class _Code:
    error: str | None = None
    has_handle: bool = False
    refs: tuple[str, ...] = ()
    multi: tuple[frozenset | None, ...] = ()      # 每次 multi_outputs 调用的键集; None = **kw


def _str_of(node: ast.AST, consts: dict[str, set[str]]) -> set[str]:
    """字面量，或一个被赋成字面量的名字。

    §4.10 的示例代码全是 `PX = "g_common…"` 再 `ctx.win(PX, w)` 这种写法，只认
    `ctx.win("字面量")` 的扫描会一条都抓不到。反过来，f-string / 参数拼出来的名字
    静态不可知，**一律跳过**——预检宁可漏报也不能误报，一条假 error 就会让人
    习惯性加 `--no-preflight`，那这套东西就废了。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name):
        return set(consts.get(node.id, ()))
    return set()


def _read_code(path: Path) -> _Code:
    """从 handle 源码里静态取出：它读了哪些 ref、它是不是多输出写法。

    只解析不导入——`import` 会执行模块顶层，那既慢又有副作用，而预检的承诺是零副作用。
    """
    if not path.exists():
        return _Code(error="missing")
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except SyntaxError as e:
        return _Code(error=f"syntax:{e.lineno}:{e.msg}")

    consts: dict[str, set[str]] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) \
                and isinstance(n.value.value, str):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    consts.setdefault(t.id, set()).add(n.value.value)

    has_handle = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and n.name == "handle" for n in tree.body)
    refs: set[str] = set()
    multi: list[frozenset | None] = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
            continue
        if n.func.attr in ("f", "win") and n.args:
            refs |= _str_of(n.args[0], consts)
        elif n.func.attr == "multi_outputs":
            # `**d` 或位置参数展开时键集合静态不可知：记 None，只保留"用了多输出写法"
            multi.append(None if (n.args or any(k.arg is None for k in n.keywords))
                         else frozenset(k.arg for k in n.keywords))
    return _Code(None, has_handle, tuple(sorted(refs)), tuple(multi))


# ----------------------------------------------------------------------- 主体
def _rel(p: Path | str) -> str:
    p = Path(p)
    try:
        return str(p.relative_to(Path.cwd()))
    except ValueError:
        return str(p)


def preflight(spec: Spec, store, *, sd: str | None = None,
              ed: str | None = None) -> list[Diagnostic]:
    """零数据预检（§15.7）。返回诊断清单；**自己不打印、不抛错、不退出**。

    不抛错是刻意的：一次调用可能带 glob 扫一族 yaml（§十二 的 PATH 语义），
    第一个文件里的一个 typo 不该让后面九个文件连检查都跑不到。判定与呈现留给
    `report()` 与 cli——预检只负责"把所有毛病一次说完"。
    """
    d: list[Diagnostic] = []
    path = _rel(spec.path)
    cat = _Cat(store)
    codes: dict[Path, _Code] = {}

    def add(sev, code, node, field, msg, sug=None):
        d.append(Diagnostic(sev, code, path, node, field, msg, sug))

    # 同一个 yaml 里靠前的节点先跑先落库（§7.1「声明顺序 = 执行顺序」），所以
    # "store 里还没有"对它们不是错误。记下每个产物由第几个节点产出，好把
    # "还没跑" 和 "写错名字" 分开——不分开的话，一条 liq→rev 的新链第一次跑
    # 就会被自己的预检拦下，那这套东西第一天就会被关掉。
    produced_at: dict[str, int] = {}
    for i, node in enumerate(spec.nodes.values()):
        for k in node.outputs:
            produced_at[str(node.ref(k))] = i

    resolved_all: set[str] = set()

    for i, node in enumerate(spec.nodes.values()):
        own = {str(node.ref(k)) for k in node.outputs}
        resolved: list[str] = []

        # ---------------------------------------------------------- deps 存在性
        for j, dep in enumerate(node.deps):
            f = f"deps[{j}]"
            if "_tc" in dep:
                # §4.9.5 的模板替换在 v0 引擎里并不存在（全仓无实现），字面查 store
                # 必然扑空。与其报一条 "名字不存在" 让人去 store 里找一个永远不会有的
                # 名字，不如直说模板没被解析。
                add(ERROR, "DEP_TC_UNRESOLVED", node.name, f,
                    f"a dep carries the `_tc` template marker but the engine does not substitute it: {dep}",
                    "v0 does not implement the cutoff substitution of §4.9.5; write the actual "
                    "cutoff literally (e.g. `adj_close_1500`)")
                continue
            if is_wildcard(dep):
                hits = cat.expand(dep)
                if not hits:
                    # §4.9 ②：通配是简写而非豁免，展开为空说明上游一个输出都没落库
                    add(ERROR, "DEP_WILDCARD_EMPTY", node.name, f,
                        f"wildcard {dep} expanded to nothing -- that node has not produced any output yet",
                        _suggest(dep[:-1], cat.known))
                    continue
                resolved.extend(hits)
                continue
            resolved.append(dep)
            if cat.exists(dep):
                continue
            at = produced_at.get(dep)
            if at is None:
                add(ERROR, "DEP_MISSING", node.name, f,
                    f"dep is not in the store: {dep}", _suggest(dep, cat.known))
            elif at > i:
                # 靠后声明的兄弟节点这一轮还没跑到，读到的要么是空要么是上一轮的旧值
                add(ERROR, "DECL_ORDER", node.name, f,
                    f"dep {dep} is produced by {list(spec.nodes)[at]}, which is declared LATER in the "
                    f"same yaml",
                    "§7.1: declaration order is execution order -- the node depended on must come first")

        resolved_all |= {r for r in resolved if not is_wildcard(r)}

        # ------------------------------------------------------- 秩 / CS 算子
        ranks = {len(o.dims) for o in node.outputs.values()}
        for o in node.outputs.values():
            for j, (op, arg) in enumerate(o.ops):
                if op in CS_OPS and not rk.is_panel(o.dims):
                    # load_spec 已在加载期拦下同一条（这份是给"程序化构造的 Spec"
                    # 兜底，走 cli 时它不会触发）；两处同码同文，出口也同一个。
                    add(ERROR, "CS_OP_RANK", node.name, f"outputs.{o.key}.ops[{j}]",
                        f"the CS op `{op}` is legal only for rank-2; this output has dims={list(o.dims)}",
                        "§3.6: rank-1 has no ii axis to cut across, and rank-3 has no unique "
                        "cross-sectional axis. For an intraday cross-section, collapse to rank-2 "
                        "inside handle first")
                if op != "neutralize":
                    continue
                fld = str(arg)
                # ------------------------------------------- §4.10 的那个陷阱
                if fld not in resolved:
                    add(ERROR, "NEUTRALIZE_NOT_IN_DEPS", node.name,
                        f"outputs.{o.key}.ops[{j}]",
                        f"the grouping field {fld} used by neutralize does not appear in deps",
                        "§4.10: every L3 this node needs in order to run must appear in deps, no "
                        "matter who reads it. handle never mentions this one, but the ops chain "
                        "reads it -- deep inside the chain")
                if not cat.exists(fld):
                    if produced_at.get(fld) is None:
                        add(ERROR, "NEUTRALIZE_MISSING", node.name,
                            f"outputs.{o.key}.ops[{j}]",
                            f"the grouping field used by neutralize is not in the store: {fld}",
                            _suggest(fld, cat.known))
                    continue
                gd = cat.dims(fld)
                if not rk.is_panel(gd):
                    add(ERROR, "NEUTRALIZE_RANK", node.name,
                        f"outputs.{o.key}.ops[{j}]",
                        f"the grouping field used by neutralize must be rank-2; {fld} has dims={gd}")
                dt = cat.dtype(fld)
                if not (dt.startswith(_INT_PREFIX) or dt in _BOOL_DTYPES):
                    add(WARN, "NEUTRALIZE_DTYPE", node.name,
                        f"outputs.{o.key}.ops[{j}]",
                        f"the grouping field for neutralize has dtype={dt}; §6.2 requires an int field",
                        "with float groups nearly every name forms its own group, so demean "
                        "returns identically 0 -- the whole alpha is silently zeroed with no error")

        # ------------------------------------------------ alpha 的秩与 scale
        if node.kind == "alpha":
            for o in node.outputs.values():
                if not rk.is_panel(o.dims):
                    add(ERROR, "ALPHA_RANK", node.name, f"outputs.{o.key}.dims",
                        f"an alpha must be rank-2 (weights are di x ii); this output has dims={list(o.dims)}")
                elif not o.ops or o.ops[-1][0] != "scale":
                    # 同 CS_OP_RANK：load_spec 已在加载期报同一条，这里只兜程序化 Spec
                    add(ERROR, "ALPHA_NO_SCALE", node.name, f"outputs.{o.key}.ops",
                        "an alpha's ops chain must end in scale (§4.4)",
                        "without it, upstream weights that each satisfy Sigma|w|=1 shrink through "
                        "cancellation once combined -- the book is under-deployed while Sharpe "
                        "still looks normal")

        # ------------------------------------------------------ universe 与秩
        if spec.universe:
            if ranks == {1}:
                add(ERROR, "UNIVERSE_RANK1", node.name, "universe",
                    f"a rank-1 node cannot declare a universe (this file declares {spec.universe})",
                    "§3.6: there is no ii axis to mask")
            elif 1 in ranks:
                add(WARN, "UNIVERSE_RANK1", node.name, "universe",
                    "this node mixes in rank-1 outputs, for which the pool has no effect (the engine masks rank-2 only)")
            if 3 in ranks:
                add(WARN, "UNIVERSE_RANK3", node.name, "universe",
                    "rank-3 outputs are not masked by the current engine (§3.6 requires broadcasting along ti)",
                    "names outside the pool carry values downstream -- a difference in convention, not an error")

        # ---------------------------------------------- 源码：deps / 输出元数
        if node.code not in codes:
            codes[node.code] = _read_code(node.code)
        code = codes[node.code]
        cf = _rel(node.code)
        if code.error == "missing":
            add(ERROR, "CODE_MISSING", node.name, "code",
                f"code file does not exist: {cf}",
                "the default is a .py beside the yaml with the same stem (§4.1)")
        elif code.error and code.error.startswith("syntax"):
            _, ln, msg = code.error.split(":", 2)
            add(ERROR, "CODE_SYNTAX", node.name, "code", f"{cf}:{ln} syntax error: {msg}")
        else:
            if not code.has_handle:
                add(ERROR, "NO_HANDLE", node.name, "code",
                    f"{cf} has no module-level handle(ctx)")
            for r in code.refs:
                try:
                    parse_ref(r)
                except ConfigError:
                    continue                  # 不是引用名的字符串，与 deps 无关
                if r in resolved:
                    continue
                if r in own:
                    # 引擎当前会替自引用兜底（panels.setdefault），所以只是告警；
                    # 但 meta["deps"] 里会少这一条，血缘从此说谎。
                    add(WARN, "SELF_REF_NOT_IN_DEPS", node.name, "code",
                        f"{cf} reads its own output {r} but does not list it in deps",
                        "§7.2 item 1: a self-referencing node must list itself in deps, or the "
                        "deps recorded in meta are incomplete and the lineage does not hold")
                else:
                    # 名字本身在不在 store 里, 决定了这是"忘了声明"还是"顺带还写错了"
                    known_ref = cat.exists(r) or r in produced_at
                    add(ERROR, "DEP_NOT_DECLARED", node.name, "code",
                        f"{cf} reads {r} via ctx.f/win but it is not in deps",
                        "that name is in the store; just add it to deps (§4.10)" if known_ref
                        else _suggest(r, cat.known))

            # §4.2 的表：单输出直接 return 裸值，≥2 个输出必须走 ctx.multi_outputs
            n_out = len(node.outputs)
            if n_out >= 2 and not code.multi:
                add(ERROR, "OUTPUTS_MULTI_MISSING", node.name, "outputs",
                    f"declares {n_out} outputs {sorted(node.outputs)} but {cf} never calls "
                    f"ctx.multi_outputs(...)",
                    "§4.2: returning a bare value with 2+ keys is an error -- one look at the "
                    "return statement should tell you how many outputs a node has")
            elif n_out == 1 and code.multi:
                add(ERROR, "OUTPUTS_SINGLE_MULTI", node.name, "outputs",
                    f"has only one output {sorted(node.outputs)} but {cf} uses ctx.multi_outputs(...)",
                    "§4.2: a single-output node returns the value directly")
            for keys in code.multi:
                if keys is None or n_out < 2:
                    continue
                for k in sorted(keys - set(node.outputs)):
                    hint = difflib.get_close_matches(k, list(node.outputs), 1)
                    add(ERROR, "OUTPUTS_KEY_UNKNOWN", node.name, "outputs",
                        f"{cf} passes an undeclared output `{k}`",
                        f"did you mean {hint[0]}?" if hint else
                        f"the yaml declares {sorted(node.outputs)}")
                for k in sorted(set(node.outputs) - keys):
                    add(ERROR, "OUTPUTS_KEY_MISSING", node.name, "outputs",
                        f"{cf} never passes the declared output `{k}`",
                        "if a value cannot be computed pass NaN rather than omitting the key -- "
                        "NaN is a legal value, while a missing key means this node does not exist "
                        "today (§4.3)")

    # ------------------------------------------------------- universe 自身的秩
    if spec.universe:
        u = spec.universe
        if not cat.exists(u):
            if produced_at.get(u) is None:
                add(ERROR, "UNIVERSE_MISSING", "-", "universe",
                    f"universe is not in the store: {u}", _suggest(u, cat.known))
        else:
            ud = cat.dims(u)
            if not rk.is_panel(ud):
                add(ERROR, "UNIVERSE_RANK", "-", "universe",
                    f"universe must be rank-2; {u} has dims={ud}",
                    "§3.5: a pool is a di x ii membership mask")
            udt = cat.dtype(u)
            if udt in _BOOL_DTYPES:
                pass
            elif udt.startswith(_INT_PREFIX):
                add(WARN, "UNIVERSE_DTYPE", "-", "universe",
                    f"universe dtype={udt}; §3.5 specifies a bool field",
                    "the engine tests membership with `> 0`, so a 0/1 integer mask works, but the "
                    "convention then rests on agreement rather than on the type")
            else:
                add(ERROR, "UNIVERSE_DTYPE", "-", "universe",
                    f"universe dtype={udt} is not a bool field (§3.5)",
                    "the engine tests membership with `> 0`, so using a float panel as the pool "
                    "silently becomes 'keep only names with a positive value' -- no error, just a "
                    "different convention")

    if spec.return_metric and not cat.exists(spec.return_metric):
        add(WARN, "RETURN_METRIC_MISSING", "-", "return_metric",
            f"return_metric is not in the store: {spec.return_metric}",
            "run does not read it (only --pnl does), so this is a warning; but --pnl will break here")

    d.extend(_window_checks(spec, store, cat, resolved_all, sd, ed, path))
    return d


def _window_checks(spec: Spec, store, cat: _Cat, deps: set[str],
                   sd: str | None, ed: str | None, path: str) -> list[Diagnostic]:
    """sd / ed 落在 session 轴上，且 ed 不越过数据的边界（§7.3）。

    这里刻意**复刻**了 `node.effective_ed` 的那几行而不是调用它：预检要多说一句
    「是谁卡住的」，而那个函数只回一个日期；更要紧的是预检必须能在不导入 runner
    （及其 numpy/pandas）的前提下跑完——它的定位是"读任何数据之前"。
    """
    out: list[Diagnostic] = []
    axes = store.axes
    if not axes.sessions:
        return out
    on_axis = set(axes.sessions)

    def near(day: str) -> str:
        hit = difflib.get_close_matches(day, axes.sessions, 1, cutoff=0.6)
        return (f"the nearest session is {hit[0]}" if hit else
                f"the session axis runs {axes.sessions[0]}..{axes.sessions[-1]} ({len(on_axis)} days)")

    for who, day in (("sd", sd), ("ed", ed)):
        if day is not None and day not in on_axis:
            out.append(Diagnostic(
                ERROR, f"{who.upper()}_NOT_SESSION", path, "-", who,
                f"{who}={day} is not on the session axis", near(day)))
    if sd and ed and sd > ed:
        out.append(Diagnostic(ERROR, "SD_AFTER_ED", path, "-", "sd",
                              f"sd={sd} is later than ed={ed}"))

    # ed 的可用性由数据新鲜度决定（§7.3）：任一依赖的最新 session 未落地则回退。
    # run() 把 return_metric 也算进去，这里照做，否则报出来的 effective_ed 会偏乐观。
    cap_i, binder = axes.n_sessions - 1, None
    watch = set(deps) | ({spec.return_metric} if spec.return_metric else set())
    for r in sorted(watch):
        if not cat.exists(r):
            continue
        ls = int(cat.meta(r).get("last_session", cap_i))
        if ls < cap_i:
            cap_i, binder = ls, r
    cap = axes.date(max(0, cap_i))
    if binder is not None and (ed is None or ed > cap):
        # 「绝不静默算半截数据」——回退本身是对的，不出声才是错的。
        want = ed or f"{axes.sessions[-1]} (ed defaults to the last session on the axis)"
        out.append(Diagnostic(
            WARN, "ED_BEYOND_DATA", path, "-", "ed",
            f"ed={want} runs past the data boundary, effective_ed={cap}",
            f"the binding constraint is {binder} (last_session={cap}); "
            f"`ak store status` shows everything that is behind"))
    return out
