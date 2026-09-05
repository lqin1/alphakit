"""数据新鲜度：`ed` 被哪个依赖卡住, 卡在哪一天（architecture.md §7.3）。

**绝不静默算半截数据。** 任一依赖的最新 session 还没落地, `ed` 就该回退到它——
回退本身是对的, 不出声才是错的。

这几行此前有两份: `runner/node.effective_ed` 一份, `runner/preflight._window_checks`
里又抄了一份。抄的那份写明了理由——预检要多说一句"是谁卡住的", 而那个函数只回一个
日期; 且预检的定位是"读任何数据之前", 不该为此拖进 runner 的 numpy/pandas。理由成立,
但结论不该是写两遍: 把它放进一个两者都能便宜地 import 的地方就同时满足了。

于是它们真的分叉过: `node.py` 在算封顶前把通配 dep 整个丢掉, 而 preflight 用的是
展开后的集合——两边对同一个 yaml 算出不同的 effective_ed, 而这个模块存在的全部理由
就是让那个日期只有一个答案。

本模块只用到 `panels.axes` / `exists` / `meta`, 不 import numpy 或 pandas。
"""
from __future__ import annotations


def cap(panels, deps) -> tuple[int, str | None]:
    """返回 (可用到的 session 下标, 卡住它的那个 ref)。

    没有任何依赖落后时 binder 是 None——预检据此决定要不要出声, runner 据此封顶。
    """
    last = panels.axes.n_sessions - 1
    binder = None
    for d in sorted(deps):
        if not panels.exists(d):
            continue
        ls = int(panels.meta(d).get("last_session", last))
        if ls < last:
            last, binder = ls, d
    return max(0, last), binder


def effective_ed(panels, ed: str | None, deps) -> str:
    """把 `ed` 压到数据真正支持的那一天。"""
    i, _ = cap(panels, deps)
    boundary = panels.axes.date(i)
    return boundary if ed is None else min(ed, boundary)
