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

from ..core.config import CS_OPS, KINDS, ConfigError, Spec, parse_ref

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
        return "store 是空的——`alphakit store status` 可查已落库的节点"
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
            near = (f"最接近的是 `{best}`（编辑距离 {dist}）；"
                    if dist <= max(3, len(r.output) // 3) else "")
            return f"该节点在 store 里，但没有输出 `{r.output}`；{near}它的输出是 {sib}"
    cand = difflib.get_close_matches(ref, known, n=5, cutoff=0.5)
    if cand:
        best = min(cand, key=lambda c: _levenshtein(ref, c))
        dist = _levenshtein(ref, best)
        if dist <= max(3, len(ref) // 4):
            return f"是否想写 {best}?（编辑距离 {dist}）"
    return "store 里没有相近的名字——跨 config 的依赖必须先跑（§7.1），`store ls` 可查"


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
        stem = pattern[:-1]                       # 含末尾的 '-'
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
                    f"依赖里带 `_tc` 模板标记但引擎不做替换：{dep}",
                    "v0 未实现 §4.9.5 的 cutoff 替换；请写死实际 cutoff"
                    "（如 `-adj_close_1500`）")
                continue
            if dep.endswith("-*"):
                hits = cat.expand(dep)
                if not hits:
                    # §4.9 ②：通配是简写而非豁免，展开为空说明上游一个输出都没落库
                    add(ERROR, "DEP_WILDCARD_EMPTY", node.name, f,
                        f"通配 {dep} 展开为空——该节点尚未产出任何输出",
                        _suggest(dep[:-2] + "-", cat.known))
                    continue
                resolved.extend(hits)
                continue
            resolved.append(dep)
            if cat.exists(dep):
                continue
            at = produced_at.get(dep)
            if at is None:
                add(ERROR, "DEP_MISSING", node.name, f,
                    f"依赖不在 store 里：{dep}", _suggest(dep, cat.known))
            elif at > i:
                # 靠后声明的兄弟节点这一轮还没跑到，读到的要么是空要么是上一轮的旧值
                add(ERROR, "DECL_ORDER", node.name, f,
                    f"依赖 {dep} 由同一 yaml 里**声明在后**的节点 "
                    f"{list(spec.nodes)[at]} 产出",
                    "§7.1：声明顺序即执行顺序，被依赖的节点要写在前面")

        resolved_all |= {r for r in resolved if not r.endswith("-*")}

        # ------------------------------------------------------- 秩 / CS 算子
        ranks = {len(o.dims) for o in node.outputs.values()}
        for o in node.outputs.values():
            for j, (op, arg) in enumerate(o.ops):
                if op in CS_OPS and o.dims != ("di", "ii"):
                    # load_spec 已在加载期拦下同一条（这份是给"程序化构造的 Spec"
                    # 兜底，走 cli 时它不会触发）；两处同码同文，出口也同一个。
                    add(ERROR, "CS_OP_RANK", node.name, f"outputs.{o.key}.ops[{j}]",
                        f"CS 类算子 `{op}` 只对秩-2 合法，该输出 dims={list(o.dims)}",
                        "§3.6：秩-1 没有 ii 轴可截面，秩-3 的截面轴不唯一；"
                        "要日内截面请先在 handle 里压成秩-2")
                if op != "neutralize":
                    continue
                fld = str(arg)
                # ------------------------------------------- §4.10 的那个陷阱
                if fld not in resolved:
                    add(ERROR, "NEUTRALIZE_NOT_IN_DEPS", node.name,
                        f"outputs.{o.key}.ops[{j}]",
                        f"neutralize 的分组字段 {fld} 没有出现在 deps 里",
                        "§4.10：凡是这个节点跑起来需要读到的 L3，无论谁去读它，"
                        "都要出现在 deps 里——handle 不提它，但算子链会去读它，"
                        "而这一读发生在算子链深处")
                if not cat.exists(fld):
                    if produced_at.get(fld) is None:
                        add(ERROR, "NEUTRALIZE_MISSING", node.name,
                            f"outputs.{o.key}.ops[{j}]",
                            f"neutralize 的分组字段不在 store 里：{fld}",
                            _suggest(fld, cat.known))
                    continue
                gd = cat.dims(fld)
                if gd != ["di", "ii"]:
                    add(ERROR, "NEUTRALIZE_RANK", node.name,
                        f"outputs.{o.key}.ops[{j}]",
                        f"neutralize 的分组字段必须是秩-2，{fld} 是 dims={gd}")
                dt = cat.dtype(fld)
                if not (dt.startswith(_INT_PREFIX) or dt in _BOOL_DTYPES):
                    add(WARN, "NEUTRALIZE_DTYPE", node.name,
                        f"outputs.{o.key}.ops[{j}]",
                        f"neutralize 的分组字段 dtype={dt}，§6.2 要求一个 int field",
                        "浮点分组几乎每只票自成一组，demean 后恒为 0——"
                        "整条 alpha 静默清零而不报错")

        # ------------------------------------------------ alpha 的秩与 scale
        if node.kind == "alpha":
            for o in node.outputs.values():
                if o.dims != ("di", "ii"):
                    add(ERROR, "ALPHA_RANK", node.name, f"outputs.{o.key}.dims",
                        f"alpha 必须是秩-2（权重是 di×ii），该输出 dims={list(o.dims)}")
                elif not o.ops or o.ops[-1][0] != "scale":
                    # 同 CS_OP_RANK：load_spec 已在加载期报同一条，这里只兜程序化 Spec
                    add(ERROR, "ALPHA_NO_SCALE", node.name, f"outputs.{o.key}.ops",
                        "alpha 的 ops 链必须以 scale 收尾（§4.4）",
                        "少了它，上游各自 Σ|w|=1 的权重线性组合后会因抵消而缩水，"
                        "账本投不满而 Sharpe 看着正常")

        # ------------------------------------------------------ universe 与秩
        if spec.universe:
            if ranks == {1}:
                add(ERROR, "UNIVERSE_RANK1", node.name, "universe",
                    f"秩-1 节点不能声明 universe（本文件声明了 {spec.universe}）",
                    "§3.6：没有 ii 轴可掩")
            elif 1 in ranks:
                add(WARN, "UNIVERSE_RANK1", node.name, "universe",
                    "本节点混有秩-1 输出，池子对它们无效（引擎只掩秩-2）")
            if 3 in ranks:
                add(WARN, "UNIVERSE_RANK3", node.name, "universe",
                    "秩-3 输出在当前引擎里不会被掩码（§3.6 要求沿 ti 广播）",
                    "池外标的会带着值进下游——这是口径差异，不会报错")

        # ---------------------------------------------- 源码：deps / 输出元数
        if node.code not in codes:
            codes[node.code] = _read_code(node.code)
        code = codes[node.code]
        cf = _rel(node.code)
        if code.error == "missing":
            add(ERROR, "CODE_MISSING", node.name, "code",
                f"代码文件不存在：{cf}",
                "缺省是同目录下与 yaml 同名的 .py（§4.1）")
        elif code.error and code.error.startswith("syntax"):
            _, ln, msg = code.error.split(":", 2)
            add(ERROR, "CODE_SYNTAX", node.name, "code", f"{cf}:{ln} 语法错误：{msg}")
        else:
            if not code.has_handle:
                add(ERROR, "NO_HANDLE", node.name, "code",
                    f"{cf} 里没有模块级的 handle(ctx)")
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
                        f"{cf} 读自己的输出 {r}，但 deps 里没写它",
                        "§7.2 第 1 条：自引用节点必须把自己列进 deps，"
                        "否则落库 meta 的 deps 缺这一条、血缘不成立")
                else:
                    # 名字本身在不在 store 里, 决定了这是"忘了声明"还是"顺带还写错了"
                    known_ref = cat.exists(r) or r in produced_at
                    add(ERROR, "DEP_NOT_DECLARED", node.name, "code",
                        f"{cf} 里 ctx.f/win 读 {r}，但它不在 deps 里",
                        "该名字在 store 里，补进 deps 即可（§4.10）" if known_ref
                        else _suggest(r, cat.known))

            # §4.2 的表：单输出直接 return 裸值，≥2 个输出必须走 ctx.multi_outputs
            n_out = len(node.outputs)
            if n_out >= 2 and not code.multi:
                add(ERROR, "OUTPUTS_MULTI_MISSING", node.name, "outputs",
                    f"声明了 {n_out} 个输出 {sorted(node.outputs)}，"
                    f"但 {cf} 里没有 ctx.multi_outputs(...)",
                    "§4.2：≥2 个 key 却返回裸值是错误——读一眼返回语句就该知道"
                    "该节点有几个输出")
            elif n_out == 1 and code.multi:
                add(ERROR, "OUTPUTS_SINGLE_MULTI", node.name, "outputs",
                    f"只有一个输出 {sorted(node.outputs)}，"
                    f"但 {cf} 用了 ctx.multi_outputs(...)",
                    "§4.2：单输出直接 return 值")
            for keys in code.multi:
                if keys is None or n_out < 2:
                    continue
                for k in sorted(keys - set(node.outputs)):
                    hint = difflib.get_close_matches(k, list(node.outputs), 1)
                    add(ERROR, "OUTPUTS_KEY_UNKNOWN", node.name, "outputs",
                        f"{cf} 传了未声明的输出 `{k}`",
                        f"是否想写 {hint[0]}?" if hint else
                        f"yaml 声明的是 {sorted(node.outputs)}")
                for k in sorted(set(node.outputs) - keys):
                    add(ERROR, "OUTPUTS_KEY_MISSING", node.name, "outputs",
                        f"{cf} 没有传声明过的输出 `{k}`",
                        "算不出值请传 NaN，不要漏 key——NaN 是合法值，"
                        "漏 key 意味着这个节点今天不存在（§4.3）")

    # ------------------------------------------------------- universe 自身的秩
    if spec.universe:
        u = spec.universe
        if not cat.exists(u):
            if produced_at.get(u) is None:
                add(ERROR, "UNIVERSE_MISSING", "-", "universe",
                    f"universe 不在 store 里：{u}", _suggest(u, cat.known))
        else:
            ud = cat.dims(u)
            if ud != ["di", "ii"]:
                add(ERROR, "UNIVERSE_RANK", "-", "universe",
                    f"universe 必须是秩-2，{u} 是 dims={ud}",
                    "§3.5：池子是 di×ii 的成员掩码")
            udt = cat.dtype(u)
            if udt in _BOOL_DTYPES:
                pass
            elif udt.startswith(_INT_PREFIX):
                add(WARN, "UNIVERSE_DTYPE", "-", "universe",
                    f"universe dtype={udt}，§3.5 说的是 bool field",
                    "引擎按 `> 0` 判成员，0/1 的整数掩码能跑通，但口径靠约定而非类型")
            else:
                add(ERROR, "UNIVERSE_DTYPE", "-", "universe",
                    f"universe dtype={udt} 不是 bool field（§3.5）",
                    "引擎按 `> 0` 判成员：把一个浮点面板当池子，"
                    "会静默变成「只留取值为正的票」，不报错、只改口径")

    if spec.return_metric and not cat.exists(spec.return_metric):
        add(WARN, "RETURN_METRIC_MISSING", "-", "return_metric",
            f"return_metric 不在 store 里：{spec.return_metric}",
            "run 不读它（只 --pnl 读），故只是告警；但 --pnl 会在这里断")

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
        return (f"最近的 session 是 {hit[0]}" if hit else
                f"session 轴是 {axes.sessions[0]}..{axes.sessions[-1]}（{len(on_axis)} 天）")

    for who, day in (("sd", sd), ("ed", ed)):
        if day is not None and day not in on_axis:
            out.append(Diagnostic(
                ERROR, f"{who.upper()}_NOT_SESSION", path, "-", who,
                f"{who}={day} 不在 session 轴上", near(day)))
    if sd and ed and sd > ed:
        out.append(Diagnostic(ERROR, "SD_AFTER_ED", path, "-", "sd",
                              f"sd={sd} 晚于 ed={ed}"))

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
        want = ed or f"{axes.sessions[-1]}（ed 缺省 = 轴末日）"
        out.append(Diagnostic(
            WARN, "ED_BEYOND_DATA", path, "-", "ed",
            f"ed={want} 越过了数据边界，effective_ed={cap}",
            f"卡住它的是 {binder}（last_session={cap}）；"
            f"`alphakit store status` 可看全部落后项"))
    return out
