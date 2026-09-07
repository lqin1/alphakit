# 中低频 Alpha 研究与回测系统 — 架构设计 v0.8

中低频 Alpha 研究与回测系统的引擎设计。**数据层现状**：250 个 session
（2025-08-29 → 2026-08-27）× 503 只 S&P 成分，五张 L2 表，除 `calendar` 外全部逐交易日 PIT。

## 读这份文档之前

**它规格化的是引擎的设计。** 操作说明在 `manual.md`，数据契约在 `l2_schema.md`，
验收基准（可手算的例子 + 不变量清单）在 `acceptance.md`，**尚未实现**的部分在 `roadmap.md`。

没有标记的章节都是**已实现的契约**；`[TARGET]` 标记的是目标架构，不要照着实现。
同一件事若本文与 `l2_schema.md` 都提到，**以 `l2_schema.md` 为准**——那边是契约，这边是设计。

| 章 | 内容 | 重建时的分量 |
|---|---|---|
| 一 | 系统总览、三层数据模型 | 背景 |
| 二 | 代码与仓库拓扑、模块清单、region 与可比性 | 必读 |
| 三 | 数据体系：L3 命名与路径（**含折叠规则**）、秩、两道掩码闸门 | **核心** |
| 四 | 统一 Node 模型：yaml schema、outputs 与返回值规则、命名约定 | **核心** |
| 五 | L2/L1 接入 `[TARGET]` | 跳过 |
| 六 | ctx 的 API 面；**ops 的精确语义**（§6.2）；**指纹**（§6.3） | **核心** |
| 七 | 执行引擎：逐日主循环、预热、新鲜度回退 | **核心** |
| 八 | 评估：仿真内核（§8.2）、交付物字段规格（§8.4b）、七道闸门（§8.5） | **核心** |
| 九 | 退市与停牌：NaN 的三分类 | **核心** |
| 十 | Ctx 的实现约束 | 必读 |
| 十一 | 质量防线 `[TARGET]` | 跳过 |
| 十二 | CLI 参考 | 必读 |
| 十三–十四 | 测试验收、技术选型 | 背景 |
| 十五 | 研究工作流（只剩已实现的两节） | 背景 |

---

## 一、系统总览

**定位**：把 alpha 研究标准化为一条可缓存、可复现、可比较、可防过拟合的流水线。researcher 只写"一份 config + 一份 Python"，其余（数据对齐、防前视、后处理、仿真、指标）全部由框架承担。

**三个可执行入口，共享一个 store 和一套 core 库：**

```
┌── L1 raw ──────────────────────────────────────────────────────┐
│   TAQ 逐笔 / vendor 原始 / 新闻流   (冷存, 按天分区)              │
└────────┬───────────────────────────────────────────────────────┘
         │  (dataset 处理管道; 允许 L1→L3 直达, 如 TAQ 日频聚合)
┌────────▼───────────────────────────────────────────────────────┐
│   L2 dataset   每 dataset 一张宽表: 行=(date, security_id),      │
│                列=多 fields; 已 PIT / 已归一 security_id         │
└────────┬───────────────────────────────────────────────────────┘
         │
         ┌────────▼──────────────────────────────┐
         │  run (统一 Node 内核, 逐日 handle)        │
         │  v0: L3 → L3; L2→L3 归 ingestion (§五)    │
         │  handle → mask(universe) → ops → 落库    │
         │  三行无分支; alpha = 写了池子和 ops 的节点  │
         └────────┬──────────────────────────────┘
                  ▼
        ┌─────────────────────────────────────────────┐
        │  L3 Store (Zarr, 全局共享轴)                  │
        │  {region}/{repo}/{node_dir}/{node}-{output}/  │
        └──────────────────────┬──────────────────────┘
                               │  权重文件 = 正式接口
                    ┌──────────▼──────────┐
                    │  pnl.py  precise 仿真 │
                    │  holding/pnl/daily/  │
                    │  metrics             │
                    └─────────────────────┘
```

- **run**：唯一执行入口。一个 node = 一个 `init/handle`，多个 L2/L3 输入 → 一或多个 L3 输出。**执行期无任何 kind 分支**：universe 缺省全集、ops 缺省空链，alpha 只是"写了池子和 ops"的普通节点。
- **store**：查询工具（status / catalog），不是执行器。日更 = cron 按 registry 逐个调 `run`。
- **pnl.py**：权重 → 指标。precise 仿真（唯一模式），维护价值账本。独立于引擎，外来权重同样可评估。

**设计原则**

1. **一切皆以 `di` 为首轴的数组**，秩由节点声明：`di`（宏观）/ `di×ii`（缺省）/ `di×ii×ti`（日内）。见 §3.6。
2. 研究 = 生产同一份代码，全系统一个契约（`init/handle`）、一种 yaml、一条 `run`。
3. 内容寻址 + append-only：修数发新版本，无覆盖。
4. 纪律由框架机械强制（API 设计、静态检查、写权限、物理隔离），不靠自觉。
5. 权重文件是引擎与评估的正式接口，两侧独立演进。
6. 每阶段只引入当下必需的组件（当前：无 cache、顺序执行、无 DB）。

---

## 二、代码与仓库拓扑

**三层，按"谁因它损坏而停摆"划分：**

```
alpha_kit/                       ← infra 维护, pip 包, 纯引擎: 零数据定义、零口径配置
  core/      naming rank opspec panels project freshness axes store config
  runner/    node ctx ops preflight
  pnl/       simulate metrics report
  cli.py

**模块清单**（17 个, 约 4470 行; 每个模块只负责一件事）：

| 模块 | 行 | 负责 |
|---|---:|---|
| `core/naming.py` | 138 | 引用名语法、折叠规则、通配、名字合法性 |
| `core/rank.py` | 91 | 秩：形状、分块、`is_panel`/`has_cross_section` 谓词、`dims` 解析 |
| `core/opspec.py` | 104 | **算子的唯一声明**：名字、参数类型、预热贡献；执行方 import 时自证覆盖 |
| `core/panels.py` | 50 | `Panels` 接口——存储的接缝（生产 zarr，测试内存） |
| `core/project.py` | 59 | 项目根发现（`repos/` + `pyproject.toml`/`.git`），`ALPHAKIT_ROOT` |
| `core/freshness.py` | 40 | `ed` 被哪个依赖卡住、卡在哪天（runner 与 preflight 共用） |
| `core/axes.py` | 138 | di/ii 轴，append-only 闸门，容量预留 |
| `core/store.py` | 295 | zarr 读写、区间 upsert、指纹闸门、catalog |
| `core/config.py` | 458 | yaml → Spec：封闭键集、秩校验、ops 归一、参数标签一致性、region 定位 |
| `runner/node.py` | 263 | 逐日主循环、预热（`declared + ops`）、自引用回灌、落库与血缘 |
| `runner/ctx.py` | 278 | handle 能看到的全部世界：惰性面板、窗口、池子掩码、多输出构造 |
| `runner/ops.py` | 410 | 算子实现与 `OpChain` 的逐日状态 |
| `runner/preflight.py` | 571 | 零数据预检：32 个诊断码、AST 解析 handle 读了什么、编辑距离建议 |
| `pnl/simulate.py` | 472 | precise 仿真内核：单值账本、冻结重分配、NaN 三分类 |
| `pnl/metrics.py` | 415 | 指标集、分年/分月切分、七道闸门 |
| `pnl/report.py` | 319 | `evaluate()` 库入口 + 控制台报表 + 四交付物 |
| `cli.py` | 361 | `run` / `store` / `pnl`；口径从 region 摊平成参数

g_common/                        ← 全员可贡献, PR + 非作者 approver; 拥有全部共享 ns
  nodes/field_base_px/           → 节点 field_base_px      (核心行情)
  nodes/field_nscope_news/       → 节点 field_nscope_news  (其他 dataset)
  nodes/factor_common_gics/      → 节点 factor_common_gics (公共 factor)
  lib/                           # 跨 node_dir 共用的工具函数
  registry.yaml                  # 日更管道登记表
  research_template/             # 个人 repo 骨架 + CI + regions/us.yaml

g_yliu/  g_lqin/                 ← 个人 repo, 本人说了算, 无需 review
  regions/us.yaml                # 自 template 而来, 可自由修改 (见下)
  lib/
  nodes/
    factor_yliu_liq/             # node_dir: 一组常一起重跑的东西, 用完整 identity 命名
      liq.yaml                   #   可含多个节点; 节点名 factor_yliu_liq
      liq.py
    alpha_yliu_rev/
      rev.yaml                   #   两个变体 alpha_yliu_rev_w005 / _w020
      rev.py
      rev_mix.yaml  rev_mix.py   #   同目录另一个 yaml: alpha_yliu_rev_mix
```

**`node_dir` 是分组单位、节点名是 identity**——`kind` 与 `ns` 从节点名解析，不在 yaml 里声明（§3.2）。

| 层 | 内容 | 维护 | 坏了谁停摆 |
|---|---|---|---|
| alpha_kit | 引擎代码 | infra | 所有人 |
| g_common | 全部共享数据定义 + registry + template | 全员贡献，需 review | 依赖该节点的人 |
| g_{user} | 个人 region 配置 + factor + alpha | 本人 | 只有本人 |

**alpha_kit 是纯引擎**——数据定义与研究口径全部下沉到 g_common，引擎升级与数据口径变更彻底解耦。registry 跟着数据走（日更是数据生产的事），也在 g_common。

**ns 与 repo 解耦**：g_common 拥有全部共享 ns（`base` / 各 dataset / `common`），个人 repo 拥有自己的 ns。保留 `base` 这个 ns 是有理由的——field 的 ns 本来就是 dataset 名，而 `g_common.field_base_px.*` 通配要能精确地只拉基础行情，不能把所有 dataset 的 field 都卷进来。物理上合并（同一 repo、同一套 review），逻辑上 ns 保持原样。

写权限按 repo 分组，误覆盖在物理上不可能：

```
store/field|factor/<共享 ns>/*        仅 g_common 的 CI / 日更管道可写
store/factor|alpha/{user}/<未登记>    本人直写                    ← 沙箱
store/factor|alpha/{user}/<已登记>    仅日更管道可写, 本人只读     ← 晋升时翻转
store/factor|alpha/<他人>/*            只读
```

**写权限在晋升那一刻翻转**，这不是洁癖：§5.2 的 cron 会 append 已登记节点，而该节点仍住在作者的 ns 下（§十一 晋升不搬家），§3.3 又规定同一节点禁止并发写（Zarr 无锁）。三条并存的后果是——作者早上在本地跑自己那个已登记的节点，撞上夜间尚未结束的 append，**数组损坏且没有任何机制会发现**（catalog 只看 `last_session`，不校验内容）。登记后该节点目录改由日更用户所有，`store.write` / `upsert` 在 `meta.registered == true` 且调用方非 pipeline 时拒绝。作者要继续迭代就用新 identity（`resid_mom_v2`），这本来也是 §一原则3 想要的形态。

想改别人的 factor：copy 到自己 ns 下（`g_yliu.factor_yliu_resid_mom.factor_yliu_resid_mom_v2-resid_mom`），命名空间天然支持 fork。**引用不需要 clone 对方 repo**——deps 解析的是 store 里的数据，不是代码；要看定义时走 catalog 里的 `code_ref`（repo + commit + path）。

### region：每人一份，靠 hash 保可比性

`regions/us.yaml` 由 template 分发到各人 repo，**可自由修改**——试不同 cutoff、不同 booksize 本来就是有价值的实验。代价是口径可能分叉：A 用 booksize 20M、B 用 50M，两人的 Sharpe/Return/Turnover 不可比，alpha 池的去重阈值与排序会失去公共尺度（这正是 BRAIN 用统一 simulation settings 给所有 alpha 排名的原因）。

缓解机制便宜且够用：**region 规范化后的内容 hash 进权重 meta**。

```json
{"region_name": "us", "region_hash": "a3f91c...", "booksize": 20000000, ...}
```

- 研究阶段随便改，互不影响；
- 提交 alpha 池时校验 `region_hash` 是否等于 template 标准值，不等则拒绝或单独分组——**自由研究、统一提交**；
- template 更新 region 时各人 `git merge template`，与 CI 规则走同一条通道。

用户日常：

```bash
pip install alpha_kit
git clone .../g_yliu && cd g_yliu
run nodes/factor_yliu_resid_mom/     --sd 2015-01-01   # 造数据
run nodes/alpha_yliu_rev_senti_mix/  --sd 2018-01-01          # 用数据
```

---

## 三、数据体系

### 3.1 三层

| 层 | 形态 | 说明 |
|---|---|---|
| **L1 raw** | vendor 原始 / TAQ 逐笔 / 新闻流 | 冷存，按天分区，不改写 |
| **L2 dataset** | 每 dataset 一张宽表：行 = (date, security_id)，列 = 多 fields | 已归一 security_id、已 PIT、已处理 corporate actions（raw + adj factor 双存）；parquet |
| **L3** | 每个节点一个数组，秩为 1/2/3（§3.6） | 策略直接消费的唯一层；**引擎的唯一输入与唯一输出**；Zarr 存储 |

路径可以是 **L1→L2→L3**（常规），也可以 **L1→L3 直达**（如 TAQ 分钟聚合，中间不落 L2 表）。

### 3.2 L3 的命名与路径

**一切由节点名承载。** 节点名的形式是 `{kind}_{ns}_{name}`，`kind` 与 `ns` 从中**解析**而来，yaml 里不再声明——少一处可以写错、也少一处可以与目录打架的地方。

```
仓库      g_{user}/nodes/{node_dir}/*.yaml + *.py     ← node_dir 分组, 用完整 identity 命名
节点名    {kind}_{ns}_{name}                          ← 如 factor_yliu_liq
L3 路径   storage/l3/{region}/{repo}/{node_dir}/{leaf}/         ← leaf 见下方折叠规则
引用名    {repo}.{node_dir}.{leaf}                    ← region 由 config 的 `region:` 提供
```

**折叠规则（必须实现）**：`node_name` 与 `node_dir` 同名时, 中间那段不携带任何信息,
**一律省略**; 展开形是**被拒绝**的, 不是同义写法。

| | 叶子形态 | 例 |
|---|---|---|
| `node_name == node_dir` | `{output}` | `g_common.field_base_px.adj_close_1500` |
| 否则 | `{node_name}-{output}` | `g_yliu.alpha_yliu_rev.alpha_yliu_rev_w005-weight` |

之所以能无歧义还原: 名字本身不许含连字符, 所以"叶子里没有连字符"唯一地表示
`node_name == node_dir`。之所以要拒绝展开形而不是当同义词收下: 同一份数据两种拼法,
迟早一半代码写这种、一半写那种, 而它们 hash 出两个不同的 fingerprint 却指向同一个数组。

**引用名与路径是一一对应的纯字符串关系**，不需要索引就能互推（路径与引用名折叠规则一致）：

```
g_yliu.factor_yliu_liq.adv20                          ← 折叠（node_dir == node_name）
   ↕
storage/l3/us/g_yliu/factor_yliu_liq/adv20/

g_yliu.alpha_yliu_rev.alpha_yliu_rev_w005-weight      ← 不折叠（一个 node_dir 装一族变体）
   ↕
storage/l3/us/g_yliu/alpha_yliu_rev/alpha_yliu_rev_w005-weight/
```

通配同理: 折叠形写 `{repo}.{node_dir}.*`, 其余写 `{repo}.{node_dir}.{node_name}-*`;
两者统一为"去掉末尾星号后前缀匹配"。

| 段 | 含义 | 治理挂载点 |
|---|---|---|
| `{repo}` | 谁负责 | 写权限按 repo 分组 |
| `{node_dir}` | 哪一组工作 | 仓库内自由组织 |
| `{node_name}` | 哪一次计算 | `{kind}_{ns}_{name}`，kind/ns 由此解析 |
| `{output}` | 哪一份数据 | 一次计算可以有多个产物 |

**`{node_name}-{output}` 用连字符连接，是刻意的**：它让"哪次计算产出的"与"是什么数据"在一个目录名里同时可读，且 `ls` 出来天然按节点聚集。连字符不会与点号冲突（点号是引用名的分段符），也不会与下划线冲突（下划线在两侧各自的内部使用）。

| kind | 含义 | 治理 |
|---|---|---|
| `field` | 简单变换（pivot、复权、简单滚动） | 共享 ns 定义在 g_common，PR + 非作者 approver |
| `factor` | 深加工（NLP、回归残差、ML 输出） | 进日更需登记 registry（§五） |
| `alpha` | 归一权重 | 可被 pnl 评估；进 alpha 池需去重 + OOS |

**kind 只是名字里的一段**，执行期零作用——差异全部退化为配置字段的取值，执行统一走 §四 的 Node 模型。

**单输出的缺省输出名**：数据节点取节点名去掉 `{kind}_{ns}_` 前缀后的部分；**单输出 alpha 取 `weight`**（它产出的就是权重，没有别的可叫）。

```yaml
deps:
  - g_common.field_base_px.*        # 通配: 编译期展开为该节点当时全部输出
  - g_common.field_base_px.adj_close_tc
  - g_yliu.factor_yliu_liq.rvol20
  - g_lqin.alpha_lqin_senti.weight    # 吃别人的 alpha
```

**通配 `{repo}.{node_dir}.{node_name}-*` 在编译期展开**为该节点当时的全部输出，展开清单写进权重 meta——config 保持一行、新增输出自动可见，同时历史运行的依赖集被冻结、可复现。引擎按 handle 实际 `ctx.f/win` 调用过的名字**惰性加载**：声明是全集、加载是子集。

### 3.3 Zarr 存储方案

**为什么 Zarr**：需求是"读起来像一个大文件 + 每日 append 便宜"。feather/parquet 的索引写在文件尾部，加一行必须重写整个文件（写放大 = 全史）。Zarr 是分块数组，**文件名即 chunk 坐标、无中央索引**，因此 append 只写末块、区间读只解压涉及块、不同块可并发写、未写区零文件。

磁盘形态（实测扒开）：`zarr.json` 是明文 JSON 元数据；每个 chunk 是一个独立文件，内容 = 压缩后的裸 C-order 字节，无 header/footer（关掉压缩可直接 `np.memmap`）。

```
storage/l3/{region}/
  _axes/               # ← 轴按 region 存, 不在 region 之上
    sessions.json      # di 轴: 日期轴, append-only
    securities.json    # ii 轴: security_id 按上市顺序单调分配, append-only
    capacity.json      # {n_active: 6142, allocated: 6500}   ← 列预留缓冲
    grids/m5.json      # ti 轴: 日内网格 {slots: 78, start: "09:30", step: "5min"}
    grids/m30.json     #        定长; 半日市不足处留 NaN, 不缩短网格
  # 没有 catalog 文件: `store.catalog()` 是扫目录现算的（派生, 无需落盘、不会过期）
  g_common/field_base_px/adj_close_1500/
    zarr.json          # shape/chunks/dtype/fill_value/codecs + attributes(per-node meta)
    c/0/0  c/1/0 ...   # chunk 文件
```

**三层元数据，职责不重叠**：

1. **全局轴**——唯一真相源，**该 region 内**所有节点共享同一坐标系。轴按 region 存而非全局：`security_id` 与 session 都是按市场定义的，美股与其他市场既不共享列轴、日历也不同；轴若放在 region 之上，接入第二个市场时要么列轴被迫混装两个市场、要么整个 store 推倒重来。这也与 `registry/security_id.{region}.csv` 的分法一致。只增不减、单调分配，故旧 chunk 永远有效。避免了每节点存一份轴（几千份重复的 security_id 列表 + 不同步风险）。
2. **per-node meta**（写在各自 `zarr.json` 的 attributes，与数据同生共死）：

```json
{"kind":"factor","ns":"yliu","version":3,"dtype":"f4","cutoff":"1500",
 "deps":["g_common.field_base_px.adj_close_1500"],"lookback":250,
 "first_session":1250,"last_session":4021,     ← 各自独立的 watermark
 "n_cols_covered":5820,"registered":true,"updated_at":"...",
 "code_ref":{"repo":"g_yliu","commit":"f3a9c1","path":"factors/..."}}
```

3. **catalog**——②的汇总缓存，供上千节点的检索、新鲜度检查、覆盖率报告。派生物，丢了 `store catalog rebuild` 重来。规模涨了换 SQLite，接口不变。

**`ti` 轴不参与容量预留**：网格是定长且事先注册的，`T` 在节点创建时即固定；换网格 = 换节点名（与"换成本模型 = 换 field 名"同理，§4.9.3）。故只有 `ii` 需要 `ensure_capacity`。

**稀疏与 universe 差异：不需要任何特殊设计**。所有节点一律铺在全局轴上，无数据处为 NaN，成本几乎正比于实际数据量（实测，全局轴 4000×6000，未压缩 96MB）：

| 覆盖情况 | 文件数 | 磁盘 |
|---|---|---|
| 稠密全覆盖 | 17 | 88.7 MB |
| 列稀疏（仅 500/6000 股） | 17 | **7.5 MB**（≈ 500/6000） |
| 时间稀疏（仅最近 2 年） | 3 | 11.1 MB |
| 双稀疏（2 年 × 500 股） | 3 | 0.9 MB |
| 空节点 | 1 | 0 MB |

NaN 压缩率极高，未写区域零文件。因此 `read` 永远返回**对齐到全局轴的完整 DataFrame**（列全在，无数据处 NaN），调用方零对齐负担——这正是 L3 相对 L2 的核心价值。

**参数定稿**（均有实测依据）：

| 项 | 值 | 依据 |
|---|---|---|
| chunks（秩-2） | `(50, N_allocated)` | 小块全面占优：append 3.4ms / 全史读 309ms / 区间读 6.6ms；对比 (250,N) 为 6.6ms / 674ms / 13.7ms。文件数 82/节点，千节点约 8 万文件，无压力 |
| chunks（秩-3） | `(1, N_allocated, T)` | 单日即 0.3–9.4 MB（§3.6），本身已是合适的块尺寸，无需再按日聚块。**一日一块**让日更仍是"只写 1 个 chunk 文件" |
| chunks（秩-1） | `(4096,)` | 整个节点 16 KB，一个块装完 |
| 压缩 | 默认 zstd | blosc+shuffle 仅多省 13% 空间却慢 2 倍 |
| fill_value | **NaN**（必须显式） | 否则未写区是 0，违反 NaN 语义 |
| dtype | float32 / bool / int8 | bool universe 比 float32 省 22 倍 |
| 列容量 | 实际 + 500 预留 | 见下 |

**两种扩容的成本不同**（重要）：
- **按日期 append 是真 O(1)**——新行落进末块，只写 1 个 chunk 文件，与历史长度无关。**三种秩同时成立**，前提是 `di` 恒为首轴（§3.6）。
- **按标的 resize 不是 O(1)**——全宽 chunk 下加一列要重写**所有** chunk。故用 `ensure_capacity` 一次预留 500 列，把它摊薄成**约一年一次**的离线维护（与年末其他维护同期）。这也是"security_id 单调分配、列只在末尾增长"原则的第二个理由。

**Store API**（`core/store.py`，295 行）：

```python
store.read(ref, sd=None, ed=None)          # 区间读 + 对齐到全局轴; 按秩返回 Series/DataFrame/ndarray
store.tail(ref, n=1)                       # 末 n 行, 只解压末块
store.write(ref, df, *, dims, dtype, grid_len, meta, rebuild, fingerprint)
                                           # **唯一的写入口**: 缺省区间 upsert, rebuild=True 才全量重建并 bump version
                                           # 指纹闸门在此, 任何写入方都要过（含 ingestion 脚本）
store.exists(ref) / store.meta(ref) / store.list_refs() / store.expand(pattern) / store.catalog()
store.path(ref) / store.check_fingerprint(ref, fp) / store.ensure_capacity(n)
```

消费方实际用到的是其中八个（`axes` `exists` `meta` `read` `write` `expand` `list_refs` `catalog`），
这八个由 `core/panels.py` 的 `Panels` 声明成接口——生产适配器是 zarr，测试适配器是内存 dict。

**写入前必须校验指纹。** §一原则3 承诺"修数发新版本、无覆盖"，但只有 `write` 会 bump `version`，`append` / `upsert` 都不会。于是**改一行公式再跑日更，同一个数组里改动日之前是定义 A、之后是定义 B**——`version` 没变、meta 没变、catalog 看不出来，事后也无法判断断点在哪天。故 per-node meta 记 `fingerprint`（yaml 子树 + code 字节 + 解析后的 deps identity + params 的 hash），写入前重算比对，不符则**拒绝写入**，要求显式 `--rebuild`（新版本）或换 identity。

**运维三条**：不同节点并行写安全（独立目录），同一节点禁止并发写（Zarr 无锁，**故已登记节点的写权限归日更用户独占**，见 §15.2）；备份用 `rsync`，目录结构天然增量同步；`/dev/shm` 的 npy 物化 + mmap 是可选加速层（并行版启用，接口不变——Zarr 解压后是普通 ndarray）。

**feather 保留在两处**：L2 长表（或 parquet）、以及 dump 出口（per-day CSV/feather，对人与下游系统）。

### 3.4 标的与时间（地基）

- **securities master**：内部 `security_id` 永不重用（美股 ticker 会被回收）；ticker 为带生效区间的属性；company↔listing 映射做 share class 去重。
  **已实现**：ID 由持久注册表 `registry/security_id.us.csv` 分配，append-only——既有条目永不重编号、新标的取 `max+1`。键用 `(CIK, ticker)`：CIK 跨改名稳定，配 ticker 又能分开 share class（GOOGL 与 GOOG 共用 CIK `0001652044`，正是本条 company↔listing 之分）。**该文件不在 `storage/` 内也不被 gitignore**——`storage/` 可整体重建，注册表不行。见 `l2_schema.md` §3.1。
- **日历注册表**：NYSE 日历，session 为 int 索引，半日市标注。`(session, offset)` 双字段仅存在于分钟管道内部，主 store 无感。
- **美股必修三件**：delisting return（退市日写入最终对价，缺此年化虚增 2–4%）、raw price + adjustment factor 双存、含退市标的的历史池子（杜绝生存者偏差）。

### 3.5 Universe：一个名字，三个角色

alpha config 只写 `universe: g_common.field_common_univ.us_top3000`——它就是 store 里一个 bool field（写全 ref 的理由见 §4.11.6；放 `univ` ns 而非 `base`，是因为 `g_common.field_base_px.*` 是 template 的默认 deps，放进 base 会被展开进每一个节点的冻结依赖列表）。引擎用它做三件事：① handle 交付的数据中**当日池外的列整列 NaN**（截面统计天然限定池内，非 skipna 的写法会立刻得到 NaN 报警——吵闹地失败）；② CS 类 ops 的默认 scope；③ 权重掩码。

**掩码作用点**：ops 链之前池外强制 NaN、`scale` 之后池外强制 0——两端夹住，中间自由。

池子**怎么生产**（PIT、含退市、ADV 门槛、缓冲带 hysteresis、月度重构、share class 去重）是该 field 生产者的内部事务，不占用 researcher 心智。

---

### 3.6 L3 的秩：`di` / `di×ii` / `di×ii×ti`

早期版本假定 L3 恒为 `date × instrument`。这条假定挡住了两类真实数据：**没有标的轴的**（宏观：CPI、失业率、国债利率、VIX）与**多一根日内轴的**（TAQ 聚合：逐 5 分钟的 RV、spread、订单不平衡）。故 L3 的形状改为**由节点声明的秩**，三根轴的含义固定：

| 轴 | 含义 | 真相源 |
|---|---|---|
| `di` | session 序号，**所有秩都必须有，且恒为首轴** | `_axes/sessions.json` |
| `ii` | `security_id` | `_axes/securities.json` |
| `ti` | 日内时间槽 | `_axes/grids/{name}.json` |

| 秩 | `dims` | 形状 | 典型节点 | 单节点满仓体积（f4） |
|---|---|---|---|---|
| 1 | `[di]` | `(D,)` | `g_common.field_macro_cpi.yoy`、`g_common.field_macro_rates.rf_1m` | 4000 × 4B = **16 KB** |
| 2 | `[di, ii]` | `(D, N)` | 绝大多数 field / factor / **全部 alpha** | 4000 × 6000 × 4B = **96 MB** |
| 3 | `[di, ii, ti]` | `(D, N, T)` | `g_common.field_taq_rv.rv_5m`、`g_common.field_taq_rv.spread_5m` | 见下 |

**首轴恒为 `di` 不是美学选择**：引擎逐日推进，日期在首轴才能让"按日 append 只写末块"这条 O(1) 性质对三种秩同时成立（§3.3）。

**秩-3 的体积必须先算清楚再用**：

| 网格 | T | 单日 | 4000 日 |
|---|---|---|---|
| `m30` | 13 | 0.31 MB | 1.2 GB |
| `m5` | 78 | 1.9 MB | 7.5 GB |
| `m1` | 390 | 9.4 MB | **37 GB** |

（6000 列满仓、未压缩；NaN 压缩率极高，实际按覆盖率折算。）

所以 §一 那条「TAQ 只作为日频字段的**原料**」的建议**依然成立**——秩-3 是存储层的能力，不是默认工作方式。绝大多数日内信息应当在 ingestion 阶段就压成秩-2 的日频 field；只有确实需要保留日内形态、且能承受体积的少数节点才落秩-3，并优先用粗网格 + 窄池子。

**`ti` 网格是注册表里的一等公民**，不是每节点自定义：`_axes/grids/m5.json` 声明槽位数与每槽的起止时间（半日市槽位不足处**留 NaN 而非缩短网格**——定长网格让 `(1, N, T)` 分块规整，且与 §3.3「未写区零文件」的稀疏免费性质一致）。节点在 `outputs` 里引用网格名。

**跨秩混用天然可行**，无需特殊机制：秩-1 的依赖在 handle 里取到的是标量，pandas / numpy 广播即可；秩-2 依赖取到 Series。

```python
def handle(ctx):
    rf  = ctx.f("g_common.field_macro_rates.rf_1m")          # 秩-1 -> 标量
    ret = ctx.f("g_common.field_base_px.ret_1d_1500")     # 秩-2 -> Series(N)
    return ret - rf / 252                     # 广播, 无需对齐代码
```

**秩对下游的硬约束**：

- **alpha 必须是秩-2**——权重是 `di×ii`。节点若是 `output` 或被别的节点当 alpha 引用，而 `dims` 不是 `[di, ii]`，**编译期报错**。
- **universe 只对秩-2/3 有意义**。秩-1 节点声明 `universe` → 编译期报错（没有 `ii` 轴可掩）。秩-3 的掩码沿 `ti` 广播：池外标的整个 `(ti)` 切片置 NaN。
- **ops 分轴**：CS 类（`rank` / `neutralize` / `truncate` / `scale`）作用在 `ii` 上，**仅秩-2 合法**；TS 类（`linear_decay` / `exp_decay` / `delay`）作用在 `di` 上，三种秩皆合法。秩-1 用 CS 算子、秩-3 用 CS 算子（轴不明确）均为编译期错误。

---

## 四、统一 Node 模型

**系统只有一种可执行单元：node。** 多个 L2/L3 输入 → 一或多个 L3 输出。没有 dmgr 与 engine 之分，没有 field/factor/alpha 之分——**执行期不存在任何按 kind 的分支**，差异全部退化为配置字段的取值。

```python
# 引擎内核: 三行, 无分支
out = handle(ctx)                                  # 裸值 or ctx.multi_outputs(...)
for name, s in normalize(out, node).items():
    s = mask(s, node.universe)                     # universe 缺省 all → 无操作
    s = ops_chain[name](s, t)                      # ops 缺省 [] → 无操作
    store.write(node.ref(name), s)          # 路径由 ref 推导, 见 §3.2
```

alpha 只是"universe 写了具体池子、ops 写了链"的普通节点。

### 4.1 Schema

**键集是封闭的**：认不得的键**报错**（带最接近的候选），不是静默丢弃。拼错 `universe:`
一个字母的代价是——池子成 `None`、掩码恒 True、alpha 悄悄按全集交易而不是 `us_top400`，
而预检里每一处 universe 检查都在 `if spec.universe:` 后面，一句话都不会说。

**文件级键**（共 9 个）：

| 键 | 类型 | 必填 | 缺省 |
|---|---|---|---|
| `region` | str | 否 | `"us"` |
| `nodes` | mapping | **是** | — |
| `universe` | 全 ref | 否 | 无（= 全集；数据节点必须留空，见 §4.4） |
| `lookback` | int | 否 | `0` |
| `cutoff` | str | 否 | region 的 `time_cutoff` |
| `booksize` | int | 否 | region 的 `booksize`；**不要写 `20e6`**，YAML 1.1 里那是字符串 |
| `return_metric` | 全 ref | 否 | region 的 `return_metric` |
| `cost_model` | 全 ref / null | 否 | region 的 `cost_model` |
| `sim` | mapping | 否 | region 的 `sim` |

**节点级键**（共 5 个，在 `nodes.{节点名}` 下）：

| 键 | 类型 | 必填 | 缺省 |
|---|---|---|---|
| `code` | str | 否 | 与 yaml 同名的 `.py`（同目录） |
| `deps` | list[全 ref] | 否 | `[]` |
| `params` | mapping **或** mapping 的 list | 否 | `{}`（四种写法归一，见下） |
| `ops` | list | 否 | `[]`；与 `outputs.*.ops` **不可并存** |
| `outputs` | mapping | 否 | 单输出，键名 = 缺省名（见下） |

**输出级键**（共 4 个，在 `outputs.{输出名}` 下）：`dims`（缺省 `[di, ii]`）、
`dtype`（缺省 `"f4"`）、`grid`（秩-3 **必填**）、`ops`。

**单输出的缺省名**：数据节点 = 节点名去掉 `{kind}_{ns}_` 前缀（`factor_yliu_liq` → `liq`）；
**alpha 恒为 `weight`**。显式写单输出时，键名**必须等于**这个缺省名（检查 ③）——
否则 identity 与它产出的数据对不上号。

**`params` 的四种写法**归一成同一个 dict，且**归一发生在指纹之前**（否则同一份定义
会 hash 出两个指纹却指向同一个数组）：

```yaml
params: {window: 5, halflife: 7}      params:            params:              params:
                                        window: 5          - window: 5          - window: 5
                                        halflife: 7          halflife: 7        - halflife: 7
```

重复键报错，不静默让后者覆盖前者。

**region 文件**（`repos/{repo}/regions/{region}.yaml`，每个 repo 各存一份）：

| 键 | 用途 | 进 `region_hash` |
|---|---|---|
| `calendar` / `time_cutoff` / `return_metric` / `universe` / `booksize` / `cost_model` / `sim` | 口径 | **是** |
| `l3_root` / `pnl_out` | 本机路径 | **否** |

`sim` 下目前两个键：`participation`（float）、`halt_proxy`（**int**，见 §九）。
路径键不进 hash 是刻意的：`region_hash` 要回答"你我用的是同一套口径吗"，不是
"你我的磁盘长得一样吗"——研究 repo 搬到别处时要给 `l3_root` 写绝对路径，那不该
让你和队友被判成口径分叉。


```yaml
region: us                # 环境: 见 §4.1.1; alpha 可覆盖其中 booksize / sim.*
universe: g_common.field_common_univ.us_top3000   # 缺省 all; 数据节点通常不写
lookback: 30
return_metric: g_common.field_base_px.vwap_return_1500_1530   # alpha 必填
booksize: 20000000        # 可选, 覆盖 region。不要写 20e6, 见 §4.1.1
cost_model: g_common.field_common_cost.bps_liquidity_v1          # 可选
sim: {participation: 0.10}                                            # 可选

nodes:
  {kind}_{ns}_{name}:     # 节点名即 identity; kind 与 ns 由它解析, 不单独声明
    code: xxx.py          # 可省略, 缺省 = 同目录下与 yaml 同名的 .py
    deps:                 # v0 唯一的输入来源 (§七), 块状列表
      - {repo}.{node_dir}.{node_name}-{output}
    params: {...}         # cutoff 等一切参数都放这里
    ops: [...]            # 单输出语法糖; 多输出时写在各 outputs.{key}.ops
    outputs:              # 省略 = 单输出
      {key}:
        dtype: f4
        dims: [di, ii]    # 缺省 [di, ii]; 秩-1 写 [di]; 秩-3 写 [di, ii, ti] (§3.6)
        grid: m5          # 仅秩-3 必填
        ops: [...]
```

**取消的三个字段**：`kind` / `ns` 由节点名解析（§3.2）；`source`（L2/L1 直读）随 §七 的 L3→L3 范围移出 v0，归 ingestion 管道（§五）；`cutoff` 并入 `params`；`output:`（原 `--pnl` 终点）——`--pnl` 对本次运行里**每个** alpha 类节点都评估，不需要指定终点。

**一个 yaml 可以装多个节点**，它们共享文件级的 `region` / `universe` / `lookback`。`node_dir` 是分组单位：一条链、一次参数扫描、或一组常一起重跑的东西放一个目录。

#### 4.1.1 `regions/{name}.yaml`

`region_hash` 这套可比性机制（§二）完全建立在这个文件上，故它的字段与**规范化规则**必须是定义好的，否则 hash 不可复现：

```yaml
calendar: nyse
time_cutoff: "1500"       # `_tc` 模板的缺省替换值 (§4.9.5)
return_metric: g_common.field_base_px.vwap_return_1500_1530
universe: g_common.field_common_univ.us_top3000
booksize: 20000000        # 必须是整数字面量
cost_model: g_common.field_common_cost.bps_liquidity_v1
sim:
  participation: 0.10     # cap = participation × adv_dollar (§8.2)
  halt_proxy: 3           # **int**; 无 is_halted field 时的降级口径 (§九)。
                          # 写 null 会让仿真器**拒绝运行**——那正是 §九 要的: 要么显式降级, 要么别跑
```

> **数值必须写成字面量，不能用科学计数法。** 实测 PyYAML：`booksize: 20e6` 解析出的是**字符串** `'20e6'`，`2.0e7` 同样是字符串——YAML 1.1 要求指数带符号才认作浮点。同理 `time_cutoff: 0930` 会被当字符串（幸而正是想要的），但 `0930` 若在别处被当数字读就是八进制。这与 §6.2 提到的 `truncate: 0.02,` 是同一类静默类型错误，故 §4.11.6 的检查 ⑦ 也要覆盖 region/spec 里的标量。

**规范化（hash 前）**：递归按键排序 · 剥离全部注释与空行 · 数值统一为最短往返表示· 字符串统一双引号 · UTF-8 无 BOM · LF 换行 · 末尾单个换行。**规范化规则本身是契约的一部分**——换一种排序或数值写法就会得到不同的 hash，"提交时校验 `region_hash` 等于模板标准值"这条随之失效。

alpha config 里覆盖了哪些字段，覆盖后的**有效值**与 region 内容一并进 hash（§4.9.1）。

### 4.2 outputs 与返回值规则

**存储路径见 §3.2：`{region}/{repo}/{node_dir}/{node_name}-{output}/`。** `outputs` 省略即单输出，dtype = f4，输出名按 §3.2 的缺省规则（数据节点 = 节点名去掉 `{kind}_{ns}_` 前缀；**单输出 alpha = `weight`**）——alpha 与单产物数据节点因此都不必写这一段。

| yaml `outputs` | handle 返回 | 结果 |
|---|---|---|
| 省略 | 裸值 | 缺省输出名，f4，`dims [di, ii]` |
| 1 个 key | 裸值 | 按声明的 key / dtype / dims 落库 |
| 省略 或 1 个 key | `ctx.multi_outputs(...)` | **报错**：单输出直接 return 值 |
| ≥2 个 key | 裸值 | **报错**：声明了 N 个输出，必须用 `ctx.multi_outputs` |
| ≥2 个 key | `ctx.multi_outputs(...)` | 构造器校验 keys / dtype / **形状**，正常落库 |
| 任意 | `None` | 沿用昨日 |

**"裸值"的具体形状由 `dims` 决定**——引擎逐日推进，handle 交付的永远是**当日那一片**，秩只改变这一片的形状：

| `dims` | handle 当日应返回 | 落库后 |
|---|---|---|
| `[di]` | 标量（`float` / 0-d） | `z[t] = v` |
| `[di, ii]` | `Series(N)` / `ndarray(N,)` | `z[t, :] = v` |
| `[di, ii, ti]` | `DataFrame(N×T)` / `ndarray(N, T)` | `z[t, :, :] = v` |

形状不符**在 handle 那一行抛错**（与 §4.3 的 keys 校验同一处），不进引擎二次校验。日循环结构对三种秩完全一致——这正是"`di` 恒为首轴"换来的：引擎只推进游标，不关心切片的秩。

**一种情形一种写法**：读一眼返回语句就知道该节点有几个输出。`ops` 在节点级与 `outputs.{key}.ops` 同时出现 → 编译期报错。

### 4.3 ctx.multi_outputs：错误发生在写错的那一行

```python
def _make_multi_outputs(spec, cols):
    want = spec.outputs
    def multi_outputs(**kw):
        if len(want) < 2:
            raise ValueError(f"{spec.name} 只有一个输出，直接 return 值即可")
        if unknown := set(kw) - set(want):
            hint = difflib.get_close_matches(sorted(unknown)[0], want, 1)
            raise ValueError(f"未声明的输出 {sorted(unknown)}"
                             + (f"；是否想写 {hint[0]}?" if hint else ""))
        if missing := set(want) - set(kw):
            raise ValueError(f"缺少输出 {sorted(missing)}；算不出值请传 NaN，不要漏 key")
        return {k: cast(to_series(v, cols), want[k]["dtype"]) for k, v in kw.items()}
    return multi_outputs
```

拼写错、漏字段、dtype 转不了，全部在 handle 的那一行抛出，堆栈直指写错位置，typo 带修复建议。多输出的正确性完全由构造器保证，引擎不再二次校验（单一职责，避免两处逻辑漂移）。

**NaN 是合法值，缺 key 不是**：某天算不出就传 NaN（"这天这只票没有值"，数据语义的一部分）；漏 key 意味着"这个节点今天不存在"，是结构错误。输出集合是节点的静态属性——store 里的 zarr 数组在首次运行时创建，键集合中途变化会让 meta / sibling_outputs / 血缘全部失稳。

### 4.4 universe 与 ops 的缺省语义

- **universe 缺省 `all`**（恒 True 的全集 bool field）。数据节点用全集是**语义必需**而非偷懒：不同 alpha 用不同池子，数据若在池内算，边缘票取不到正确值、进出池处留下滚动窗口断口。这条从"引擎特判"降级为"配置默认值"。
- **ops 缺省 `[]`**。数据节点想 rank 就写 `ops: [rank]`，和 alpha 用同一套算子。
- **`scale` 不再自动补**，改为编译期校验：节点是 `output` 或被其他节点当 alpha 引用 → ops 必须以 `scale` 收尾，否则报错提示。显式优于隐式，且执行期依然无分支。

### 4.5 例 1：L3 → 多个 L3（一次回归两个产物）

> 落库两条：`storage/l3/us/g_yliu/beta_decomp/factor_yliu_beta_decomp-mkt_beta_w250/` 与
> `…-resid_mom_w250/`。**节点名整个进了路径**，所以即便别处另有一个节点也产出叫 `resid_mom` 的东西，
> 两者也不会撞在一起——这是 §3.2 把 `{node_name}-{output}` 一起写进叶子换来的。

```yaml
# g_yliu/nodes/factor_yliu_beta_decomp/beta_decomp.yaml   ← node_dir 用完整 identity
region: us                                        # kind / ns 由节点名解析, 不声明
lookback: 250

nodes:
  factor_yliu_beta_decomp:                        # {kind}_{ns}_{name}
    deps:                                         # code: 省略 -> 同目录 factor_yliu_beta_decomp.py
      - g_common.field_base_px.adj_close_tc
      - g_common.field_base_px.market_ret
    params:
      window: 250
    outputs:
      mkt_beta_w250:  {dtype: f4}
      resid_mom_w250: {dtype: f4, ops: [rank]}    # 数据节点也能用 ops
```

```python
# g_yliu/nodes/factor_yliu_beta_decomp/beta_decomp.py
def handle(ctx):
    w = ctx.params["window"]
    px  = ctx.win("g_common.field_base_px.adj_close_tc", w + 1)
    mkt = ctx.win("g_common.field_base_px.market_ret", w + 1)
    ret, mr = px.pct_change(), mkt.pct_change()   # ret 是 (w,N); mr 是 (w,) 秩-1

    if ret.iloc[1:].isna().all().all():                    # 历史不足
        nan = pd.Series(np.nan, index=ctx.cols)
        return ctx.multi_outputs(mkt_beta_w250=nan, resid_mom_w250=nan)  # 传 NaN, 别漏 key

    # 必须 .mul(axis=0)：`ret * mr` 会把 mr 的 index 当成列名去对齐, 见下方警告
    beta = (ret.mul(mr, axis=0).mean() - ret.mean() * mr.mean()) / mr.var()
    return ctx.multi_outputs(mkt_beta_w250=beta,
                             resid_mom_w250=(ret.sub(beta * mr.iloc[-1], axis=1)).sum())
```

### 4.6 例 2：单输出数据节点（直接 return 值）

```yaml
# g_yliu/nodes/factor_yliu_intraday_vol/intraday_vol.yaml
region: us

nodes:
  factor_yliu_intraday_vol:                       # code: 省略 -> 同目录 intraday_vol.py
    deps: 
        - g_common.field_taq_bar.ret_5m     # v0 只吃 L3 (§七)
    params:                      # cutoff 是参数, 不是独立字段
      cutoff: "1500"
    # 无 outputs -> 单输出, 输出名 = 节点名去掉 {kind}_{ns}_ 前缀 = intraday_vol
    #   落 storage/l3/us/g_yliu/factor_yliu_intraday_vol/intraday_vol/
```

```python
# g_yliu/nodes/factor_yliu_intraday_vol/intraday_vol.py
import numpy as np, pandas as pd

def handle(ctx):
    r = ctx.f("g_common.field_taq_bar.ret_5m")        # 秩-3 当日片 (N, T)
    return pd.Series(np.sqrt((r ** 2).sum(axis=1)), index=ctx.cols)   # 裸值, 压回秩-2
```

### 4.7 例 3：秩-1（宏观）与秩-3（日内）

**秩-1**——没有标的轴，handle 每天交付一个标量：

```yaml
# g_common/nodes/field_macro_cpi/cpi.yaml
region: us

nodes:
  field_macro_cpi:
    deps:
        - g_common.field_macro_cpi.index
    outputs:
      cpi: {dtype: f4, dims: [di]}         # 秩-1: 无 ii 轴 -> …/field_macro_cpi/cpi/
      # 单输出的键**必须**等于缺省名（节点名去掉 kind_ns_ 前缀 = `cpi`, 见 §4.11.6 检查③）;
      # 想叫 `yoy` 就得显式声明多个输出, 或者把节点改名成 field_macro_yoy
```

```python
# g_common/nodes/field_macro_cpi/cpi.py
def handle(ctx):
    ix = ctx.win("g_common.field_macro_cpi.index", 253)   # 秩-1 依赖 -> Series(253)
    return ix.loc[0] / ix.loc[-252] - 1          # 标量
```

宏观序列在日频轴上多数日子无新值（CPI 月频）。**是否沿用最后可得值由该 field 自己的 meta 声明**，与附录 B 对价格类 field 的处置同一条规则——消费方不必分别处理，`ctx.f` 拿到的永远是那天的既定值。秩-1 节点**不得声明 `universe`**，也不得使用 CS 类 ops（§3.6）。

**秩-3**——多一根 `ti` 轴，handle 每天交付一个 `(N, T)` 切片：

```yaml
# g_common/nodes/field_taq_rv/rv.yaml
region: us

nodes:
  field_taq_rv:
    deps: [g_common.field_taq_bar.ret_5m]
    outputs:
      rv_5m:    {dtype: f4, dims: [di, ii, ti], grid: m5}   # 秩-3, 78 槽
      rv_daily: {dtype: f4}                                  # 缺省 [di, ii] -> 秩-2
```

```python
# g_common/nodes/field_taq_rv/rv.py
def handle(ctx):
    r = ctx.f("g_common.field_taq_bar.ret_5m")            # 秩-3 当日片 -> DataFrame(N x 78)
    return ctx.multi_outputs(
        rv_5m    = r ** 2,                   # (N, 78) 落秩-3
        rv_daily = (r ** 2).sum(axis=1),     # (N,)    落秩-2
    )
```

**同一节点可以同时产出不同秩的输出**——这正是「TAQ 作为原料」在存储层的落地方式：细网格留给少数确实需要日内形态的研究，日频聚合供绝大多数节点消费，两者出自同一份代码、同一次遍历，不会漂移。绝大多数下游只 `deps: [g_common.field_taq_rv.rv_daily]`，按 §3.2 的惰性加载根本不会碰到那 7.5 GB 的秩-3 数组。

### 4.8 例 4：alpha 与 combo

```yaml
# g_yliu/nodes/alpha_yliu_rev/rev.yaml
region: us                        # kind 缺省 alpha, ns 由 repo 目录推导
universe: g_common.field_common_univ.us_top3000   # 数据节点不写 = 全集; alpha 写具体池子
lookback: 30

nodes:
  alpha_yliu_rev_w005:
    params:
      window: 5
    deps: [g_common.field_base_px.*]
    ops:
      - rank
      - neutralize: g_common.factor_common_gics.sector
      - linear_decay: 3
      - truncate: 0.02
      - scale: book
```

```yaml
# g_yliu/nodes/alpha_yliu_rev/rev_senti_mix.yaml
region: us
universe: g_common.field_common_univ.us_top3000
nodes:
  alpha_yliu_rev_senti_mix:       # combo = deps 含 alpha 的普通节点, 不是特殊 kind
    deps: [g_yliu.alpha_yliu_rev.alpha_yliu_rev_w005-weight, g_lqin.alpha_lqin_senti.weight]
    ops:
      - truncate: 0.02
      - scale: book
```

```python
# g_yliu/nodes/alpha_yliu_rev/rev.py
def handle(ctx):
    n  = ctx.params["window"]
    px = ctx.win("g_common.field_base_px.adj_close_tc", n + 1)
    return -(px.loc[0] / px.loc[-n] - 1)                  # 单输出, 裸值

# g_yliu/nodes/alpha_yliu_rev/rev_senti_mix.py
def handle(ctx):
    return 0.6 * ctx.f("g_yliu.alpha_yliu_rev.alpha_yliu_rev_w005-weight") + 0.4 * ctx.f("g_lqin.alpha_lqin_senti.weight")
```

```bash
run nodes/field_base_px_adj/         --sd 2010-01-01   # 数据与 alpha 同一条命令
run nodes/factor_yliu_beta_decomp/   --sd 2015-01-01
run nodes/alpha_yliu_rev_senti_mix/  --sd 2018-01-01
```

### 4.9 config 通用规则（沿用）

1. **alpha config 可覆盖 region 字段**（booksize、sim.*），覆盖显式且与 region 内容一并 hash 进权重 meta（`region_hash`）——自由研究、统一提交（§二）。
2. **deps 必须显式，通配 `{repo}.{node_dir}.{node_name}-*` 是简写而非豁免**：编译期展开进 meta，引擎按实际调用惰性加载。
3. **成本模型 = 有版本的 L3 field**，换模型 = 换 field 名，pnl 一行不改。
4. **多参数变体手写展开**，每个变体独立节点独立评估；Jinja 暂缓（原则"渲染前置"）。
5. **time_cutoff 模板**：`*_tc` 统一替换；前视静态检查一行：`time_cutoff ≤ return_metric 执行起点`。
   **`deps` 里的 `_tc` 按消费节点自身的有效 cutoff 解析**（节点级 > 文件级 > region 的 `time_cutoff`），而不是按生产者的。这条必须写死：ingestion 产出的 `field_base_px` 在**节点级**写了 `cutoff: "1500"`、产出 `adj_close_1500`，而 §4.5 的 `beta_decomp` 依赖 `g_common.field_base_px.adj_close_tc` 却自身没有 `cutoff`——不定规则的话，消费者会**静默绑到另一个 cutoff 的数据上**，而这正是本节第 2 道闸门要防的那类错误、却发生在闸门的上游。解析后**断言展开出的名字确实存在于 store**，不存在则报错并列出该 ns 下可用的 cutoff。
6. **return_metric 对齐约定**：第 t 行 = 昨执行价 → 今执行价收益，`pnl_t = Σ value_{t-1} · ret_t`。

---

### 4.10 一条完整的研究链（三例连读）

§4.5–§4.8 是**契约的最小演示**，每例只讲一件事。本节把它们串成一条真实的链——同一个研究想法从因子到 alpha 到 combo——好让命名、依赖与落库位置在同一个上下文里看清楚。

故事：**用波动归一的短期反转，再与他人的情绪 alpha 混合**。

#### 例 5：多个 L3 → 多个 L3 因子

```yaml
# g_yliu/nodes/factor_yliu_liq/liq.yaml
region: us
lookback: 20

nodes:
  factor_yliu_liq:
    deps:
      - g_common.field_base_px.adj_close_tc
      - g_common.field_base_px.volume_tc
      - g_common.field_base_px.ret_1d_tc
    params:
      window: 20
    outputs:
      adv20:   {dtype: f4}      # 20 日平均成交额 (美元)
      illiq20: {dtype: f4}      # Amihud 非流动性
      rvol20:  {dtype: f4}      # 已实现波动 (年化)
```

```python
# g_yliu/nodes/factor_yliu_liq/liq.py
import numpy as np

def handle(ctx):
    w   = ctx.params["window"]
    px  = ctx.win("g_common.field_base_px.adj_close_tc", w)      # (w, N)
    vol = ctx.win("g_common.field_base_px.volume_tc",    w)
    ret = ctx.win("g_common.field_base_px.ret_1d_tc",    w)

    dollar = px * vol                                 # (w, N) 逐日成交额
    return ctx.multi_outputs(
        adv20   = dollar.mean(),                      # (N,) 列向聚合 -> 当日截面
        illiq20 = (ret.abs() / dollar).mean() * 1e6,
        rvol20  = ret.std() * np.sqrt(252),
    )
```

**落库位置**——叶子里**节点名与输出名都在**：

```
storage/l3/us/g_yliu/factor_yliu_liq/adv20/    -illiq20/    -rvol20/
```

这是 §3.2 的规则：叶子 = `{node_name}-{output}`。节点是"一次计算"的单位（一次窗口读取算出三个产物，避免重复读盘），输出是"一份数据"的单位——两者都要能读出来，所以两者都在名字里。这也让唯一性由路径结构保证：别的节点即便也产出叫 `adv20` 的东西，落的是自己的 `{node_name}-adv20`，不会撞车。

**窗口聚合的形状**：`ctx.win` 给的是 `(w, N)`，pandas 的 `.mean()` / `.std()` 默认沿行聚合，得到 `(N,)` 的当日截面——正好是 §4.2 要求秩-2 节点交付的形状。不需要写 `axis=`，但**写错轴不会报错、只会算出一个形状恰好也是 N 的错误值**（当 w == N 时连形状都对），所以这类节点值得配一个断言测试。

#### 例 6：多个 L3 → 一个 alpha

```yaml
# g_yliu/nodes/alpha_yliu_rev/rev.yaml      ← 一个 yaml 装整族; node_dir = rev
region: us                          # kind 缺省 alpha, ns 由 repo 目录推导 (§4.11.6)
universe: g_common.field_common_univ.us_top3000     # 全 ref, 不是裸名 (§4.11.6)
lookback: 30

nodes:
  alpha_yliu_rev_w005:              # 标签形式: 一族有 2 个成员即强制 (§4.11.4)
    code: rev.py                    # 全族共用一份代码
    params:               # 与名字里的 w005 编译期校验一致
      window: 5
    deps:
      - g_common.field_base_px.adj_close_tc
      - g_yliu.factor_yliu_liq.rvol20          # 吃例 5 的产出
      - g_common.factor_common_gics.sector        # 供 neutralize 用, 见下
    ops:
      - rank
      - neutralize: g_common.factor_common_gics.sector
      - linear_decay: 3
      - truncate: 0.02
      - scale: book

```

```yaml
  alpha_yliu_rev_w020:              # 变体手写展开 (§4.9.4), 同一个 yaml
    code: rev.py                    # 同一份代码
    params:
      window: 20
    deps: [g_common.field_base_px.adj_close_tc, g_yliu.factor_yliu_liq.rvol20, g_common.factor_common_gics.sector]
    ops:
      - rank
      - neutralize: g_common.factor_common_gics.sector
      - linear_decay: 3
      - truncate: 0.02
      - scale: book
```

```python
# g_yliu/nodes/alpha_yliu_rev/rev.py  —— 两个变体共用, 差异全在 params
def handle(ctx):
    n   = ctx.params["window"]
    px  = ctx.win("g_common.field_base_px.adj_close_tc", n + 1)
    raw = -(px.loc[0] / px.loc[-n] - 1)               # 反转: 跌得多的买
    return raw / ctx.f("g_yliu.factor_yliu_liq.rvol20")          # 波动归一, 单输出直接 return
```

**`ops` 用到的分组 field 也必须写进 `deps`。** `neutralize: g_common.factor_common_gics.sector` 由引擎在 ops 链里解析，handle 里根本没提它——但它是**编译期就要能解析、运行期要能加载**的依赖，漏写则 §7.1 的"deps 不存在则报错"兜底会在运行时才炸，而且报错点在算子链里、离 yaml 很远。规则：**凡是这个节点跑起来需要读到的 L3，无论谁去读它，都要出现在 `deps` 里**。

落库：`…/g_yliu/alpha_yliu_rev/alpha_yliu_rev_w005-weight/` 与 `…-rev_w020-weight/`——**单输出 alpha 的输出名缺省为 `weight`**（§3.2）。两个变体是**两个独立节点、独立评估**——这正是 §4.9.4 "多参数变体手写展开"的形态，代价是 yaml 里有重复，换来的是每个变体在 store / catalog / alpha 池里都是一等公民，可以被单独引用、单独去重、单独晋升。

#### 例 7：多个 alpha → 一个 combo

```yaml
# g_yliu/nodes/alpha_yliu_rev/rev_mix.yaml   ← 同一个 node_dir 下的另一个 yaml
region: us
universe: g_common.field_common_univ.us_top3000

nodes:
  alpha_yliu_rev_mix:               # code: 省略 -> 同名的 rev_mix.py
    deps:
      - g_yliu.alpha_yliu_rev.alpha_yliu_rev_w005-weight
      - g_yliu.alpha_yliu_rev.alpha_yliu_rev_w020-weight
      - g_lqin.alpha_lqin_senti.weight            # 吃别人的 alpha
    ops:
      - truncate: 0.02
      - scale: book
```

```python
# g_yliu/nodes/alpha_yliu_rev/rev_mix.py
def handle(ctx):
    return (0.4 * ctx.f("g_yliu.alpha_yliu_rev.alpha_yliu_rev_w005-weight")
          + 0.3 * ctx.f("g_yliu.alpha_yliu_rev.alpha_yliu_rev_w020-weight")
          + 0.3 * ctx.f("g_lqin.alpha_lqin_senti.weight"))
```

三个要点：

1. **combo 不是一种特殊节点**（§附录A：combo 概念取消）。它只是 `deps` 里含 `alpha.*` 的普通节点，走同一条 `run`、同一个内核、同样的 ops 链。要判断某个节点是不是合成层，看它的 deps 有没有 `alpha.*` 即可，不需要命名前缀。

2. **吃别人的 alpha 不需要 clone 对方的 repo**（§二）。`deps` 解析的是 store 里的**数据**，不是代码；想看 `g_lqin.alpha_lqin_senti.weight` 是怎么算的，走 catalog 里的 `code_ref`（repo + commit + path）。

3. **`scale: book` 在 combo 里不是可有可无的收尾。** 三个上游各自满足 `Σ|w| = 1`，混合权重 `0.4 + 0.3 + 0.3` 也正好是 1.0——直觉上组合后应该还是 1。但实测：合成用例上组合后 `Σ|w|` 只有 **0.484**，而在本仓库真实跑出来的三个 alpha 上是 **0.5088**——两者都远小于 1。原因是：不同 alpha 在同一只票上方向相反时会互相抵消，抵消掉的部分不会凭空回到别的票上。少了这一步，账本**只投出去约 51%**，而 Sharpe 看着还挺正常（收益和风险同比例缩水），只有 `daily.long_value + |short_value|` 会露馅。§4.4 把 `scale` 从"自动补"改成"编译期校验必须以 scale 收尾"，防的就是这个。

#### 整条链跑起来

```bash
run nodes/factor_yliu_liq/        --sd 2015-01-01        # 因子: 3 个产物
run 'nodes/alpha_yliu_rev_w*'     --sd 2018-01-01        # alpha: 整族, 一个进程
run nodes/alpha_yliu_rev_mix/     --sd 2018-01-01        # combo
pnl --node g_yliu.alpha_yliu_rev.alpha_yliu_rev_mix-weight  # 评估
```

跨 config 的依赖必须**已经在 store 里**（§7.1：v0 不做图分析，引擎唯一兜底是"deps 不存在则报错"），所以顺序不能颠倒。`store status g_yliu.factor_yliu_liq.rvol20` 可查。

产出的 L3 结构：

```
storage/l3/us/
  g_yliu/factor_yliu_liq/  factor_yliu_liq-adv20/  -illiq20/  -rvol20/     ← 例 5
  g_yliu/alpha_yliu_rev/  alpha_yliu_rev_w005-weight/  alpha_yliu_rev_w020-weight/   ← 例 6
               alpha_yliu_rev_mix-weight/                       ← 例 7
  g_lqin/alpha_lqin_senti/ weight/                        ← 他人产出, 只读
weights/
  g_yliu.alpha_yliu_rev.alpha_yliu_rev_mix-weight.feather + meta.json      ← §7.4 dump, pnl 的正式接口
```

**引用名的四段在这里各司其职**：`{repo}` 说明谁负责、`{node_dir}` 说明属于哪一组工作、`{node_name}` 说明是哪一次计算（`{kind}_{ns}_` 前缀让 kind 与 ns 无需另行声明）、`{output}` 说明是哪一份数据。四段与路径一一对应、纯字符串可互推，不需要任何索引——完整规则见 §3.2 与 §4.11。

---

### 4.11 命名约定

§3.2 定了形式，本节定**取名的规则**与编译期检查。

#### 4.11.1 语法

```
node_name ::= {kind}_{ns}_{name}      kind ∈ field|factor|alpha
                                      解析即 `kind, ns, name = s.split("_", 2)`
ns        ::= ^[a-z][a-z0-9]*$        **单段, 不含下划线** —— 否则 name 含下划线时切分有歧义
name      ::= ^[a-z][a-z0-9]*(_[a-z0-9]+)*$     ≤ 40
output    ::= 同 name 的语法
node_dir  ::= 同 name 的语法
ref       ::= {repo}.{node_dir}.{node_name}-{output}
```

四条硬约束，**每条都来自已有机制，不是品味**：

1. **`name` 与 `output` 必须是合法 Python 标识符，且非关键字。** §4.3 的 `ctx.multi_outputs(**kw)` 把输出名当**关键字参数**传递——`ctx.multi_outputs(5dr_250d=...)` 是 `SyntaxError: invalid decimal literal`，**语法错误意味着模块根本加载不了**，§4.3 精心设计的"在写错那一行抛错、typo 带修复建议"完全执行不到。这条也顺带禁掉 `return`。
2. **不含点号。** 点号是引用名的分段符（`{repo}.{node_dir}.…`），叶子里再有点号，解析与 dump 文件名都会歧义。
3. **连字符只出现在 `{node_name}-{output}` 这一处接缝上**，`name` / `output` / `node_dir` 内部一律用下划线。这样 `split("-", 1)` 就能无歧义地把叶子拆回节点与输出。
4. **不含大写。** 大小写不敏感的文件系统（macOS APFS 默认）上，`MktBeta` 与 `mktbeta` 在一台机器上是同一个目录、在另一台上是两个。

#### 4.11.2 缺省与坍缩

一次研究涉及的名字，绝大多数应当由**一个**决定推导出来：

| | 规则 |
|---|---|
| `code:` | 省略 → 同目录下与 yaml 同名的 `.py` |
| 单输出的 `output` | 数据节点 = 节点名去掉 `{kind}_{ns}_` 前缀；**单输出 alpha = `weight`** |
| `kind` / `ns` | 从节点名解析，不声明 |
| L3 路径 / dump / pnl_out | 全部由 ref 推导（§3.2、§7.4） |

单输出 alpha 取 `weight` 是因为它产出的就是权重，没有别的可叫；这也让 `…-weight` 成为"这是个可评估的 alpha"的可 grep 标志。

#### 4.11.3 节点名 vs 输出名

节点是**一次计算**（一次窗口读取算出多个产物，避免重复读盘），输出是**一份数据**。二者不同名是常态且是好事：`factor_yliu_beta_decomp` 产出 `mkt_beta_w250` 与 `resid_mom_w250`，两个名字说的都是"这份数据是什么"而非"谁算的"。

**唯一性由路径结构保证，不靠约定。** 叶子是 `{node_name}-{output}`，节点名又含 `{kind}_{ns}_`，所以两个不同节点即便产出同名输出也不会撞车。这是新路径形式相对旧的 `{kind}/{ns}/{name}` 的实质改进——后者需要一条"输出名在 ns 内全局唯一"的编译期检查来兜底，现在那条检查不再必要。

#### 4.11.4 参数变体：标签形式

§4.9.4 要求变体手写展开。命名规则：

- **参数标签放最后**，形如 `_{tag}{value}`，在 `_tc` 之前。
- **同一族内数值定宽补零**（session 数用 3 位）。`w060 / w120 / w250` 排序正确；`w60 / w120 / w250` 在任何文件列表里都排成 `w120, w250, w60`。
- **标签字典短而封闭**：`w` 窗口 · `h` 半衰期 · `k` 显式滞后 · `q` 分位(%) · `n` 计数 · `_m5`/`_m30` 直接用注册的网格名。
- **`params:` 是真相，名字是标签，编译期校验二者一致。** 凡 `params` 里出现字典内的键（`window`→`w`），名字里必须有对应标签且值相同。这条抓的是"复制了一个变体却只改了 `params` 忘了改名"。
- **标签只增不改。** 加第二个被扫描的参数时给全族每个成员追加标签，绝不重新解释既有名字——store 是 append-only（§一原则3）。

一个从未被扫描、且数字是行业惯用语的名字可以粘连（`adv20` / `illiq20` / `rvol20`），但**一旦这一族出现第 2 个成员，标签形式即成强制**。注意不对称：粘连名若已被 dump 或被 deps 过就**不能**改名，这一族会永远长得不一致——所以拿不准时从第一天就带标签。

#### 4.11.5 版本：改名还是 bump `version`

判据一句话：**已经按这个名字锚定的消费者，会想要静默拿到新值吗？**

- **会 → 同名，`meta.version` bump**：修复了让序列不符合其既定定义的 bug、上游数据修订、回填。
- **不会 → 新名字，旧数组原封不动**：定义变了、缩尾方式变了、换了源、换了 cutoff、换了网格、换了参数。

**名字是契约（它意味着什么），`version` 是构建（哪一次运行产出了这些字节）。** 三条推论：参数变化永远不是 `_v2`（那是标签的事）· `_vN` 单调递增、永不复用、**永不把原名回改成 `_v1`**（改名会打断所有 deps 与历史 meta）· **fork 换 `{ns}` 就够了**——copy 别人的节点到自己名下，节点名里的 ns 段已经区分开了，不该再加 `_v2`（那会暗示这是第二次迭代）。

> **一个命名解决不了的洞**：`store.write` 会 bump `version` 但**原地重写字节**，所以 §一原则3 的"修数发新版本、无覆盖"目前只对轴成立、对节点数据不成立。补法在产物侧：**§7.4 的权重 meta 必须记录 `deps_versions`**（每个已解析依赖的 `{ref: version}` 与其 `code_ref.commit`），否则半年前的一份权重文件说不出它当时看到的是哪一个版本的上游。

#### 4.11.6 保留字与编译期检查

**保留**：`all`（缺省 universe）· 下划线开头（`_axes` / `_catalog`）· **store 中不得有以 `_tc` 结尾的名字**（`_tc` 只是源码形态的模板标记，§4.9.5）· Python 关键字 · 全部 schema 键（`nodes` `outputs` `deps` `code` `params` `ops` `region` `universe` `lookback` `dims` `grid` `dtype` `sim`）。

**`ns` 段必须等于所在 repo 的 owner**（`g_yliu` → `yliu`，g_common 拥有 `base` / `common` / 各 dataset ns）。这把 §二 的写权限模型表达成了一条名字检查：个人 repo 里写不出别人 ns 的节点名。

编译期检查：① 语法 / 长度 / 标识符 / 关键字 / 保留字 · ② 节点名的 `{ns}` 段对本 repo 可写 · ③ 单输出 key == 缺省名（数据节点去前缀 / alpha 为 `weight`）· ④ 标签与 `params` 一致、同族定宽 · ⑤ `_tc` 按**消费节点**的有效 cutoff 解析（§4.9.5），解析后断言该名字在 store 中存在，报错时列出可用的 cutoff · ⑥ 通配展开非空并冻进 meta · ⑦ 算子参数按签名类型校验（`truncate` float / `linear_decay` 正 int / `neutralize` 一个 int field 的全 ref）· ⑧ alpha 节点 `dims == [di, ii]` 且 ops 以 `scale` 收尾 · ⑨ 往返断言 `ref` 拆解后能拼回原路径。

## 五、L2/L1 接入与日更 `[TARGET]`

**不在 v0 范围内。** v0 引擎只吃 L3、只吐 L3（§七）；把 L2 变成 L3 是
`pipeline/build_l3_base.py` 一次性的事，契约见 `l2_schema.md`。
把摄入也统一进 Node 模型（`source:` 字段、日更编排、registry）的设计见 `roadmap.md`。

---

## 六、ctx 与 ops

### 6.1 ctx：按秩返回（数据与 alpha 共用同一个）

| API | 返回 | 语义 |
|---|---|---|
| `ctx.win(name, w)` | 见下表 | 首轴标签 `-(w-1)…0`：**0 = 当前处理日，-1 = 前一天**；历史不足 pad NaN、首轴长度恒为 w；**声明了 universe 的节点**当日池外整列 NaN（判据是有没有池子，不是 kind——执行期无 kind 分支）；`w` 无上限 |
| `ctx.f(name)` | 见下表 | `win(name,1)` 取当日那一片的语法糖 |
| `ctx.cs.rank/zscore/demean(x, by=…)` | Series | 截面工具，nan-aware，作用在 `ii` 轴 |
| `ctx.state` | dict（可 pickle） | 跨日状态；数据节点尽量无状态（保"任意区间可重算"） |
| `ctx.t` / `ctx.today()` / `ctx.params` / `ctx.cols` | — | 只读游标、日期、参数、全局列轴 |
| `ctx.grid(name)` | Index | 秩-3 依赖的 `ti` 槽位标签 |

**返回形状随被访问节点的秩而变**（不随当前节点的秩）：

| 依赖的 `dims` | `ctx.f(name)` | `ctx.win(name, w)` |
|---|---|---|
| `[di]` | 标量 | `Series(w)` |
| `[di, ii]` | `Series(N)` | `DataFrame(w × N)` |
| `[di, ii, ti]` | `DataFrame(N × T)` | **`ndarray(w, N, T)`** |

秩-3 的窗口**是 ndarray 不是 DataFrame**——pandas 没有三维结构，而 §3.3 已经说明 Zarr 解压后本就是普通 ndarray，多包一层只会增加拷贝。轴序固定为 `(di, ii, ti)`；`ctx.cols` 给出 `ii` 轴的标签（= `security_id` 列表，长度 `n_active`）。ti 轴的标签在 v0 里不由 ctx 提供。

**`ctx.l2` / `ctx.l1` 不在 v0 的 ctx 里**——v0 引擎只吃 L3、只吐 L3（§七 范围声明）。目标架构中它们的语义见 §五。

无日期参数、无绝对索引、无 store 写句柄——API 面积越小，防前视的证明义务越小。实现细节（预对齐、按日缓存、永远返回副本、init 期报错、op-state 归 OpChain）见 §十。

### 6.2 ops：alpha 的出口算子链

| op | 类型 | 作用轴 | 适用秩 | 语义 |
|---|---|---|---|---|
| op | 类型 | 作用轴 | 适用秩 | 参数 | 预热贡献 |
|---|---|---|---|---|---|
| `rank` | CS | `ii` | 仅 2 | 无 | 0 |
| `neutralize: <ref>` | CS | `ii` | 仅 2 | int field 的**全 ref** | 0 |
| `truncate: x` | CS | `ii` | 仅 2 | float | 0 |
| `scale: book` | CS | `ii` | 仅 2 | `book`，可省（省略即 `book`） | 0 |
| `linear_decay: n` | TS | `di` | 1/2/3 | 正 int | `n - 1` |
| `exp_decay: h` | TS | `di` | 1/2/3 | 正 int（半衰期） | `4h` |
| `delay: k` | TS | `di` | 1/2/3 | 正 int | `k` |

**这七行是算子的完整清单**——多一个少一个都是错。预热贡献沿链**相加**（§7.1）。

#### 逐个的精确语义

设当日截面为 `v`（Series，index = `ctx.cols`），`m` = `v` 中非 NaN 的个数。

**`rank`** — `r = v.rank(method="average")`（skipna；NaN 不参与排名、不占名次，输出位仍是 NaN）

```
m ≤ 1:  非 NaN 位 → 0.0            # 独苗没有截面, 给 0 而非 ±0.5, 免得它在 scale 后独吞整本账
m > 1:  out = (r - 1) / (m - 1) - 0.5
```

端点**取到** ±0.5（闭区间）。平均名次法下名次和恒定，故输出截面均值恒为 0——rank 之后天然美元中性。

**`neutralize: <ref>`** — 分组 demean：

```
out = v - v.groupby(g, dropna=False).transform("mean")     # g 为分组字段, 按 index 对齐
```

`dropna=False` 是**要害**：默认的 `dropna=True` 会把分组字段缺失的名字整组丢掉，
它们的组均值变 NaN，于是原本有值的票静默变 NaN——覆盖率掉一块而不报错。
分组缺失的名字自成一组；组内均值 skipna；**单元素组 demean 后恒为 0**（一只票的组不携带截面信息）。

**`truncate: x`** — 单票 |w| 上限：

```
gross = nansum(|v|)                 # 截断**前**的 gross
gross ≤ 0 或非有限:  原样返回        # 无仓可截; cap=0 会把全票夹成 0
否则:  out = clip(v, -x*gross, +x*gross)     # 保持 NaN
```

**单次夹紧，不迭代到不动点。** 迭代在 `x < 1/有效票数` 时没有非退化解，会把整本账
迭代成 0。代价是紧随其后的 `scale` 按缩水后的 gross 归一，把被夹的票顶回 x 之上
一点点（比例 `gross_前 / gross_后`）；被夹的是少数票时可忽略。

**`scale: book`** — Σ|w| = 1，并落下 §3.5 的第二道闸门：

```
v = v.where(mask)                   # ① 池外先出局
g = nansum(|v|)                     # ② 再算 gross
g ≤ 0 或非有限:  out = as_weights(v)          # 不做除法
否则:            out = as_weights(v / g)

as_weights(w) = w.where(mask).fillna(0.0)     # 池外 → 0, NaN → 0
```

**顺序要紧**：反过来（先归一、再抹池外）两条承诺不能同时成立——TS 算子会把昨天的值
搬到今天，那只票今天若已出池，它的权重先进了分母再被抹掉，Σ|w| 就悄悄小于 1。
`g = 0`（全抵消 / 全 NaN）时**不做除法**：0/0 会把整本账变成 NaN 或 inf。
收口强制 **0 而不是 NaN**：NaN 的权重会在 pnl 的 `pos * (1 + NaN)` 里摧毁持仓并向后传染。
`scale` **不改变净敞口**——美元中性来自 `rank` / `neutralize`，不来自这里。

**`linear_decay: n`** — 按 age 加权，权重 `[n, n-1, …, 1]`（今日 `n`，`n-1` 天前为 `1`）：

```
out = Σ_j w_j · x_{t-j}  /  Σ_{j: x_{t-j} 非 NaN} w_j        w_j = n - j,  j = 0..n-1
分母全为 0（整条缓冲皆 NaN）→ NaN
```

分母是**有效权重之和**而非常数 `n(n+1)/2`：缓冲里的 NaN 只是"该票该日按权重 0 参与"，
不传染整条缓冲。预热期走同一条路（少一天历史 = 少一个观测），所以无需特判。

**`exp_decay: h`** — `h` 是**半衰期**，lag `j` 的权重 `0.5^(j/h)`，递推实现：

```
a = 0.5 ** (1/h)
num ← a·num ;  den ← a·den
x 非 NaN 处:  num += (1-a)·x ;  den += (1-a)
out = num / den   （den > 0 处），否则 NaN
```

递推是同一个加权平均的**精确**形式（窗口无限长），只占 O(N)；定长缓冲要约 10 个
半衰期才能把权重截到可忽略（h=250 时是 2500 天 × N，百 MB 级）。两个累加器同步衰减，
故与整体缩放无关。NaN 那天两个累加器都不加 = 权重 0。
预热取 **4 个半衰期**（覆盖 93.75% 的稳态权重）——递推第一天就是合法加权平均，
这里要的不是"算得出"而是"与更长的历史算得一样"。

**`delay: k`** — 显式滞后 k 个 session；**前 k 次调用无历史，输出 NaN**。
执行滞后由撮合边界全局施加一次，这里只服务**故意**做的滞后版本；惯性再加一次会白白
多丢一天信息且不报错。

**算子参数在编译期按签名做类型校验。** `truncate` 收 float、`linear_decay` 收 正 int、`neutralize` 收一个 int field 的全 ref、`scale` 收枚举值。理由是 YAML 会**静默**把类型写错的值收下：`- truncate: 0.02,`（多一个逗号）解析出来是**字符串 `"0.02,"`** 而非数字 `0.02`，不报任何错，然后在算子里变成一次隐晦的比较失败或一个恒不触发的截断。这类错误不检查就只能靠回测结果异常时倒查。

**CS 类只对秩-2 合法**（§3.6）：秩-1 没有 `ii` 轴可截面；秩-3 有 `ii` 也有 `ti`，"在哪根轴上排序"无唯一答案——与其猜一个默认值，不如编译期报错，要日内截面就先在 handle 里显式压成秩-2。TS 类沿 `di` 作用，对三种秩都是同一份实现（op-state 的缓冲形状随秩而变，语义不变）。

顺序即语义。`ctx.cs.*` 与 ops 的 CS 算子**共用同一份实现**（`ctx.cs.rank` 即 `cs_rank`，`ctx.cs.demean(x, by=)` 即 `cs_neutralize`），所以在 handle 里做和在链里做给出同一个数。**delay 双重身份**：执行 delay 全局一次（所有节点以数据 ≤ T cutoff 算 T 日权重，combo 读上游为同日权重无隐式滞后）；ops 的 `delay: k` 仅用于故意的滞后版本。**推荐风格**：塑形 ops（rank/neutralize）放叶子，换手类（decay/truncate）上提到靠近 output。

---


### 6.3 指纹（写入闸门的判据）

指纹守的是「**定义变了而名字没变**」：改一行公式再跑日更，同一个数组里改动日之前是
定义 A、之后是定义 B，且无从察觉。`store.write` 在写之前比对，不符即拒绝（`--rebuild`
显式绕过并 bump version）。因此**指纹的组成必须逐字实现**——组成不同的两个实现无法
写入对方的库。

```python
h = sha256()
h.update(src.encode())                                  # ① 节点 yaml 子树的规范化文本
h.update(f"|universe={universe}|lookback={lookback}".encode())   # ② spec 级口径
h.update(code.read_bytes() if code.exists() else b"")   # ③ handle 源码的原始字节
for d in sorted(deps):                                  # ④ 解析后的依赖, 排序
    h.update(d.encode())
fingerprint = "sha256:" + h.hexdigest()[:16]            # 取前 16 个十六进制字符
```

- **① `src`** = `yaml.safe_dump(解析后的节点子树, sort_keys=True, allow_unicode=True)`。
  是**解析后**的对象再 dump，不是原始文本——所以缩进、行序、`{a: 1}` 与块式写法都不影响指纹。
  `params` 的四种写法（见 §4.2）已在此之前归一，故它们 hash 相同。
  例：`factor_yliu_mom` 的 `src` 是
  `'deps:\n- g_common.field_base_px.adj_close_1500\nparams:\n  window: 60\n'`。
- **② `universe` / `lookback`** 写在 yaml **顶层**而不在节点子树里，故不在 ① 中，必须单列。
  两者都逐值改变输出（池外整列 NaN；预热决定 TS 算子初值），不进指纹就能靠改一行
  yaml 头绕过闸门。
- **③ 代码是原始字节**，所以 CRLF 与 LF 会给同一份逻辑算出两个指纹——仓库须以
  `.gitattributes` 钉死 `eol=lf`。
- **④ 依赖是 yaml 里写的那一份**，只做过 `_tc` 替换，**通配保持字面**（`…-*` / `….*`
  原样喂入），排序后逐个 `update`。通配的展开发生在别处（执行期 `resolve_deps`，
  结果进 `meta["deps"]`），**不进指纹**——所以上游多出一个输出不会改变本节点的指纹。

`region_hash` 是另一回事：它只覆盖 region 文件里的**口径**键，不含 `l3_root` / `pnl_out`
这类本机路径键（§4.1.1）。

## 七、执行引擎（v0：无 cache、顺序执行）

**范围声明**：v0 砍掉 cache/指纹/checkpoint/依赖解析，目标是把统一 Node 契约端到端跑通。config 里 `nodes` 按**声明顺序挨个全量跑**。

**v0 引擎只处理 L3 → L3**：输入全部来自 `deps`（store 里已有的 L3 节点），输出全部落 L3。`source:`（L2/L1 直读）与 `ctx.l2` / `ctx.l1` **不在 v0 引擎内**——L2 → L3 的入库由 ingestion 管道承担（§五），它有自己的 schema 强制、缺文件告警与逐列容错需求，与引擎的"逐日 handle + ops 链"是两类问题。混在一起会让引擎同时背上文件格式、路径模板、vendor 容错三副担子，而这三样都与 alpha 研究无关。

这条范围划分带来两个直接后果：v0 的节点 schema 里 `source` 恒为空，`deps` 是唯一输入来源；以及**基础 field 的生产不属于引擎**——它是 ingestion 的产物，引擎见到它时已经在 store 里了。

### 7.1 执行语义

- **声明顺序 = 执行顺序**：被依赖的节点写在前面（先跑完落 store，后者读到）。跨 config 依赖须已在 store（`store status <名>` 可查），引擎唯一兜底：deps 不存在则报错，不做图分析。
- `--only` 只跑指定节点。**`run` 缺省会评估本次跑过的每个 alpha**（`--no-pnl` 关掉）：算完紧接着看指标是研究期最短的回路。因子与数据节点不评（没有权重可仿真）；`--probe` 也不评——那一趟本来就不落库。
- **lookback 预热**：`warmup = lookback(声明值) + ops 链的预热贡献`（§6.2 那张表, 沿链**相加**）。
  预热段照常执行 handle 推进 state，只喂状态不进输出。

  > **是相加, 不是取大。** 链吃的是 handle 的产出: 要让链在第一个**请求日**就已填满缓冲,
  > handle 必须在那之前 `ops_lookback` 天就在产出有效值——而 handle 本身要 `lookback` 天
  > 才有效。两段不是同一段时间。这里曾经写的是 `max()`: `lookback: 5` + `ctx.win(px, 6)`
  > + `linear_decay: 3` 的真实需求是 5+2=7, max 给 5, 于是起跑后前 2 天的输出来自未填满的
  > 衰减缓冲——实测同一个 session 与预热充足时相比 **501/503 只票全不一样, 最大差 0.11**,
  > 且不报任何警。取大之所以诱人, 是两段各自都"够"。
  >
  > 由此可导出一条可测的不变量：**一个 session 的值不该取决于从哪天起跑**。

### 7.2 主循环

```python
def run(spec, only, sd, ed, flags):
    ed = calendar.effective_ed(ed, all_deps(spec) | ({spec.return_metric} if spec.return_metric else set()))
    todo = spec.nodes if only is None else [spec.nodes[only]]
    for node in todo:                                   # 声明顺序
        run_node(node, spec, sd, ed, flags)
    dump(...); flags.pnl and invoke_pnl(...)

def run_node(node, spec, sd, ed, flags):
    # 惰性面板: 持有 loader, 首次 ctx.f/win 才 store.read + 预对齐 (§十)
    panels = {d: PanelLoader(d, sd - node.lookback, ed) for d in node.deps}
    universe = PanelLoader(node.universe, sd - node.lookback, ed)
    ctx = Ctx(panels, universe, resolver(spec), node)    # v0: 无 l2 源
    mod = load_module(node.code); mod.init(ctx)
    # OpChain 需要池子: scale 后的池外归零与 CS 算子的 scope 都靠它 (§3.5)
    chains = {k: OpChain(o.ops, universe) for k, o in node.outputs.items()}
    rows, last = defaultdict(dict), None
    for t in range(calendar.pos(sd) - node.lookback, calendar.pos(ed) + 1):
        ctx._advance(t)
        out = mod.handle(ctx)
        out = last if out is None else out; last = out
        for name, v in normalize(out, node).items():     # 裸值 → 单键; 多输出由构造器保证
            v = chains[name](mask(v, ctx.universe), t)   # 无 kind 分支
            rows[name][t] = v
            panels[str(node.ref(name))].publish(t, v)   # ← 当日产出回灌内存面板
    for name, r in rows.items():
        ref = str(node.ref(name))
        if flags.rebuild:
            store.write(ref, assemble(r).loc[sd:ed])     # 全量重建, bump version
        else:
            store.upsert(ref, assemble(r).loc[sd:ed])    # 区间 upsert, 不 bump
```

`mask` 在 universe 缺省 `all` 时是恒等；`OpChain([])` 同理。多输出 keys 由 `ctx.multi_outputs` 保证齐全，引擎只需守跨日恒定这一条不变量。各输出独立落库、last_session 同步推进、meta 记 `sibling_outputs`。

**四处不是风格问题，写错了会静默出错**：

1. **当日产出必须回灌内存面板**（`panels.publish`）。ingestion 产出的 `field_base_px` 读自己昨天的输出（`ctx.win("g_common.field_base_px.adj_close_tc", 2).loc[-1]`）是合法且常见的写法，但面板是在循环**之前**一次性载入的，落库又在循环**之后**——不回灌的话，全量回填时 store 里根本还没有数据，`prev` 每天都是 NaN，`ret_1d_tc` **整段历史全 NaN 且不报错**。增量场景更阴：头几天读到的是上次运行的旧值，"看起来正常"，只有中间某段是错的。
   自引用节点**必须把自己列进 `deps`**——否则 `panels` 里没有这个键，取值时 KeyError。

2. **增量走 `upsert`，只有 `--rebuild` 才走 `write`。** §3.3 定义 `store.write` 为"全量重建、bump version"。日更是 `run --ed today`，`assemble(r).loc[sd:ed]` 只有**一行**——照 `write` 的字面语义执行就是用一行覆盖整个数组，**历史全毁**，同时 `version` 退化成天数计数器。研究员为了跑得快写 `--sd 2024-01-01` 是同一个地雷的另一种触发方式（§15.7 用 `--probe` 从构造上堵掉它）。

3. **`OpChain` 必须拿到池子。** §3.5 要求掩码**两端夹住**——ops 前池外置 NaN、`scale` 后池外强制 0——且 CS 类算子的默认 scope 就是 universe。只传 `ops` 的话第二道闸门无处落地：池外票会带着非零权重进 dump 与 pnl，`rank` / `neutralize` 的 scope 也悄悄退化成全集。两者都只改变口径、不报错。

4. **面板惰性加载，且按区间读。** template 默认给 `g_common.field_base_px.*`（§3.2），编译期展开成该 ns 下全部 field。eager 全量读入意味着 20 个秩-2 面板约 2 GB 常驻，而 handle 可能只碰 2 个；一旦该 ns 里出现一个秩-3 节点（m5 满仓 7.5 GB，而 §4.7 恰恰鼓励秩-3 与秩-2 同 ns 混放），就是直接 OOM。§3.3 实测区间读 6.6 ms vs 全史读 309 ms，所以 `PanelLoader` 一并带上 `[sd - lookback, ed]` 区间。§十 的"构造 Ctx 前全部预对齐"相应改为"每个面板首次触碰时对齐一次"。

**升级路径**：加回 cache 时只把循环起点换成 `watermark+1 / checkpoint`，落库旁加 watermark 推进——主循环结构与其余模块零改动。

### 7.3 sd / ed

**评估窗口，非计算窗口**。`ed = today` 的可用性由数据新鲜度决定：任一依赖最新 session 未落地则自动回退并提示 `effective_ed=...`，绝不静默算半截数据。

### 7.4 权重的对外接口

权重的正式对外接口就是 **`pnl_out/{ref}/` 的四交付物**（§8.3），不是另一套 dump：
`daily.psv` / `pnl.psv` / `holding.psv` / `metrics.json`。`metrics.json` 的 `snapshot`
块携带口径（`region_hash` / `return_metric` / `booksize` / `sd` / `ed` / `cost_bps` /
`participation`），落库节点的血缘（`deps_versions` / `code_ref` / `cutoff`）在 L3 的
`zarr.json` 里（见 `l2_schema.md` §14）。

> 早期设计里另有一套 `weights/*.feather + meta.json` 的 dump，**未实现且已取消**：
> 同一份权重两种出口, 迟早两边不一致, 而四交付物已经覆盖了它的全部用途。

---

## 八、评估（pnl.py）

**权重文件是正式接口**：引擎与评估解耦，两侧独立重跑；外来权重（他人给的、手改实验的）同样可评估。**precise 是唯一模式**——pnl 不是"算指标的评估器"而是**仿真器**：维护持仓账本，指标只是账本的汇总视图。

```
pnl.py --weight weights/g_yliu.alpha_yliu_rev.alpha_yliu_rev_w005_h250-weight.feather     # 独立文件入口
pnl.py --node g_yliu.alpha_yliu_rev.alpha_yliu_rev_w005_h250-weight --sd ... --ed ...     # 直接读 store 入口
       [--rm g_common.field_base_px.vwap_return_1500_1530]            # 覆盖收益序列: 执行敏感性
       [--booksize 20e6]                                  # 参与率约束的必备输入
       [--cost-model g_common.field_common_cost.bps_liquidity_v1]
```

### 8.1 账本：单一价值账本

持仓账本唯一，记**逐票美元价值** `pos_value (T,N)`，按复权 ret 推进——拆股/分红/退市对价/复牌累计天然安全，**corporate action 问题在此模型下不存在**。（曾考虑"股数账本 + split_factor + 双账本对账"，已废除：股数唯一的实质用途是产线订单生成，那是下单时刻 `value/px` 一次除法的事，不必让账本全程背着。）回测所需的一切——pnl/turnover/成本/容量/gap/归因——价值维度全覆盖，且参与率约束（ADV 美元额）在价值维度是原生表达。

**输入面板必备**：权重、return_metric（损益权威，复权口径）、`adv_dollar`、cost field、`delist_date`、**`is_halted`**（§九 ghost 检测的正向判据；缺失须显式降级或拒绝运行）。

### 8.2 simulate 内核（逐日，numpy 向量化，5000 天秒级）

```python
r    = ret[t]                                          # 保留原始 NaN: 它是可交易性判据
prev = pos_value.copy()                                # 推进前捕获, pnl 要用昨仓
# --- 推进 ---
pos_value = pos_value * (1 + r.fillna(0))              # 复权推进; 退市对价、复牌累计已在 ret
pnl[t]    = prev * r.fillna(0) - cost[t]
tradable  = r.notna() & ~delisted[t]                   # delisted[t] ≡ date > delist_date
                                                       #   严格大于 -> 退市当日仍可交易
frozen    = ~tradable & (pos_value != 0)               # 有仓却动不了
# --- 冻结重分配 (§九) ---
frozen_value = pos_value[frozen].abs().sum()           # gross 口径, 见下
avail = max(booksize - frozen_value, 0.0)
w = target_w[t]; w[frozen] = 0
gross = w.abs().sum()
w = w / gross if gross > 0 else w                      # 全员冻结时不做无意义的归一
target_value = w * avail
# --- 执行 ---
delta = target_value - pos_value
delta[~tradable] = 0                                   # 停牌: 价值原地推进(= 股数不变)
delta = delta.clip(-cap, cap)                          # cap = participation × adv_dollar[t]
pos_value += delta                                     # 滞留缺口每日重试
pos_value[delist_today] = 0                            # 退市: 对价已由 ret 兑现, 平仓回收
```

**三处必须照此写，否则回测会静默算错**：

1. **停牌日的 NaN 不得进入推进式**。附录 B 规定停牌日 `return_metric = NaN`，而 `NaN` 参与乘法的结果是 NaN——若直接写 `pos_value * (1 + ret[t])`，该票持仓当场变 NaN 并**向后传染整条序列**，`daily.pnl` 当日为 NaN，Sharpe / MaxDD 全线失效。§九 说的"价值冻结（delta=0 自动成立）"要到执行段才生效，那时持仓已被污染。故推进用 `r.fillna(0)`，可交易性判据用**原始** `r`——两者必须分开，不能图省事先填充再判断。

2. **`prev` 必须在推进前捕获**。`pnl[t]` 的定义是"昨仓 × 今日收益"（§4.9.6），推进之后 `pos_value` 已是今仓。

3. **`frozen_value` 取 gross 而非带符号和**。账本是 gross 口径——`scale: book` 保证 `Σ|w| = 1`，故 `Σ|target_value| = avail`。若写 `pos_value[frozen].sum()`，多空组合两侧都有停牌票时带符号和 **≈ 0** → `avail ≈ booksize` → §九 要求的"停牌 = 资金占用"被**静默抹掉**，冻结重分配等于没做。极端情形：多头冻结 5M、空头冻结 5M，账面显示占用 0，而实际 10M 动弹不得。

现金腿退化为小量（仅参与率滞留产生），`cash` 列偏大即容量警报。

### 8.3 四交付物

```
pnl_out/g_yliu.alpha_yliu_rev.alpha_yliu_rev_w005_h250-weight/
  holding.psv       # date × instrument: holding_value + holding_weight(= value/booksize,
                    #   与目标权重同尺度可逐股对比); 股数视图归未来订单生成模块
  pnl.psv           # date × instrument 逐股逐日损益 —— 归因("收益集中在哪些票/哪个月/
                    #   是否三只退市股贡献一半")变成一行 groupby, 无需重仿真
  daily.psv         # 日度汇总, 列清单见下（`|` 分隔, 与 L2 同格式）
  metrics.json      # 标量 + 分年度 + 口径快照(region@ver/rm/booksize/cost/区间/权重hash)
```

**daily 列清单**：

```
long_value / short_value          # Σ pos_value 正/负部
long_count / short_count
trade_dollar                      # Σ|delta|, 当日成交额
holding_pnl                       # pos_value_prev · ret
trading_pnl                       # 执行价与记账价差异 (v0 为 0, 留列)
cost / cash
pnl                               # holding_pnl + trading_pnl - cost
return                            # = pnl / booksize   ← 分母恒定, 见下
alpha_turnover / realloc_turnover # 信号换手 vs 冻结重分配引起的摩擦换手
gap_participation / gap_realloc / gap_reprice     # 目标 vs 实现的三分解
```

**`return` 的分母定为 booksize（恒定）**。候选中 `pnl/gross_value` 在停牌滞留时分母缩水，会造成 return 序列的虚假波动；`pnl/long_value` 是 BRAIN 单边口径。**权威列用 booksize，BRAIN 风格的单边 Return 只在报表层做展示换算**。

### 8.4 指标（全部从 daily 派生）

对齐 BRAIN：Sharpe（基于 `return` 列）、Ann.Return、Turnover = trade_dollar/booksize、Margin(bps) = Σpnl/Σtrade_dollar×1e4、`Fitness = Sharpe×√(|Ret|/max(TO,0.125))`（`Ret` = 年化算术收益，`TO` = **日均**换手；0.125 的下限即按日频标定）、MaxDD、avg long/short value·count、分年度表。

自增：holding_pnl 与 cost 的分解占比（收益里多少被成本吃掉——对高换手 alpha 是关键读数）、long_short_ratio、审计类（`ghost_days / ghost_detection / frozen_value_avg / frozen_reprice_pnl / realloc_turnover_avg / delist_events / cash_avg`）——`ghost_detection` 取 `field` / `proxy(K)` / `disabled`，使这道防线的实际状态在报表上可见（§九）。IC 及分位单调性需 raw signal，待引擎可选落盘后加。

**容量分析（precise 白拿）**：同一份权重扫 booksize ∈ {10M, 50M, 200M} 看 Sharpe 衰减曲线——"这个 alpha 能装多少钱"的直接答案。

`run --pnl` = subprocess 调同一入口，杜绝双路径不一致。

---



### 8.4b 交付物字段规格

四个文件, 全部 `|` 分隔的纯文本（`metrics.json` 除外）。

**`daily.psv`** —— 每个 session 一行, `date` + **29 列**, 顺序固定如下表：

| 列 | 定义 |
|---|---|
| `long_value` / `short_value` | 当日多头市值 / **空头市值（负数, 带符号）** |
| `long_count` / `short_count` | 多空各多少只 |
| `trade_dollar` | `Σ\|delta\|`, 当日成交额（结算流不计入） |
| `holding_pnl` / `trading_pnl` | 持仓损益 / 交易损益（v0 恒 0, 留列） |
| `cost` | 当日成本 |
| `cash` | `booksize − Σ\|pos\|`——**投不出去的那部分**, 偏大即容量警报 |
| `pnl` | `holding_pnl + trading_pnl − cost` |
| `return` | `pnl / booksize`（**分母恒定**, 不是上期净值） |
| `alpha_turnover` / `realloc_turnover` | 见下 |
| `gap_participation` | `Σ\|想交易的量 − 实际成交\|`（仅可交易票）= 被 ADV 参与率卡掉的部分 |
| `gap_realloc` | `Σ\|target_value − target_free\|`（仅可交易票）= 冻结重分配把瞄准点挪走的量 |
| `gap_reprice` | `Σ\|target_free − pos\|`（仅**不可**交易票）= 停牌票与目标的偏离 |
| `cash_account` / `nav` | 现金腿 / 净值 |
| `frozen_value` / `frozen_count` / `avail` / `frozen_reprice_pnl` | 冻结仓位的 **gross**（非带符号和）、只数、可投金额、复牌重估损益 |
| `ghost_count` / `ghost_value` / `delist_close_value` | 幽灵持仓只数 / 价值 / 当日因退市清掉的价值 |
| `market_ret` / `weight_gross` / `target_count` / `oop_weight` | 市场收益、`Σ\|w\|`、目标持仓只数、池外权重最大值 |

**两个换手的拆分规则**（这是唯一非显然的一处）：

```
d_alpha   = target_free  - pos_adv        # 无冻结时这就是全部想交易的量
d_realloc = target_value - target_free    # 冻结重分配把瞄准点挪走的部分
den       = |d_alpha| + |d_realloc|
share_a   = |d_alpha| / den               # den = 0 处取 1
alpha_turnover   = Σ(|delta| · tradable · share_a)       / booksize
realloc_turnover = Σ(|delta| · tradable · (1 - share_a)) / booksize
```

按**实际成交额**成比例分摊, 使 `alpha_turnover + realloc_turnover ≡ trade_dollar/booksize`
**恒成立**。直接取 `|d_alpha|` 与 `|d_realloc|` 的话, 两者反号时和会大于真实换手,
"换手拆不平"就成了报表上的常态噪声。

**`holding.psv`** —— `date` + `2N` 列, 两块拼接, 块内按 `ctx.cols` 顺序：
`holding_value:{security_id}` × N, 然后 `holding_weight:{security_id}` × N。
`holding_weight ≡ holding_value / booksize`（booksize 为常量, 见 `metrics.json` 的 snapshot）。
列名接缝是**冒号**——`|` 已是字段分隔符。

**`pnl.psv`** —— `date` + N 列, 逐股逐日**持仓**损益（不减成本；成本只在 `daily.psv` 里）。

**`metrics.json`** —— 五个顶层块：

| 块 | 内容 |
|---|---|
| `scalar` | **26** 个全区间标量：`sharpe` `ann_return` `ann_return_dollar` `ann_return_long_side` `turnover` `margin_bps` `fitness` `max_drawdown(_dollar/_from/_to)` `avg_long_value` `avg_short_value` `avg_long_count` `avg_short_count` `long_short_ratio` `pnl_total` `holding_pnl_total` `trading_pnl_total` `cost_total` `trade_dollar_total` `cost_share_of_gross` `net_share_of_gross` `return_std_daily` `hit_rate` `n_days` |
| `by_year` / `by_month` | 每段 **19** 个字段（键为 `"2026"` / `"2026-03"`）（同口径）：`days` `sharpe` `ann_return` `pnl` `holding_pnl` `cost` `cost_share_of_gross` `margin_bps` `max_drawdown` `hit_rate` `return_std_daily` `turnover` `trade_dollar` `avg_long_value` `avg_short_value` `avg_long_count` `avg_short_count` `long_share` `long_short_ratio` |
| `audit` | **23** 个：`ghost_detection` `ghost_days` `ghost_cells` `ghost_rate` `ghost_examples` `delist_source` `delist_events` `halt_cells` `frozen_*` `realloc_turnover_avg` `alpha_turnover_avg` `cash_*` `gap_*` `weight_nan_cells` `adv_uncapped_cells` `nav_final` |
| `gates` / `n_pass` / `n_total` / `all_pass` / `summary` | 见 §8.5 |
| `snapshot` | **18** 个口径字段：`booksize` `participation` `sd` `ed` `n_sessions` `n_securities` `ann_days` `cost_model` `cost_bps(_avg)` `adv_constrained` `ghost_detection` `delist_source` `node` `return_metric` `known_defects` |

**标量的定义**（`ann = 252`）：

```
sharpe      = mean(return) / std(return, ddof=1) · √ann        # 无无风险利率
ann_return  = mean(return) · ann                               # 算术年化, 分母恒定不复利
turnover    = mean(trade_dollar) / booksize                    # 日均
margin_bps  = Σpnl / Σtrade_dollar · 1e4
fitness     = sharpe · √( |ann_return| / max(turnover, 0.125) )   # TO 下限 0.125
max_drawdown = 最大回撤(累计 pnl) / booksize
hit_rate    = mean(pnl > 0)
cost_share_of_gross = Σcost / Σholding_pnl
```

**`--weight FILE`（外来权重）的 schema**：首列是日期, 其余列名为 `security_id`,
值为目标权重；日期必须是 session 轴上**连续**的一段（有洞会被拒绝, 见 §8.2）。
认 `.psv`（`|` 分隔）/ `.csv` / `.feather` / `.parquet`。

### 8.5 七道闸门（每次评估自动亮）

设计约束取自 §九 的教训（`ghost_days` 恒为 0——"一道永远不会触发的告警比没有更危险"）：
**每道闸门都打印自己的状态和数字，通过也打印**。空白绝不能在"干净"与"没查"之间有歧义，
所以没有判据时报 **`NO-BASIS`** 而不是 `PASS`——三态，不是两态。全部七道 < 200 ms，
全部从四交付物派生。

缺省阈值（可整体替换）：

```python
beta_r2_max = 0.25          conc_top1_max = 0.20      conc_top5_max = 0.50
conc_day1_max = 0.20        stab_half_ratio_min = 0.25
stab_roll1y_sharpe_min = 0.0    breakeven_min = 2.0
ls_ratio_lo = 0.67          ls_ratio_hi = 1.50        pool_gross_tol = 1e-6
```

| # | 闸门 | 输入 | PASS 条件 | NO-BASIS 条件 |
|---|---|---|---|---|
| 1 | `market beta` | 组合日收益对 `market_ret` 回归 | `r2 ≤ beta_r2_max` | 有效观测 < 30，或方差为 0 |
| 2 | `concentration` | 逐股 `Σ\|pnl\|`、逐日 `\|pnl\|` | `top1 ≤ .20` **且** `top5 ≤ .50` **且** `单日 ≤ .20` | 全区间 `Σ\|pnl\| = 0` |
| 3 | `period stability` | 分年 Sharpe、上下半场、滚动 1 年 | `滚动1年 > 0` **且** `下半场/上半场 ≥ .25` | 样本 < 253 天**且**半场比不可算 |
| 4 | `cost breakeven` | `breakeven = pnl_gross / cost_total` | `breakeven ≥ 2.0` | `cost ≡ 0`（未接成本模型） |
| 5 | `long/short balance` | 多空日均市值 | `0.67 ≤ 多/空 ≤ 1.50` | 账本每日皆空仓（纯多头是 **FAIL**，不是 NO-BASIS） |
| 6 | `pool hygiene` | 池外权重最大值、`Σ\|w\|` 偏差 | 池外恰为 `0.0` **且** `\|Σ\|w\|−1\| ≤ 1e-6` | 没有提供 universe 面板 |
| 7 | `lookahead status` | `ghost_detection` / `delist_source` / `region_hash` | 三项皆无异常 | `deps_tc_resolved` 或 `region_hash` 为 None |

闸门 7 的 **FAIL** 条件（任一命中）：`ghost_detection == "disabled"`；`delist_source == "none"`；
`region_hash != region_hash_canonical`（前者是本次评估用的口径，后者是权重**算出来时**
所用的口径，取自节点 meta）。

**成本临界倍数值得单独说**：报告 turnover 回答不了"这东西能不能投"，**它能**——
量纲恰好是"成本模型可以错多少倍"。

末行恒打一行 `submission readiness: n/7 gates pass`。研究期只 warn（硬失败只会教会
大家绕过它）。

## 九、退市与停牌

**总原则**：权重是"意图"，pnl 是"现实"。引擎落盘的目标权重表达**纯意图，不做停牌处理**；冻结、重分配、强平全部收在仿真侧。

| 事件 | 数据层（field 生产者） | pnl 仿真 |
|---|---|---|
| **退市** | return_metric 末日 = **最终对价收益**（收购对价/最后成交/破产保守估计如 −30%，meta 注明），之后 NaN；提供 `delist_date` field | 退市日平仓，**frozen_value 立即释放进 avail**（资金真实回收） |
| **停牌** | 停牌日 ret = NaN；**复牌日 = 跨停牌期累计收益**（分母为停牌前执行价）；**必须另外提供 `is_halted` bool field**（见下方 ghost 一节） | 价值冻结（delta=0 自动成立）；**frozen_value 扣减 avail**（资金占用，按 gross）；复牌日恢复自由、跳空损益经累计 ret 自动入 pnl |

退市与停牌不共用路径：一个是资金回收，一个是资金占用。

**冻结重分配（满仓口径）**：`avail = booksize − frozen_value`，其余票重归一后按 avail 换算——可交易部分始终满仓。三个钉死的细节：

1. `frozen_value` 按停牌前最后价冻结估值——停牌期间是账面数，复牌跳空后一次性重估、avail 跳变、其他票再平衡。**固有代价：停牌票的价格风险被隐性放大**（复牌大跌时不仅亏它本身，此前"多分出去的钱"也是虚的）。metrics 单列 `frozen_value_avg / frozen_reprice_pnl` 使其可见。
2. 重归一含多空两侧，保持多空比例结构；由此产生的非信号换手单列为 `realloc_turnover`，不混入 alpha 换手。
3. **顺序：先重分配、后参与率 clip**——前者定义"今天想要什么"，后者决定"做得到多少"；被 clip 的缺口每日重试。

**防御性检测**：

**NaN 三分类**——每个 `ret = NaN` 必须能归入且仅归入一类，判据只用当日及以前的信息（不用"往后看有没有数"：那是前视，且对停牌中的票根本无法判断）：

| 判据（按序） | 归类 |
|---|---|
| `date > delist_date` | 退市后（永久） |
| `is_halted[t]` 为真 | 停牌（暂时） |
| 两者皆否 | **幽灵持仓（ghost）** |

- **幽灵持仓（ghost）**：`昨仓非零 × ret=NaN` 且不属于上述任一**已知原因**——skipna 会让它无声蒸发（零成本退出，生存者偏差后门）。少量 → warning + 按最后可得价当日平仓；超阈值 → 报错。

- **这道防线必须能响，而不只是存在**。初稿只有 `delist_date` 一个判据，于是"非退市即停牌"，第三类恒为空、`ghost_days` **恒为 0**——一道永远不会触发的告警比没有更危险，因为它给出的是虚假的安全感。故停牌必须由**独立的正向信号** `is_halted` 提供，不能用兜底推断代替。这是"纪律由框架机械强制"（§一原则4）的直接推论：**判据必须是正向可证的，不能是"排除法剩下的"**。

- **`is_halted` 缺失时不得静默降级**。无该 field 时 pnl 二选一，且必须**显式配置**：
  1. `--halt-proxy consecutive:K`——连续 ≥ K 个 session 的 `ret=NaN` 且 `delist_date` 未到，视作停牌；短于 K 的记 ghost。这是降级口径，会漏掉真正的一日停牌。
  2. 不配置 → **拒绝运行**，而不是把 `ghost_days` 记 0 继续跑。

  `metrics.json` 恒含 `ghost_detection` 字段，取值 `field` / `proxy(K)` / `disabled`，让这道防线的**实际状态在报表上可见**——否则读报表的人无法区分"没有幽灵持仓"与"根本没在查"。

> **数据可得性**：`is_halted` 与 `delist_date` 同属"免费源拿不到、需随行情数据一并采购"的一类（§十四 待定决策 1）。当前落地的美股 base 数据集**两者都没有**，故 pnl 在该数据集上只能走 `--halt-proxy` 降级口径。见 [`l2_schema.md`](l2_schema.md) §0.1。

---

## 十、Ctx 设计

对外极简，对内扛三条纪律：防前视、池外 NaN、性能。API 见 §6.2。

**内部规则**：

- **面板首次触碰时对齐一次**：`Ctx` 持有的是 loader 而非数据，首次 `ctx.f/win` 才 `store.read` 该面板的 `[sd - lookback, ed]` 区间并 reindex 到同一（日期轴, 列轴），之后 win = 纯 numpy 位置切片，O(1)/日。全局共享轴让对齐通常是零操作。**不能在构造 Ctx 前把 deps 全部读入**——`g_common.field_base_px.*` 展开后 eager 加载是约 2 GB 常驻、且 ns 里一旦混入秩-3 节点就是 OOM（§7.2 第 4 条）。
- **行/窗缓存按日清空**：`_advance(t)` 由 runner 独占调用，推进游标并清缓存；同日重复 `ctx.f("x")` 只构造一次。
- **永远返回副本**：handle 就地改（`px[px<0]=nan`）不写穿底层面板——写穿会污染后续所有日期与所有 alpha，灾难级且难查。日频拷贝成本无感。
- **init 期无游标**：`t=None` 时调 `ctx.f` 报友好错误（"数据访问只能在 handle 里"）。
- **op-state 不在 ctx**：decay 缓冲属于 OpChain。ctx 只装 handle 的世界，ops 是引擎的世界——否则 handle 能摸到自己的 decay 缓冲，语义即脏。

---

## 十一、质量防线

**防前视——三道机械闸门**

1. API 设计：ctx 无日期/绝对索引参数，游标引擎持有，语法上写不出未来。
2. cutoff 静态检查：`time_cutoff ≤ return_metric 执行起点`，编译期一行断言。
3. 毒化测试：置毒 D 日后数据重跑，D 前权重与 pnl 逐位不变。

**防过拟合——三件套**

1. **OOS 物理隔离**：研究环境（含分钟 bar 沙箱）数据物理截止于 OOS 起点；提交后由独立进程评估回写，提交前不可窥探。full-stack 团队没有人为防火墙，物理隔离是隔离的**必要条件**。
   **这需要两个 store**：§5.2 的日更把已登记节点更新到 today，而它住在研究员自己的 ns 里——同一个 store 不可能既截断于 OOS 起点、又有到今天的数据。故研究 store 截断于 `T_embargo`、生产 store 全史，日更落生产、单向推送截断后的历史进研究环境，永不反向拉取。推论反直觉但正确：**一个节点被晋升后，它的最新值对它自己的作者不可见**——研究必须始终是 IS-only。
   **且"物理隔离"这句话需要出网策略才成立**：`pipeline/fetch_yahoo.py` 实测 60 秒、无需任何凭证就能从公开端点抓回被截断的那一段。要么给研究机限制出网（仅内部包镜像），要么把口径老实降级为"隔离 + 出网策略 + 审计 + 提交次数预算"。**这是一笔需要单独计价的基础设施成本，不能默认它已经存在。**
2. **Alpha 池**：入库存代码/ops 指纹 + **日度 PnL 向量**；去重靠 PnL 相关性（阈值 <0.7）而非文本相似；强制在 canonical universe 复评作公共尺度（top3000 Sharpe 2.5 → top1500 掉到 0.8 的基本是小票流动性溢价）。
3. **晋升 = 登记进日更**（不是搬家、也不只是盖章）：个人 factor 想让平台每天替它更新，提 PR 到 g_common 的 `registry.yaml` 登记 repo + ref + 节点名，同时被 review 一次（毒化、cutoff 一致性、覆盖率、owner）。

```yaml
# g_common/registry.yaml
version: 2
pipelines:
  - {node: g_common.field_base_px.adj_close_1500, repo: g_common, commit: 7e21ab..., owner: infra, tier: 1}
  - {node: g_yliu.factor_yliu_resid_mom.resid_mom,     repo: g_yliu,   commit: f3a9c1..., owner: yliu,  tier: 2}
```

（完整字段与"按 identity 登记、钉 commit 不钉分支"的理由见 §5.2。）

**「identity 不变、只改状态」是关键。** 注意这里不变的是 identity `{repo}.{node_dir}.{node_name}-{output}`——它来自节点名与所在 repo/分组，与 yaml 文件在目录里怎么摆**无关**；`code_ref` 是 `{repo, commit, path}`，commit 已把那一刻的树钉死，旧 path 在那个 commit 里永远存在。所以移动文件既不会让 deps 失效、也不会让历史 meta 悬空（仓库布局因此可以自由组织，§15.1）；真正不能动的是 identity——它被冻进每一份下游 meta 与历史权重文件。

**未登记的节点别人也能 deps**（内部团队不需硬隔离），引擎在 config 校验时发 warning。但**已登记节点依赖未登记节点必须是编译期错误**，不是 warning：那意味着生产日更有一个不受管理的输入。失败场景很隐蔽——作者某天不再手工跑那个上游，它的 `last_session` 停在三个月前，日更照常执行、照常成功，每天拿三个月前的值算出"新"数据而**零告警**，因为 §7.3 的 `effective_ed` 只在依赖**落后**时回退，而这里上游根本没更新过。

---

## 十二、CLI 参考 `[SHIPPED]`

装上四个 console script：`alphakit` 与 `ak`（同一个入口）、`run`、`pnl`。
`run`/`pnl` 只是 `ak run`/`ak pnl` 的直呼形式。

```
ak [--store PATH] [--region NAME] {run|store|pnl} ...
        --store    L3 根目录; 缺省取 region 的 l3_root（相对路径钉到项目根）
        --region   缺省 us

run PATH                        # 唯一执行入口; PATH 可是节点目录、yaml、或 glob
    --sd DATE --ed DATE         # ed 缺省 = 轴末日, 并按依赖新鲜度回退（§7.3）
    --only NODE                 # 只跑指定节点
    --probe [K=20]              # 暖机尾段试跑 K 天, 不写 store; 也不评估
    --rebuild                   # 全量重建并 bump version; 缺省是区间 upsert（§7.2）
    --no-pnl                    # 跳过对 alpha 的自动评估
    --by year|month|both|none   # 自动评估里的分段表; 缺省 both
    --record PATH               # 落一份机器可读的运行记录（JSON）

store {status|ls|meta} [REF]    # 查询工具, 不是执行器
        status [PREFIX]         # catalog 表 + 打开的是哪个库 + 轴规模
        ls     [PREFIX]         # 逐行列出 ref
        meta   REF              # 该 ref 的完整 meta; 打错名字给编辑距离候选

pnl --node REF | --weight FILE  # 权重 → 四交付物 + 指标
    --sd DATE --ed DATE
    --booksize N                # 缺省取 region
    --rm REF                    # return_metric; 缺省取 region
    --cost-bps X                # 常数 bps 成本模型; 缺省 10.0
    --participation X           # ADV 参与率上限; 缺省取 region（0.10）
    --halt-proxy K              # int; 无 is_halted 时的显式降级（§九）。缺省取 region
    --by year|month|both|none   # 缺省 both
    --out DIR                   # 四交付物落地处; 缺省取 region 的 pnl_out
```

**缺省值的来源顺序**：命令行 > region 文件 > 内置常量。相对路径**只在取缺省值时**
钉到项目根；使用者亲手敲的 `--store ./x` 照 cwd 解析（那个 `./` 是他相对自己说的）。

**项目根**的判据：向上找同时含 `repos/` 与 `pyproject.toml`(或 `.git`) 的那一层；
`ALPHAKIT_ROOT` 覆盖。研究 repo 搬到引擎仓库之外时用它（或在自己的 region 里给
`l3_root` 写绝对路径——那不影响 `region_hash`）。

**`run` 缺省会评估本次跑过的每个 alpha**（`--no-pnl` 关掉）。因子与数据节点不评
（没有权重可仿真）；`--probe` 也不评（那一趟不落库，评的会是上一次的权重）。

日更 = cron 按 registry 逐个调 `run --ed today`，无需独立命令。

> **以下命令属于目标架构, 尚未实现** `[TARGET]`——不要照着实现：
> `alpha submit`、`store search`、`store set-status`、`store catalog rebuild`、
> `pnl --gate strict`、`run --universe/--time/--dump-format/--cache-*`、
> `pnl --cost-model/--adv/--delist-date`。设计意图见 `roadmap.md`。

---

## 十三、测试与验收

系统可信度不靠 review 靠断言。五类测试进 CI（第 5 类随 cache 版启用），任何一条红了不许合并：

1. **会计恒等式**：逐日逐位 `pos_value_t ≡ pos_value_{t−1} + pnl_t + 净流入`——单账本自封闭。含拆股/分红/退市/停牌复牌的构造用例各至少一个，corporate action 正确性由恒等式而非人眼保证。
   **另需三个专门针对 §8.2 的用例**（对应曾经写错的三处）：
   ① **多空两侧同时有停牌票**——带符号求和会让 `frozen_value ≈ 0`、`avail ≈ booksize`，断言 `frozen_value` 等于两侧 gross 之和；
   ② **停牌日 `ret = NaN`**——断言持仓保持前值且不为 NaN、当日 `pnl` 为有限数、复牌日跳空损益一次性入账；
   ③ **全员冻结**——断言不出现除零，且 `avail` 为 0 时不产生任何交易。
2. **毒化测试**：见 §十一。作为引擎测试常驻，晋升关卡复用同一实现。
3. **Ctx 单测**：窗口不足 pad NaN 且首轴长度恒为 w / 池外列 NaN / 副本不写穿 / 缓存日内命中跨日清空 / `_tc` 替换 / init 期报错 / None carry-forward / 掩码两端夹住。
   **秩相关**：三种秩的 `f` / `win` 返回形状（标量、Series、DataFrame、ndarray(w,N,T)）/ 秩-1 依赖在秩-2 handle 里正确广播 / 秩-3 掩码沿 `ti` 广播 / 秩-1 声明 `universe` 报错 / 秩-1 与秩-3 用 CS 类 ops 报错 / alpha 节点 `dims` 非 `[di, ii]` 报错 / handle 返回形状与 `dims` 不符时在 handle 那一行抛错。
4. **store 单测**：append 幂等（同日重跑不重复写）、稀疏读写、列扩容后旧 chunk 仍可读、并发写不同节点安全。
5. **golden 一致性**（cache 版启用）：随机切分日期段增量跑 N 次 vs 一次全量，逐位相等。

另有两条**治理类**断言常驻 CI：改一行公式后重跑日更必须被**指纹校验拦下**（§3.3），而不是静默 upsert；以及短区间 `run --sd <晚于历史起点>` 不得缩短已有数组——它必须走 upsert 而非 `write`（§7.2 第 2 条）。

**v0 完成的定义**：合成数据端到端（run 数据节点 → run alpha → dump → pnl 四交付物）+ 上述 1–4 全绿 + 一个真实 alpha 跑通并出分年度表。

---

## 十四、技术选型与阶段路线

| 组件 | 当前阶段 | 演进（触发条件） |
|---|---|---|
| L3 存储 | **Zarr**（全局共享轴 + 三层元数据） | `/dev/shm` npy 物化 + mmap 零拷贝（并行版）；catalog 换 SQLite（节点数上千） |
| 节点调度 | 顺序执行，声明顺序 | 拓扑排序 + 并行 |
| engine | 顺序执行，无 cache | watermark 增量 + 指纹失效 + checkpoint；拓扑并行 + 进程池 |
| 计算 | pandas / numpy | numba/bottleneck 下沉热点（profiling 说话） |
| L2 | parquet | ClickHouse（灵活查询需求） |
| 评估 | precise 仿真（价值账本） | 成本模型接 TAQ spread；IC 族；订单生成模块（value→shares） |
| 数据 | 日频行情 + 基本面 | TAQ 日频聚合字段（spread/RV/隔夜日内分解/尾盘行为…） |
| L3 秩 | 秩-2 为主，秩-1 随宏观数据接入 | 秩-3（`di×ii×ti`）随 TAQ 管道启用；先上粗网格（m30/m5），m1 需先评估 37 GB/节点的存储预算 |
| 引擎范围 | **L3 → L3**；L2 入库归 ingestion 管道 | 把 ingestion 收回统一 Node 模型（§五），届时 `source` / `ctx.l2` 进入引擎 |
| 治理 | 路径 + 写权限 + registry | alpha 池 + correlation service + OOS 独立评估 |
| 参数扫描 | 手写变体 | Jinja 模板（渲染前置：只生成静态 config） |

**落地顺序**：① securities master + 日历 + L2 入库 → ② Zarr store + 全局轴 + 统一 Node 内核 + base fields → ③ ops 链 + pnl.py ← **第一条端到端 alpha 在此** → ④ universe 生产 + CS ops → ⑤ registry 日更 + 监控 → ⑥ alpha 池 + OOS 隔离 → ⑦ TAQ 聚合管道 → ⑧ cache/并行/毒化测试（穿插）。

**待定决策**（按优先级）：
1. **数据源选型**——唯一花钱买错会疼的：CRSP/Compustat（PIT 质量最好、更新慢）vs FactSet/Refinitiv（贵、省心）vs Polygon+Sharadar（平价，delisting return 与 PIT 基本面需自补工程）。
   **采购清单里必须含 `delist_date` / delisting return / `is_halted` 三项**——它们不是"锦上添花的字段"：前两者是 §九 退市路径的地基（缺 delisting return 年化虚增 2–4%），`is_halted` 是 §九 ghost 检测唯一的正向判据，缺它这道防线只能降级或关闭。免费源三项全无。
2. NaN 语义规范（草案见附录 B，待批）。
3. Return 年化报表分母：跟 BRAIN 单边 vs 按 gross——**建议跟 BRAIN**（团队肌肉记忆 + Fitness 量纲按此标定）。
4. 风险模型来源（`neutralize: risk_model` 与风格暴露分解的依赖）：自建 Barra-style vs 采购。
5. borrow cost 数据源（做空启用的前置）。

---

## 十五、研究工作流 `[SHIPPED 的那两节]`

本章的绝大部分是**研究组织方式的提案, 尚未实现**, 已整体移入 `roadmap.md`。
留在这里的两节是真的在跑的：

### 15.7 `--probe`：最高杠杆的一项，且它顺带堵掉一个地雷

```
run nodes/alpha_yliu_rev_w005/ --probe     # K=20
```

跑 `[ed - (lookback + K), ed]`、完整 ops 链、在这 K 天上做迷你 pnl，**一个字节都不写 store**。短窗口 alpha 约 **1.0 秒**（对比 14 秒），长窗口 factor 约 11 秒（对比 86 秒）。

排它第一的三个理由：**①** 它攻击的是迭代**次数**而非延迟——一个想法早期最常见的迭代是"我写对了吗"，不是"Sharpe 好不好"，而前者 1 秒就能答。**②** 它自动算预热长度，不像手工缩短 `--sd` 那样返回一屏 NaN 把人送去 debug 一个不存在的 bug。**③ 它堵掉一个当前存在的破坏性操作**：`run nodes/x.yaml --sd 2024-01-01` 这个所有人都会做的"缩短区间跑快点"，在 §7.2 收尾是 `store.write(...loc[sd:ed])` 而 §3.3 定义 `write` 为全量重建的前提下，**会把该节点 2010–2023 的历史整段覆盖成一年的碎片**，且所有下游静默继承。把快速路径做成**构造上不落盘**，就是这个问题的解。

配套一个零数据的预检，让一次完整运行不会在第 12 秒才死于一个 typo：

```
run ...                 # 预检恒在每次调用的最前面跑, 不需要单独的开关
run ... --probe 20      # 再往前一步: 暖机尾段试跑 20 天, 不写 store
```

它检查的全是元数据（catalog 查询，< 50 ms）：deps 是否存在、`dims`/秩 与 CS 算子的合法性（§3.6）、alpha 的 ops 链是否以 `scale` 收尾（§4.4）、`_tc` 能否解析到存在的名字（§4.9.5）、universe 是否秩-2 bool、`output:` 是否单输出、声明的 outputs 键与 handle 返回是否一致。**这些在每一次调用读取任何数据之前就跑——它不是一个可选开关。**

### 15.9 闸门的严重度策略

七道闸门本身**已实现**，规格见 **§8.5**。这里只留策略：研究期只 warn（硬失败只会
教会大家绕过它），提交路径（15.10，未实现）则七道全部转为硬阻断。

## 附录 B：NaN 语义规范

全系统唯一真相，所有算子/ops/仿真实现向此表对齐；批准后冻结，改动走版本。

| 场景 | 表示 / 行为 |
|---|---|
| 未上市 / 已退市之后 | 节点值 NaN；universe 必为 False |
| 停牌日 | return_metric = NaN（可交易性判据）；价格类 field 沿用最后可得值还是 NaN **由各 field meta 声明**，默认 NaN |
| 当日池外 | ctx 交付整列 NaN（不改 store 中原值） |
| `rank` / `cs.*` | skipna：NaN 不参与排名/统计，输出位保持 NaN |
| `ts_*` 窗口含 NaN | 有效样本 ≥ `min_periods`（默认 = 窗口长 × 0.75）则计算，否则 NaN |
| `ts_backfill` | 最大回看 5 个 session，超过保持 NaN |
| `decay` 缓冲含 NaN | 该票该日按权重 0 参与加权（跳过），不传染整条缓冲 |
| ops 链传播 | NaN 全链保持；`scale` 时 NaN → 权重 0 |
| handle 返回 None | 整行沿用昨日 raw signal |
| 仿真中 ret = NaN | 不可交易。**三分类**：`date > delist_date` → 退市后；`is_halted` 为真 → 停牌；两者皆否 → 幽灵持仓（§九）。判据只用当日及以前的信息 |
| 停牌日的 NaN 进入推进式 | **禁止**。`pos_value * (1 + NaN)` 会摧毁持仓并向后传染；推进用 `fillna(0)`，可交易性判据用原始 NaN（§8.2） |
| Zarr 未写区域 | fill_value = NaN（必须显式设置，否则为 0，会与真实 0 混淆） |
