"""全局轴：所有 L3 节点共享同一坐标系（architecture.md §3.3）。

只增不减、单调分配，故旧 chunk 永远有效。轴是唯一真相源——节点自己不存轴，
避免几千份重复的 security_id 列表和随之而来的不同步风险。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Axes:
    """di 轴（sessions）与 ii 轴（securities）。ti 轴按需从 grids/ 载入。"""

    root: Path
    sessions: list[str]          # ISO 日期，位置即 session 序号
    securities: list[int]        # security_id，位置即列号
    allocated: int               # 预留列容量 ≥ len(securities)

    # ---- 位置索引（构造时建好，避免每日线性查找）
    def __post_init__(self) -> None:
        self._sid_pos = {s: i for i, s in enumerate(self.sessions)}
        self._sec_pos = {s: i for i, s in enumerate(self.securities)}

    @classmethod
    def load(cls, root: str | Path) -> "Axes":
        root = Path(root)
        a = root / "_axes"
        sessions = json.loads((a / "sessions.json").read_text())
        securities = json.loads((a / "securities.json").read_text())
        cap = json.loads((a / "capacity.json").read_text())
        return cls(root, sessions, securities, cap["allocated"])

    @classmethod
    def create(cls, root: str | Path, sessions: list[str], securities: list[int],
               reserve: int = 500, *, overwrite: bool = False) -> "Axes":
        """建轴。**已有轴时默认拒绝重放**。

        轴是所有节点共享的坐标系，且 chunk 里存的是**位置**而非名字。在已有轴上重放
        create 并插入一个新标的，会让每个历史 chunk 的第 0 列悄悄指向另一只票——
        数据没坏、形状没变、没有任何报错，但全部历史的含义都错位了。这正是 §3.4
        「security_id 永不重用、单调分配」要防的事，故它必须是一道硬闸门而非约定。
        """
        root = Path(root)
        a = root / "_axes"
        if not overwrite:
            # 两条轴同一条理由, 同一道闸门。此前只有 securities 有守卫, sessions 是
            # 无条件覆写——而 di 轴错位比 ii 轴更糟: 日历里补进一个半日市或删掉一个
            # 节假日, 每个 chunk 仍在原来的行位置上, 于是**全库所有面板整体错开一天**,
            # 等于一次性给每个 alpha 注入一天前视。形状没变、日期范围看着干净、指纹
            # 不动（定义确实没改）, 没有任何地方会喊。
            for fname, new_list, what, why in (
                ("securities.json", securities, "security_ids",
                 "would shift the column meaning of every historical chunk"),
                ("sessions.json", sessions, "sessions",
                 "would shift every panel in time -- a one-day lookahead injected store-wide"),
            ):
                f = a / fname
                if not f.exists():
                    continue
                old = json.loads(f.read_text())
                if old != new_list[:len(old)]:
                    raise ValueError(
                        f"{a} already holds {len(old)} {what} and the new list is not an "
                        f"extension of it -- replaying {why}.\n"
                        f"  Append-only: new entries go on the end. To truly rebuild, pass "
                        f"overwrite=True.")
        a.mkdir(parents=True, exist_ok=True)
        allocated = len(securities) + reserve
        (a / "sessions.json").write_text(json.dumps(sessions))
        (a / "securities.json").write_text(json.dumps(securities))
        (a / "capacity.json").write_text(
            json.dumps({"n_active": len(securities), "allocated": allocated}))
        return cls(root, sessions, securities, allocated)

    def ensure_sessions(self, new: list[str]) -> int:
        """按日期 append，返回新增条数。轴 append-only：只接受排在末尾之后的日期。"""
        add = sorted({d for d in new if d not in self._sid_pos})   # 去重: 同一天不能占两个位置
        if not add:
            return 0
        if self.sessions and add[0] <= self.sessions[-1]:
            raise ValueError(
                f"session axis is append-only: {add[0]} is not later than the current last session "
                f"{self.sessions[-1]}")
        base = len(self.sessions)
        self.sessions.extend(add)
        self._sid_pos.update({d: base + i for i, d in enumerate(add)})
        (self.root / "_axes" / "sessions.json").write_text(json.dumps(self.sessions))
        return len(add)

    # ---- 查询
    def pos(self, date: str) -> int:
        try:
            return self._sid_pos[date]
        except KeyError:
            raise KeyError(f"{date} is not on the session axis") from None

    def date(self, i: int) -> str:
        return self.sessions[i]

    def slice(self, sd: str | None, ed: str | None) -> tuple[int, int]:
        """[sd, ed] 闭区间 → [i0, i1) 半开位置区间。None 表示不设限。"""
        i0 = 0 if sd is None else next(
            (i for i, d in enumerate(self.sessions) if d >= sd), len(self.sessions))
        i1 = len(self.sessions) if ed is None else next(
            (i for i, d in enumerate(self.sessions) if d > ed), len(self.sessions))
        return i0, i1

    @property
    def n_sessions(self) -> int:
        return len(self.sessions)

    @property
    def n_securities(self) -> int:
        return len(self.securities)
