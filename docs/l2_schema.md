# L2 数据契约 v1 — 美股日频 PV / cax / sec master

> 本文件是 pipeline 各部件之间的**唯一共享合同**。改动此文件需同步改 `pipeline/` 与校验器。
> 上位文档：`docs/architecture.md` §3.1（L2 定义）、§3.4（标的与时间）、§5.1（L2 = 外部文件路径模板）。

## 0. 范围与来源

| 项 | 取值 |
|---|---|
| 市场 | 美股，日频 |
| 试跑区间 | 请求 `2025-08-29` → `2026-08-28`；**实际落地 `2025-08-29` → `2026-08-27`，250 个 session**（末段未结算 session 被裁，见 §0.1.4） |
| 试跑标的 | S&P 500 现任成分 503 只（`storage/data/base/l1/ref/sp500_constituents_20260830.csv`） |
| 行情源 | Yahoo `query2.finance.yahoo.com/v8/finance/chart`，`interval=1d&events=div,split` |
| 参考源 | NasdaqTrader `nasdaqtraded.txt`；SEC `company_tickers_exchange.json`；上述 GICS 清单 |

### 0.1 已知缺陷（必须写进 meta，不得静默）

1. **生存者偏差**：Yahoo 与 NasdaqTrader 当前快照都只含存活标的，这一年内退市的票拿不到（实测 `RDS-A` → Not Found）。S&P 清单亦为现任快照，不含期内被剔除的成分。`architecture.md` §3.4 把"含退市标的的历史池子"列为美股必修，§十四 待定决策 #1 指出这需要采购数据源（CRSP / Sharadar）。sec_master 的 `security_id` 分配方式设计成**可后续追加退市标的而不破坏既有 ID**。
2. **无 vwap**：Yahoo 日线不提供 vwap。**不生造 `(H+L+C)/3` 冒充 vwap**——那会静默污染任何以执行价为主题的研究。`architecture.md` §4.5 示例里的 `bar["vwap"]` 在本数据集上不可用，需要 vwap 的节点必须等接入日内数据或采购源。
3. **`adj_factor` 不是 PIT**：厂商 `adjclose` 是**向后复权**的，每次新分红都会改写全部历史因子。故本契约中因子的权威真相是 `cax` 里的**逐事件原始事实**（PIT 稳定），`pv.adj_factor` 只是带 `asof` 的派生快照。这正是 §3.4 要求"raw price + adjustment factor 双存"的原因。
4. **最后一个 session 可能未结算**：实测 2026-08-28 对 NVDA / AAPL 均返回 open/high/low/volume 有值但 `close` 与 `adjclose` 为 **null**——厂商在收盘价结算前就发布了该 bar。无 close 即无 `adj_factor` 锚点，故此类 bar 整行丢弃（§5「无报价的标的不出现在该文件中」）。构建器按**日期**统计丢弃数，用以区分"厂商整段 session 未结算"（占比 >50%）与"个别标的的真实缺口"，并在 `_meta.json` 同时记录 `ed`（请求区间）与 `ed_actual`（实际落地区间）——两者不符时必须显式告警，绝不让声明区间与实际数据静默错位。

5. **厂商序列可能局部损坏（MNST）**：MNST 在其 2026-08-11 的 2:1 拆股**之前四周**，价格在"拆前尺度"与"拆后尺度"之间来回翻转六次（07-20、07-23、07-31、08-03、08-06、08-07），比值恰为 2。这不是行情，是厂商在个别 bar 上零星施加了复权。构建器按"**无 cax 事件解释的 |复权收益| > 40%**"检出此类跳变，并把**同一标的出现 ≥2 次**的聚合进 `_meta.json.suspect_securities`——单次多半是真事件，多次是系统性损坏。
   本区间共 8 次告警：6 次为 MNST 损坏；另 2 次经人工判读为**真实行情**——`MRNA` 2026-08-19 +177%（开 116 收 174，成交 1.99 亿股 vs 平时约 500 万，且其后持续在 133–158）与 `FISV` 2025-10-29 −44%。**本版不做修复，只如实标注**：用启发式改写厂商数据是引入隐性污染的典型路径，是否排除 MNST 交由消费方决定。

## 1. 目录布局

```
storage/
  data/                                          # 摄入层: 重新获取代价高, append-only
    {data_name}/                                 #   数据集名, 本数据集 = base
      l1/                                        #   原始落地, 不改写
        yahoo/chart/{YAHOO_SYMBOL}.json          #     逐标的原始 payload
        yahoo/_fetch_manifest_{YYYYMMDD}.csv     #     抓取回执(来源/状态/bar 数)
        ref/nasdaqtraded_{YYYYMMDD}.txt
        ref/sp500_constituents_{YYYYMMDD}.csv
        ref/sec_company_tickers_exchange_{YYYYMMDD}.json
      l2/                                        #   交付层, 全部 | delimited csv
        {country}/                               #   本数据集 = us
          {category}/{YYYY}/{mm}/{subdata}.{YYYYMMDD}
            pv/2025/08/pv.20250829               #     行情面板, 逐 session
            cax/2025/09/cax.20250905             #     公司行动, 稀疏, 仅事件日
            sec_master/2025/08/sec_master.20250829  #  证券主表, 逐 session (PIT)
            industry/2025/08/industry.20250829   #     行业分类, 逐 session (PIT)
          calendar/{YYYY}/calendar.{YYYY}        #     session 轴, 一年一个文件
          _meta.json                             #     区间/来源/asof/已知缺陷/行数
registry/
  security_id.us.csv                             # security_id 注册表: append-only, 必须入库
storage/
  l3/                                            # 派生层, 可从 data + 代码完全重建
    {region}/{repo}/{node_dir}/{node_name}-{output}/
      us/g_common/base_px/field_base_px-adj_close_tc/
      us/g_yliu/liq/factor_yliu_liq-rvol20/
      us/g_yliu/rev/alpha_yliu_rev_mix-weight/
```

**`data` 与 `cache` 的分界是"重建代价"**：`data/` 里的东西丢了要重新向厂商取（且当前快照类文件事后取不回，见 §0.1.1）；`l3/` 里的东西丢了跑一遍 `run` 就有——这正是 `architecture.md` §一「内容寻址 + append-only」与 L3 完全可复现的立意。整个 `storage/` 已进 `.gitignore`。

> **叶子是 `{node_name}-{output}`**：节点名本身含 `{kind}_{ns}_` 前缀，输出名说明是哪一份数据。一次计算可以有多个产物（`factor_yliu_beta_decomp-mkt_beta_w250` 与 `-resid_mom_w250`），而不同节点即便产出同名输出也不会撞车。引用名 `{repo}.{node_dir}.{node_name}-{output}` 与该路径一一对应、纯字符串可互推。完整规则见 `architecture.md` §3.2 与 §4.11。

**分区与命名**：`{category}/{YYYY}/{mm}/{subdata}.{YYYYMMDD}`，`category` 与 `subdata` 同名（目录分区 + 文件自描述，单文件拷出后仍知道自己是什么）。文件名**不带扩展名**。

**除 `calendar` 外全部逐 session、全部 PIT**：`pv` / `cax` / `sec_master` / `industry` 每个交易日一个文件。`calendar` 是唯一例外——它记的是交易日与节假日本身，一年一个文件 `calendar/{YYYY}/calendar.{YYYY}`，无 `mm` 层。

对应 `architecture.md` §5.1 的 `source:` 声明写法：

```yaml
source:
  pv:
    path: storage/data/base/l2/us/pv/{date:%Y}/{date:%m}/pv.{date:%Y%m%d}
    key: security_id
  cax:
    path: storage/data/base/l2/us/cax/{date:%Y}/{date:%m}/cax.{date:%Y%m%d}
    key: security_id
```

### 1.1 如何重建

```bash
python3 -m venv .venv && .venv/bin/pip install pandas pyarrow   # 系统 python 受 PEP 668 限制
.venv/bin/python pipeline/fetch_yahoo.py     # 抓取(幂等可重入) -> storage/data/base/l1/yahoo/chart/*.json
.venv/bin/python pipeline/build_l2.py        # 反演 + 写出      -> storage/data/base/l2/us/**
.venv/bin/python pipeline/validate_l2.py \
    --l2-dir storage/data/base/l2/us \
    --raw-dir storage/data/base/l1/yahoo/chart # 验收闸门, 非零退出即失败

# build_ref_join.py 无需单独运行——build_l2.py 直接 import 其 join_rows() 在内存里消费。
# 单独跑它只输出核对报告（503 行 join 的逐项验证），不落任何中间文件。
.venv/bin/python pipeline/build_ref_join.py  # 可选: 参考数据 join 的自检报告
```

整个 `storage/` 已进 `.gitignore`——由上述步骤完全可重建，不入库。**注意**：`l1/ref/` 下三个参考快照是当日快照，事后无法重新取到同一版本；若要严格可复现，应单独归档。

## 2. 文件格式（所有 L2 文件统一）

- 分隔符 `|`（**pipe**），UTF-8，LF 换行，**首行为表头**。
- **不使用引号包裹**。任何文本字段在写出前必须把 `|`、`\r`、`\n` 替换为空格——这是不加引号的前提。
- **NaN / 缺失 = 空字段**（两个连续分隔符），不写 `NaN`/`NULL`/`nan`。对齐 `architecture.md` 附录 B。
- 价格保留 6 位小数，因子保留 10 位小数，`volume` 为整数。
- **所有 `date` 列一律 `YYYY-MM-DD`**（ISO），而**文件名一律 `YYYYMMDD`**（无分隔符）。两者指同一个 session，校验器需按此对照。
- 时间戳→日期一律按 `America/New_York` 换算（厂商 bar 时间戳是 09:30 ET；实测 753 根 bar 上 UTC 日期与 NY 日期无分歧，但按交易所时区换算是构造上正确的写法）。
- 每个文件内 `security_id` 唯一，按 `security_id` 升序。

## 3. `sec_master`

单文件参考表。一行一个标的。

```
date|security_id|ticker|ticker_nasdaq|ticker_cqs|ticker_yahoo|name|exchange|cik|
is_etf|round_lot|financial_status|first_trade_date|currency|ref_asof|source
```

**逐 session 一个文件，行集是 PIT 的**：某标的只在 `first_trade_date <= date` 时出现在该日文件里。这不是把一份快照复制 250 遍——`Q`(2025-10-27)、`FDXF`(2026-05-27)、`HONA`(2026-06-15) 是区间内上市的，故早期文件 500 行、末期 503 行。

**`ref_asof` 与 `date` 是两个不同的列，这是刻意的**：我们的参考源（NasdaqTrader / SEC / S&P）都只有当前快照、没有历史，所以 2025 年某行的 name / exchange / sector 实际是从 2026-08-30 的观测回填的。`ref_asof != date` 把这件事**摆在数据里**而不是只写在文档里——一行自称 PIT 却暗含未来属性，才是真正要防的东西。等接入有历史的参考源后，`ref_asof` 会逐步向 `date` 收敛。

**覆盖率三列（`first_session`/`last_session`/`n_sessions`）已移除**：在逐日 PIT 行里它们本身就是前视——2025-09-01 那天说"这只票有 250 个 session"是未来信息。覆盖率从 `pv` 一数就有，汇总与例外记在 `_meta.json` 的 `coverage_full` / `coverage_partial`。

| 列 | 说明 |
|---|---|
| `security_id` | 内部 ID，**int，永不重用**。由 `registry/security_id.us.csv` 持久分配，append-only，详见 §3.1 |
| `ticker` | 展示用主代码 = `ticker_yahoo` |
| `ticker_nasdaq` / `ticker_cqs` | NasdaqTrader 的 `NASDAQ Symbol` / `CQS Symbol` 原值 |
| `ticker_yahoo` | **Yahoo 抓取键**，推导规则见 §3.2。实测 `BRK-B` ✅ / `BRK.B` ❌ |
| `cik` | SEC CIK，跨 ticker 变更稳定；空表示未匹配到 |
| `first_trade_date` | Yahoo `meta.firstTradeDate`（epoch → date），security_id 排序依据 |
| `ref_asof` | 参考数据快照日期，见上 |

### 3.1 `security_id`：持久注册表，append-only

`architecture.md` §3.4 要求内部 ID **永不重用**（美股 ticker 会被回收，以 ticker 为键会把退市公司的历史静默焊到继承该代码的新公司上），§3.3 要求列轴**只在末尾单调增长**。

**按当次运行的内容排序生成 ID 两条都不满足**——加一只标的、或补进退市名单，全部 ID 就重排，`42` 昨天是 HUBB 今天就成了别的。故 ID 存放在跨构建持久的注册表里：

```
registry/security_id.us.csv
security_id|cik|ticker_yahoo|name|first_trade_date|added_asof
```

- 既有条目**永不重编号**；真正的新标的取 `max+1`。上市顺序只用于**首次播种**，不是每次重排。
- 键用 `(CIK, ticker)` 而非单独 ticker：CIK 跨改名稳定，配上 ticker 又能把 share class 分开（GOOGL 与 GOOG 共用 CIK `0001652044`，正是 §3.4 说的 company↔listing 之分）。
- **此文件必须入库，且不在 `.gitignore` 的 `storage/` 之内**。`storage/` 可以整个删掉重建，注册表不行——删了它，全部历史 ID 的含义就丢了。

实测验证：重跑分配 0 个新 ID、输出逐字节相同；抽掉 `GILD`（原 id 250）后重跑，它拿到 **504**（max+1）而非退回 250，其余 502 条 ID 一个未动。

### 3.2 Yahoo 代码推导规则（全量扩容时的关键，实测确立）

`nasdaqtraded.txt` 里三个代码列各有一套后缀编码，**且它们对不同证券类型的行为不一致**——想当然会静默丢掉整类标的：

| 证券类型 | `Symbol` | `NASDAQ Symbol` | 样例 | 处置 |
|---|---|---|---|---|
| 普通股 | 无后缀 | 同左 | `MMM` | 直接用 |
| **Class share** | `.X` | **`.X`（不变！）** | `BRK.B` `BF.B` `AKO.A` | **`.` → `-`** 得到 Yahoo 键 |
| 优先股 | `$X` | `-X` | `BFH$A` → `BFH-A` | 排除（非股票池） |
| Unit | `.U` | `=` | `AAC.U` → `AAC=` | 排除 |
| Warrant | `.W` | `+` | `ACHR.W` → `ACHR+` | 排除 |
| Right | `.R` | `^` | `AIIA.R` → `AIIA^` | 排除 |

**判别式**（170 个含 `.` 的代码中，133 个两列不等、37 个相等）：

```
若 Symbol == NASDAQ Symbol 且含 "."  →  class share  →  ticker_yahoo = Symbol.replace(".", "-")
若 Symbol != NASDAQ Symbol           →  优先股/unit/warrant/right  →  排除出股票池
否则                                  →  ticker_yahoo = Symbol
```

实测验证：`AGM-A` `AKO-A` `AKO-B` `BF-A` `BF-B` 全部解析成功；同批只有 `ATEST.*` / `CTEST.*` / `NTEST.*` / `ZXYZ.A` 失败，而它们本就被 `Test Issue = Y` 过滤掉。

> **v1 试跑不受此影响**：503 只 S&P 成分的 `ticker_yahoo` 是从 S&P 清单的 `Symbol` 列做 `.`→`-` 得到的，与本规则等价，503/503 抓取成功。本节是为全量扩容准备的。

### 3.3 `industry`

行业分类**单独成表**，不放在 `sec_master` 里。两个理由：它是**随时间变化**的 PIT 属性（公司会改行业），塞进静态参考表会把这件事说错；来源也不同（S&P/GICS vs NasdaqTrader/SEC）。且 `neutralize: sector` 本就需要它作为独立的 date × instrument field。

```
date|security_id|ticker|gics_sector_code|gics_sector|gics_sub_industry|ref_asof|source
```

与 `sec_master` 同样逐 session、同样的 PIT 行集与 `ref_asof` 语义。

`gics_sector_code` 用**官方 GICS sector 编码**——10 Energy / 15 Materials / 20 Industrials / 25 Consumer Discretionary / 30 Consumer Staples / 35 Health Care / 40 Financials / 45 Information Technology / 50 Communication Services / 55 Utilities / 60 Real Estate。选它而不是自造序号，是因为它稳定、通用，且最大值 60 正好落在 `architecture.md` §5.1 为 sector field 声明的 `dtype: i1` 内。

> **结构是 PIT 的，内容还不是**：`industry` 已逐 session 落盘，行集按 `first_trade_date <= date` 变化；但 S&P/GICS 清单只有现任分类，拿不到期内的行业变更，故各日的 `gics_*` 取值目前相同、且由 `ref_asof` 标出是回填的。等接入有历史的分类源后，只有内容会变，结构与消费方式都不动。

## 4. `calendar/{YYYY}/calendar.{YYYY}`

```
session|date|is_half_day|n_securities
```

- `session`：int，从 0 起单调递增，**全局日期轴**（`architecture.md` §3.3 `_axes/sessions.json` 的 L2 前身）。
- **按年分文件，但 `session` 跨年继续累加、绝不逐文件重置**——它就是那根全局轴，重置等于把轴切断。实测：`calendar.2025` 止于 session 85（2025-12-31），`calendar.2026` 起于 session 86（2026-01-02），合并后连续 `0..249`。
- 本版 session 集合由数据并集推出，故首尾两年只覆盖数据区间内的交易日，不是完整自然年。
- `date`：`YYYY-MM-DD`。
- session 集合 = 全部标的 timestamp 的并集（等价于 NYSE 交易日）。
- `is_half_day`：半日市标记，**以交易所日历规则为权威**（感恩节次日、平安夜、7 月 3 日落在交易日时）——提前收市是公布的日历事实，不该由数据反推。成交量只用作**反证**：规则判定为半日市却跑出正常成交量时告警。反向不告警——圣诞到元旦那一周实测只有中位数的 0.47–0.64 倍成交量，却并非半日市，用成交量正推会产生假阳性。
- 本区间内命中：`2025-11-28`、`2025-12-24`。（2026-07-03 因独立日落在周六而全天休市，不在日历中。）

## 5. `pv`

一行一个当日有报价的标的。

```
date|security_id|ticker|open|high|low|close|volume|adj_factor|adj_close_vendor
```

| 列 | 语义 |
|---|---|
| `open/high/low/close` | **原始（as-traded）价格**，见 §7 反演 |
| `volume` | **原始股数**（未经拆股调整） |
| `adj_factor` | 累计复权因子，**由 `cax` 事件日志自算**（§7.3），非取自厂商。`adj_close = close × adj_factor`。**非 PIT**，`asof` 见 `_meta.json`；因子只在相差常数倍下确定，故只有它算出的**收益**有绝对意义 |
| `adj_close_vendor` | 厂商 `adjclose` 原值，**只作独立交叉校验用**（§8 V1），不供研究直接消费——实测它在 MNST 上是错的 |

当日停牌 / 无报价的标的**不出现在该文件中**（不写全 NaN 行）——`architecture.md` §5.1 规定缺失由引擎补 NaN。

## 6. `cax`

稀疏事件日志，**长格式，一行一个事件**。这是复权的 PIT 权威真相。

```
date|security_id|ticker|event_type|div_amount|split_num|split_den|split_ratio
```

- `event_type` ∈ `div` | `split`。同日同标的两类事件写两行。
- `div`：`div_amount` 为每股现金分红（原始货币），split 三列为空。
- `split`：`split_num`/`split_den` 来自 `"10:1"` → `10`/`1`；`split_ratio = num/den`。`div_amount` 为空。
- `date` = **ex-date**。
- 无事件的 session **不生成文件**（引擎按 §5.1「文件缺失 = 该源当日全 NaN + warning」处理）。

> **与 `architecture.md` §4.5 示例的一处有意偏离**：该示例从 `cax` 表读 `adj_factor` 与 `ex_date_flag`，隐含公司行动表是逐日稠密的。本契约把稠密的 `adj_factor` 放进 `pv`（它本就是 (date,security) 的价格属性），`cax` 只保留稀疏事件日志。理由是 PIT：事件是不变的事实，累计因子是带 asof 的派生快照，两者混在一张表会让"L2 已 PIT"这个承诺失真。消费侧改动为一行：`bar["close"] * bar["adj_factor"]`。

## 7. 原始价格反演（核心算法）

Yahoo 的 `quote.*` 是**已复权拆股、未复权分红**的。实测证据：NVDA 于 2024-06-10 执行 10:1 拆股，而 06-07 收 120.89 → 06-10 收 121.79，**无 10 倍跳空**。

对每个标的，设 `S(t) = Π{ split_ratio(e) : e 为拆股事件, ex_date(e) > t }`（t 之后所有拆股比例之积，无后续拆股则为 1）：

```
raw_open(t)   = quote.open(t)   × S(t)
raw_high(t)   = quote.high(t)   × S(t)
raw_low(t)    = quote.low(t)    × S(t)
raw_close(t)  = quote.close(t)  × S(t)
raw_volume(t) = round( quote.volume(t) / S(t) )
```

边界：`ex_date > t` 用**严格大于**——拆股当日的价格已经是拆后价。此约定由 §8 V2 断言校验。

### 7.1 厂商并非对每个拆股都做了复权（必须逐事件判别）

**实测**：17 个拆股事件中 16 个已被厂商回填到 `quote`，但 **MNST 2026-08-11 的 2:1（最近的一个）没有**——`quote` 与 `adjclose` 里都还留着完整的 1.985 倍跳空。对它照常反演会把拆股前的每一个 MNST 价格翻倍。

故 `S(t)` 只累乘**厂商确实施加过**的拆股。逐事件判别用对数空间最近假设：

```
jump = quote.close(ex 前一根) / quote.close(ex 当根)
  已复权   ⇒ 序列连续，jump ≈ 1
  未复权   ⇒ 台阶仍在，jump ≈ ratio
取 |log(jump)| 与 |log(jump/ratio)| 中较小者
```

当 `ratio ≈ 1`（下面的分拆伪拆股）两个假设不可分，此式会落到"已复权"——既是常见情形，误差也被限制在百分之几。实测该判别式对 17 个事件全部分类正确。

### 7.2 分拆被编码成拆股（7/17）

`ratio` 非整数比的事件不是真拆股，而是**分拆或特别分配**：

| ex_date | 标的 | ratio | 实质 |
|---|---|---|---|
| 2025-10-30 | HON | 1061:1000 | 分拆 |
| 2025-11-03 | DD | 239:100 | 分拆（Qnity，对应新标的 `Q`） |
| 2026-01-05 | CMCSA | 1067:1000 | 分拆 |
| 2026-02-10 | BDX | 1272:1000 | 分拆 |
| 2026-06-01 | FDX | 1241:1000 | 分拆（对应 `FDXF`） |
| 2026-06-29 | HON | 1907:2000 | 分拆（对应 `HONA`） |
| 2026-07-01 | SPGI | 1057:1000 | 分拆 |

分拆时**股数不变**，与拆股经济含义不同。价格序列的处理方式相同（都是乘性调整），但 `cax` 必须如实保留原始 `num:den`，让下游能按非整数比把它们识别出来——任何依赖"股数变化"的逻辑都不能把这 7 个当拆股用。

### 7.3 `adj_factor` 由事件日志自算（不取厂商 adjclose）

把 `adj_factor` 定义成 `adjclose / raw_close` 是**同义反复**——V1 会恒等成立（实测往返误差 1e-16），什么也没验证，而且会把厂商的错误原样继承（MNST 的 adjclose 序列里有一个假的 −50% 收益）。

改为从事件日志推导，使 `cax` 真正成为契约声称的 PIT 权威：

```
adj_close(t) = raw_close(t) × DF(t) / SF(t)
SF(t) = Π{ ratio(e)                      : e 为拆股, ex_date(e) > t }      ← 全部拆股
DF(t) = Π{ 1 − D_raw(e)/C_raw(e 前一 session) : e 为分红, ex_date(e) > t }
D_raw(e) = D_reported(e) × S(ex_date(e))
```

最后一项是必需的：**厂商按当前股本重述历史分红**（NVDA 在 10:1 拆股前实付 $0.04/股，feed 里记 `0.004`），拿它直接除以 raw close 会小 10 倍。

**验证结果**：这样独立推导出的复权日收益，在 **502/503** 只标的上与厂商 `adjclose` 吻合到 **1 个基点以内**；唯一分歧是 MNST 在 2026-08-11 的 50.39%，即上述厂商未处理的拆股——我们对，厂商错。

因子只在相差一个常数倍的意义下确定（常数在收益率里约掉）。厂商的锚点比我们晚一天，故 TAP/NEE/TMUS/HII/EBAY/LH 六只在 2026-08-28 除息的票，**水平**上与厂商差一个恒定倍数而**收益率完全一致**。因此交叉校验一律比收益率，不比水平。

## 8. 校验断言（校验器必须全绿才算交付）

| # | 断言 | 说明 |
|---|---|---|
| V1 | 逐标的比较**日收益**：`r_ours(t) = raw_close(t)·adj_factor(t) / (raw_close(t-1)·adj_factor(t-1)) − 1` 对 `r_vendor(t) = adj_close_vendor(t)/adj_close_vendor(t-1) − 1`，最大差 < 1e-4（1bp）。**比收益不比水平**（§7.3：因子只在相差常数倍下确定）。已知且允许的例外：`MNST`（厂商未处理其 2026-08-11 的 2:1 拆股），须逐一列名放行而非放宽阈值 | 对自算因子的真实交叉校验 |
| V2 | 对**厂商已复权**的拆股事件：`raw_close(t-1)/raw_close(t) ≈ split_ratio`（±15%），且 `quote.close` 在同处**无**该跳变。对厂商**未复权**的事件（见 §7.1，本版为 MNST）断言相反：`raw_close` 与 `quote.close` 一致且**都**保留台阶 | 反演方向正确；方向写反会让两边同时失败 |
| V3 | `calendar.csv` 的每个 session 都有对应 `pv` 文件；反之亦然，无孤儿文件 | 日期轴完整 |
| V4 | 每个 `pv` / `cax` 文件内 `security_id` 无重复；全部 `security_id` 都能在 `sec_master` 找到 | 参照完整性 |
| V5 | `low ≤ min(open,close)`、`high ≥ max(open,close)`、`high ≥ low`、`volume ≥ 0`、价格 `> 0` | OHLC 合理性 |
| V6 | `security_id` 在 `sec_master` 内唯一、连续、从 1 起；`(security_id, ticker_yahoo)` 一一对应 | ID 分配纪律 |
| V7 | 覆盖率报告：逐标的 session 数分布；`n_sessions < 0.5 × 区间 session 数` 的标的单独列出 | 数据完整性可见 |
| V8 | 全部 L2 文件每行字段数 == 表头字段数；无字段含裸 `\|` | 格式自洽 |

校验结果写 `<l2-dir>/_validation_report.txt`，任一断言失败**必须非零退出**。

## 9. `_meta.json`

```json
{"dataset":"us_daily_pv_pilot","version":1,"asof":"2026-08-30",
 "sd":"2025-08-29","ed":"2026-08-28",
 "sd_actual":"2025-08-29","ed_actual":"2026-08-27",
 "n_sessions":250,"n_securities":503,
 "sources":{"prices":"yahoo v8 chart","ref":"nasdaqtrader nasdaqtraded.txt",
            "gics":"datasets/s-and-p-500-companies","cik":"SEC company_tickers_exchange"},
 "known_defects":["survivorship_bias_no_delisted","no_vwap","adj_factor_not_pit"],
 "row_counts":{"pv":125325,"cax":1614,"sec_master":125327,"industry":125327,"calendar":250},
 "coverage_full":498,
 "coverage_partial":{"HONA":{"security_id":503,"n_sessions":52,...}, ...},
 "trimmed_trailing_sessions":["2026-08-28"],
 "off_axis_events_dropped":{"2026-08-28":["BAX","EBAY","HII","LH","NEE","TAP","TMUS"]},
 "splits_vendor_had_not_applied":[{"symbol":"MNST","ex_date":"2026-08-11","ratio":"2:1",...}],
 "vendor_return_divergence":[{"symbol":"MNST","worst_return_gap":0.503873,...}],
 "unexplained_jumps_gt_40pct":[...], "suspect_securities":[{"symbol":"MNST",...}],
 "validation":"PASS"}
```
