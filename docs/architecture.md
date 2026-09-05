# 中低频 Alpha 研究与回测系统 — 架构设计 v0.8

> 参考 WorldQuant BRAIN 的研究流水线思想，面向内部研究团队的自建系统。
> 市场：美股；主频率：日频。TAQ 等日内数据只作为日频字段的**原料**（BRAIN 模式），引擎不直接消费分钟数据。

**版本地图**（先读这张表，避免混层）：

| 章节 | 性质 | 说明 |
|---|---|---|
| §一 ~ §五 | **目标架构** | 总览、三层仓库、数据与存储、统一 Node 模型、L2/L1 接入与日更 |
| §六 ~ §十 | **组件规格** | ctx 与 ops、引擎、评估、退市停牌、Ctx 实现；v0 范围标注于各节 |
| §十一 ~ §十四 | **质量与路线** | 防线、CLI、测试验收、选型与阶段 |
| §十五 | **研究工作流与仓库管理** | 仓库组织、晋升护栏、发现、死节点、OOS 两 store、内循环、闸门、提交 |
| 附录 | 决策记录 / NaN 规范草案 | 讨论纪要与待批口径 |

**配套文档**：

| 文档 | 内容 |
|---|---|
| [`l2_schema.md`](l2_schema.md) | **L2 数据契约**——目录布局、五张表的 schema、复权反演算法、验收断言。已落地并通过全部校验 |

**本次修订**（在 v0.8 基础上）：**§4.10 / §4.11 / §十五 新增**——三例连读的完整研究链、六个名字的命名约定（含"一个节点一个目录"的仓库布局）、以及研究工作流与仓库管理。**通读全文整理出的 16 处待修问题已全部并入相应章节**（原独立的 `architecture_findings.md` 因此撤销）：自引用节点读陈旧数据、日更 `write` 毁历史、`OpChain` 拿不到池子、`frozen_value` 带符号求和、停牌 NaN 摧毁持仓、ghost 检测恒不触发、schema 缺 `return_metric` 等字段、`_tc` 在 deps 侧未定义、eager/lazy 三处矛盾、registry 三种形状、写权限三方冲突、OOS 与日更不能共存于一个 store、`append` 静默拼接两个定义、已登记节点可依赖未登记节点、"物理隔离"缺出网策略、以及 §5.3 的基准数字歧义。 另：**§3.6 新增**——L3 不再恒为 `date × instrument`，改为节点声明的秩 `[di]` / `[di, ii]` / `[di, ii, ti]`，配套改动落在 §一原则1、§3.1、§3.3（`ti` 轴与分秩分块）、§4.1（`dims`）、§4.2（按秩的返回形状）、§4.7（秩-1/秩-3 示例）、§6.1（ctx 按秩返回）、§6.2（ops 按轴分秩合法性）。**§七 新增范围声明**——v0 引擎只处理 L3 → L3，`source` / `ctx.l2` / `ctx.l1` 移出 v0，L2 入库归 ingestion 管道（§五已相应标注）。

**数据层现状**：美股日频 base 数据集已落地并通过验收闸门——250 个 session（2025-08-29 → 2026-08-27）× 503 只 S&P 成分，`pv` / `cax` / `sec_master` / `industry` / `calendar` 五张表，除 `calendar` 外全部逐交易日 PIT。管道 `pipeline/{fetch_yahoo,build_l2,validate_l2}.py`，契约见 `l2_schema.md`。

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
  core/      store.py axes.py calendar.py config.py     # 存储/轴/日历/配置
  runner/    node.py ctx.py ops.py l2_reader.py dump.py  # 统一 Node 内核
  pnl/       simulate.py metrics.py report.py            # precise 仿真与指标
  cli.py     run / store / pnl 三个入口

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
run nodes/alpha_yliu_rev_senti_mix/  --sd 2018-01-01 --pnl   # 用数据
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
L3 路径   storage/l3/{region}/{repo}/{node_dir}/{node_name}-{output}/
引用名    {repo}.{node_dir}.{node_name}-{output}      ← region 由 config 的 `region:` 提供
```

**引用名与路径是一一对应的纯字符串关系**，不需要索引就能互推：

```
g_yliu.factor_yliu_liq.adv20
   ↕
storage/l3/us/g_yliu/liq/factor_yliu_liq-adv20/
```

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
  _catalog.json        # 该 region 全部节点 meta 的汇总索引 (派生, 可重建)
  g_common/field_base_px/field_base_px-adj_close_1500/
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

**Store API**（~120 行）：

```python
store.read(name, sd=None, ed=None) -> pd.DataFrame   # z[i0:i1] + 全局轴 → DataFrame
store.tail(name, n=1)                                # z[-n:], 只解压末块, 产线路径
store.append(name, date, row)                        # O(1) 单行, 每日任务可重入
store.upsert(name, df)                               # 按区间覆盖, 不动区间外, 不 bump version
store.write(name, df, fingerprint=…, rebuild=…)      # 缺省区间 upsert; rebuild 才 bump version
                                                     # 指纹闸门在此, 任何写入方都要过
store.meta(name) / store.catalog()
```

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
  halt_proxy: null        # 无 is_halted field 时的降级口径 (§九)
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
    params: {window: 250}
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
    params: {cutoff: "1500"}                      # cutoff 是参数, 不是独立字段
    # 无 outputs -> 单输出, 输出名 = 节点名去掉 {kind}_{ns}_ 前缀 = intraday_vol
    #   落 storage/l3/us/g_yliu/intraday_vol/factor_yliu_intraday_vol-intraday_vol/
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
      yoy: {dtype: f4, dims: [di]}         # 秩-1: 无 ii 轴 -> …/field_macro_cpi-yoy/
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
    params: {days: 5}
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
    n  = ctx.params["days"]
    px = ctx.win("g_common.field_base_px.adj_close_tc", n + 1)
    return -(px.loc[0] / px.loc[-n] - 1)                  # 单输出, 裸值

# g_yliu/nodes/alpha_yliu_rev/rev_senti_mix.py
def handle(ctx):
    return 0.6 * ctx.f("g_yliu.alpha_yliu_rev.alpha_yliu_rev_w005-weight") + 0.4 * ctx.f("g_lqin.alpha_lqin_senti.weight")
```

```bash
run nodes/field_base_px_adj/         --sd 2010-01-01   # 数据与 alpha 同一条命令
run nodes/factor_yliu_beta_decomp/   --sd 2015-01-01
run nodes/alpha_yliu_rev_senti_mix/  --sd 2018-01-01 --pnl
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
    params: {window: 20}
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
storage/l3/us/g_yliu/liq/factor_yliu_liq-adv20/    -illiq20/    -rvol20/
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
    params: {days: 5}               # 与名字里的 w005 编译期校验一致
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
    params: {days: 20}
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
    n   = ctx.params["days"]
    px  = ctx.win("g_common.field_base_px.adj_close_tc", n + 1)
    raw = -(px.loc[0] / px.loc[-n] - 1)               # 反转: 跌得多的买
    return raw / ctx.f("g_yliu.factor_yliu_liq.rvol20")          # 波动归一, 单输出直接 return
```

**`ops` 用到的分组 field 也必须写进 `deps`。** `neutralize: g_common.factor_common_gics.sector` 由引擎在 ops 链里解析，handle 里根本没提它——但它是**编译期就要能解析、运行期要能加载**的依赖，漏写则 §7.1 的"deps 不存在则报错"兜底会在运行时才炸，而且报错点在算子链里、离 yaml 很远。规则：**凡是这个节点跑起来需要读到的 L3，无论谁去读它，都要出现在 `deps` 里**。

落库：`…/g_yliu/rev/alpha_yliu_rev_w005-weight/` 与 `…-rev_w020-weight/`——**单输出 alpha 的输出名缺省为 `weight`**（§3.2）。两个变体是**两个独立节点、独立评估**——这正是 §4.9.4 "多参数变体手写展开"的形态，代价是 yaml 里有重复，换来的是每个变体在 store / catalog / alpha 池里都是一等公民，可以被单独引用、单独去重、单独晋升。

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
run nodes/alpha_yliu_rev_mix/     --sd 2018-01-01 --pnl  # combo + 评估
```

跨 config 的依赖必须**已经在 store 里**（§7.1：v0 不做图分析，引擎唯一兜底是"deps 不存在则报错"），所以顺序不能颠倒。`store status g_yliu.factor_yliu_liq.rvol20` 可查。

产出的 L3 结构：

```
storage/l3/us/
  g_yliu/liq/  factor_yliu_liq-adv20/  -illiq20/  -rvol20/     ← 例 5
  g_yliu/rev/  alpha_yliu_rev_w005-weight/  alpha_yliu_rev_w020-weight/   ← 例 6
               alpha_yliu_rev_mix-weight/                       ← 例 7
  g_lqin/senti/ alpha_lqin_senti-weight/                        ← 他人产出, 只读
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

秩-3 的窗口**是 ndarray 不是 DataFrame**——pandas 没有三维结构，而 §3.3 已经说明 Zarr 解压后本就是普通 ndarray，多包一层只会增加拷贝。轴序固定为 `(di, ii, ti)`，`ctx.cols` / `ctx.grid(name)` 给出后两轴的标签。

**`ctx.l2` / `ctx.l1` 不在 v0 的 ctx 里**——v0 引擎只吃 L3、只吐 L3（§七 范围声明）。目标架构中它们的语义见 §五。

无日期参数、无绝对索引、无 store 写句柄——API 面积越小，防前视的证明义务越小。实现细节（预对齐、按日缓存、永远返回副本、init 期报错、op-state 归 OpChain）见 §十。

### 6.2 ops：alpha 的出口算子链

| op | 类型 | 作用轴 | 适用秩 | 语义 |
|---|---|---|---|---|
| `rank` | CS | `ii` | 仅 2 | scope 内映射 [-0.5, 0.5]，NaN 保持 |
| `neutralize: <int field 全 ref>` | CS | `ii` | 仅 2 | 分组 demean。**必须写全 ref**，裸名会解析进研究员自己的 ns（§4.11.6） |
| `truncate: x` | CS | `ii` | 仅 2 | 单票 \|w\| ≤ x × gross |
| `scale: book` | CS | `ii` | 仅 2 | Σ\|w\| = 1；**不自动补**，改编译期校验（§4.4） |
| `linear_decay: n` / `exp_decay: h` | TS | `di` | 1 / 2 / 3 | 滚动缓冲由引擎持有（op-state） |
| `delay: k` | TS | `di` | 1 / 2 / 3 | **显式**滞后；执行滞后归撮合边界统一施加，勿惯性加 |

**算子参数在编译期按签名做类型校验。** `truncate` 收 float、`linear_decay` 收 正 int、`neutralize` 收一个 int field 的全 ref、`scale` 收枚举值。理由是 YAML 会**静默**把类型写错的值收下：`- truncate: 0.02,`（多一个逗号）解析出来是**字符串 `"0.02,"`** 而非数字 `0.02`，不报任何错，然后在算子里变成一次隐晦的比较失败或一个恒不触发的截断。这类错误不检查就只能靠回测结果异常时倒查。

**CS 类只对秩-2 合法**（§3.6）：秩-1 没有 `ii` 轴可截面；秩-3 有 `ii` 也有 `ti`，"在哪根轴上排序"无唯一答案——与其猜一个默认值，不如编译期报错，要日内截面就先在 handle 里显式压成秩-2。TS 类沿 `di` 作用，对三种秩都是同一份实现（op-state 的缓冲形状随秩而变，语义不变）。

顺序即语义。ops 算子与普通算子共用实现；handle 内 `ctx.to_weight(x, **overrides)` 可就地调用同一链。**delay 双重身份**：执行 delay 全局一次（所有节点以数据 ≤ T cutoff 算 T 日权重，combo 读上游为同日权重无隐式滞后）；ops 的 `delay: k` 仅用于故意的滞后版本。**推荐风格**：塑形 ops（rank/neutralize）放叶子，换手类（decay/truncate）上提到靠近 output。

---

## 七、执行引擎（v0：无 cache、顺序执行）

**范围声明**：v0 砍掉 cache/指纹/checkpoint/依赖解析，目标是把统一 Node 契约端到端跑通。config 里 `nodes` 按**声明顺序挨个全量跑**。

**v0 引擎只处理 L3 → L3**：输入全部来自 `deps`（store 里已有的 L3 节点），输出全部落 L3。`source:`（L2/L1 直读）与 `ctx.l2` / `ctx.l1` **不在 v0 引擎内**——L2 → L3 的入库由 ingestion 管道承担（§五），它有自己的 schema 强制、缺文件告警与逐列容错需求，与引擎的"逐日 handle + ops 链"是两类问题。混在一起会让引擎同时背上文件格式、路径模板、vendor 容错三副担子，而这三样都与 alpha 研究无关。

这条范围划分带来两个直接后果：v0 的节点 schema 里 `source` 恒为空，`deps` 是唯一输入来源；以及**基础 field 的生产不属于引擎**——它是 ingestion 的产物，引擎见到它时已经在 store 里了。

### 7.1 执行语义

- **声明顺序 = 执行顺序**：被依赖的节点写在前面（先跑完落 store，后者读到）。跨 config 依赖须已在 store（`store status <名>` 可查），引擎唯一兜底：deps 不存在则报错，不做图分析。
- `--only` 只跑指定节点；`--pnl` 对本次运行中**每个** kind 为 alpha 的节点都评估（不只终点），扫描族因此只读一遍面板。
- **lookback 预热**：config 声明，引擎取 **max(声明值, ops 可推导下限)**；预热段照常执行 handle 推进 state，只喂状态不进输出。语义承诺"预热充分则一致"，严格逐位一致等 cache 版。

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

### 7.4 dump

```
weights/g_yliu.alpha_yliu_rev.alpha_yliu_rev_senti_mix-weight.feather     # 或 per-day CSV (--dump-format)
+ meta.json  # region_hash / return_metric / universe / booksize / sd / ed / code_ref
             # + deps_versions（每个依赖当时的 version）+ cutoff + l2_asof
```

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
  holding.csv       # date × instrument: holding_value + holding_weight(= value/booksize,
                    #   与目标权重同尺度可逐股对比); 股数视图归未来订单生成模块
  pnl.csv           # date × instrument 逐股逐日损益 —— 归因("收益集中在哪些票/哪个月/
                    #   是否三只退市股贡献一半")变成一行 groupby, 无需重仿真
  daily.csv         # 日度汇总, 列清单见下
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

## 十二、CLI 参考

三个可执行文件：`run` 算、`store` 查、`pnl.py` 评。

```
run PATH                         # 唯一执行入口; PATH 可是节点目录、yaml、或 glob
    --only NODE                  # [v0] 只跑指定节点; 缺省全跑
    --probe [K=20]               # 暖机尾段试跑, 不写 store (§15.7)
    --dry-run                    # 编译检查 + 单日执行, 不预热
    --rebuild                    # 全量重建并 bump version; 缺省是区间 upsert (§7.2)
    --universe NAME              # 探索期临时覆盖
    --time                       # 分阶段耗时
    --sd DATE --ed DATE          # 评估窗口; ed 缺省 today(按数据新鲜度回退)
    --pnl                        # 对 alpha 输出串 pnl.py
    --dump-format feather|csv
    --cache-read / --cache-write full|off|lag:k / --force    # [目标] cache 版引入

store status [NODE | --base]     # last_session / 覆盖率 / 落后告警
store catalog rebuild

pnl.py --weight FILE | --node NODE
    [--sd DATE --ed DATE] [--rm REF] [--booksize N] [--cost-model REF]
    [--adv REF] [--delist-date REF]          # §8.1 必备面板, 需可配置
    [--halt-proxy consecutive:K | none]      # 无 is_halted 时的显式降级 (§九)
    [--gate strict]                          # CI: 七道闸门任一红即非零退出

alpha submit PATH [--dry-run]    # §15.10: 规范化 region → canonical 复评 → 去重 → 冻结提交
store search [--tag T] [--dims D] [--similar-to REF --min-corr X] [--all] [--tombstones]
store set-status REF {wip|keep|deprecated}   # §15.4 的生命周期迁移
```

日更 = cron 按 registry 逐个调 `run --ed today`，无需独立命令。

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

### 15.7 `--probe`：最高杠杆的一项，且它顺带堵掉一个地雷

```
run nodes/alpha_yliu_rev_w005/ --probe     # K=20
```

跑 `[ed - (lookback + K), ed]`、完整 ops 链、在这 K 天上做迷你 pnl，**一个字节都不写 store**。短窗口 alpha 约 **1.0 秒**（对比 14 秒），长窗口 factor 约 11 秒（对比 86 秒）。

排它第一的三个理由：**①** 它攻击的是迭代**次数**而非延迟——一个想法早期最常见的迭代是"我写对了吗"，不是"Sharpe 好不好"，而前者 1 秒就能答。**②** 它自动算预热长度，不像手工缩短 `--sd` 那样返回一屏 NaN 把人送去 debug 一个不存在的 bug。**③ 它堵掉一个当前存在的破坏性操作**：`run nodes/x.yaml --sd 2024-01-01` 这个所有人都会做的"缩短区间跑快点"，在 §7.2 收尾是 `store.write(...loc[sd:ed])` 而 §3.3 定义 `write` 为全量重建的前提下，**会把该节点 2010–2023 的历史整段覆盖成一年的碎片**，且所有下游静默继承。把快速路径做成**构造上不落盘**，就是这个问题的解。

配套一个零数据的预检，让一次完整运行不会在第 12 秒才死于一个 typo：

```
run ... --dry-run       # 只做编译检查 + 在一个 session 上执行 handle, 不预热
```

它检查的全是元数据（catalog 查询，< 50 ms）：deps 是否存在、`dims`/秩 与 CS 算子的合法性（§3.6）、alpha 的 ops 链是否以 `scale` 收尾（§4.4）、`_tc` 能否解析到存在的名字（§4.9.5）、universe 是否秩-2 bool、`output:` 是否单输出、声明的 outputs 键与 handle 返回是否一致。**这些应当在每一次调用读取任何数据之前就跑，而不只在 `--dry-run` 下跑。**

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

### 15.9 每次 `--pnl` 自动亮的七道闸门

设计约束直接取自 §九 的教训（`ghost_days` 恒为 0——"一道永远不会触发的告警比没有更危险"）：**每道闸门都打印自己的状态和数字，即使通过也打印。空白绝不能在"干净"与"没查"之间有歧义。** 全部七道总开销 < 200 ms，且全都从既有的四交付物派生。

| 闸门 | 抓的失败模式 | 打印 |
|---|---|---|
| **market beta** | "其实是个 beta 押注" | beta、R²、对冲后 Sharpe |
| **集中度** | "收益就是三只票" / 就是一天 | top-1/5/20 名占 `Σ\|pnl\|` 的比例 + 票名 |
| **区间稳定性** | "只在 2015 年前有效" | 分年 Sharpe、上下半场比、滚动 1 年最差 |
| **成本临界倍数** | "算完成本没法投" | `breakeven_cost = 2.7x` |
| **多空平衡** | 号称中性实则 80% 多头 | L/S 比、多空只数 |
| **池子卫生** | §7.2 第 3 条那道两端夹住的掩码是否真的生效 | `scale` 后池外权重（必须恰为 0）、覆盖率序列 |
| **前视状态** | cutoff 静默改绑、region 不可比 | `ghost_detection` 取值、各 dep 解析出的 `_tc` 实名、`region_hash` 是否等于模板标准值 |

**成本临界倍数值得单独说**：报告 turnover 并不能回答"这东西能不能投"，**成本临界倍数能**——它是一个数，量纲恰好是"成本模型可能错多少倍"。§8.4 已指出 precise 仿真让容量扫描"白拿"，这是同一个技巧换到成本维度。

严重度policy 对齐 §二 的"自由研究、统一提交"：研究期只 warn（硬失败只会教会大家打 `--force`），`--gate strict` 供 CI 用非零退出，提交路径（15.10）则七道全部转为硬阻断。每次 `--pnl` 末尾恒打一行：`submission readiness: 5/7 gates pass`。

### 15.10 从想法到 alpha 池：一条命令

§十一 要求四件事（PnL 相关性去重 <0.7、canonical universe 复评、独立进程 OOS、`region_hash` 等于模板标准值）。**清单会被跳过，命令不会。**

```
alpha submit nodes/alpha_yliu_rev_mix/ [--dry-run]
```

它是**对 `run --pnl` 的一次预设，不是新机器**：① 把研究员的 `regions/us.yaml` 换成模板标准值、重算 `region_hash`、**在该口径下重跑**（§二 允许本地自由修改，正是因为有这一步——工具必须**执行**这次重跑，而不是只校验 hash 然后拒绝）· ② canonical universe 复评，并把 `us_top3000` 与 `us_top1500` **并排打印**（§十一 点名了这个诊断："top3000 Sharpe 2.5 → top1500 掉到 0.8 的基本是小票流动性溢价"），不要等 reviewer 来问 · ③ 对池中已有向量做相关性去重（5000 个 alpha 实测 13 ms，池就是 store 里一个 (K×D) f4 数组、5000 个才 40 MB，不需要 DB），**报告最近的 5 个及其相关系数而非只给判决**——"0.68 vs `g_lqin.alpha_lqin_rev_w003.weight`"是可行动的，"拒绝"只会让人瞎猜 · ④ 毒化测试作为提交闸门而非只在 CI · ⑤ 七道闸门全部硬阻断 · ⑥ 冻结提交记录（`code_ref`、config hash、`region_hash`、权重 hash、IS 指标、闸门块、最近邻）· ⑦ 交给 OOS——研究员的环境物理截断于 OOS 边界，他**跑不了**这一步。

回传的东西刻意很窄：accept/reject、完整的 canonical IS 指标、以及 OOS **只以有界摘要形式**返回（Sharpe 分桶、收益符号、OOS/IS 衰减比、与池中最相关成员及其名字）。**OOS 日收益向量永不回传。** 再加一个**提交次数预算**（如每人每季 6 次，同 `family` 的变体共用一份家族预算）——没有预算，评估器就是一台神谕机，OOS 会以每次提交约一比特的速度退化成 IS，**这才是真正的失效模式，而不是数据泄漏**。

`--dry-run` 在本地跑完 ①–⑤ 并打印清单但不建记录，让研究员的最后一公里迭代就是对着真闸门做的，提交本身永远不会有意外。让这条路径立得住的设计性质是：**submit 是拿到 OOS 数字的唯一途径，而且它比手工凑齐证据更省事。**

---

## 附录 A：已定决策记录

**数据与存储**：日频主体、TAQ 仅作原料 · L3 三分类 field/factor/alpha，**由节点名 `{kind}_{ns}_{name}` 承载，yaml 里不再声明 kind/ns** · 路径 `storage/l3/{region}/{repo}/{node_dir}/{node_name}-{output}/`，引用名 `{repo}.{node_dir}.{node_name}-{output}` 与之一一对应、纯字符串可互推；无 `.zarr` 后缀，`ls` 出来就是 catalog · 单输出 alpha 的输出名缺省为 `weight`· **L3 主存 Zarr**：全局共享轴 + per-node meta(zarr attributes) + catalog 派生索引；chunks=(50,N)、默认 zstd、fill_value=NaN、bool/int8 省空间；稀疏免费（成本正比实际数据量）；按日期 append 真 O(1)、按标的 resize 需预留 500 列摊薄成年度维护 · feather 保留于 L2 与 dump 出口 · securities master + 全局轴 append-only 单调分配。

**配置与组织**：**三层 repo：alpha_kit（infra，纯引擎，零数据定义零口径配置）+ g_common（全员贡献，拥有全部共享 ns：base/各 dataset/common，含 registry 与 template）+ g_{user}（个人 region + factor + alpha）** · ns 与 repo 解耦（保留 `base` ns 以保证 `g_common.field_base_px.*` 通配的精确性）· 写权限按 repo 分组（共享 ns 仅 g_common CI 可写 / 个人 ns 直写 / 他人只读），fork 靠 copy · **region 每人一份、可自由修改，规范化内容 hash 进权重 meta；提交 alpha 池时按 hash 校验可比性——自由研究、统一提交**（原 `region@vN` 版本耦合方案由此取消）· **deps 必须显式，通配 `{repo}.{node_dir}.{node_name}-*` 是简写而非豁免**；template 默认给 `g_common.field_base_px.*`，编译期展开进 meta，引擎按实际调用惰性加载 · 成本模型 = 有版本的 L3 field · 多参数变体手写展开（Jinja 暂缓，原则"渲染前置"）· time_cutoff 模板替换 + 一行前视静态检查 · return_metric 显式声明与 t 行对齐约定 · 晋升 = 登记进 g_common 的 registry 日更（**identity 不变**，按 identity 登记、钉 commit 不钉分支，写权限同时翻转给日更用户）。

**秩与引擎范围**：**L3 不再恒为 `date × instrument`**，改为节点声明的秩——`[di]`（宏观）/ `[di, ii]`（缺省）/ `[di, ii, ti]`（日内），`di` 恒为首轴以保住"按日 append 真 O(1)"对三种秩同时成立 · `ti` 网格是 `_axes/grids/` 里的注册表条目、定长、半日市留 NaN；换网格 = 换节点名 · 分块 (50,N) / (1,N,T) / (4096,) 按秩取 · **alpha 必须是秩-2**（权重是 `di×ii`），`universe` 仅对秩-2/3 有意义，**CS 类 ops 仅秩-2 合法**（秩-1 无 `ii`；秩-3 的 `ii`/`ti` 二义，与其猜默认值不如编译期报错），TS 类三秩通用 · 同一节点可同时产出不同秩的输出（TAQ 原料模式：细网格 + 日频聚合出自同一次遍历）· 秩-3 体积须先算：m5 网格满仓 7.5 GB/节点、m1 达 37 GB，故「TAQ 只作原料」的建议依然成立 · **v0 引擎只处理 L3 → L3**：`deps` 是唯一输入来源，`source` / `ctx.l2` / `ctx.l1` 移出 v0，L2 → L3 入库归 ingestion 管道（它要背文件格式、路径模板、vendor 容错三副担子，与 alpha 研究无关）。

**组件与契约**：**统一 Node 模型（终态）：系统唯一可执行单元，多 L2/L3 进、一或多 L3 出；一种 yaml、一个 init/handle 契约、一条 run 命令；执行期无任何 kind 分支**（handle → mask(universe) → ops → 落库，三行）· **universe 缺省 all（全集）、ops 缺省 []，alpha = 写了池子和 ops 的普通节点**；数据节点全集计算是语义必需（池内算会让边缘票取不到值、进出池处留窗口断口）· `kind` 纯路径标签（缺省 alpha），**存储路径 = §3.2 的四段式** · **outputs 省略 = 单输出（名 = 节点名、dtype f4）**；单输出直接 return 裸值，**多输出必须 `ctx.multi_outputs(...)`**——一种情形一种写法，构造器在写错那一行抛错（未声明/缺失/dtype 不可转，typo 带修复建议）；NaN 是合法值、缺 key 是结构错误；keys 跨日恒定 · `scale` 不再自动补，改编译期校验（output 或被当 alpha 引用的节点必须以 scale 收尾）· **dmgr 组件取消**：日更 = cron 按 registry 调 run，数据侧只剩 `store` 查询工具 · **L2/L1 = 外部文件路径模板**（`format` 显式声明——L2 文件名无扩展名，{date} strftime，key 列归一全局轴，缺失 = 当日 NaN + warning，强制 schema）；`ctx.l2/l1` 仅在声明 source 时可用，**且 v0 不在引擎内**（见上条「秩与引擎范围」）；声明式简写（无 code + outputs 的 col/expr）保留，逐列 try 部分失败不回滚 · **逐日 handle 实测不慢于批量向量化**（2000×6000：235ms vs 713ms；state 增量 46ms），批量 build(sessions) 仅作可选性能出口不进手册 · 数据节点尽量无状态（保任意区间可重算）· ctx DataFrame 0/-1 行标签、永远返回副本、win 无上限、op-state 归 OpChain · 掩码两端夹住（ops 前 NaN、scale 后 0）· combo 概念取消（deps 含 alpha 的普通节点）· v0 无 cache/无依赖解析：声明顺序 = 执行顺序 + lookback 预热 · sd/ed = 评估窗口、ed 按新鲜度回退。

**退市与停牌（本次修订）**：`frozen_value` 取 **gross**（`.abs().sum()`）而非带符号和——多空两侧同时停牌时带符号和 ≈ 0，会把"停牌 = 资金占用"静默抹掉 · 停牌日 NaN **不得进入推进式**，推进用 `fillna(0)`、可交易性判据用原始 NaN，`prev` 须在推进前捕获 · **NaN 三分类**（退市后 / 停牌 / ghost），停牌须由**独立正向信号 `is_halted`** 判定而非"非退市即停牌"的兜底推断——后者使第三类恒空、ghost 检测永不触发；**判据必须正向可证，不能是排除法剩下的** · `is_halted` 缺失时**拒绝运行或显式降级**（`--halt-proxy consecutive:K`），`metrics.json` 恒含 `ghost_detection` 使防线状态可见。

**评估**：权重文件为正式接口、pnl 双入口、市场摩擦归评估侧 · **pnl 仅 precise 一个模式，仿真器定位** · **单一价值账本**（pos_value × 复权 ret 推进，CA 天然安全；股数账本/split_factor/双账本对账废除，股数归未来订单生成模块）· holding/pnl/daily/metrics 四交付物，逐股 pnl 矩阵为一等交付物 · daily 列含 long/short value·count、trade_dollar、holding_pnl、return · **`return` 分母 = booksize（恒定）** · 停牌沿用冻结价值、冻结重分配满仓口径（avail = booksize − frozen_value、剩余票重归一、先重分配后 clip）· 退市 = 资金回收 vs 停牌 = 资金占用，路径分叉；引擎侧不做停牌处理 · booksize 进 region/meta，容量分析 = 扫 booksize · gap 三分解与 realloc_turnover 单列 · ghost 检测 + delist_date 权威判据。

---

## 附录 B：NaN 语义规范（草案，待批）

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
