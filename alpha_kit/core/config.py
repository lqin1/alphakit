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
            f"region `{region}` 在各 repo 下内容不一致——口径已分叉, 跨 repo 结果不再可比:\n"
            f"{detail}\n  这两份必须逐字一致; 要改口径就一起改。")
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
            f"`{ref}` 用了 `_tc` 模板，但没有有效的 cutoff 可替换——"
            f"请在节点 params.cutoff、文件级 cutoff、或 regions/{{region}}.yaml 的 "
            f"time_cutoff 里给出一个。")
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
            raise ConfigError(f"{where}: 算子写法不合法：{item!r}")
        if op not in OP_TYPES:
            raise ConfigError(f"{where}: 未知算子 {op}（可用：{sorted(OP_TYPES)}）")
        want = OP_TYPES[op]
        if isinstance(want, tuple):                 # 参数可省
            if arg is not None and not isinstance(arg, want):
                raise ConfigError(f"{where}: {op} 的参数须是名字或省略，却收到 {arg!r}")
            out.append((op, arg))
            continue
        if want is type(None):
            if arg is not None:
                raise ConfigError(f"{where}: {op} 不接受参数，却收到 {arg!r}")
        elif want is float:
            if not isinstance(arg, (int, float)) or isinstance(arg, bool):
                raise ConfigError(
                    f"{where}: {op} 需要一个数，却收到 {arg!r}（{type(arg).__name__}）。"
                    f"YAML 会静默把 `- {op}: 0.02,` 里多出的逗号连成字符串。")
        elif want is int:
            if not isinstance(arg, int) or isinstance(arg, bool) or arg <= 0:
                raise ConfigError(f"{where}: {op} 需要一个正整数，却收到 {arg!r}")
        elif want is str:
            if not isinstance(arg, str):
                raise ConfigError(f"{where}: {op} 需要一个名字，却收到 {arg!r}")
            if op == "neutralize":
                parse_ref(arg)          # 必须是全 ref，不能是裸名
        out.append((op, arg))
    return out


def load_spec(path: str | Path, repo: str | None = None) -> Spec:
    path = Path(path)
    doc = yaml.safe_load(path.read_text()) or {}
    node_dir = path.parent.name
    repo = repo or path.parent.parent.parent.name

    region = doc.get("region", "us")
    rdoc, rhash = load_region(path, region)
    def pick(key):                      # config 覆盖 region；两者都没有就是 None
        return doc.get(key, rdoc.get(key))
    cutoff = doc.get("cutoff", rdoc.get("time_cutoff"))
    bs = pick("booksize")
    if isinstance(bs, str):
        raise ConfigError(
            f"{path}: booksize={bs!r} 是字符串——YAML 1.1 里 `20e6` / `2.0e7` 都不是数字"
            f"（指数要带符号）。写成整数字面量 20000000。")

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
                f"{path}: 节点名须以 {'/'.join(KINDS)} 之一开头（{{kind}}_{{ns}}_{{name}}）：{name}")
        bits = name.split("_")
        if len(bits) < 3:
            raise ConfigError(f"{path}: 节点名须是 {{kind}}_{{ns}}_{{name}}：{name}")
        ns, short = bits[1], "_".join(bits[2:])
        if not NS_RE.match(ns):
            raise ConfigError(
                f"{path}: 节点 {name} 的 ns 段 `{ns}` 不合语法——ns 必须是单段小写标识符"
                f"（不含下划线），否则 {{kind}}_{{ns}}_{{name}} 无从切分")
        check_name(short, f"{path}:{name} 的 name 段")
        if repo.startswith("g_") and repo != "g_common" and ns != repo[2:]:
            raise ConfigError(
                f"{path}: 节点 {name} 的 ns 段是 `{ns}`，但它住在 {repo} 里——"
                f"个人 repo 只能写自己的 ns（{repo[2:]}）。")

        code = path.parent / (body.get("code") or f"{path.stem}.py")
        node_ops = _norm_ops(body.get("ops"), f"{path}:{name}.ops")
        raw_out = body.get("outputs")
        outs: dict[str, Output] = {}
        if not raw_out:
            default = "weight" if kind == "alpha" else short
            outs[default] = Output(default, ops=node_ops)
        else:
            if node_ops and any((o or {}).get("ops") for o in raw_out.values()):
                raise ConfigError(f"{path}:{name}: 节点级 ops 与 outputs.*.ops 不能同时出现")
            if node_ops and len(raw_out) > 1:
                raise ConfigError(
                    f"{path}:{name}: 节点级 `ops` 是**单输出的语法糖**，但这里声明了 "
                    f"{len(raw_out)} 个输出（{sorted(raw_out)}）——该给哪一个用是无解的。"
                    f"请写在各 outputs.{{key}}.ops 下。")
            if len(raw_out) == 1:
                default = "weight" if kind == "alpha" else short
                (only,) = raw_out
                if only != default:
                    raise ConfigError(
                        f"{path}:{name}: 单输出的 key 必须等于缺省名 `{default}`，"
                        f"却写成 `{only}`（§4.11.6 检查③）。要用别的名字就显式声明多个输出，"
                        f"或改节点名——否则 identity 与它产出的数据对不上号。")
            for k, o in raw_out.items():
                o = o or {}
                check_name(k, f"{path}:{name} 的输出名")
                outs[k] = Output(k, dtype=o.get("dtype", "f4"),
                                 dims=tuple(o.get("dims", ("di", "ii"))),
                                 grid=o.get("grid"),
                                 ops=_norm_ops(o.get("ops"), f"{path}:{name}.{k}.ops")
                                     or (node_ops if len(raw_out) == 1 else []))
        for o in outs.values():
            if o.dims == ("di", "ii", "ti") and not o.grid:
                raise ConfigError(f"{path}:{name}.{o.key}: 秩-3 必须声明 grid")
            for op, _ in o.ops:
                if op in CS_OPS and o.dims != ("di", "ii"):
                    raise ConfigError(
                        f"{path}:{name}.{o.key}: CS 类算子 `{op}` 只对秩-2 合法，"
                        f"该输出是 dims={list(o.dims)}（§3.6）")
        # `_tc` 按**本节点**的有效 cutoff 解析（节点 params > 文件级 > region）
        node_cutoff = (body.get("params") or {}).get("cutoff", cutoff)
        deps = [resolve_tc(str(d), node_cutoff) for d in (body.get("deps") or [])]
        for d in deps:
            parse_ref(d[:-1] + "x" if is_wildcard(d) else d)
        for o in outs.values():
            o.ops = [(op, resolve_tc(a, node_cutoff) if op == "neutralize" else a)
                     for op, a in o.ops]
        node = NodeSpec(name=name, node_dir=node_dir, repo=repo, code=code, deps=deps,
                        params=body.get("params") or {}, outputs=outs,
                        src=yaml.safe_dump(body, sort_keys=True, allow_unicode=True),
                        universe=spec.universe, lookback=spec.lookback)
        spec.nodes[name] = node

        if kind == "alpha":
            for o in node.outputs.values():
                if o.dims != ("di", "ii"):
                    raise ConfigError(f"{path}:{name}: alpha 必须是秩-2，权重是 di×ii")
                if not o.ops or o.ops[-1][0] != "scale":
                    raise ConfigError(
                        f"{path}:{name}.{o.key}: alpha 的 ops 链必须以 scale 收尾（§4.4）。"
                        f"少了它，上游各自 Σ|w|=1 的权重线性组合后会因抵消而缩水，"
                        f"账本投不满而 Sharpe 看着正常。")
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
                    f"{path}:{node.name}: 这是一族变体中的一个，params 里有 {key}={val}，"
                    f"名字里却没有 `_{tag}...` 标签（§4.11.4：一族有第 2 个成员即强制带标签）")
            continue
        if int(m.group(1)) != int(val):
            raise ConfigError(
                f"{path}:{node.name}: 名字说 {tag}={m.group(1)}，params 说 {key}={val}——"
                f"多半是复制了一个变体却只改了 params。")
