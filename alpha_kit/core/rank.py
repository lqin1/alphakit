"""秩：一个 L3 输出的形状语义（architecture.md §3.6）。

单独成模块, 是因为"秩是什么"此前不属于任何一个模块——它只以 `dims == ("di","ii")`
这样的字面元组比较散落在 store / ctx / node / config / preflight 五处, 每一处各自
再推导一遍形状、分块、掩码是否适用、返回值该长什么样。后果有两个:

  · 加一种秩要在五个文件的近三十处分支里各补一刀;
  · 没有任何一处在**解析** dims, 所以它压根没有成员校验——`dims: [di, zz]` 会被
    原样收下, 在每一处比较里落到 else, 最后以 "rank-3 必须声明 grid" 的形式炸出来,
    报的是另一个问题, 而错在写下 dims 的那一行。

这里只放**定义**: 有哪几种秩、各自的形状与分块、以及别人真正会问的那几个谓词。
逐日主循环里"这一秩的返回值该是标量/截面/矩阵"那种分派仍留在 ctx——那是取值契约,
不是形状语义, 合并进来只会让这个模块重新变浅。
"""
from __future__ import annotations

from .naming import ConfigError

# di 恒为第一轴：chunk 沿 di 切, 日更才是 O(1) 追加而不是重写全history（§3.3）
DI = ("di",)
DI_II = ("di", "ii")
DI_II_TI = ("di", "ii", "ti")
ALL = (DI, DI_II, DI_II_TI)

CHUNK_DI = 50          # 秩-2：50 天一块, 全宽
CHUNK_RANK1 = 4096     # 秩-1：一维, 块开大些


def parse(raw, where: str) -> tuple[str, ...]:
    """dims → 一个合法的秩, 否则在写下它的那一行报错。"""
    d = tuple(raw)
    if d not in ALL:
        raise ConfigError(
            f"{where}: dims={list(d)} is not a rank; must be one of "
            f"{[list(r) for r in ALL]}")
    return d


def canon(dims) -> tuple[str, ...]:
    """store 里存的是 list（JSON 没有元组）, 比较前先归一。"""
    return tuple(dims)


def has_cross_section(dims) -> bool:
    """有没有 ii 轴——CS 算子、池子掩码、按列统计都只对这一类成立（§3.5 / §3.6）。"""
    return "ii" in canon(dims)


def is_panel(dims) -> bool:
    """恰好是 di×ii。

    与 has_cross_section 的区别要紧: 秩-3 也有 ii 轴, 但它的截面轴不唯一（还有 ti）,
    所以 CS 算子、alpha 权重这些"沿 ii 做截面"的东西要的是**恰好秩-2**, 不是"有 ii"。
    两者混用过一次就会得到"秩-3 上允许 rank 算子"这种说不清的口径。
    """
    return canon(dims) == DI_II


def n_axes(dims) -> int:
    return len(canon(dims))


def shape(dims, n_sessions: int, n_alloc: int, grid_len: int | None) -> tuple[int, ...]:
    d = canon(dims)
    if d == DI:
        return (n_sessions,)
    if d == DI_II:
        return (n_sessions, n_alloc)
    return (n_sessions, n_alloc, _grid(grid_len))


def chunks(dims, n_alloc: int, grid_len: int | None) -> tuple[int, ...]:
    """分块策略。

    秩-3 取 `(1, N, T)`——一个 session 一个文件——与秩-2 的 `(50, N)` 是同一条理由的
    两端: di 必须是第一轴且块沿 di 切, 否则日更要重写所有块。秩-3 的面板大到一天
    就值得单独一块。
    """
    d = canon(dims)
    if d == DI:
        return (CHUNK_RANK1,)
    if d == DI_II:
        return (CHUNK_DI, n_alloc)
    return (1, n_alloc, _grid(grid_len))


def _grid(grid_len: int | None) -> int:
    if not grid_len:
        raise ConfigError("a rank-3 output must declare grid")
    return int(grid_len)
