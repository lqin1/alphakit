"""算子的**声明**：有哪些算子、各自收什么参数、要多少预热（architecture.md §3.6 / §6.2）。

这一份是单一出处。此前同一件事被写了七遍——config 里四处（`CS_OPS`、`TS_OPS`、
`OP_TYPES`、`_norm_ops` 的类型阶梯）、ops 里三处（`_CS_OPS`、`_TS_FACTORY`、
`ops_lookback` 的阶梯）——两边靠 `tests/test_core.py` 的一条断言互相盯着。
用一条测试去守住两份重复, 说明这份知识本来就该只有一份: 测试能告诉你它们不一致了,
不能阻止你只改其中一份。

**为什么放在 core 而不是 runner**：编译期（config 校验 yaml）与执行期（OpChain 分派）
都要它, 而 core 不能反向依赖 runner。所以这里只放**声明**, 实现留在 `runner/ops.py`;
那边在 import 时按本表核对自己的分派表, 少一个多一个都是 ImportError——接缝从一条
测试搬进了代码本身。

加一个算子: 在 `OPS` 里加一条, 在 `runner/ops.py` 里加实现。两处, 且第二处漏了会
在 import 时就炸。此前是五到六处, 漏了要等测试跑。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .naming import ConfigError

# 递推形式自带按有效权重归一, 第一天就是一个合法的加权平均, 故这里要的不是「算得出」
# 而是「与更长的历史算得一样」——4 个半衰期覆盖 93.75% 的稳态权重。
EXP_DECAY_WARMUP_HALFLIVES = 4

CS, TS = "cs", "ts"


@dataclass(frozen=True)
class OpSpec:
    name: str
    kind: str                       # CS（沿 ii 截面）或 TS（沿 di 有状态）
    arg: type | tuple               # 期望的参数类型；元组含 NoneType 表示可省
    lookback: Callable[[object], int] = lambda a: 0   # 这个算子要多少天先前历史
    ref_arg: bool = False           # 参数必须是全 ref 而非裸名

    @property
    def optional_arg(self) -> bool:
        return isinstance(self.arg, tuple) and type(None) in self.arg


# n 日窗口只需 n-1 天**先前**数据——当日自己算第 n 天。delay k 则要整 k 天。
OPS: dict[str, OpSpec] = {
    "rank":         OpSpec("rank", CS, type(None)),
    "neutralize":   OpSpec("neutralize", CS, str, ref_arg=True),
    "truncate":     OpSpec("truncate", CS, float),
    # 裸写 `scale` 时 OpChain 把 None 当作 book；编译期不接受就会拒掉执行期明确支持的写法
    "scale":        OpSpec("scale", CS, (str, type(None))),
    "linear_decay": OpSpec("linear_decay", TS, int, lambda a: int(a) - 1),
    "exp_decay":    OpSpec("exp_decay", TS, int,
                           lambda a: EXP_DECAY_WARMUP_HALFLIVES * int(a)),
    "delay":        OpSpec("delay", TS, int, lambda a: int(a)),
}

CS_OPS = frozenset(k for k, v in OPS.items() if v.kind == CS)
TS_OPS = frozenset(k for k, v in OPS.items() if v.kind == TS)


def lookback(ops: list[tuple[str, object]]) -> int:
    """一条 ops 链要多少天先前历史。

    **TS 算子串联时窗口相加**：`delay:2` 接 `linear_decay:5` 要 2 + 4 = 6 天。
    曾经 runner 另有一份独立实现, 对这个例子给 5——预热不足会让最初几天的输出
    来自未填满的缓冲, 数值看着合理却是错的。
    """
    return sum(OPS[op].lookback(arg) for op, arg in ops if op in OPS)


def check_covers(names, who: str) -> None:
    """实现方在 import 时自证覆盖了本表, 不多不少。"""
    have, want = set(names), set(OPS)
    if have != want:
        raise ImportError(
            f"{who} does not match core.opspec.OPS: "
            f"missing {sorted(want - have)}, extra {sorted(have - want)}")


def validate_arg(op: str, arg, where: str) -> None:
    """编译期参数校验。报错要说清收到的是什么类型——YAML 的坑多半是类型而非值。"""
    if op not in OPS:
        raise ConfigError(f"{where}: unknown op {op} (available: {sorted(OPS)})")
    spec = OPS[op]
    want = spec.arg
    if spec.optional_arg:
        if arg is not None and not isinstance(arg, tuple(t for t in want if t is not type(None))):
            raise ConfigError(
                f"{where}: the argument to {op} must be a name or omitted, got {arg!r}")
        return
    if want is type(None):
        if arg is not None:
            raise ConfigError(f"{where}: {op} takes no argument, got {arg!r}")
    elif want is float:
        if not isinstance(arg, (int, float)) or isinstance(arg, bool):
            raise ConfigError(
                f"{where}: {op} needs a number, got {arg!r} ({type(arg).__name__}). YAML will "
                f"silently turn a stray comma in `- {op}: 0.02,` into a string.")
    elif want is int:
        if not isinstance(arg, int) or isinstance(arg, bool) or arg <= 0:
            raise ConfigError(f"{where}: {op} needs a positive integer, got {arg!r}")
    elif want is str:
        if not isinstance(arg, str):
            raise ConfigError(f"{where}: {op} needs a name, got {arg!r}")
