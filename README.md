# alphakit

中低频 Alpha 研究与回测引擎。美股，日频。数据随仓库入库，clone 完装上就能跑。

```bash
python3 -m venv .venv && .venv/bin/pip install -e . && source .venv/bin/activate
run repos/g_yliu/nodes/alpha_yliu_rev/rev.yaml --sd 2025-12-01   # 算 alpha, 并自动评估
```

## 文档

**要用它**：读 1，然后 2。**要从文档重建它**：1 → 3 → 4（先读 §〇 的不变量清单）→ 2 → 5。

| # | 文档 | 谁看 | 内容 |
|---|---|---|---|
| 1 | [`docs/architecture.md`](docs/architecture.md) | 所有人 | **架构总览**：系统长什么样、为什么这么长。十分钟读完 |
| 2 | [`docs/manual.md`](docs/manual.md) | 使用者 | 装、跑、写自己的节点、读报表、常见报错 |
| 3 | [`docs/l2_schema.md`](docs/l2_schema.md) | 使用者 / 重建者 | 数据契约：L2 五张表与复权反演（§0–§9）、**L3 存储契约**（§10–§17） |
| 4 | [`docs/implementation.md`](docs/implementation.md) | 重建者 | **实现规格**：不变量清单（§〇）、命名、yaml schema、**ops 精确语义**、指纹、执行循环、仿真器、闸门、CLI |
| 5 | [`docs/roadmap.md`](docs/roadmap.md) | — | **尚未实现**的部分。不要照着实现 |

`implementation.md` 每节带 `[SHIPPED]` / `[TARGET]` 标记；没有标记的都是已实现的契约。

## 现状

数据层与引擎都已跑通。250 个 session（2025-08-29 → 2026-08-27）× 503 只 S&P 成分，
125,325 行面板；L3 里 15 个节点（7 个 base + 8 个示例产出）。

```bash
.venv/bin/python tests/run_all.py      # 六套自检 239 项断言, 非零退出即失败
```

`storage/` 入库（约 63 MB）：`pipeline/` 将来要独立成单独的 repo，它一搬走本仓库就
再没有东西能重建 storage/。`registry/security_id.us.csv` 同样入库——它是 append-only 的
ID 注册表，删了历史 ID 的含义就没了。唯一不入库的是 `pnl_out/`。

## 已知缺陷

免费数据源。前三条写进 `_meta.json` 的 `known_defects`，一律不做静默修补：
**生存者偏差**（拿不到期内退市的标的）、**无 vwap**（不以 `(H+L+C)/3` 顶替）、
**`adj_factor` 非 PIT**（厂商 `adjclose` 向后复权，故权威真相是 `cax` 的逐事件事实）。
另有：无 `is_halted` / `delist_date`（pnl 只能走 `--halt-proxy` 降级）、无股本、
参考数据是当前快照回填。详见 `l2_schema.md` §0.1。

复权因子由 `cax` 事件日志独立推导，不取自厂商——交叉校验显示 **502/503 只标的的日收益
与厂商吻合到 1bp 内**，唯一分歧是 MNST 拆股日，我们对、厂商错。
