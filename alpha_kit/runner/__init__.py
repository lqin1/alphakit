"""执行引擎（architecture.md §七）。v0：无 cache、顺序执行、只吃 L3 只吐 L3。"""
from .ops import OpChain

__all__ = ["OpChain"]
