"""alphakit —— 中低频 alpha 研究与回测引擎。

契约见 `docs/architecture.md`；数据契约（L2 与 L3）见 `docs/l2_schema.md`。
v0 范围：L3 → L3，无 cache，顺序执行。

此前这里只有一个 `__version__`, 于是包本身没有接口——每个调用方都得伸进
`alpha_kit.core` / `.runner` / `.pnl` 去拿, 而"alphakit 对外是什么"这件事没有任何
一处说得出来。下面这一层就是答案, 按使用顺序排: 读配置 → 读写面板 → 跑节点 → 评估。
"""
from .core.axes import Axes
from .core.config import Spec, load_spec
from .core.naming import ConfigError, Ref, parse_ref
from .core.panels import Panels
from .core.store import Store, StoreError
from .pnl.metrics import metrics
from .pnl.simulate import SimError, simulate
from .runner.node import run

__version__ = "0.1.0"

__all__ = [
    # 配置：yaml → Spec
    "load_spec", "Spec", "ConfigError",
    # 命名：ref ↔ 路径
    "Ref", "parse_ref",
    # 存储：Panels 是接缝, Store 是生产适配器
    "Panels", "Store", "StoreError", "Axes",
    # 执行
    "run",
    # 评估
    "simulate", "SimError", "metrics",
    "__version__",
]
