# alphakit 用户手册

面向两类读者：**要在新机器上把它跑起来的人**（§1–§3），以及**要写自己 alpha 的人**（§4–§6）。
设计原理与取舍见 [`architecture.md`](architecture.md)；数据契约（L2 与 L3）见
[`l2_schema.md`](l2_schema.md)。

---

## 0. 研究菜单（先看这张表）

装好之后日常只用到四条命令。`ak` 是 `alphakit` 的简写，`run` / `pnl` 是两个动词的直呼。

| 想做什么 | 敲什么 |
|---|---|
| 看库里现在有什么 | `ak store status` |
| 查一个节点的元数据 | `ak store meta <ref>` |
| 只做编译检查，不跑数 | `run <节点目录> --dry-run` |
| 跑一个 alpha | `run <节点目录> --sd 2025-12-01` |
| 评估它 | `pnl --node <ref> --sd 2025-12-01` |
| 自检整条链 | `.venv/bin/python tests/run_all.py` |

一条完整的研究回路：

```bash
run repos/g_yliu/nodes/alpha_yliu_rev/rev.yaml --sd 2025-12-01   # 算
pnl --node g_yliu.alpha_yliu_rev.alpha_yliu_rev_w005-weight --sd 2025-12-01   # 评
```

**`--sd` 要跟依赖的可用区间对齐。** 不给 `--sd` 会从轴的第一天起跑，而上游因子往往
要晚若干天才有值——前面那些天会算出空仓（`Σ|w|=0`），指标被无声地拖低。
`ak store status` 的 `first` 列就是每个依赖的起点。

---

## 1. 五分钟看懂它在做什么

三层数据，一条命令：

```
L1  厂商原始落地（json / csv，不改写）
     ↓  pipeline/  —— ingestion，不属于引擎
L2  规整的日频宽表（| 分隔 csv，逐交易日一个文件，全部 PIT）
     ↓  pipeline/build_l3_base.py  —— 一次性
L3  Zarr 数组，全局共享轴（引擎的唯一输入与唯一输出）
     ↓  run          ←── 你写的 yaml + py 只在这一层
    权重文件
     ↓  pnl
    指标
```

**你只写一份 yaml + 一份 py**，其余（数据对齐、防前视、后处理、仿真、指标）由框架承担。

引擎的范围是 **L3 → L3**：输入全部来自 `deps`，输出全部落 L3。L2 入库是 ingestion 管道的事，
它要背文件格式、路径模板、vendor 容错三副担子，与 alpha 研究无关。

---

## 2. 新机器：从零到能跑

### 2.1 前置

只需要 **Python 3.11+** 与 **git**。不需要数据库、不需要 Docker。
磁盘：引导数据集约 60 MB（L1 18 MB + L2 41 MB + L3 2 MB）。

```bash
git clone <repo> alphakit && cd alphakit
python3 -m venv .venv
.venv/bin/pip install -e .
```

四个运行期依赖（pandas / pyarrow / zarr / pyyaml）与它们的下界都写在 `pyproject.toml` 里，
不用手抄。`-e` 是可编辑安装：研究期你会改引擎代码，改完不必重装。

这一步装上**四个命令**，都在 `.venv/bin/` 下：

| 命令 | 等价于 |
|---|---|
| `alphakit` | 全名 |
| `ak` | 同上，简写 |
| `run` | `alphakit run` |
| `pnl` | `alphakit pnl` |

`source .venv/bin/activate` 之后直接敲 `run` / `pnl` 即可。`run` 和 `pnl` 是很通用的
名字，所以它们**只在这个 venv 内生效**——这也正是应当用 venv 而不是 `--user` 装它的理由；
不想冒名字冲突的风险就一律用 `ak run` / `ak pnl`。

> **系统 Python 装不上包？** 较新的发行版按 PEP 668 锁住了全局 site-packages，
> 报 `externally-managed-environment`。上面的 venv 就是正解，不要用 `--break-system-packages`。

### 2.2 取数据（约 2 分钟，需要外网）

```bash
.venv/bin/python pipeline/fetch_yahoo.py     # 503 只 S&P 成分, 约 60 秒
.venv/bin/python pipeline/build_l2.py        # 复权反演 + 五张表, 约 4 秒
.venv/bin/python pipeline/validate_l2.py     # 验收闸门, exit 0 才算数
```

第三条必须 **exit 0**。它跑 V1–V8 契约断言加 X1–X15 结构断言，其中 V1 是把我们自算的复权
日收益与厂商的 `adjclose` 逐点对比——502/503 只标的吻合在 1 个基点内。

`storage/` 整个在 `.gitignore` 里，因为它由上述三步完全可重建。**唯一的例外是
`registry/security_id.us.csv`**：它是 append-only 的 ID 注册表，删掉就丢失了全部历史 ID 的含义。

### 2.3 生成 L3（一次性，约 3 秒）

```bash
.venv/bin/python pipeline/build_l3_base.py
```

产出 7 个基础节点：

| 引用名 | 秩 | 内容 |
|---|---|---|
| `g_common.field_base_px.adj_close_1500` | di×ii | 复权收盘 |
| `…-volume_1500` | di×ii | 原始成交股数 |
| `…-ret_1d_1500` | di×ii | 复权日收益 |
| `…-adv_dollar` | di×ii | 20 日平均成交额 |
| `…-market_ret` | **di** | 等权市场收益（秩-1 示例） |
| `g_common.factor_common_gics.sector` | di×ii | GICS sector 码（i1） |
| `g_common.field_common_univ.us_top400` | di×ii | 按 ADV 排名的池子（bool） |

查看：

```bash
.venv/bin/ak store status
```

### 2.4 跑通三个例子

下文一律写 `run`；没激活 venv 则写全 `.venv/bin/run`。

```bash
run repos/g_yliu/nodes/factor_yliu_liq/             --sd 2025-12-01   # 例5 因子 → 3 个产物
run repos/g_yliu/nodes/factor_yliu_mom/             --sd 2025-12-01   # 动量因子
run repos/g_lqin/nodes/alpha_lqin_senti/            --sd 2025-12-01   # 他人的 alpha
run repos/g_yliu/nodes/alpha_yliu_rev/rev.yaml      --sd 2025-12-01   # 例6 两个变体
run repos/g_yliu/nodes/alpha_yliu_rev/rev_mix.yaml  --sd 2025-12-01   # 例7 combo
```

实际长这样：

```
preflight OK    repos/g_yliu/nodes/alpha_yliu_rev/rev.yaml  2 节点 / 2 输出 / 6 依赖  0 error 0 warn  5.6 ms
run alpha_yliu_rev/rev.yaml  2025-12-01..2026-08-27
  alpha_yliu_rev_w005                216 日 (预热 30) 写入 1 输出  0.65s
  alpha_yliu_rev_w020                216 日 (预热 30) 写入 1 输出  0.68s
```

`预热 30` 是 `lookback` 撑出来的：算 2025-12-01 那天要用到之前 30 个 session 的数据，
引擎自己往前取，不用你操心。

一条命令自证整条链是通的：

```bash
.venv/bin/python tests/run_all.py      # 五套 213 项断言, exit 0 才算装好
```

它把五套自检串起来跑，红一套即非零退出：

| 套件 | 查什么 |
|---|---|
| `tests/test_ops.py` | 算子链（纯内存，不碰 store） |
| `tests/test_simulate.py` | pnl 仿真器：会计恒等式 / 停牌退市三分类 / 七道闸门 |
| `tests/smoke.py` | **引擎**端到端：轴 / store 读写 / 命名检查 / 逐日主循环 / 预热 / 落库路径 / 两道掩码闸门 |
| `pipeline/validate_l2.py` | **数据**的验收闸门（§2.2 那一条，此处重跑一遍） |

`smoke.py` 里有两项值得单看：`adv20` 与独立重算逐点相符（最大相对误差 0.0e+00），
以及 combo 的 `Σ|w| = 1.000000` 且**池外权重恰为 0**。单跑某一套直接
`.venv/bin/python tests/smoke.py` 即可，`run_all.py` 只是把它们串起来。

顺序不能颠倒：**v0 引擎不做图分析**，跨 config 的依赖必须已经在 store 里
（引擎唯一的兜底是"deps 不存在则报错"）。`store status` 可查谁已经落地。

---

## 3. 目录长什么样

```
alpha_kit/              引擎（pip 包, 纯代码零数据定义）
  core/    naming.py axes.py store.py config.py   命名 / 轴 / Zarr store / 配置
  runner/  ctx.py ops.py node.py preflight.py   handle 的世界 / 算子链 / 主循环 / 预检
  pnl/     simulate.py metrics.py report.py  precise 仿真 / 指标与闸门 / 入口
  cli.py                                    run / store / pnl
pipeline/               ingestion（L1 → L2 → L3），不属于引擎
tests/                  五套自检: run_all.py 一条命令跑完
repos/                  研究仓库（目标架构里是三个独立 repo）
  g_common/  共享 ns：base / common / univ / sector …
  g_yliu/  g_lqin/   个人沙箱
registry/               security_id 注册表 —— append-only, 必须入库
storage/                数据（gitignore；可完全重建）
  data/base/l1  l2      摄入层
  l3/us/                派生层
tests/                  自检（不进 wheel）：run_all / test_ops / test_simulate / smoke
docs/
pyproject.toml          包元数据：四个依赖 + `alphakit` 命令 + 只打这四个包
```

`tests/` 在包外而不在 `alpha_kit/` 里：`pyproject.toml` 的 `packages` 是显式白名单，
装到用户机器上的只有引擎四个包——测试是仓库的资产，不是运行期的负担。

---

## 4. 写你自己的节点

### 4.1 名字先想清楚

一次研究涉及六个名字，但**只有输出名是全局的**，其余都由它推导：

```
仓库   repos/g_yliu/nodes/{node_dir}/xxx.yaml + xxx.py    一个 yaml 可含多个节点
节点名 {kind}_{ns}_{name}          kind 与 ns 从这里解析, yaml 里不写
L3     storage/l3/{region}/{repo}/{node_dir}/{node_name}-{output}/
引用   {repo}.{node_dir}.{node_name}-{output}
```

- `kind` ∈ `field`（简单变换）/ `factor`（深加工）/ `alpha`（归一权重）
- **口径从 `regions/{region}.yaml` 继承**：`return_metric` / `booksize` / `cost_model` / `sim` /
  `time_cutoff`，config 里写了就覆盖。**但 `universe` 不继承**——§4.4 规定数据节点缺省是全集，
  这是语义必需：数据若在池内算，边缘票取不到正确值、进出池处会留下滚动窗口断口。
  region 里的 `universe` 是给 alpha 的规范池子，alpha config 自己显式写出来
- 名字里结尾的 `_tc` 会按**本节点**的有效 cutoff 替换（节点 `params.cutoff` > 文件级 > region）
- `ns` 必须等于你的 repo 名去掉 `g_` —— 这条是编译期检查，你写不出别人 ns 的节点
- 单输出的输出名缺省：数据节点取节点名去掉前缀；**alpha 取 `weight`**
- 参数变体带标签：`alpha_yliu_rev_w005` / `_w020`，且 `params.window` 必须与 `w005` 对得上

完整规则见 `architecture.md` §4.11。

### 4.2 最小的一个因子

```yaml
# repos/g_yliu/nodes/factor_yliu_mom/mom.yaml
region: us
lookback: 60

nodes:
  factor_yliu_mom:
    deps: [g_common.field_base_px.adj_close_1500]
    params: {window: 60}
```

```python
# repos/g_yliu/nodes/factor_yliu_mom/mom.py
PX = "g_common.field_base_px.adj_close_1500"

def handle(ctx):
    n  = ctx.params["window"]
    px = ctx.win(PX, n + 1)          # (n+1, N) 窗口, 行标签 -(n)…0
    return px.loc[0] / px.loc[-n] - 1   # 单输出直接 return 裸值
```

```bash
run repos/g_yliu/nodes/factor_yliu_mom/ --sd 2025-12-01
```

落到 `storage/l3/us/g_yliu/mom/factor_yliu_mom-mom/`。

### 4.3 handle 能看到什么

| API | 秩-1 返回 | 秩-2 返回 | 秩-3 返回 |
|---|---|---|---|
| `ctx.f(ref)` | 标量 | `Series(N)` | `DataFrame(N×T)` |
| `ctx.win(ref, w)` | `Series(w)` | `DataFrame(w×N)` | `ndarray(w,N,T)` |

`win` 的行标签是 `-(w-1)…0`，**`0` 就是当前处理日**，历史不足自动 pad NaN、行数恒为 `w`。

其余：`ctx.params` / `ctx.cols` / `ctx.state`（跨日字典）/ `ctx.today()` /
`ctx.cs.rank|zscore|demean` / `ctx.multi_outputs(...)`。

**没有日期参数、没有绝对索引、没有 store 写句柄**——你在语法上就写不出前视。

### 4.4 多个产物

```yaml
    outputs:
      adv20:   {dtype: f4}
      illiq20: {dtype: f4}
```

```python
    return ctx.multi_outputs(adv20=..., illiq20=...)
```

一种情形一种写法：**单输出直接 return 裸值，多输出必须用 `ctx.multi_outputs`**，
读一眼返回语句就知道有几个产物。拼错、漏 key、dtype 转不了，全部在 handle 那一行抛出。

> 算不出值就传 NaN，**不要漏 key**。NaN 是合法值（"这天这只票没有值"）；
> 漏 key 意味着"这个节点今天不存在"，是结构错误。

### 4.5 写成 alpha

alpha 就是**写了池子和 ops 的普通节点**，没有别的机制：

```yaml
region: us
universe: g_common.field_common_univ.us_top400
lookback: 30

nodes:
  alpha_yliu_rev_w005:
    code: rev.py
    params: {window: 5}
    deps: [...]
    ops:
      - rank
      - neutralize: g_common.factor_common_gics.sector
      - linear_decay: 3
      - truncate: 0.02
      - scale: book
```

三条会被编译期挡住的错：

1. **ops 链必须以 `scale` 收尾**。少了它，上游各自 `Σ|w|=1` 的权重线性组合后会因方向相反处
   互相抵消而缩水。**本仓库真实跑出来的三个 alpha 按 0.4/0.3/0.3 混合，`Σ|w|` 只剩 0.5088**
   ——账本只投出去 51%，而 **Sharpe 看着完全正常**，因为收益和风险同比例缩水。
   跑 `tests/smoke.py` 可以复现这个数。
2. **`ops` 用到的分组 field 也要写进 `deps`**。`neutralize` 由引擎在算子链里解析，
   handle 里根本没提它，漏写会在运行期才炸、且报错点离 yaml 很远。
3. **`neutralize` 要写全 ref**，不能写裸名 `sector`——裸名会解析进你自己的 ns。

### 4.6 秩：不是所有数据都是 date × instrument

```yaml
    outputs:
      cpi_yoy: {dtype: f4, dims: [di]}              # 秩-1: 宏观, 无标的轴
      rv_5m:   {dtype: f4, dims: [di, ii, ti], grid: m5}   # 秩-3: 日内
```

秩-1 的依赖在 handle 里取到标量，直接广播即可，不需要对齐代码：

```python
rf  = ctx.f("g_common.field_macro_rates.rf_1m")   # 标量
ret = ctx.f("g_common.field_base_px.ret_1d_1500")  # Series(N)
return ret - rf / 252
```

**alpha 必须是秩-2**（权重就是 di×ii），**CS 类算子只对秩-2 合法**——秩-1 没有 `ii` 轴，
秩-3 的 `ii`/`ti` 二义。这两条都是编译期报错。

### 4.7 先做预检，再跑

**每次 `run` 都会先做一遍零数据的预检**（3–5 ms，只查元数据、不读任何面板），
所以一次完整运行不会在第 12 秒才死于一个拼写错误：

```bash
run repos/g_yliu/nodes/alpha_yliu_rev/rev.yaml --dry-run   # 预检 + 只执行一天, 不落库
```

拼错一个 60 字符的引用名，光靠肉眼很难看出来，所以诊断会给出最近的候选：

```
error DEP_MISSING  …/t.yaml:factor_yliu_t:deps[0]  依赖不在 store 里：…-adj_close_1550
error DEP_MISSING  …/t.yaml:factor_yliu_t:deps[0]  ↳ 该节点在 store 里，但没有输出
   `adj_close_1550`；最接近的是 `adj_close_1500`（编辑距离 1）；它的输出是
   ['adj_close_1500', 'adv_dollar', 'market_ret', 'ret_1d_1500', 'volume_1500']
```

预检查的都是元数据能回答的问题：依赖在不在、通配展开是否为空、秩与 CS 算子是否合法、
alpha 的 ops 是否以 `scale` 收尾、`neutralize` 的分组字段有没有写进 `deps`、
universe 是不是秩-2 bool、`sd`/`ed` 在不在轴上、`ed` 有没有越过数据边界。

它还会用 AST 读你的 `.py`，把 `ctx.win(PX, w)` 里的 `PX` 常量解出来——所以
**「handle 读了一个没写进 deps 的名字」也在预检期就报**，而不是等到算子链深处才炸。

`--dry-run` 再往前一步：预检通过后**只执行一天**（不预热、不落库），
用来抓元数据看不出的形状与返回类型错误，比如 `multi_outputs` 漏了一个 key。

### 4.8 评估：`pnl`

```bash
pnl --node g_yliu.alpha_yliu_rev.alpha_yliu_rev_w005-weight --sd 2025-12-01
```

ref **要带输出名**。单输出的 alpha 那个输出叫 `weight`，所以是 `…_w005-weight`；
漏掉后缀会得到一句明确的报错，不会拿错数据。

**`halt_proxy` 现在从 region 文件来**，不必每次在命令行上敲。`repos/*/regions/us.yaml`
里写着 `sim: {halt_proxy: 3}`，`pnl` 启动时读它；命令行上给 `--halt-proxy` 仍然优先。
`booksize` / `participation` / `return_metric` / `pnl_out` / `l3_root` 同理——口径归 region 管，
这样两个人评估同一份权重才会得到可比的数（`region_hash` 会写进 metrics）。

这个降级口径本身**不是可选的**：本数据集没有 `is_halted` field，§九 规定此时要么显式降级、
要么**拒绝运行**——不允许把 `ghost_days` 记 0 继续跑。仿真器还会拒绝 `K=1`：那会让每个 NaN
都变成"停牌"、第三类恒空，正是 §九 killed 的那个失效模式。

**K 取几是实测出来的，不能拍脑袋。** 本面板的 `ret=NaN` 连续长度分布是
`{1: 500, 2: 2, 41: 1, 186: 1, 199: 1}`——长度 1 的 500 段是首个 session（全体收益未定义），
41/186/199 三段是区间内才上市的 `Q`/`FDXF`/`HONA`，而**真正的盘中缺口只有 2 段、长度都是 2**。
所以 `K=2` 会把它们判成停牌、`ghost_days` 又回到 0；`K=3` 才让那唯一的幽灵持仓浮出来
（实测 `ghost_days=1`，2026-08-10 的一笔空头）。

终端上是这样一张报表：

```
  warn  无 delist_date：`delisted` 恒为 False，退市路径是死代码、delist_events 恒为 0。……
  warn  幽灵持仓 1 个持仓日（1 个 session，占持仓日 0.001%），已按最后可得价当日平仓。检测口径 proxy(3)。

==============================================================================
 g_yliu.alpha_yliu_rev.alpha_yliu_rev_w005-weight
 2025-12-01 .. 2026-08-27   186 sessions × 503 names   book $20.00M
==============================================================================
 收益   Sharpe=-0.254             年化收益=-2.05%           年化美元=$-410.61K
        Fitness=-0.05501          命中率=47.85%             日波动=0.51%
 成本   换手=43.77%               Margin=-1.861 bps         成本合计=$1.63M
        成本/毛利=122.87%         毛利=$1.33M               净利=$-303.07K
 风险   MaxDD=10.98%              回撤额=$2.20M             回撤区间=2025-12-01→2026-05-13
 持仓   多头=$9.99M / 200.3 只    空头=$-10.01M / 199.7 只  多空比=0.9988
------------------------------------------------------------------------------
 七道闸门   3/7 通过
   [PASS    ] market beta    beta=0.08234  r2=0.01412  sharpe_hedged=-0.5015
   [PASS    ] 集中度            total_abs_pnl=6.494e+06  n_names_with_pnl=467  top1_share=0.038
   [NO-BASIS] 区间稳定性          sharpe_by_year={'2025': -5.1156, '2026': 0.1133}  sharpe_first_half=-2.919
   [FAIL    ] 成本临界倍数         cost_total=1.628e+06  pnl_gross=1.325e+06  pnl_net=-3.031e+05
   [PASS    ] 多空平衡           avg_long_value=9.994e+06  avg_short_value=-1.001e+07  avg_long_count=200.3
   [NO-BASIS] 池子卫生           empty_weight_days=0  weight_gross_dev_max=4.511e-09  coverage_min=399
   [FAIL    ] 前视状态           ghost_detection=proxy(3)  ghost_days=1  delist_source=none
------------------------------------------------------------------------------
 审计   ghost_detection=proxy(3)  ghost_days=1  delist_source=none
 缺陷   survivorship_bias_no_delisted, no_vwap, no_shares_outstanding, equal_weighted_market_proxy
 结论   submission readiness: 3/7 gates pass
 交付   pnl_out/g_yliu.alpha_yliu_rev.alpha_yliu_rev_w005-weight/  →  daily.csv  pnl.csv  holding.csv  metrics.json
==============================================================================
```

四交付物落在 `pnl_out/{ref}/`，**全是纯文本**：`holding.csv` / `pnl.csv`（逐股逐日，
归因就是一行 groupby）/ `daily.csv` / `metrics.json`。选 CSV 是为了能 grep、能 diff、
不装 pyarrow 也打得开；代价是体积和读写速度，真要快就别读交付物、直接读 store。

**每道闸门通过也打印数字**，且没有判据时报 `NO-BASIS` 而不是 `PASS`——空白绝不能在
"干净"与"根本没查"之间有歧义。上面这次运行就很说明问题：

- **成本临界倍数 FAIL** 是真结论：10 bps × 0.44 换手，成本 163 万把 133 万毛利全吃掉了，
  净利 −30 万。报 Turnover 回答不了"能不能投"，这个数能。
- **前视状态 FAIL** 是因为我们确实在降级口径下跑（无 `is_halted`、无 `delist_date`）。
  它应该 FAIL，直到接入真数据为止。
- **区间稳定性 NO-BASIS** 是因为 250 个 session 只跨两个**不完整**年份。

外来权重（别人给的、或别的工具算的）走 `--weight FILE`，认 `.csv` / `.feather` / `.parquet`：

```bash
pnl --weight some_weights.csv --sd 2025-12-01
```

---

## 5. 常见报错怎么读

| 报错 | 含义与处置 |
|---|---|
| `依赖 X 不在 store 里` | 上游还没跑。v0 不做图分析，按依赖顺序手动跑；`store status` 查谁已落地 |
| `X 不在 deps 里` | handle 读了一个没声明的 ref。凡是跑起来要读到的 L3 都要写进 `deps` |
| `节点名须以 field/factor/alpha 之一开头` | 节点名要写成 `{kind}_{ns}_{name}` |
| `ns 段是 X，但它住在 g_Y 里` | 个人 repo 只能写自己的 ns |
| `alpha 的 ops 链必须以 scale 收尾` | 见 §4.5 第 1 条 |
| `名字说 w=020，params 说 window=99` | 复制了变体却只改了 params |
| `truncate 需要一个数，却收到 '0.02,'` | YAML 里多了个逗号，会被**静默**解析成字符串 |
| `数据访问只能在 handle 里` | 在 `init` 里调了 `ctx.f`——那时还没有游标 |
| `指纹不符——定义已改变而名字未变` | 改了公式却想往同一个数组里追加。用 `--rebuild` 出新版本，或换 identity |
| `指纹不符` 而你确实改了定义 | 加 `--rebuild`——它的语义就是出新版本，会跳过这道闸门 |
| `booksize='20e6' 是字符串` | YAML 1.1 里 `20e6` / `2.0e7` 都不是数字（指数要带符号），写整数字面量 |
| `用了 _tc 模板但没有有效的 cutoff` | 在节点 `params.cutoff`、文件级 `cutoff:`、或 `regions/{region}.yaml` 的 `time_cutoff` 给一个 |
| `error DEP_MISSING … ↳ 最接近的是 …` | 预检给的候选，多半就是你要的名字 |
| pnl 拒绝运行，要求 `--halt-proxy` | 无 `is_halted` field 时必须显式降级，见 §4.8 |

---

## 6. 这份数据能做什么、不能做什么

引导数据集是 **250 个 session（2025-08-29 → 2026-08-27）× 503 只 S&P 现任成分**，
免费源，以下缺陷**写在 `_meta.json` 的 `known_defects` 里，不做静默修补**：

| 缺口 | 影响 |
|---|---|
| **无退市标的** | 生存者偏差。回测结果系统性偏乐观，不可用于任何对外结论 |
| **无 vwap** | 不以 `(H+L+C)/3` 顶替。任何以执行价为主题的研究做不了 |
| **无股本 / 市值** | `market_ret` 只能等权，算不了市值加权，也没有 size 因子 |
| **无 `delist_date` / `is_halted`** | pnl 的退市/停牌分路缺权威判据，只能走 `--halt-proxy` 降级口径 |
| **只有 503 只票** | `us_top3000` 无意义，示例用 `us_top400`。全量扩容规则见 `l2_schema.md` §3.2 |
| **MNST 2026-07-20 → 08-07** | 厂商序列在该窗口损坏，已记入 `suspect_securities`，建议排除 |

**扩到全量**（约 7500 只非 ETF）：`l2_schema.md` §3.2 有实测确立的代码推导规则——
最容易踩的坑是 class share（`BRK.B` 在 Yahoo 是 `BRK-B`），用错列会**静默丢掉整类标的**。
