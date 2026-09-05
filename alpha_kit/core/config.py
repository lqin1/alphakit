"""配置与命名（architecture.md §3.2 / §4.1 / §4.11）。

节点名 {kind}_{ns}_{name} 是 identity，kind 与 ns 从中解析、yaml 里不声明。
引用名 {repo}.{node_dir}.{node_name}-{output} 与 L3 路径一一对应。
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# 命名规则住在 naming.py；这里 re-export，既有 `from ...config import parse_ref`
# 的写法不受影响——拆分是为了让 store 不必依赖加载器，不是为了制造迁移工作。
from .naming import (KINDS, NAME_RE, NS_RE, RESERVED, ConfigError, Ref,  # noqa: F401
                     check_name, is_wildcard, parse_ref)
TAGS = {"window": "w", "halflife": "h", "lag": "k", "quantile": "q", "count": "n"}
CS_OPS = {"rank", "neutralize", "truncate", "scale"}
TS_OPS = {"linear_decay", "exp_decay", "delay"}
# 算子 → 参数类型。元组表示"这些之一"，含 NoneType 即**参数可省**：
# `scale` 裸写时 OpChain 把 None 当作 book，编译期若不接受就会拒掉执行期明确支持的写法。
# 键集合必须与 runner 的 OPS 一致（tests/test_core.py 与 tests/test_ops.py 各查一遍）。
OP_TYPES = {"truncate": float, "linear_decay": int, "exp_decay": int,
            "delay": int, "neutralize": str, "rank": type(None),
            "scale": (str, type(None))}


# --------------------------------------------------------------------- Spec
@dataclass
class Output:
    key: str
    dtype: str = "f4"
    dims: tuple[str, ...] = ("di", "ii")
    grid: str | None = None
    ops: list = field(default_factory=list)


@dataclass
class NodeSpec:
    name: str                       # {kind}_{ns}_{name}
    node_dir: str
    repo: str
    code: Path
    deps: list[str] = field(default_factory=list)
    params: dict = field(default_factory=dict)
    outputs: dict[str, Output] = field(default_factory=dict)
    src: str = ""                   # 该节点 yaml 子树的规范化文本, 用于指纹
    universe: str | None = None     # 来自 spec, 进指纹
    lookback: int = 0               # 来自 spec, 进指纹

    @property
    def kind(self) -> str:
        return self.name.split("_", 1)[0]

    @property
    def ns(self) -> str:
        return self.name.split("_")[1]

    def ref(self, output: str) -> Ref:
        return Ref(self.repo, self.node_dir, self.name, output)

    def fingerprint(self) -> str:
        """定义的指纹：yaml 子树 + 代码 + 解析后的 deps + **spec 级口径**。

        universe 与 lookback 都逐值改变输出（池外整列 NaN；预热决定 TS 算子初值），
        不进指纹的话，改一行 yaml 头就能绕过 §3.3 那道"定义变了就拒绝写入"的闸门。
        """
        h = hashlib.sha256()
        h.update(self.src.encode())
        h.update(f"|universe={self.universe}|lookback={self.lookback}".encode())
        h.update(self.code.read_bytes() if self.code.exists() else b"")
        for d in sorted(self.deps):
            h.update(d.encode())
        return "sha256:" + h.hexdigest()[:16]


@dataclass
class Spec:
    region: str
    path: Path
    repo: str
    node_dir: str
    nodes: dict[str, NodeSpec]
    universe: str | None = None
    lookback: int = 0
    return_metric: str | None = None
    booksize: float | None = None
    cost_model: str | None = None
    sim: dict = field(default_factory=dict)
    cutoff: str | None = None          # `_tc` 模板的有效替换值
    region_hash: str | None = None     # 规范化后的 region 内容 hash（§二）


# ------------------------------------------------------------------ 加载器

RANKS = {("di",), ("di", "ii"), ("di", "ii", "ti")}

# yaml 的键集是**封闭**的。此前没有白名单, 认不得的键被 doc.get 静默丢掉:
# `universe:` 拼错一个字母, spec.universe 就是 None, 池子是 None, 掩码恒 True,
# 预检里每一处 universe 检查都在 `if spec.universe:` 后面——于是 alpha 悄悄按
# 全部 503 只票交易而不是 us_top400, 同一个 .py 因为一个它从未提到的 yaml 键
# 而给出不同的数。`lookback:` 拼错同理, 预热直接变 0。
FILE_KEYS = {"region", "universe", "lookback", "nodes", "cutoff", "booksize",
             "return_metric", "cost_model", "sim"}
NODE_KEYS = {"code", "params", "deps", "ops", "outputs"}
OUTPUT_KEYS = {"dtype", "dims", "grid", "ops"}


def _closed(got, allowed: set[str], where: str, what: str) -> None:
    """认不得的键必须报错, 且要给出最接近的候选——拼错的键静默消失是最贵的一类错。"""
    import difflib
    unknown = sorted(set(got) - allowed)
    if not unknown:
        return
    hint = difflib.get_close_matches(unknown[0], sorted(allowed), 1)
    raise ConfigError(
        f"{where}: unknown {what} key(s) {unknown}"
        + (f"; did you mean {hint[0]}?" if hint else "")
        + f"\n  allowed: {sorted(allowed)}")


def _dims(raw, where: str) -> tuple[str, ...]:
    """dims 必须是三种秩之一（§3.6）。

    此前这里只做 `tuple(...)`, 没有任何成员检查: `dims: [di, zz]` 会被原样收下, 然后
    在 store / ctx / node 的每一处 `dims == (...)` 分支里全部落到 else, 最后以
    "rank-3 必须声明 grid" 的形式炸出来——报的是另一个问题, 而错在写下 dims 的那一行。
    """
    d = tuple(raw)
    if d not in RANKS:
        raise ConfigError(
            f"{where}: dims={list(d)} is not a rank; must be one of "
            f"{sorted(list(r) for r in RANKS)}")
    return d


def _params(raw, where: str) -> dict:
    """params 认两种写法, 归一成一个 dict。

        params:                 params:
          window: 5               - window: 5
          halflife: 7               halflife: 7

    列表形是手写 yaml 时很自然的一种笔误/习惯（想着"一串参数"就先敲了个横杠）,
    两种在语义上没有任何差别, 与其让它以 `list has no attribute get` 的形式在
    半里深处炸开, 不如在这里收下并归一。

    **归一必须发生在指纹之前**: NodeSpec.src 是 safe_dump(解析后的 body), 两种写法
    若各自原样进去就会 hash 出两个指纹、指向同一份定义——那正是折叠规则要防的那种分叉。

    重复键必须报错: {window: 5} 与 {window: 7} 合并时后者悄悄覆盖前者, 是最难查的
    一类配置错误。
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, list):
        out: dict = {}
        for item in raw:
            if not isinstance(item, dict):
                raise ConfigError(
                    f"{where}: every entry in a params list must be `name: value`, got {item!r}")
            dup = set(item) & set(out)
            if dup:
                raise ConfigError(f"{where}: params declares {sorted(dup)} more than once")
            out.update(item)
        return out
    raise ConfigError(f"{where}: params must be a mapping or a list of mappings, got {type(raw).__name__}")


def _canon(obj) -> str:
    """region 的规范化文本（§4.1.1）：递归按键排序、数值最短往返、UTF-8、LF。

    规范化规则本身是契约的一部分——换一种排序或数值写法就会得到不同的 hash，
    "提交时校验 region_hash 等于模板标准值"这条随之失效。
    """
    if isinstance(obj, dict):
        return "{" + ",".join(f"{k}:{_canon(obj[k])}" for k in sorted(obj)) + "}"
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(_canon(x) for x in obj) + "]"
    if isinstance(obj, bool) or obj is None:
        return repr(obj)
    if isinstance(obj, (int, float)):
        return repr(float(obj)) if isinstance(obj, float) else repr(obj)
    return str(obj)


def load_region(path: Path, region: str) -> tuple[dict, str | None]:
    """从 config 所在 repo 找 regions/{region}.yaml。

    §二 的可比性机制完全建立在它之上：口径（booksize / sim / return_metric）来自这里，
    规范化后的内容 hash 进权重 meta，提交 alpha 池时按 hash 校验——自由研究、统一提交。
    """
    for base in list(path.parents)[:5]:
        f = base / "regions" / f"{region}.yaml"
        if f.exists():
            doc = yaml.safe_load(f.read_text()) or {}
            return doc, "sha256:" + hashlib.sha256(_canon(doc).encode()).hexdigest()[:16]
    return {}, None


def find_region(region: str, repo: str | None = None,
                root: Path | None = None) -> tuple[dict, str | None, Path | None]:
    """不带 config 路径时定位 `regions/{region}.yaml`——`pnl` 的入口是一个 store ref。

    按 §二, region 文件是**可比性的锚**: 口径 hash 进权重 meta, 提交 alpha 池时按 hash
    校验。各 repo 各存一份、内容必须一致。所以这里扫到多份就逐一比 hash, 不一致直接
    报错——口径一旦悄悄分叉, 两个人算出来的 Sharpe 就不再可比, 而这件事没有任何
    别的地方会喊出来。给了 repo 就先按 repo 找（alpha 属于哪个 repo, 就按那个 repo
    声明的口径评估）。
    """
    root = root or Path.cwd()
    cands: list[Path] = []
    if repo:
        f = root / "repos" / repo / "regions" / f"{region}.yaml"
        if f.exists():
            cands = [f]
    if not cands:
        cands = sorted(root.glob(f"repos/*/regions/{region}.yaml"))
    if not cands:
        return {}, None, None
    seen: dict[str, list[Path]] = {}
    for f in cands:
        doc = yaml.safe_load(f.read_text()) or {}
        h = "sha256:" + hashlib.sha256(_canon(doc).encode()).hexdigest()[:16]
        seen.setdefault(h, []).append(f)
    if len(seen) > 1:
        detail = "\n".join(f"  {h}  {', '.join(str(x) for x in fs)}" for h, fs in seen.items())
        raise ConfigError(
            f"region `{region}` differs between repos -- the convention has forked and cross-repo "
            f"results are no longer comparable:\n"
            f"{detail}\n  These copies must be byte-identical; change the convention in all of them together.")
    doc = yaml.safe_load(cands[0].read_text()) or {}
    return doc, next(iter(seen)), cands[0]


def resolve_tc(ref: str, cutoff: str | None) -> str:
    """把引用名里结尾的 `_tc` 换成有效 cutoff（§4.9.5）。

    **按消费节点自身的 cutoff 解析, 不是按生产者的**——生产者可能在节点级覆盖过,
    不定这条规则的话消费者会静默绑到另一个 cutoff 的数据上, 而这正是前视检查
    要防的那类错误、却发生在检查的上游。
    """
    if not ref.endswith("_tc"):
        return ref
    if not cutoff:
        raise ConfigError(
            f"`{ref}` uses the `_tc` template but there is no effective cutoff to substitute -- "
            f"supply one via the node's params.cutoff, a file-level cutoff, or time_cutoff "
            f"in regions/{{region}}.yaml.")
    return ref[:-3] + "_" + str(cutoff)
def _norm_ops(raw, where: str) -> list:
    """块状或内联都接受；参数按签名类型校验（§4.11.6 ⑦）。"""
    out = []
    for item in raw or []:
        if isinstance(item, str):
            op, arg = item, None
        elif isinstance(item, dict) and len(item) == 1:
            (op, arg), = item.items()
        else:
            raise ConfigError(f"{where}: malformed op entry: {item!r}")
        if op not in OP_TYPES:
            raise ConfigError(f"{where}: unknown op {op} (available: {sorted(OP_TYPES)})")
        want = OP_TYPES[op]
        if isinstance(want, tuple):                 # 参数可省
            if arg is not None and not isinstance(arg, want):
                raise ConfigError(f"{where}: the argument to {op} must be a name or omitted, got {arg!r}")
            out.append((op, arg))
            continue
        if want is type(None):
            if arg is not None:
                raise ConfigError(f"{where}: {op} takes no argument, got {arg!r}")
        elif want is float:
            if not isinstance(arg, (int, float)) or isinstance(arg, bool):
                raise ConfigError(
                    f"{where}: {op} needs a number, got {arg!r} ({type(arg).__name__}). YAML will silently "
                    f"turn a stray comma in `- {op}: 0.02,` into a string.")
        elif want is int:
            if not isinstance(arg, int) or isinstance(arg, bool) or arg <= 0:
                raise ConfigError(f"{where}: {op} needs a positive integer, got {arg!r}")
        elif want is str:
            if not isinstance(arg, str):
                raise ConfigError(f"{where}: {op} needs a name, got {arg!r}")
            if op == "neutralize":
                parse_ref(arg)          # 必须是全 ref，不能是裸名
        out.append((op, arg))
    return out


def load_spec(path: str | Path, repo: str | None = None) -> Spec:
    path = Path(path)
    doc = yaml.safe_load(path.read_text()) or {}
    node_dir = path.parent.name
    repo = repo or path.parent.parent.parent.name

    _closed(doc, FILE_KEYS, str(path), "file-level")
    region = doc.get("region", "us")
    rdoc, rhash = load_region(path, region)
    def pick(key):                      # config 覆盖 region；两者都没有就是 None
        return doc.get(key, rdoc.get(key))
    cutoff = doc.get("cutoff", rdoc.get("time_cutoff"))
    bs = pick("booksize")
    if isinstance(bs, str):
        raise ConfigError(
            f"{path}: booksize={bs!r} is a string -- in YAML 1.1 neither `20e6` nor `2.0e7` is a "
            f"number (the exponent needs a sign). Write the integer literal 20000000.")

    # universe **不从 region 继承**：§4.4 规定数据节点缺省是全集, 而且这是语义必需
    # 而非偷懒——数据若在池内算, 边缘票取不到正确值、进出池处会留下滚动窗口断口。
    # region 里的 universe 是给 alpha 的规范池子, alpha config 自己显式写出来。
    spec = Spec(region=region, path=path, repo=repo, node_dir=node_dir, nodes={},
                universe=doc.get("universe"), lookback=int(doc.get("lookback", 0)),
                return_metric=pick("return_metric"),
                booksize=(float(bs) if bs is not None else None),
                cost_model=pick("cost_model"), sim=pick("sim") or {},
                cutoff=cutoff, region_hash=rhash)
    if spec.universe:
        spec.universe = resolve_tc(spec.universe, cutoff)
        parse_ref(spec.universe)
    if spec.return_metric:
        spec.return_metric = resolve_tc(spec.return_metric, cutoff)

    for name, body in (doc.get("nodes") or {}).items():
        body = body or {}
        kind = name.split("_", 1)[0]
        if kind not in KINDS:
            raise ConfigError(
                f"{path}: a node name must start with one of {'/'.join(KINDS)} ({{kind}}_{{ns}}_{{name}}): {name}")
        bits = name.split("_")
        if len(bits) < 3:
            raise ConfigError(f"{path}: a node name must be {{kind}}_{{ns}}_{{name}}: {name}")
        ns, short = bits[1], "_".join(bits[2:])
        if not NS_RE.match(ns):
            raise ConfigError(
                f"{path}: the ns segment `{ns}` of node {name} is malformed -- ns must be a single "
                f"lowercase identifier with no underscore, or {{kind}}_{{ns}}_{{name}} cannot be split")
        check_name(short, f"the name segment of {path}:{name}")
        if repo.startswith("g_") and repo != "g_common" and ns != repo[2:]:
            raise ConfigError(
                f"{path}: node {name} has ns segment `{ns}` but lives in {repo} -- a personal repo may "
                f"only write its own ns ({repo[2:]}).")

        _closed(body, NODE_KEYS, f"{path}:{name}", "node")
        code = path.parent / (body.get("code") or f"{path.stem}.py")
        node_ops = _norm_ops(body.get("ops"), f"{path}:{name}.ops")
        raw_out = body.get("outputs")
        outs: dict[str, Output] = {}
        if not raw_out:
            default = "weight" if kind == "alpha" else short
            outs[default] = Output(default, ops=node_ops)
        else:
            if node_ops and any((o or {}).get("ops") for o in raw_out.values()):
                raise ConfigError(f"{path}:{name}: node-level ops and outputs.*.ops cannot both be present")
            if node_ops and len(raw_out) > 1:
                raise ConfigError(
                    f"{path}:{name}: node-level `ops` is sugar for the SINGLE-OUTPUT case, but this node "
                    f"declares {len(raw_out)} outputs ({sorted(raw_out)}) -- which one it should "
                    f"apply to has no answer. Put it under each outputs.{{key}}.ops instead.")
            if len(raw_out) == 1:
                default = "weight" if kind == "alpha" else short
                (only,) = raw_out
                if only != default:
                    raise ConfigError(
                        f"{path}:{name}: the single output key must equal the default name `{default}`, but is "
                        f"written `{only}` (§4.11.6 check 3). To use another name, declare multiple "
                        f"outputs explicitly or rename the node -- otherwise the identity no longer "
                        f"matches the data it produces.")
            for k, o in raw_out.items():
                o = o or {}
                check_name(k, f"the output name of {path}:{name}")
                _closed(o, OUTPUT_KEYS, f"{path}:{name}.{k}", "output")
                outs[k] = Output(k, dtype=o.get("dtype", "f4"),
                                 dims=_dims(o.get("dims", ("di", "ii")), f"{path}:{name}.{k}"),
                                 grid=o.get("grid"),
                                 ops=_norm_ops(o.get("ops"), f"{path}:{name}.{k}.ops")
                                     or (node_ops if len(raw_out) == 1 else []))
        for o in outs.values():
            if o.dims == ("di", "ii", "ti") and not o.grid:
                raise ConfigError(f"{path}:{name}.{o.key}: a rank-3 output must declare grid")
            for op, _ in o.ops:
                if op in CS_OPS and o.dims != ("di", "ii"):
                    raise ConfigError(
                        f"{path}:{name}.{o.key}: the CS op `{op}` is legal only for rank-2; this output has "
                        f"dims={list(o.dims)} (§3.6)")
        # `_tc` 按**本节点**的有效 cutoff 解析（节点 params > 文件级 > region）
        node_params = _params(body.get("params"), f"{path}:{name}")
        if "params" in body:
            # 归一后回写, 指纹只认这一种形状（两种写法必须 hash 成同一个）。
            # 只在本来就有这个键时回写——没写 params 的节点凭空多出一个 `params: {}`
            # 会让 safe_dump 的结果变样, 指纹随之改变, 而定义其实一个字都没动。
            body["params"] = node_params
        node_cutoff = node_params.get("cutoff", cutoff)
        deps = [resolve_tc(str(d), node_cutoff) for d in (body.get("deps") or [])]
        for d in deps:
            parse_ref(d[:-1] + "x" if is_wildcard(d) else d)
        for o in outs.values():
            o.ops = [(op, resolve_tc(a, node_cutoff) if op == "neutralize" else a)
                     for op, a in o.ops]
        node = NodeSpec(name=name, node_dir=node_dir, repo=repo, code=code, deps=deps,
                        params=node_params, outputs=outs,
                        src=yaml.safe_dump(body, sort_keys=True, allow_unicode=True),
                        universe=spec.universe, lookback=spec.lookback)
        spec.nodes[name] = node

        if kind == "alpha":
            for o in node.outputs.values():
                if o.dims != ("di", "ii"):
                    raise ConfigError(f"{path}:{name}: an alpha must be rank-2 -- weights are di x ii")
                if not o.ops or o.ops[-1][0] != "scale":
                    raise ConfigError(
                        f"{path}:{name}.{o.key}: an alpha's ops chain must end in scale (§4.4). Without it, "
                        f"upstream weights that each satisfy Sigma|w|=1 shrink through "
                        f"cancellation once combined -- the book is under-deployed while Sharpe "
                        f"still looks normal.")
    for n in spec.nodes.values():
        _check_tags(n, path, list(spec.nodes))
    return spec


def _in_family(name: str, siblings: list[str]) -> bool:
    """同一 yaml 里是否还有共享词干的兄弟节点——即这是不是一族变体。"""
    short = "_".join(name.split("_")[2:])
    for other in siblings:
        if other == name:
            continue
        o = "_".join(other.split("_")[2:])
        common = len(os.path.commonprefix([short, o]))
        if common >= 3:
            return True
    return False


def _check_tags(node: NodeSpec, path: Path, siblings: list[str]) -> None:
    """名字里的参数标签必须与 params 一致（§4.11.4）。

    标签**只在一族有 ≥2 个成员时才强制**——单个从未被扫描、数字是行业惯用语的名字
    可以粘连（adv20 / illiq20 / rvol20）。但只要标签写了, 就必须与 params 对得上,
    这条抓的是"复制了一个变体却只改了 params 忘了改名"。
    """
    short = "_".join(node.name.split("_")[2:])
    family = _in_family(node.name, siblings)
    for key, tag in TAGS.items():
        if key not in node.params:
            continue
        val = node.params[key]
        m = re.search(rf"_{tag}(\d+)(?:_|$)", short)
        if not m:
            if family:
                raise ConfigError(
                    f"{path}:{node.name}: this is one of a family of variants and params carries {key}={val}, "
                    f"but the name has no `_{tag}...` tag (§4.11.4: a tag becomes mandatory as soon "
                    f"as a family has a second member)")
            continue
        if int(m.group(1)) != int(val):
            raise ConfigError(
                f"{path}:{node.name}: the name says {tag}={m.group(1)} but params says {key}={val} -- "
                f"most likely a variant was copied and only params was edited.")
