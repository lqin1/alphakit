"""命名层：引用名的语法与解析（architecture.md §3.2 / §4.11）。

单独成模块，是因为它有**两个互不相关的消费者**：`store` 只需要"引用名 ↔ 路径"这一条
（它不关心 yaml 长什么样），`config` 需要全套语法与保留字检查。存储层不该为了拿一个
`parse_ref` 就依赖整个配置加载器。
"""
from __future__ import annotations

import keyword
import re
from dataclasses import dataclass

KINDS = ("field", "factor", "alpha")
NAME_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")
# ns 是**单段**：node_name 按 split("_", 2) 解析, ns 含下划线就无从切分
NS_RE = re.compile(r"^[a-z][a-z0-9]*$")
RESERVED = {"all", "nodes", "outputs", "deps", "code", "params", "ops", "region",
            "universe", "lookback", "dims", "grid", "dtype", "sim", "booksize",
            "return_metric", "cost_model"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Ref:
    repo: str
    node_dir: str
    node_name: str
    output: str

    def __str__(self) -> str:
        # node_name 与 node_dir 同名时那一段不携带任何信息（§4.11）:
        # `g_common.field_base_px.field_base_px-adj_close_1500` 中间那次重复纯是噪声。
        # 折叠成 {repo}.{node_dir}.{output} 后仍可无歧义还原——名字里不允许出现连字符,
        # 所以"叶子里没有连字符"唯一地表示 node_name == node_dir。
        if self.node_name == self.node_dir:
            return f"{self.repo}.{self.node_dir}.{self.output}"
        return f"{self.repo}.{self.node_dir}.{self.node_name}-{self.output}"

    @property
    def leaf(self) -> str:
        """落到磁盘上的那一段, 与 __str__ 的折叠规则保持一致。"""
        return self.output if self.node_name == self.node_dir else f"{self.node_name}-{self.output}"

    @property
    def kind(self) -> str:
        return self.node_name.split("_", 1)[0]

    @property
    def ns(self) -> str:
        return self.node_name.split("_")[1]


def parse_ref(ref: str) -> Ref:
    """`{repo}.{node_dir}.{node_name}-{output}` → Ref。纯字符串，无需索引。"""
    parts = ref.split(".")
    if len(parts) != 3:
        raise ConfigError(f"a ref must have three segments {{repo}}.{{node_dir}}.{{node_name}}-{{output}}: {ref}")
    repo, node_dir, leaf = parts
    if ref != ref.lower():
        # §4.11.1 第 4 条：大小写不敏感的文件系统（macOS APFS 默认）上,
        # MktBeta 与 mktbeta 在一台机器上是同一个目录、在另一台上是两个。
        raise ConfigError(f"a ref must not contain uppercase: {ref}")
    if "-" in leaf:
        node_name, output = leaf.split("-", 1)
        if "-" in output:
            # 连字符只允许出现在 {node_name}-{output} 这一处接缝上, 否则 split 无从还原
            raise ConfigError(f"an output name must not contain a hyphen (the hyphen marks only the node/output seam): {ref}")
        if node_name == node_dir:
            # 不接受"展开形"作为同义拼法: 同一份数据有两种写法, 迟早一半代码写这种、
            # 一半写那种, 而它们会 hash 出不同的 fingerprint 却指向同一个数组。
            # 只留一种拼法, 差异就无处藏身。
            raise ConfigError(
                f"when node_name equals node_dir the middle segment must be omitted: {ref}\n"
                f"  write it as {repo}.{node_dir}.{output}")
    else:
        # 折叠形: 叶子就是输出名, 节点名由所在目录给出
        node_name, output = node_dir, leaf
    if not output:
        raise ConfigError(f"the output name is empty -- the directory name would end in a bare hyphen: {ref}")
    bits = node_name.split("_")
    if len(bits) < 3 or bits[0] not in KINDS:
        raise ConfigError(
            f"a node name must be {{kind}}_{{ns}}_{{name}} with kind in {'/'.join(KINDS)}: "
            f"{node_name} (from {ref})")
    if not NS_RE.match(bits[1]):
        raise ConfigError(f"the ns segment must be a single lowercase identifier with no underscore: `{bits[1]}` (from {ref})")
    return Ref(repo, node_dir, node_name, output)


def is_wildcard(ref: str) -> bool:
    """通配 dep 的两种写法。

    折叠形节点（node_name == node_dir）没有连字符, 其通配是 `{repo}.{node_dir}.*`;
    其余仍是 `{repo}.{node_dir}.{node_name}-*`。两者统一为"去掉末尾星号后前缀匹配",
    所以下游只需要认得这一个谓词, 不必各自去判断折叠与否。
    """
    return ref.endswith("-*") or ref.endswith(".*")


def check_name(s: str, what: str) -> None:
    if not NAME_RE.match(s):
        raise ConfigError(f"{what} is malformed (must start lowercase, use [a-z0-9_], no dot/hyphen/uppercase): {s}")
    if len(s) > 40:
        raise ConfigError(f"{what} exceeds 40 characters: {s}")
    if not s.isidentifier() or keyword.iskeyword(s):
        raise ConfigError(
            f"{what} must be a valid, non-keyword Python identifier: {s}\n"
            f"  Reason: output names are passed as keyword arguments to ctx.multi_outputs(**kw), "
            f"so an invalid name is a SyntaxError -- the module never loads and a friendly "
            f"error would never get the chance to run.")
    if s in RESERVED:
        raise ConfigError(f"{what} is a reserved word: {s}")
    if s.endswith("_tc"):
        raise ConfigError(f"{what} must not end in _tc (_tc is only a source-level template marker): {s}")


