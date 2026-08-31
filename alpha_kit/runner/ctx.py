"""Ctx：handle 能看到的全部世界（architecture.md §6.1 / §十）。

对外极简，对内扛三条纪律：防前视、池外 NaN、性能。
无日期参数、无绝对索引、无 store 写句柄——API 面积越小，防前视的证明义务越小。
"""
from __future__ import annotations

import numpy as np
import pandas as pd



class PanelLoader:
    """一个依赖面板：持有 loader 而非数据，首次触碰才读盘并对齐一次（§十）。

    不能在构造 Ctx 前把 deps 全部读入：template 默认的 `…-*` 通配展开后 eager 加载
    是约 2 GB 常驻，而 handle 可能只碰两个；ns 里一旦有秩-3 节点（m5 满仓 7.5 GB）
    就是直接 OOM。
    """

    __slots__ = ("store", "ref", "sd", "ed", "optional", "_data", "_dims", "_overlay")

    def __init__(self, store, ref: str, sd: str | None, ed: str | None,
                 optional: bool = False, dims: list[str] | None = None):
        self.store, self.ref, self.sd, self.ed = store, ref, sd, ed
        # optional：节点**自己的**输出面板。首次运行时它当然还不存在——自引用节点
        # （§4.5 的 px_adj 读自己昨天的输出）若因此在冷 store 上直接崩掉, 这条被文档
        # 标为"合法"的写法就永远跑不起来。缺数组时退化成只有 overlay 的空面板。
        self.optional = optional
        self._data = None
        self._dims: list[str] | None = list(dims) if dims else None
        self._overlay: dict[int, object] = {}   # 当日回灌：{session -> 值}

    def _load(self):
        if self._data is None:
            if self.optional and not self.store.exists(self.ref):
                self._data = []
                return self._data
            self._dims = list(self.store.meta(self.ref)["dims"])
            self._data = self.store.read(self.ref, self.sd, self.ed)
            if self._dims == ["di", "ii"]:
                self._data = self._data.to_numpy(dtype="f4", copy=False)
            elif self._dims == ["di"]:
                self._data = self._data.to_numpy(dtype="f4", copy=False)
        return self._data

    @property
    def dims(self) -> list[str]:
        if self._dims is None:
            self._load()
        return self._dims or ["di", "ii"]

    def publish(self, t: int, v) -> None:
        """把当日产出写回内存面板，让自引用节点读得到今天算出的昨天（§7.2 第 1 条）。"""
        self._overlay[t] = v

    def row(self, t: int, i0: int):
        """第 t 个 session 的那一片；越界返回 None 由调用方 pad。"""
        if t in self._overlay:
            return self._overlay[t]
        a = self._load()
        j = t - i0
        if j < 0 or j >= len(a):
            return None
        return a[j]


class UniverseView:
    """池子的两个角色：当日成员掩码，以及 neutralize 的分组取值（§3.5）。"""

    def __init__(self, store, ref: str | None, sd, ed, cols, i0: int):
        self.cols, self.i0 = cols, i0
        self._u = PanelLoader(store, ref, sd, ed) if ref else None
        self._g: dict[str, PanelLoader] = {}
        self._store, self._sd, self._ed = store, sd, ed

    def mask(self, t: int) -> pd.Series:
        if self._u is None:                       # 缺省 all：恒 True 的全集
            return pd.Series(True, index=self.cols)
        r = self._u.row(t, self.i0)
        if r is None:
            return pd.Series(False, index=self.cols)
        return pd.Series(np.nan_to_num(np.asarray(r), nan=0.0) > 0, index=self.cols)

    def group(self, ref: str, t: int) -> pd.Series:
        if ref not in self._g:
            self._g[ref] = PanelLoader(self._store, ref, self._sd, self._ed)
        r = self._g[ref].row(t, self.i0)
        if r is None:
            return pd.Series(np.nan, index=self.cols)
        return pd.Series(np.asarray(r), index=self.cols)


class _CS:
    """截面工具，nan-aware，作用在 ii 轴。"""

    @staticmethod
    def rank(x: pd.Series) -> pd.Series:
        r = x.rank(method="average", na_option="keep")
        n = r.notna().sum()
        return (r - 1) / (n - 1) - 0.5 if n > 1 else r * 0.0

    @staticmethod
    def zscore(x: pd.Series) -> pd.Series:
        s = x.std()
        return (x - x.mean()) / s if s and np.isfinite(s) and s > 0 else x * 0.0

    @staticmethod
    def demean(x: pd.Series, by: pd.Series | None = None) -> pd.Series:
        if by is None:
            return x - x.mean()
        return x - by.map(x.groupby(by).mean())


class Ctx:
    """逐日游标 + 惰性面板。handle 拿到的永远是「当日那一片」。"""

    def __init__(self, store, panels: dict[str, PanelLoader], universe: UniverseView,
                 node, i0: int, cols, dates: list[str]):
        self._store, self._panels, self._universe = store, panels, universe
        self._node, self._i0, self._cols, self._dates = node, i0, cols, dates
        self.params = dict(node.params)
        self.state: dict = {}
        self.cs = _CS()
        self._t: int | None = None
        self._cache: dict = {}

    # ------------------------------------------------------------------ 游标
    def _advance(self, t: int) -> None:
        """由 runner 独占调用：推进游标并清当日缓存（§十）。"""
        self._t = t
        self._cache.clear()

    @property
    def t(self) -> int:
        self._require_cursor()
        return self._t

    def today(self) -> str:
        return self._dates[self._t - self._i0]

    @property
    def cols(self):
        return self._cols

    @property
    def universe(self) -> pd.Series:
        return self._universe.mask(self._t)

    def _require_cursor(self):
        if self._t is None:
            raise RuntimeError(
                "数据访问只能在 handle 里——init 期还没有游标，此时读到的任何值都是无意义的。")

    def _panel(self, ref: str) -> PanelLoader:
        if ref not in self._panels:
            raise KeyError(
                f"{self._node.name}: `{ref}` 不在 deps 里。\n"
                f"  凡是这个节点跑起来需要读到的 L3，无论谁去读它，都要写进 deps。\n"
                f"  已声明: {sorted(self._panels)}")
        return self._panels[ref]

    # ------------------------------------------------------------------ 取数
    def win(self, ref: str, w: int):
        """(w, …) 窗口，行标签 -(w-1)…0，0 = 当前处理日。历史不足 pad NaN、行数恒为 w。"""
        self._require_cursor()
        key = ("win", ref, w)
        if key in self._cache:
            return self._cache[key]
        p = self._panel(ref)
        dims = p.dims
        rows = [p.row(t, self._i0) for t in range(self._t - w + 1, self._t + 1)]
        idx = list(range(-(w - 1), 1))
        if dims == ["di"]:
            v = pd.Series([np.nan if r is None else float(r) for r in rows], index=idx)
        elif dims == ["di", "ii"]:
            nan = np.full(len(self._cols), np.nan, dtype="f4")
            v = pd.DataFrame(np.vstack([nan if r is None else np.asarray(r) for r in rows]),
                             index=idx, columns=self._cols)
            v = self._apply_pool(v)
        else:                                    # 秩-3：pandas 无三维结构, 返回 ndarray
            shape = next((np.asarray(r).shape for r in rows if r is not None), None)
            nan = np.full(shape or (len(self._cols), 1), np.nan, dtype="f4")
            v = np.stack([nan if r is None else np.asarray(r) for r in rows])
        self._cache[key] = v
        return v

    def f(self, ref: str):
        """当日那一片。秩-1 → 标量；秩-2 → Series(N)；秩-3 → DataFrame(N × T)。"""
        self._require_cursor()
        key = ("f", ref)
        if key in self._cache:
            return self._cache[key]
        p = self._panel(ref)
        r = p.row(self._t, self._i0)
        dims = p.dims
        if dims == ["di"]:
            v = float("nan") if r is None else float(r)
        elif dims == ["di", "ii"]:
            v = (pd.Series(np.nan, index=self._cols) if r is None
                 else pd.Series(np.asarray(r), index=self._cols).copy())
            v = self._apply_pool(v)
        else:
            v = pd.DataFrame(np.asarray(r), index=self._cols) if r is not None else None
        self._cache[key] = v
        return v

    def _apply_pool(self, v):
        """声明了 universe 的节点, 当日池外整列 NaN（§3.5 ①）。

        截面统计因此天然限定池内；非 skipna 的写法会立刻得到 NaN 报警——吵闹地失败。

        判据是"有没有池子"而不是 kind——执行期不存在 kind 分支（§一原则）。

        窗口是 (w, N)：掩码必须沿**列**广播。`df.where(series)` 会把 Series 的 index
        当成 df 的**行**索引去对齐，security_id 与行标签 -(w-1)…0 零重叠，于是整个窗口
        静默变成 NaN——这是 pandas 最容易踩且不报错的一类对齐陷阱。而
        `where(cond, scalar, axis=1)` 在 pandas 3.0 上另有 bug（会去下标那个标量），
        故直接广播成同形状的 numpy 布尔数组，绕开整套对齐机制，也更快。
        """
        if self._universe is None or self._universe._u is None:
            return v
        m = self._universe.mask(self._t).to_numpy()
        if isinstance(v, pd.DataFrame):
            return v.where(np.broadcast_to(m, v.shape), np.nan)
        return v.where(m, np.nan)

    # ---------------------------------------------------------- 多输出构造器
    def multi_outputs(self, **kw):
        """错误发生在写错的那一行（§4.3）。"""
        import difflib
        want = self._node.outputs
        if len(want) < 2:
            raise ValueError(f"{self._node.name} 只有一个输出，直接 return 值即可")
        unknown = set(kw) - set(want)
        if unknown:
            hint = difflib.get_close_matches(sorted(unknown)[0], list(want), 1)
            raise ValueError(f"未声明的输出 {sorted(unknown)}"
                             + (f"；是否想写 {hint[0]}?" if hint else ""))
        missing = set(want) - set(kw)
        if missing:
            raise ValueError(
                f"缺少输出 {sorted(missing)}；算不出值请传 NaN，不要漏 key——"
                f"NaN 是合法值（这天这只票没有值），漏 key 是结构错误（这个节点今天不存在）。")
        return {k: self._coerce(v, want[k]) for k, v in kw.items()}

    def _coerce(self, v, out):
        """按声明的秩校验形状，写错在这一行就炸（§4.2）。"""
        if out.dims == ("di",):
            if hasattr(v, "__len__") and not np.isscalar(v):
                raise ValueError(f"{out.key}: dims=[di] 应返回标量，收到 {type(v).__name__}")
            return float(v)
        if out.dims == ("di", "ii"):
            if isinstance(v, pd.DataFrame):
                raise ValueError(
                    f"{out.key}: dims=[di,ii] 应返回**当日截面**（长度 {len(self._cols)} 的 Series），"
                    f"收到的是 {v.shape} 的 DataFrame——多半是忘了在窗口上做列向聚合"
                    f"（如 .mean() / .loc[0]）。")
            s = v if isinstance(v, pd.Series) else pd.Series(np.asarray(v), index=self._cols)
            if len(s) != len(self._cols):
                raise ValueError(
                    f"{out.key}: dims=[di,ii] 应返回长度 {len(self._cols)} 的截面，收到 {len(s)}")
            return s.reindex(self._cols)
        a = np.asarray(v)
        if a.ndim != 2:
            raise ValueError(f"{out.key}: dims=[di,ii,ti] 应返回 (N, T) 二维，收到 {a.shape}")
        return a
