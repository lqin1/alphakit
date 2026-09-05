"""L3 store：Zarr 数组 + 全局共享轴（architecture.md §3.2 / §3.3）。

路径      storage/l3/{region}/{repo}/{node_dir}/{node_name}-{output}/
引用名    {repo}.{node_dir}.{node_name}-{output}
两者一一对应、纯字符串可互推，不需要索引。

秩决定形状与分块（§3.6）：
    [di]           (D,)      chunks (4096,)
    [di, ii]       (D, N)    chunks (50, N_alloc)
    [di, ii, ti]   (D, N, T) chunks (1, N_alloc, T)   —— 单日即一块, 日更仍只写 1 个文件
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import zarr

from .axes import Axes
from .naming import Ref, is_wildcard, parse_ref

def _covered(z, dims: list[str], n: int) -> int:
    """有数据的列数（秩-1 没有列轴, 记 0）。"""
    if dims == ["di"]:
        return 0
    a = np.asarray(z[:, :n] if dims == ["di", "ii"] else z[:, :n, :])
    if a.dtype.kind == "f":
        ok = np.isfinite(a)
    else:
        ok = a != 0
    axes = tuple(i for i in range(ok.ndim) if i != 1)
    return int(ok.any(axis=axes).sum())


def _fill(dt) -> object:
    """该 dtype 的"无数据"取值：浮点是 NaN, 其余是 0/False（§3.3 fill_value 必须显式）。"""
    return np.nan if np.dtype(dt).kind == "f" else 0


CHUNK_DI = 50
CHUNK_RANK1 = 4096


class StoreError(RuntimeError):
    pass


class Store:
    def __init__(self, root: str | Path, region: str = "us"):
        root = Path(root)
        # region 文件里的 l3_root 写的是**它自己那一层**（storage/l3/us）: 一个 region
        # 文件指着别人的父目录, 读的人还得自己在脑子里补一段, 写全反而不容易搞错。
        # 于是这里两种写法都认——末段已经是 region 时不再重复拼接, 否则
        # storage/l3/us 会变成 storage/l3/us/us。
        if root.name == region:
            root = root.parent
        self.root = root
        self.region = region
        # 轴按 region 存：`security_id` 与 session 都是**按市场**定义的——美股与其他市场
        # 不共享列轴, 日历也不同。轴若放在 region 之上, 接入第二个市场时要么列轴被迫
        # 混装、要么整个 store 推倒重来。`registry/security_id.{region}.csv` 已是按
        # region 分的, 轴跟着它走才自洽。
        self.axes = Axes.load(self.root / region)

    # ------------------------------------------------------------ 路径与引用
    def path(self, ref: str | Ref) -> Path:
        r = parse_ref(ref) if isinstance(ref, str) else ref
        return self.root / self.region / r.repo / r.node_dir / r.leaf

    def exists(self, ref: str | Ref) -> bool:
        return (self.path(ref) / "zarr.json").exists()

    def list_refs(self) -> list[str]:
        out = []
        base = self.root / self.region
        if not base.exists():
            return out
        for z in sorted(base.glob("*/*/*")):
            if (z / "zarr.json").exists():
                out.append(f"{z.parent.parent.name}.{z.parent.name}.{z.name}")
        return out

    def expand(self, pattern: str) -> list[str]:
        """通配 → 该节点当时的全部输出（§3.2）。折叠形写 `{repo}.{node_dir}.*`。"""
        if not is_wildcard(pattern):
            return [pattern]
        stem = pattern[:-1]                      # 含末尾的 '-' 或 '.'
        hits = sorted(r for r in self.list_refs() if r.startswith(stem))
        if not hits:
            raise StoreError(f"通配 {pattern} 展开为空——该节点尚未产出任何输出")
        return hits

    # ---------------------------------------------------------------- 元数据
    def meta(self, ref: str | Ref) -> dict:
        z = zarr.open_array(str(self.path(ref)), mode="r")
        return dict(z.attrs)

    def catalog(self) -> pd.DataFrame:
        rows = []
        for ref in self.list_refs():
            m = self.meta(ref)
            rows.append({"ref": ref, **{k: m.get(k) for k in
                        ("dims", "dtype", "version", "first_session", "last_session",
                         "n_cols_covered", "status", "registered", "updated_at")}})
        return pd.DataFrame(rows)

    # ------------------------------------------------------------------ 读取
    def read(self, ref: str | Ref, sd: str | None = None, ed: str | None = None):
        """区间读。秩-1 → Series(date)；秩-2 → DataFrame(date × security_id)；秩-3 → ndarray。

        永远对齐到全局轴：列全在、无数据处 NaN，调用方零对齐负担（§3.3）。
        """
        p = self.path(ref)
        if not (p / "zarr.json").exists():
            raise StoreError(f"依赖不存在: {ref}\n  期望路径 {p}")
        z = zarr.open_array(str(p), mode="r")
        dims = list(z.attrs["dims"])
        i0, i1 = self.axes.slice(sd, ed)
        idx = self.axes.sessions[i0:i1]
        n = self.axes.n_securities
        # 轴可能已经长过这个节点的数组：ingestion 推进了 session 轴而下游节点还没重跑,
        # 这是日更的常态中间态。§3.3 承诺 read 永远返回**对齐到全局轴**的完整结果,
        # 无数据处 NaN——所以这里要补齐, 不能让形状不符冒出一个裸 ValueError。
        want = i1 - i0
        got = np.asarray(z[i0:min(i1, z.shape[0])] if dims == ["di"]
                         else z[i0:min(i1, z.shape[0]), :n] if dims == ["di", "ii"]
                         else z[i0:min(i1, z.shape[0]), :n, :])
        if len(got) < want:
            pad = np.full((want - len(got),) + got.shape[1:], _fill(z.dtype), got.dtype)
            got = np.concatenate([got, pad], axis=0)
        if dims == ["di"]:
            return pd.Series(got, index=idx, name=str(ref))
        if dims == ["di", "ii"]:
            return pd.DataFrame(got, index=idx, columns=self.axes.securities)
        return got                                   # 秩-3: pandas 无三维结构

    def tail(self, ref: str | Ref, n: int = 1):
        i1 = self.axes.n_sessions
        return self.read(ref, self.axes.date(max(0, i1 - n)), self.axes.date(i1 - 1))

    # ------------------------------------------------------------------ 写入
    def _open_or_create(self, ref: Ref, dims: list[str], dtype: str,
                        grid_len: int | None) -> zarr.Array:
        p = self.path(ref)
        if (p / "zarr.json").exists():
            return zarr.open_array(str(p), mode="r+")
        p.mkdir(parents=True, exist_ok=True)
        D, N = self.axes.n_sessions, self.axes.allocated
        if dims == ["di"]:
            shape, chunks = (D,), (CHUNK_RANK1,)
        elif dims == ["di", "ii"]:
            shape, chunks = (D, N), (CHUNK_DI, N)
        else:
            if not grid_len:
                raise StoreError(f"{ref}: 秩-3 必须声明 grid")
            shape, chunks = (D, N, grid_len), (1, N, grid_len)
        fill = _fill(dtype)
        return zarr.create_array(store=str(p), shape=shape, chunks=chunks,
                                 dtype=dtype, fill_value=fill, overwrite=False)

    def _resize_di(self, z: zarr.Array, need: int) -> None:
        if z.shape[0] < need:
            z.resize((need,) + z.shape[1:])

    def write(self, ref: str | Ref, df, *, dims=None, dtype="f4", grid_len=None,
              meta: dict | None = None, rebuild: bool = False,
              fingerprint: str | None = None):
        """区间 upsert（缺省）或全量重建（rebuild=True，bump version）。

        §7.2：日更是 `run --ed today`，交付的只有一行——若按"全量重建"语义执行，
        历史会被一行覆盖。故缺省是 upsert，只有显式 --rebuild 才 bump version。
        """
        r = parse_ref(ref) if isinstance(ref, str) else ref
        dims = list(dims or ["di", "ii"])

        # ---- 先校验再建数组：校验失败时若数组已建、attrs 未写, 后续读会撞上裸 KeyError
        dates = list(df.index) if hasattr(df, "index") else list(meta["dates"])
        i0 = self.axes.pos(dates[0]); i1 = self.axes.pos(dates[-1]) + 1
        if dates != self.axes.sessions[i0:i1]:
            # 不能只比宽度：错序的索引（01-01, 01-03, 01-02, 01-04）宽度也对得上,
            # 然后按给定顺序落库, 第 2、3 天的值被悄悄互换。必须逐位等于轴上那一段。
            why = ("日期不连续（有缺口）" if len(dates) != i1 - i0
                   else "日期顺序错乱（与轴上的顺序不一致）")
            raise StoreError(
                f"{ref}: {why}——交付的必须是 session 轴上连续且同序的一段。\n"
                f"  给的  {dates[:3]}…{dates[-1:]}（{len(dates)} 行）\n"
                f"  期望  {self.axes.sessions[i0:i0+3]}…{self.axes.sessions[i1-1:i1]}（{i1-i0} 行）")
        if dims == ["di", "ii"]:
            extra = [c for c in getattr(df, "columns", []) if c not in self.axes._sec_pos]
            if extra:
                raise StoreError(
                    f"{ref}: 交付的面板含 {len(extra)} 个不在列轴上的标的（如 {extra[:3]}）——"
                    f"直接 reindex 会让它们无声消失。先把它们加进 securities 轴。")

        # 指纹闸门在这里而不是在调用方：它守的是"同一个名字下的定义变了"，
        # 而这件事与谁在写无关。放在 runner 里的话，ingestion 脚本（build_l3_base）
        # 就绕过去了——而那正是最容易悄悄改一行公式的地方。
        if fingerprint and not rebuild:
            self.check_fingerprint(ref, fingerprint)

        z = self._open_or_create(r, dims, dtype, grid_len)
        self._resize_di(z, self.axes.n_sessions)

        if dims == ["di"]:
            z[i0:i1] = np.asarray(df, dtype=dtype)
        elif dims == ["di", "ii"]:
            # 缺列补的是该 dtype 的 fill 而非 NaN：bool 面板经 reindex 得到 NaN,
            # 而 np.asarray(NaN, dtype=bool) 是 **True**——未覆盖的票会被标成池内。
            v = df.reindex(columns=self.axes.securities, fill_value=_fill(np.dtype(dtype)))
            z[i0:i1, :self.axes.n_securities] = np.asarray(v, dtype=dtype)
        else:
            z[i0:i1, :self.axes.n_securities, :] = np.asarray(df, dtype=dtype)

        # catalog 承诺了这几列, 就必须有人填——否则它们永远是 None,
        # 而"覆盖了多少只票"正是 §5.2 一致性检查要看的第一个数。
        covered = _covered(z, dims, self.axes.n_securities)
        old = dict(z.attrs)
        z.attrs.update({
            "dims": dims, "dtype": dtype,
            "n_cols_covered": covered,
            # §15.4 的生命周期：首次写入默认 wip；已登记的由 registry 侧改写, 不在这里降级
            "status": old.get("status", "wip"),
            "registered": bool(old.get("registered", False)),
            **({"fingerprint": fingerprint} if fingerprint else {}),
            "version": (old.get("version", 0) + 1) if rebuild else old.get("version", 1),
            "first_session": min(old.get("first_session", i0), i0),
            "last_session": max(old.get("last_session", i1 - 1), i1 - 1),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **(meta or {}),
        })
        return z

    def ensure_capacity(self, need: int) -> int:
        """按标的扩容（§3.3）。

        全宽 chunk 下加一列要重写**所有** chunk，故一次预留一批、把它摊薄成年度维护。
        这也是"security_id 单调分配、列只在末尾增长"原则的第二个理由。
        """
        if need <= self.axes.allocated:
            return self.axes.allocated
        raise StoreError(
            f"列轴容量 {self.axes.allocated} 不够放 {need} 个标的。\n"
            f"  按标的扩容不是 O(1)——全宽 chunk 下要重写所有 chunk，属离线维护，"
            f"不在日更路径上做。请先重建轴并预留足够列。")

    def check_fingerprint(self, ref: str | Ref, fingerprint: str) -> None:
        """写入前比对指纹（§3.3）。

        `append`/`upsert` 都不 bump version，故改一行公式再跑日更，同一个数组里
        改动日之前是定义 A、之后是定义 B，且无从察觉。指纹不符即拒绝写入。
        """
        if not self.exists(ref):
            return
        old = self.meta(ref).get("fingerprint")
        if old and old != fingerprint:
            raise StoreError(
                f"{ref}: 指纹不符——定义已改变而名字未变。\n"
                f"  store 中 {old}\n  本次   {fingerprint}\n"
                f"  改定义请显式 --rebuild（新版本）或换一个 identity。")
