"""`FakePanels` —— `core.panels.Panels` 的第二个适配器, 纯内存。

它存在的理由不是"跑得快", 而是**让那 1490 行第一次可测**。在它之前, `run_node` /
`Ctx` / `preflight` 唯一的触碰方式是起子进程打 CLI、对着仓库里那份真实的
storage/l3 断言 returncode == 0——happy path、非 hermetic、且要先有数据; 预热对不对、
自引用回灌有没有生效、秩-3 掩码走没走, 一条都断言不了。

两个适配器才算一道真接缝: 生产是 zarr on disk, 这里是 dict。所以本文件不 import
zarr, 也不碰文件系统——一旦它开始依赖真实存储的行为, 这道缝就名存实亡了。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from alpha_kit.core import rank as rk
from alpha_kit.core.naming import parse_ref
from alpha_kit.core.store import StoreError


class FakeAxes:
    """轴：一份 session 列表 + 一份 security 列表, 与真轴同样的查询面。"""

    def __init__(self, sessions: list[str], securities: list[int], reserve: int = 8):
        self.sessions = list(sessions)
        self.securities = list(securities)
        self.allocated = len(securities) + reserve
        self._sid = {d: i for i, d in enumerate(self.sessions)}
        self._sec_pos = {c: i for i, c in enumerate(self.securities)}

    @property
    def n_sessions(self) -> int:
        return len(self.sessions)

    @property
    def n_securities(self) -> int:
        return len(self.securities)

    def pos(self, date: str) -> int:
        try:
            return self._sid[date]
        except KeyError:
            raise KeyError(f"{date} is not on the session axis") from None

    def date(self, i: int) -> str:
        return self.sessions[i]

    def slice(self, sd=None, ed=None) -> tuple[int, int]:
        lo = 0 if sd is None else next((i for i, d in enumerate(self.sessions) if d >= sd),
                                       len(self.sessions))
        hi = len(self.sessions) if ed is None else next(
            (i for i, d in enumerate(self.sessions) if d > ed), len(self.sessions))
        return lo, max(lo, hi)


class FakePanels:
    """dict 背后的 Panels。写进去什么, 读出来就是什么, 对齐到全局轴。"""

    def __init__(self, sessions: list[str], securities: list[int]):
        self.axes = FakeAxes(sessions, securities)
        self._data: dict[str, np.ndarray] = {}
        self._meta: dict[str, dict] = {}
        self.writes: list[tuple[str, int, int]] = []      # 供断言"写了哪些区间"

    # ------------------------------------------------------------------ 读
    def exists(self, ref) -> bool:
        return str(ref) in self._meta

    def meta(self, ref) -> dict:
        return dict(self._meta.get(str(ref), {}))

    def read(self, ref, sd=None, ed=None):
        r = str(ref)
        if r not in self._data:
            raise StoreError(f"dependency does not exist: {r}")
        lo, hi = self.axes.slice(sd, ed)
        a, dims = self._data[r], self._meta[r]["dims"]
        idx = self.axes.sessions[lo:hi]
        if rk.n_axes(dims) == 1:
            return pd.Series(a[lo:hi], index=idx)
        if rk.n_axes(dims) == 2:
            return pd.DataFrame(a[lo:hi, :self.axes.n_securities],
                                index=idx, columns=self.axes.securities)
        return a[lo:hi, :self.axes.n_securities, :]

    def list_refs(self) -> list[str]:
        return sorted(self._meta)

    def expand(self, pattern: str) -> list[str]:
        from alpha_kit.core.naming import is_wildcard
        if not is_wildcard(pattern):
            return [pattern]
        stem = pattern[:-1]
        hits = sorted(r for r in self.list_refs() if r.startswith(stem))
        if not hits:
            raise StoreError(f"wildcard {pattern} expanded to nothing")
        return hits

    # ------------------------------------------------------------------ 写
    def write(self, ref, df, *, dims=None, dtype="f4", grid_len=None,
              meta=None, rebuild=False, fingerprint=None):
        r = str(ref)
        parse_ref(r)                                   # 名字规则与真 store 同一份
        dims = list(dims or rk.DI_II)
        dates = list(df.index) if hasattr(df, "index") else list(meta["dates"])
        i0 = self.axes.pos(dates[0]); i1 = self.axes.pos(dates[-1]) + 1
        if dates != self.axes.sessions[i0:i1]:
            raise StoreError(f"{r}: dates are not contiguous / out of order")
        if r not in self._data:
            shape = rk.shape(dims, self.axes.n_sessions, self.axes.allocated, grid_len)
            fill = np.nan if np.dtype(dtype).kind == "f" else 0
            self._data[r] = np.full(shape, fill, dtype=dtype)
        a = self._data[r]
        if rk.n_axes(dims) == 1:
            a[i0:i1] = np.asarray(df, dtype=dtype)
        elif rk.n_axes(dims) == 2:
            fill = np.nan if np.dtype(dtype).kind == "f" else 0
            v = df.reindex(columns=self.axes.securities, fill_value=fill)
            a[i0:i1, :self.axes.n_securities] = np.asarray(v, dtype=dtype)
        else:
            a[i0:i1, :self.axes.n_securities, :] = np.asarray(df, dtype=dtype)
        old = self._meta.get(r, {})
        self._meta[r] = {**old, "dims": dims, "dtype": dtype,
                         "version": (old.get("version", 0) + 1) if rebuild
                         else old.get("version", 1),
                         "first_session": min(old.get("first_session", i0), i0),
                         "last_session": max(old.get("last_session", i1 - 1), i1 - 1),
                         **({"fingerprint": fingerprint} if fingerprint else {}),
                         **(meta or {})}
        self.writes.append((r, i0, i1))
        return a

    # ---------------------------------------------------------------- 造数据
    def seed(self, ref: str, values, dims=rk.DI_II, dtype="f4") -> None:
        """直接铺一个满区间的面板, 不走 write 的校验——造上游依赖用。"""
        idx = self.axes.sessions
        if rk.n_axes(dims) == 2:
            df = pd.DataFrame(values, index=idx, columns=self.axes.securities)
        else:
            df = pd.Series(values, index=idx)
        self.write(ref, df, dims=list(dims), dtype=dtype)
