# alphakit

中低频 Alpha 研究与回测系统。美股，日频。

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | 架构设计 v0.8——统一 Node 模型、Zarr 存储、ops 链、precise 仿真、退市停牌口径 |
| [`docs/manual.md`](docs/manual.md) | **用户手册**——新机器从零跑通、写你自己的节点、常见报错 |
| [`docs/l2_schema.md`](docs/l2_schema.md) | **数据契约**——L2（目录布局、五张表 schema、复权反演、V1–V8 断言）与 **L3**（引用名折叠规则、秩与分块、轴、meta、写入语义、口径锚） |

## 现状

**数据层与引擎 v0 都已跑通。**

数据（`storage/data/base/l2/us`）：250 个 session（2025-08-29 → 2026-08-27）× 503 只 S&P 500 成分，
125,325 行面板，稠密度 99.7%；五张表 `pv` / `cax` / `sec_master` / `industry` / `calendar`，
**除 `calendar` 外全部逐交易日 PIT**。校验 V1–V8 + X1–X15 全绿，60 个变异用例逐条证明每道断言会响。

引擎（`alpha_kit/`）：统一 Node 内核、Zarr L3 store（秩-1/2/3）、ops 算子链、编译期命名检查、CLI。
`storage/l3/us` 现有 7 个 base 节点 + 三个示例产出的 8 个节点。

```bash
.venv/bin/python tests/run_all.py      # 五套自检 213 项断言, 全绿
```

三个示例（`architecture.md` §4.10）全部可运行：因子 → alpha（两变体）→ combo（含跨 repo 依赖）。
其中两个实测值得一看：`adv20` 与独立重算**逐点相符**（最大相对误差 0.0e+00）；三个 alpha 按
0.4/0.3/0.3 混合后 `Σ|w|` 只剩 **0.5088**——少了收尾 `scale`，账本只投出去 51%，而 Sharpe 看着正常。

## 布局

```
registry/security_id.us.csv        security_id 注册表: append-only, 必须入库
storage/
  data/base/l1/                    原始落地: vendor payload + 参考快照, 不改写
  data/base/l2/us/                 交付层: {category}/{YYYY}/{mm}/{subdata}.{YYYYMMDD}
  l3/{region}/                     派生层 = L3, 可完全重建
pipeline/
  fetch_yahoo.py                   抓取(幂等可重入)
  build_ref_join.py                参考数据 join (被 build_l2 内存调用)
  build_l2.py                      复权反演 + 五表写出
  validate_l2.py                   验收闸门, 非零退出即失败
tests/
  run_all.py                       五套自检一次跑完, 非零退出即失败
  test_ops.py  test_simulate.py    算子链 / pnl 仿真器
  smoke.py                         引擎端到端
docs/
```

`storage/` **入库**（约 63 MB）：`pipeline/` 将来要独立成单独的 repo，一旦它搬走，
本 repo 里就再没有东西能重建 storage/，所以数据跟着引擎走，clone 下来即可跑。
`registry/` 同样入库：`storage/` 原则上可重建，注册表不行，删了历史 `security_id` 的含义就没了。
唯一不入库的是 `pnl_out/`——一条命令即可再生，且每跑一次全变。

## 重建

数据已随仓库入库，clone 完装上就能跑：

```bash
python3 -m venv .venv && .venv/bin/pip install -e .   # 系统 python 受 PEP 668 限制
source .venv/bin/activate                             # 之后可直接敲 ak / run / pnl

run repos/g_yliu/nodes/alpha_yliu_rev/rev.yaml --sd 2025-12-01
pnl --node g_yliu.alpha_yliu_rev.alpha_yliu_rev_w005-weight --sd 2025-12-01
```

装上四个命令：`alphakit`、`ak`（简写）、`run`、`pnl`。后两个名字很通用，
只在本 venv 内生效。

要从原始数据重造一遍（`pipeline/` 独立成 repo 之前）：

```bash
.venv/bin/python pipeline/fetch_yahoo.py     # ~60s, 503 标的
.venv/bin/python pipeline/build_l2.py        # ~4s
.venv/bin/python pipeline/validate_l2.py     # ~5s, exit 0 才算交付
.venv/bin/python pipeline/build_l3_base.py   # ~3s, L2 → L3
```

## 已知缺陷

数据源为免费源，以下缺陷**写在 `_meta.json` 的 `known_defects` 里，不做静默修补**：

- **生存者偏差**：拿不到期内退市的标的。`architecture.md` §3.4 列为美股必修，需采购数据源
- **无 vwap**：不以 `(H+L+C)/3` 顶替——那会污染任何以执行价为主题的研究
- **`adj_factor` 非 PIT**：厂商 `adjclose` 向后复权，故因子的权威真相是 `cax` 的逐事件事实
- **`ref_asof != date`**：参考源只有当前快照，历史行的 name/exchange/sector 是回填的
- **MNST 2026-07-20 → 08-07 序列损坏**：厂商在个别 bar 上零星施加了拆股复权，已记入 `suspect_securities`

复权因子由 `cax` 事件日志独立推导，不取自厂商——交叉校验显示 **502/503 只标的的日收益与厂商吻合到 1bp 内**，唯一分歧是 MNST 拆股日的 50.39%，我们对、厂商错。
