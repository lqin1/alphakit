# 路线图 —— 目标架构中尚未实现的部分

**这份文档里的东西都不存在。** 从文档重建引擎的人不要照着它实现；它记录的是设计意图,
用来解释已实现部分为什么长成这样, 以及下一步往哪走。

架构总览在 `architecture.md`；已实现的规格在 `implementation.md`（不变量清单与验收
基准在它的 §〇）、`l2_schema.md`、`manual.md`。

---

## 十五、研究工作流与仓库管理

> 本章回答两个问题：**5 个人各积累 100+ 节点之后，仓库怎么不烂**；以及**一个 alpha 从想法到进池，每一步怎么才最省事**。
> 前者的约束大多来自 §二 / §十一，后者的约束来自 §七（v0 无 cache、顺序执行）。

### 15.1 个人 repo：一个节点一个目录，扁平摆放

先纠正一个前提。§十一 说"**路径不变**是关键：移动文件会改变引用名"——**这个理由不成立**。§3.2 规定引用名 = `{repo}.{node_dir}.{node_name}-{output}`，其中只有 `{node_dir}` 与仓库目录同名——而它是**分组名**，不是"文件放在哪"；把一个 yaml 在同一个 `node_dir` 内挪来挪去、或增删同目录下的 yaml，引用名一个字都不变。`code_ref` 是 `{repo, commit, path}`，commit 已把那一刻的树钉死，旧 path 在那个 commit 里永远存在。**真正会被打断的只有两件事：改 `node_dir` 名（那等于改 identity，本就不该做）与 §5.2 registry 里的 `config:` 路径键。**

所以规则是 **「identity 不变、只改状态」**，仓库布局自由。配套两件事：registry 改为按 identity 登记（§15.2），以及 CI 生成一份 identity → 文件路径的索引 `nodes.lock`（提交入库、禁止手改、陈旧则 CI 失败）。

```
g_yliu/
  nodes.lock                       # CI 生成: identity -> {path, node, code, fingerprint, status, tags}
  regions/us.yaml
  lib/                             # 跨节点共用的代码 (扫描族的那一份就在这)
  nodes/
    alpha_yliu_rev/                # node_dir 用完整 identity, ls 一眼看清 kind 与归属
      README.md                    # 假设 / 数据 / 结论 —— 一年后唯一还记得"为什么"的地方
      rev.yaml  rev.py             #   alpha_yliu_rev_w005 / _w020
      rev_mix.yaml  rev_mix.py     #   alpha_yliu_rev_mix
    factor_yliu_liq/       README.md  liq.yaml  liq.py
    factor_yliu_resid_mom/ README.md  ...
```

**`node_dir` 按"常一起重跑的东西"分组，一个 README 说清这组在做什么。** 而节点自身的 kind / ns / 参数全在**节点名**里（`alpha_yliu_rev_w005`），所以 `ls storage/l3/us/g_yliu/rev/` 出来就是 `alpha_yliu_rev_w005-weight/`、`alpha_yliu_rev_w020-weight/`——**分组由目录给、身份由名字给，两者不重复**。四种曾经的备选方案各自的代价：

| 方案 | 代价 |
|---|---|
| 按 kind 分子目录（`factors/` vs `alphas/`） | 重复了节点名里已有的 `{kind}_` 段，且一个 node_dir 里本来就可能既有 factor 又有 alpha（例：`rev/` 下的因子与它的 combo） |
| 按主题分（`reversal/`…） | 分类会漂；一个节点常同时属于两个主题；重新切分主题 = 大规模移动。主题应当是 **tag**（写进节点 meta）而非目录 |
| 按状态分（`wip/` vs `promoted/`） | 恰恰在 registry 指着这个节点的那一刻强迫移动文件。状态应当写在 meta 里（§15.4） |
| 按 study/年份分 | `node_dir` 本身就已经是 study 的粒度；再套一层年份只会让路径变长，而归档靠的是 `status`（§15.4）不是目录 |

> 一个 yaml 里的多个节点共享文件级的 `region` / `universe` / `lookback`。若同一组里的节点需要不同的 `lookback`，拆成同目录下的另一个 yaml 即可——`node_dir` 允许多个 yaml，这正是 `rev.yaml` 与 `rev_mix.yaml` 并存的形态。

### 15.2 晋升：登记而非搬家，以及它的护栏

§十一 规定晋升是提 PR 登记进 registry、**节点仍住在个人 repo**。这条设计避免了改名，但它要能站住，需要四件配套的东西——它们的规则已分别写在 §5.2（按 identity 登记、钉 commit）、§二（写权限在晋升时翻转）、§3.3（写入前指纹校验）与 §十一（已登记节点的未登记依赖 = 编译期错误）。本节只补**运维侧**剩下的两件：

**归档与所有权。** 一次 force-push 若丢掉了被钉的 commit，会让所有历史 `code_ref` 真正悬空（这跟文件移动不同，后者不会）。故：repo 归组织所有而非个人；个人 `main` 开启分支保护（禁止 force-push / 改写历史 / 删库，作者照常推送不受影响）；**登记时把该 commit 的 git bundle 归档进 g_common**——晋升不搬文件，但要取一份不可变副本。registry 条目带 `owner` + `backup_owner`：5 个人一年下来，所有权移交是必然会发生的事。

**落库后的验收检查**，把 §5.2 承诺的"一致性检查告警"说具体：覆盖率对比滚动中位数、NaN 比例落在声明的 `sla` 带内、分位漂移检查、无未来日期的值。任一不过则**不落库**、告警 owner、并在 catalog 标 `stale` 让下游看得见。

**晋升清单**（g_common 的 PR 模板，CI 阻断项）：`nodes.lock` 已重新生成且与钉住的 commit 一致 · registry 钉的是 commit 而非分支 · `owner` / `backup_owner` 均在职 · meta 带 `title` / `tags` / `status` / `region_hash` / `l2_asof` · **所有 deps 均已登记** · 不依赖任何 `status: wip` 的节点 · `region_hash` 等于模板标准值 · 毒化测试与 cutoff 静态检查绿 · alpha 另需 `dims == [di, ii]` 且 ops 以 `scale` 收尾 · 最近 250 个 session 的覆盖率与 NaN 比例在 `sla` 内 · 探针 PnL 已算、与任一已登记节点的最大相关性写进 PR 正文 · 回填 dry-run 对最近 20 个 session 与 store 现值逐位一致。
人工项（非作者 approver）：读该节点目录里的 README，假设是否说清、节点是否与假设相符 · 若最大相关性 > 0.7 需书面说明或撤回 · 商定 tier 与 SLA · 被它取代的旧节点要有废弃计划并设 `replaced_by`。
合并时自动执行：store 节点目录 `chown` 给日更用户、归档 git bundle、meta 置 `status: registered` 与 `promoted_at`、接入监控并把 owner 挂上滞后告警。

### 15.3 发现：靠 PnL 相关性，不靠名字

500+ 节点跨 6 个 repo 时，"是不是已经有人做过 5 日反转"这个问题，**靠名字和 tag 大概只能查到六成**——人取名字是不可靠的。真正能回答的是 **PnL 向量相关性**，而 §十一 为了 alpha 池去重本来就要这套机制。把它变成全局的、每晚跑的：

每个 `dims: [di, ii]` 且非 wip 的节点，夜间跑一次**标准探针**（`ops: [rank, neutralize, scale]`，canonical region/universe）→ `pnl.py` → 存下日收益向量。**4000 session × f4 = 16 KB/节点**，500 个节点 8 MB，500×500 相关矩阵瞬时完成。这是整份计划里性价比最高的一项，也是唯一能抓到"同一个信号、不同公式"的方法。

```
store search --tag reversal --dims di,ii
store search --similar-to g_yliu.factor_yliu_rev.w005 --min-corr 0.6
  → g_lqin.factor_lqin_rev.st     corr 0.93   registered, owner lqin
  → g_common.factor_common_rev.w005 corr 0.88   registered
```

为此 §3.3 的 per-node meta 必须补上：`title` / `tags[]` / `status` / `owner`（CI 强制，缺则不给合）· `node` / `config` / `params`（现有 `code_ref` 只指到**文件**，而 §4.10 里两个变体共用一个 `.py`，光靠 path 说不清是哪个节点、哪组参数）· `fingerprint` · **`l2_asof`**（L2 的 `adj_factor` 是向后复权、每次新分红都会改写历史，见 `l2_schema.md` §0.1.3——不记这个，"重建"在原理上就不可复现）· 探针指标与最近邻。

### 15.4 死节点：不是磁盘问题，是可见性问题

研究产出的绝大多数是失败品。但先把量级摆正：秩-2 稠密节点 96 MB、稀疏的 1–12 MB，500 个约 10–48 GB，**不算问题**；而**一个 m5 秩-3 节点就是 7.5 GB、m1 是 37 GB**。所以策略是：秩-3 激进回收、秩-2 懒回收、**可见性对所有秩都激进**。真正的成本是 catalog 污染与通配的波及面。

**扫描产物靠 `status: wip` 隔离，而不是靠一个沙箱 ns**。§4.9.4 强制手写展开变体，一次扫描就是 20 个节点——"每年 100+ 节点"主要就是这么来的；只有胜出者才被作者显式改成 `keep`。

> 早先的方案是把扫描产物丢进 `{user}_lab` 这样一个沙箱 ns，**但它在本文档自己的语法下不可表达**：§4.11.1 规定 `ns ::= ^[a-z][a-z0-9]*$`（单段、不含下划线，否则 `{kind}_{ns}_{name}` 无从切分），`yliu_lab` 过不了；§4.11.6 又要求 ns 段等于所在 repo 的 owner，个人 repo 也写不出它。而 `wip` 状态已经做到了同样的三件事——不进 `*` 通配、不进默认 catalog、有 TTL——**用一个已有的机制，胜过为同一件事新增一个不可表达的命名空间**。

| status | 进通配 `*` | 默认 catalog | 数据保留 |
|---|---|---|---|
| `wip`（首次写入的默认值） | 否 | `--all` 才见 | 90 天无写入 → tombstone |
| `keep`（作者显式设，需写一行理由） | **是** | 是 | 至废弃 |
| `registered` | **是** | 是 | 永久 |
| `deprecated` | 否 | 灰显 | 180 天 → tombstone |
| `tombstone` | 否 | `--tombstones` | **数据删除，meta 卡片永久保留** |

**`{node_name}-*` 通配应当展开成什么**：仅 `keep` / `registered`；**永不含秩-3**（§7.2 第 4 条：通配 + eager 加载 + 一个 m5 节点 = 直接 OOM，而 §4.7 恰恰鼓励秩-3 与秩-2 同 ns 混放）；且引擎要把本次展开与 meta 里冻结的上次展开做 diff，**移除项报错**（新增才是通配的目的，移除是危险）。

**GC 的正当性来自"L3 是 cache"这个声明本身**（README 已明说 `cache/` 丢了跑一遍就有）。所以 GC 删数据、留 **tombstone 卡片**：`code_ref`、fingerprint、冻结的展开 deps、params、`region_hash`、`l2_asof`、覆盖率、探针指标、以及一条字面的 `rebuild_cmd`。GC **拒绝**碰：已登记的、任何已登记节点冻结依赖列表里的、alpha 池条目引用的、90 天内写过的。

### 15.5 OOS 隔离：两个 store，以及一笔要单独计价的成本

规则已写在 §十一——研究 store 截断于 `T_embargo`、生产 store 全史、单向推送、且"物理隔离"这句话需要出网策略才成立。本节补提交侧的两个设计点：

**每个 alpha 的提交时点才是基准。** 固定的墙会过期；纯滚动的封禁期对一个反复提交的人最终会把一切都揭开。两者都要：封禁期保证提交时**至少**有多长的 OOS 窗口，而池子为每个 alpha 冻结 `submitted_at` / `is_end` / `oos_start = is_end + 1`。评估器此后永远从 `oos_start` 跑到今天——**OOS 证据按 alpha 单调累积**，即使封禁期后来滑过了那一段。

**提交次数预算**（如每人每季 6 次，同 `family` 的变体共用一份家族预算）。没有预算，评估器就是一台神谕机，OOS 会以每次提交约一比特的速度退化成 IS——**这才是真正的失效模式，而不是数据泄漏**。

### 15.6 研究内循环：时间花在哪

先看清 §5.3 的基准（注意那些数是**跑完 2000 天的总耗时**，不是单日）：逐日 handle 约 **118 µs/日**，与"每天做几次全窗口 pandas 运算"相比可以忽略。真正的驱动因素不是你**请求**多大的窗口，而是 handle 在窗口上**做几遍全量运算**：

| | w=6 | w=251 |
|---|---|---|
| 只碰窗口的两行（`rev_w005` 那种） | ~0.3 ms/日 | ~0.9 ms/日 |
| 做一遍全窗口运算 | ~1.4 ms/日 | ~5.4 ms/日 |
| §4.5 `beta_decomp` 那种（约 7 遍，w=251） | — | **~36 ms/日** |

**经验法则：w=250、N=6000 上做一遍全窗口 pandas 运算 ≈ 4 ms/日 ≈ 8 年跑一次多 8 秒。** `ctx.win(250)` 不贵，贵的是在它上面 `pct_change()`。

一次 8 年迭代的构成（短窗口 alpha、`deps: [g_common.field_base_px.*]` 展开约 20 个 field）：进程启动 0.3 s + **读 20 个面板的全史 6.2 s（约 1 GB 常驻）** + handle 循环 0.6 s + ops 链 2.7 s + 落库与 dump 0.7 s + `--pnl` 子进程 3.6 s ≈ **14 秒，其中研究员自己的代码只占 4%**。长窗口 factor 则相反：`beta_decomp` 约 86 秒、95% 在 handle 里。

**所以最高杠杆的改动不是加 cache。** 没有任何 cache 能把第一种情形压到"进程启动 + I/O + 写产物"这约 10 秒之下；第二种情形的开销是研究员自己的算术，cache 同样跳不过。而且**store 本身就已经是叶子 cache**——每个 field、每个节点输出都是物化的 Zarr 数组；v0 缺的不是存储，是失效判定，而 `--only NODE` 已经是手工替代品。

### 15.8 变体比较：把 6 个 metrics.json 变成一个决策

§7.2 已经在一个进程里 `for node in spec.nodes` 循环了，离扫描运行器只差一步：**`--pnl` 对每个 alpha 类节点都评估，而不只对 `output:`**。一行改动，且它是让"一个 yaml 装一次扫描"真正可用的前提；顺带把 6 次独立运行（84 秒）变成一次（约 28 秒），因为面板只读一遍。

产出 `pnl_out/_compare/{config}.md`，一行一个变体，列是 §8.4 的指标集，按 Fitness 排序。两件事让它成为**决策面**而非表格转储：

- **自动识别参数轴**——从各节点 meta 里读 `params`，跨变体做 diff，把有差异的键提到前列。免费，且它把手写展开丢掉的扫描结构又找了回来，不需要 Jinja。
- **一行 `spread`**（各指标在变体间的极差）。这是最具决策价值的一个数：Sharpe 跨度 1.78–1.91 说明这个参数不重要、别再调了；跨度 0.4–2.1 说明你几乎肯定在拟合噪声。

```
                 days  decay | Sharpe  Ret    TO    Fitness  MaxDD | gates
rev_w005_dc7        5      7 |   1.91  10.4%  0.31     1.42  -7.1% | ok      ← best
rev_w005_dc3        5      3 |   1.82  11.2%  0.42     1.31  -8.4% | ok
rev_w020_dc7       20      7 |   1.44   8.1%  0.18     1.19  -9.2% | WARN conc
spread                       |   0.47   3.1%  0.24     0.23   2.1% |
```

外加两张单个 metrics.json 永远给不出的图：**变体间 PnL 相关矩阵**（同一想法的变体通常 0.95+，某个掉到 0.6 要么是另一个想法要么是 bug，且这与 §十一 池去重是同一套计算）与**变体 × 年份的 Sharpe 网格**（这是过拟合的读数：如果 Fitness 冠军只在 2019 年冠军，那个排名就是噪声）。

### 15.10 从想法到 alpha 池：一条命令

§十一 要求四件事（PnL 相关性去重 <0.7、canonical universe 复评、独立进程 OOS、`region_hash` 等于模板标准值）。**清单会被跳过，命令不会。**

```
alpha submit nodes/alpha_yliu_rev_mix/ [--dry-run]
```

它是**对 `run --pnl` 的一次预设，不是新机器**：① 把研究员的 `regions/us.yaml` 换成模板标准值、重算 `region_hash`、**在该口径下重跑**（§二 允许本地自由修改，正是因为有这一步——工具必须**执行**这次重跑，而不是只校验 hash 然后拒绝）· ② canonical universe 复评，并把 `us_top3000` 与 `us_top1500` **并排打印**（§十一 点名了这个诊断："top3000 Sharpe 2.5 → top1500 掉到 0.8 的基本是小票流动性溢价"），不要等 reviewer 来问 · ③ 对池中已有向量做相关性去重（5000 个 alpha 实测 13 ms，池就是 store 里一个 (K×D) f4 数组、5000 个才 40 MB，不需要 DB），**报告最近的 5 个及其相关系数而非只给判决**——"0.68 vs `g_lqin.alpha_lqin_rev_w003.weight`"是可行动的，"拒绝"只会让人瞎猜 · ④ 毒化测试作为提交闸门而非只在 CI · ⑤ 七道闸门全部硬阻断 · ⑥ 冻结提交记录（`code_ref`、config hash、`region_hash`、权重 hash、IS 指标、闸门块、最近邻）· ⑦ 交给 OOS——研究员的环境物理截断于 OOS 边界，他**跑不了**这一步。

回传的东西刻意很窄：accept/reject、完整的 canonical IS 指标、以及 OOS **只以有界摘要形式**返回（Sharpe 分桶、收益符号、OOS/IS 衰减比、与池中最相关成员及其名字）。**OOS 日收益向量永不回传。** 再加一个**提交次数预算**（如每人每季 6 次，同 `family` 的变体共用一份家族预算）——没有预算，评估器就是一台神谕机，OOS 会以每次提交约一比特的速度退化成 IS，**这才是真正的失效模式，而不是数据泄漏**。

`--dry-run` 在本地跑完 ①–⑤ 并打印清单但不建记录，让研究员的最后一公里迭代就是对着真闸门做的，提交本身永远不会有意外。让这条路径立得住的设计性质是：**submit 是拿到 OOS 数字的唯一途径，而且它比手工凑齐证据更省事。**

---


---

## 五、L2/L1 接入与日更

> **本章不在 v0 引擎范围内**（§七 范围声明）。v0 引擎只吃 L3、只吐 L3；L2 → L3 的入库由独立的
> ingestion 管道承担。本章描述的是**目标架构**下把这件事收回统一 Node 模型时的形态，以及当前
> ingestion 管道事实上遵循的语义（路径模板、缺文件告警、schema 强制、逐列容错）。
> 已落地的美股 base 数据集见 [`l2_schema.md`](l2_schema.md)。

### 5.1 L2 = 外部文件路径模板

L2 不是引擎管理的存储，是**外部文件**（vendor 落地、上游管道产出），在节点的 `source` 里声明：

```yaml
source:
  pv:
    path: storage/data/base/l2/us/pv/{date:%Y}/{date:%m}/pv.{date:%Y%m%d}
    format: psv                                  # {date} 按 session 渲染, 支持 strftime
    cols: [open, close]
    key: security_id                             # 标的列, 归一到全局轴
  bars:
    l1: minute_bar                               # L1 源同理
```

> **`format` 是必填的，不能再靠扩展名推断。** 本节初稿写「格式由扩展名判定」，但已定稿的 L2 命名是
> `{subdata}.{YYYYMMDD}`（如 `pv.20250829`）——**不带扩展名**，那条规则在真实布局上失效。
> 当前只有一种取值 `psv`（pipe-separated，`|` 分隔、首行表头、缺失为空字段，见 `docs/l2_schema.md` §2）；
> 将来接入 parquet 源时再扩枚举。缺省 `psv`。

引擎行为：格式按 `format` 声明；psv/csv 按 meta 强制 schema（防 dtype 漂移：日期解析、代码前导零）；**文件缺失 = 该源当日全 NaN + warning**（数据晚到不崩管道，catalog 可见落后）；路径模板进节点 meta，血缘可追。`ctx.l2("名字")` 返回当天的长表切片（index=security_id，已归一到全局轴），`ctx.l1("名字")` 返回当天原始数据并按 `cutoff` 物理截断。

**两者仅在节点声明了 `source` 时存在**——没声明的节点语法上碰不到外部文件，这是"策略只吃标准化数据"的机械保障。

**声明式简写**（无 `code` 的纯拆列节点）：

```yaml
nodes:
  field_base_bar:
    source: {bar: {path: storage/data/base/l2/us/pv/{date:%Y}/{date:%m}/pv.{date:%Y%m%d}, format: psv}}
    outputs:
      open_tc:      {col: open,  dtype: f4}
      adj_close_tc: {expr: "close * adj_factor", dtype: f4}
      sector:       {col: gics_sector, dtype: i1}
```

`col` 取列，`expr` 限同表内简单表达式——**field 的定义应该看一眼就懂，需要解释的写 code**。声明式编译出的 handle 内部逐列 try：失败列当日 NaN + 告警，其余正常。

### 5.2 日更

没有独立的数据管理组件——日更就是 **cron 按 registry 逐个调 `run --ed today`**。registry 在 g_common：

```yaml
version: 2
pipelines:
  - node: g_common.field_base_px.adj_close_1500      # 按 identity 登记, 不按文件路径
    repo: g_common
    commit: 7e21ab...                    # 钉死 commit, 永不写分支名
    fingerprint: sha256:4d02...
    owner: infra
    tier: 1
  - node: g_yliu.factor_yliu_resid_mom.resid_mom
    repo: g_yliu
    commit: f3a9c1...
    fingerprint: sha256:9c1e...
    owner: yliu
    backup_owner: lqin
    tier: 2
    sla: {max_lag_sessions: 1, min_coverage: 0.50, max_nan_ratio: 0.40}
```

**按 identity 登记而非按文件路径**，两个理由：① 仓库内的文件布局可以自由调整（§15.1），路径键会让每次重组都变成生产事故；② 一个 repo 里有几十个实验节点，按 `config:` 或按整个 repo 登记都会把它们一并拖进生产日更。

**`commit` 钉死，不写 `ref: main`。** §二 规定个人 repo 无需 review——若登记的是分支，"晋升时 review 一次"审的是**今天的产物**，明天早上跑的是**另一个产物**。钉 commit 把"未经审查的生产依赖"变成"经过审查的发布"，代价是每次有意变更多提一个 PR，而这正是目的。

每日流程：全局轴 `ensure_session` → **按冻结的 deps 做拓扑排序**后逐节点 `run --ed today`（区间 upsert，非 `write`，见 §7.2）→ 指纹校验 → 重建 catalog → 一致性检查告警。晋升 = 提 PR 登记进 registry（**identity 不变**，review 一次）。

依赖既已冻结在 meta 里，调度器就能自己拓扑排序，不必依赖人工维护的登记顺序——顺序只在**单个 config 内部**才是研究员的责任（§7.1）。

数据侧只剩一个**查询工具**（不是执行器）：

```
store status [NODE | --base]     # last_session / 覆盖率 / NaN 比例 / 落后告警
store catalog rebuild
```

engine 的 `effective_ed`（取依赖 last_session 的 min）由 registry 顺序保证上游先跑。

### 5.3 批量性能出口（可选，不进手册）

统一逐日 handle 后实测无性能损失。基准：2000 天 × 6000 标的、20 日均值——**逐日循环跑完全程 235 ms**（即约 **118 µs/日**）vs pandas 一次性向量化 713 ms；配 state 增量 46 ms。

> **这三个数都是"跑完 2000 天的总耗时"，不是单日耗时。** 读成单日会得出完全相反的结论：235 ms/日 × 2000 天 = 7.8 分钟，比向量化慢 **659 倍**，与本节"无性能损失"的结论直接矛盾。逐日之所以能反超，是因为它只在窗口上做一次增量更新，而向量化要物化整张中间表。

极少数需要跨全样本的计算（PCA、协方差矩阵类）可选实现 `build(ctx, sessions)`，引擎检测到即优先使用——渐进式复杂度，新人只学 handle。

---

