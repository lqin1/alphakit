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
        raise ConfigError(f"引用名必须是三段 {{repo}}.{{node_dir}}.{{node_name}}-{{output}}：{ref}")
    repo, node_dir, leaf = parts
    if ref != ref.lower():
        # §4.11.1 第 4 条：大小写不敏感的文件系统（macOS APFS 默认）上,
        # MktBeta 与 mktbeta 在一台机器上是同一个目录、在另一台上是两个。
        raise ConfigError(f"引用名不得含大写：{ref}")
    if "-" in leaf:
        node_name, output = leaf.split("-", 1)
        if "-" in output:
            # 连字符只允许出现在 {node_name}-{output} 这一处接缝上, 否则 split 无从还原
            raise ConfigError(f"输出名不得含连字符（连字符只用于节点名与输出名的接缝）：{ref}")
        if node_name == node_dir:
            # 不接受"展开形"作为同义拼法: 同一份数据有两种写法, 迟早一半代码写这种、
            # 一半写那种, 而它们会 hash 出不同的 fingerprint 却指向同一个数组。
            # 只留一种拼法, 差异就无处藏身。
            raise ConfigError(
                f"node_name 与 node_dir 同名时须省略中间那段：{ref}\n"
                f"  应写作 {repo}.{node_dir}.{output}")
    else:
        # 折叠形: 叶子就是输出名, 节点名由所在目录给出
        node_name, output = node_dir, leaf
    if not output:
        raise ConfigError(f"输出名为空——目录名会以一个裸连字符结尾：{ref}")
    bits = node_name.split("_")
    if len(bits) < 3 or bits[0] not in KINDS:
        raise ConfigError(
            f"节点名须是 {{kind}}_{{ns}}_{{name}} 且 kind ∈ {'/'.join(KINDS)}："
            f"{node_name}（来自 {ref}）")
    if not NS_RE.match(bits[1]):
        raise ConfigError(f"ns 段须是单段小写标识符（不含下划线）：`{bits[1]}`（来自 {ref}）")
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
        raise ConfigError(f"{what} 不合语法（小写字母开头、[a-z0-9_]、不含点号/连字符/大写）：{s}")
    if len(s) > 40:
        raise ConfigError(f"{what} 超过 40 字符：{s}")
    if not s.isidentifier() or keyword.iskeyword(s):
        raise ConfigError(
            f"{what} 必须是合法 Python 标识符且非关键字：{s}\n"
            f"  理由：输出名会作为 ctx.multi_outputs(**kw) 的关键字参数传递，"
            f"非法名字是 SyntaxError——模块根本加载不了，友好报错也就执行不到。")
    if s in RESERVED:
        raise ConfigError(f"{what} 是保留字：{s}")
    if s.endswith("_tc"):
        raise ConfigError(f"{what} 不得以 _tc 结尾（_tc 只是源码形态的模板标记）：{s}")


