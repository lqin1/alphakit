"""`Panels`：执行期看得见的存储切面（architecture.md §3.3 / §七）。

引擎此前没有这个接缝。`run_node` / `Ctx` / `preflight` 用的是 `Store` 上一组**事实上
的**八个成员, 而这组成员从未被声明过, 于是:

  · 第二个后端（parquet、内存）没有任何东西可以去满足——只能去改那三处 `Store(...)`
    的构造点, 或者 monkeypatch 模块;
  · 测试也没有任何东西可以替换。后果是 `node.py` / `ctx.py` / `preflight.py` /
    `report.py` / `cli.py` 合计约 1490 行**没有一行直接测试**, 唯一的触碰是 smoke
    起子进程打 CLI、对着仓库里那份真实的 storage/l3 断言 returncode == 0。
    happy path、非 hermetic、且要先有数据。

接口窄是有意的: 只放消费方**真的调**的那几个。写侧的 `ensure_capacity`、
`check_fingerprint` 这些属于 `Store` 自己的实现细节, 不进这道缝。

两个适配器才算一道真接缝, 不是一个假设: 生产用 `core.store.Store`（zarr on disk）,
测试用 `tests/fakes.FakePanels`（内存 dict）。两个都在, 这个 Protocol 才是被用来
"面向它编程"的, 而不是事后贴上去的一层注解。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Panels(Protocol):
    """L3 面板的读写面。`runtime_checkable` 只查成员名, 查不了签名——够用: 它挡的是
    "换了个后端却漏了一个方法", 而签名对不对由那 1490 行的测试去证。"""

    @property
    def axes(self):
        """全局共享的（session 轴, 列轴）。"""

    def exists(self, ref) -> bool:
        """这个 ref 是否已落库**且属性已提交**（半成品不算存在）。"""

    def meta(self, ref) -> dict:
        """落库时冻结的那份元数据: dims/dtype/version/deps/deps_versions/region_hash…"""

    def read(self, ref, sd=None, ed=None):
        """[sd, ed] 区间, 对齐到全局轴。秩-1 给 Series, 秩-2 给 DataFrame, 秩-3 给 ndarray。"""

    def write(self, ref, df, **kw):
        """区间 upsert（缺省）或全量重建（rebuild=True）。"""

    def expand(self, pattern: str) -> list[str]:
        """通配 → 该节点当时的全部输出。非通配名原样返回。"""

    def list_refs(self) -> list[str]:
        """库里所有已提交的 ref。"""
