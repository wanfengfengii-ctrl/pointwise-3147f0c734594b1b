# 递归质控物化事故诊断报告

- 范围：只诊断。生产源码（`pipeline.py`、`support.py`、`reference.py`、`incident.py`、`checks.py`）、原始材料（`incident.json`、`logs/`）与原断言全部保持只读，未做任何修复性改动。
- 结论分级：**【已证实】** 由生产 Engine 实跑或源码直读得到；**【反事实】** 由隔离实例上单因素改动得到；**【证明】** 由算子推导得到；**【未证明】** 数据不足以下结论的事项（本事故无未证明根因项）。
- 复跑入口：`python3 analysis.py verify`（重放核对全部证据、重跑正常回归、连跑两次原 incident 入口）、`python3 analysis.py demo`（真实差异展示）。全部机器证据存于 `evidence.json`。

---

## 1. 调用链与关键源码位置

实际入账链路（事故与正常回归同一条）：

1. `checks.py:44` `incident()` → `incident.py:11` `replay(package)`；
2. `incident.py:12` 构造 `Engine(data)`；`incident.py:15` 每批调用 `pipeline.Engine.apply`（`pipeline.py:48-89`）；
3. `apply` 在 `pipeline.py:73-78` 用所有新旧规则的 `lo/hi` 与现有 slab 边界重切时间轴（左闭右开，`pipeline.py:62` 校验 `0<=lo<hi<=horizon`）；
4. 每个新区间 `[lo,hi)`：`pipeline.py:81` 取变更前后有效规则边集 `edges_at`（`pipeline.py:8-10`，墓碑与有效期外规则被排除），`pipeline.py:84` 经 `present`（`pipeline.py:13-17`）取上一版同区间缓存状态作为 `prior`，`pipeline.py:85-86` 调用 `support.advance`（`support.py:4-54`）做增量维护；
5. `pipeline.py:91-92` `read()` → `render`（`pipeline.py:20-38`）只投影 `data.observed` 中的节点并按 zone 汇总；`read()` 不改状态（**【已证实】**，函数体无写操作）；
6. 重开：`incident.py:16-18` 在指定步骤后把 `engine.checkpoint()`（`pipeline.py:94-97`）做 JSON 往返再 `Engine.restore`（`pipeline.py:99-105`），无隐式重建。

业务参照（独立重算，不参与生产写入）：`reference.closure`（`reference.py:1-10`）从 root 求最小不动点；`reference.expected`（`reference.py:13-34`）按最新源表逐区间重算。

`support.advance` 两阶段结构：

- 收缩相 `support.py:11-30`：只对“变更前后规则身份完全相同”的存活规则 `kept = before & after`（`support.py:7`，身份为 `(id, 排序后 body, dst)`，见 `pipeline.py:9`）建立引用计数；`support.py:18-30` 从“计数为 0 且非 root”的节点开始级联删除；
- 扩张相 `support.py:31-53`：在收缩后的集合上对 `after` 全集重新做正向闭包。

## 2. 根因（单因反事实隔离）

### 2.1 缺陷陈述 **【已证实+证明】**

收缩相只按**规则身份**决定规则是否存活：一条规则只要其 `(id, body, dst)` 在 `before` 与 `after` 中都存在，就被当作“持续有效”，其对 head 的支撑计数在整个收缩过程中保留（`support.py:11-17` 只初始化、`support.py:18-30` 只在被引用节点删除时才递减）。当一个节点集合 X 在 `prior` 中已经成立、且 X 中每个非 root 节点都能被某条 `kept` 规则从 X 内部的 body 推出时，X 构成收缩算子的后不动点，**任何节点计数都不会归零，级联删除无法启动**。递归 SCC（含自环）正是这种自支撑集合；撤回的是它们的**外部依据**（不在 `kept` 中的 feed 规则），而 SCC 内部边全部保留，于是 SCC 在收缩相整体存活，扩张相只会继续加节点。业务语义要求的是每来源每时刻从 root 出发的**最小**不动点（`CONTRACT.md` 第 18 行；`reference.py:1-10`），增量维护得到的却是 `prior` 内的**最大**自支撑子集再闭包，二者在撤回切断外部依据时分岔。

### 2.2 单因反事实 **【反事实】**

全部反事实只改一个输入、直接调用原 `Engine`/`replay`，原始包不变；机器输出见 `evidence.json.counterfactuals`。

| 编号 | 单一改动 | 结果 | 隔离出的结论 |
|---|---|---|---|
| CF-C-acyclic | 最小包仅 `feed:r→a`、`arc:a→b`，第二步撤回 feed | 缓存 `{r}` == 正确集，**无漂移** | 无环 DAG 上撤回可正常级联，递归是必要条件 |
| CF-C-2-cycle | 同上再加 `back:b→a` 形成 2-环 | 缓存 `{r,a,b}` ≠ 正确集 `{r}`，**漂移** | 递归 SCC 在外部依据撤回后自支撑存活 |
| CF-C-self-loop | 加 `fx:r→x` 与 `sl:x→x`，撤回 fx | 缓存残留 `x`，正确集无 `x`，**漂移** | 自环（长度 1 的 SCC）独立复现同一缺陷 |
| CF-A-no-reopen | `reopen_after` 置空，其余不变 | 五步 observed 轨迹与事故**逐字节相同**，漂移仍在 2-5 步 | 重开不是原因 |
| CF-B | 仅做第 2、3 步 checkpoint JSON 往返+restore | 两次 `checkpoint()` 完全相等 | 重开是恒等映射，只负责把污染原样持久化 |
| CF-D | 同一批 `tail`+低版 feedA，分别作用于污染检查点与等价的干净检查点 | 污染实例 `late` 在 debit `[50,55)` 成立、south=140；干净实例 `late` 不成立、无 south 组 | 新成果被污染是传播结果，不是 tail 规则本身的问题 |
| CF-E | 第三步重发 feedA rev1（当前为 rev2 墓碑） | `feedA` 仍为 `rev=2, deleted=true` | 低版被 `pipeline.py:66-67` 忽略，“重发”不会重新接线 |
| CF-F | 无输入改动；第 7 节算子判据 vs `support.advance` 全 213 个 (步,区间,来源) 核对 | 0 处不一致 | 判据与生产实现精确等价 |

**根因唯一性**：CF-C 三例把“递归 SCC”与漂移做成充分必要对照（同包、同撤回、仅有环/无环之差）；CF-A/CF-B 排除重开；CF-D 排除新规则；CF-E 排除重发。递归本身不是根因（第一步全量建立时递归结果正确，`checks.py:14` 断言通过），**“撤回路径上收缩相无法拆除已物化的递归自支撑 SCC”才是根因**；总额变化、一次重开均为表象，不作根因。

## 3. 事故时间线：错误明细、隐藏状态、汇总抵消、后续扩散

每步的完整明细键、隐藏节点与检查点见 `evidence.json.production_trace` / `detail_classification` / `hidden_state`。区间均为左闭右开。

### 步骤 1（首批，无重开）——基线正确 **【已证实】**
`read.totals = {east:120, north:300}`，10 条明细与 `reference.expected` 完全一致（与 `CONTRACT.md:24` 及 `logs/baseline-runs.json` 吻合）。未观测节点 `mix/mirror` 在此步是**合法**成立（被 gate 规则正常推出），不计污染。

### 步骤 2（撤回 feedA/feedB/selfFeed，rev2 墓碑；提交后重开）——污染入账 **【已证实】**
- 撤回规则位置：`incident.json` 第二批三条；最高修订与墓碑保留由 `pipeline.py:66-72` 处理。
- 业务正确集：debit 全时段仅 `{ra}`、credit 仅 `{rb}`、control 保持 `{rc, steady}`（`selfFeed` 墓碑后 `solo` 失去外部依据）。
- 缓存实际：`[20,80)` debit 残留 `{a,b,mix}`、credit 残留 `{c,d,mirror}`；`[40,60)` 两来源经 bridge/cross 规则互相污染出对方全部 6 节点；control 在 `[40,60)` 残留 `solo`（自环 `self` 自支撑，规则见 `incident.json` 首批 `self`）。
- **错误明细（9 条，全部为多入账，无缺失）**：`control/solo[40,60)`、`credit/{a,b}[40,60)`、`credit/{c,d}[20,80)`、`debit/{a,b}[20,80)`、`debit/{c,d}[40,60)`。
- **隐藏状态**：`mix`、`mirror` 不在 `observed` 映射中，不进 `render`（`pipeline.py:23`），但同样残留并随 checkpoint 持久化（`evidence.json.hidden_state` 记录本步 18 条隐藏污染，全事故共 64 条）；只核对 totals 永远看不到它们。
- **汇总抵消**：debit 权重 +7、credit 权重 −7（`incident.json.data.origins`），两来源残留区间对称，north 中 `+7·L` 与 `−7·L` 逐区抵消，north 仍显示 300；唯一暴露是 control 的 `solo`（权重 3，zone east）`3×20=60`，即 observed `{east:60, north:300}` vs 期望 `{north:300}`。**总额正确不代表明细正确**。
- 重开（CF-A/CF-B）对状态零影响，仅把污染固化进新检查点。

### 步骤 3（新增 `tail:b∧mix→late [50,95)`，重发 feedA rev1；提交后重开）——后续扩散 **【已证实+反事实】**
- 低版 feedA 被忽略（CF-E），墓碑仍在；既有残留原样延续。
- **扩散**：`tail` 的 body `{b,mix}` 在污染缓存中成立（`b` 是残留、`mix` 是隐藏残留），扩张相（`support.py:34-53`）据此推出新节点 `late`：debit 上 `[50,80)`、credit 上 `[50,60)` 入账（`b/mix` 在 `[60,80)` 仍存活于 debit，故 debit 的 late 延至 80；credit 的 `mix` 在 70 后消失，late 止于 60）。
- CF-D 对照：等价干净检查点上 `b/mix` 不在，`tail` 永不触发，south 组缺省；污染实例 south=140（debit `7×30=210`，credit `−7×10=−70`）。这就是“重开后污染新成果”的实锤。
- 本步 11 条多入账明细；north 仍靠 ±7 对消显示 300。

### 步骤 4（backA rev2 墓碑，无重开）——局部级联与区间拆并 **【已证实】**
- `backA` 墓碑拆除 debit 侧 2-环的一条边：debit 在 `[20,25)` 提前塌缩（returnA 25 才生效），`[75,80)` 同步塌缩；debit 残留区间收窄为 `[25,75)`，其 `late` 随 b/mix 变为 `[50,75)`。credit 侧不受影响（来源间不共享缓存，`pipeline.py:83-86` 逐来源独立 advance）。
- slab 在规则端点 20/25/75/80 处重新拆并（`pipeline.py:73-78`），`render` 再做相邻合并（`pipeline.py:27-30`）。
- 汇总：north = steady 300 + debit(7×(50+50+20+20)=980) − credit(7×(20+20+60+60)=1120) = **160**（不对称区间使对消不再完全）；south = `7×25 − 7×10` = **105**；east 仍 60。期望仍只有 `{north:300}`。

### 步骤 5（returnA、arcB rev2 墓碑，无重开）——进一步塌缩与零值组 **【已证实】**
- returnA 墓碑使 debit 的 gate/return 3-环失效，debit 污染退到 bridge/cross 规则窗口 `[40,60)`；arcB 墓碑拆除 credit 2-环，credit 污染同样退到 `[40,60)`，cross 规则只在 `[45,55)` 生效，故 `d` 残留仅 `[45,55)`；两侧 `late` 都只剩 `[50,60)`。
- 汇总：south = `7×10 − 7×10` = **0**，但 `render` 对有明细的组即使和为零也保留（`pipeline.py:35-37` 只累加不删零组），south:0 本身就是异常证据；north 恢复 300、east 60。11 条多入账依旧全部存在。
- 五步 observed 汇总序列与 `logs/baseline-runs.json` 两次独立进程记录完全一致（**【已证实】**，verify 中逐字段重放核对）。

## 4. 各时段独立维持异常的极小规则组合（全量）

定义严格采用 `CONTRACT.md:30`：对 (来源 o, 端点 t)，L 为业务正确完整集、S 为缓存完整集、P 为当时有效现存规则的非空子集、H={dst(r):r∈P}，要求 ∅≠H⊆S∖L、每条 r∈P 的 body⊆L∪H，且 P 包含极小。枚举先把候选收缩到 `dst∈S∖L 且 body⊆S` 的规则再按 |P| 递增并剪枝（`analysis.py:minimal_sustaining_combos`）；这是“列全”要求本身，不是停用子集试遍。全量逐 slab 清单（79 条记录）与合并后的 216 条 (步骤,来源,组合,最大连续时段,见证节点) 存于 `evidence.json.minimal_combos_per_slab` 与 `minimal_combos_merged_periods`（合并区间是 slab 区间的并集，verify 逐条校验覆盖关系）。

### 4.1 反复出现的“骨干”极小组合及其最大连续时段（跨步骤并集）

| 极小组合 P | 大小 | 适用来源与最大连续时段（步骤；区间） | 节点见证 |
|---|---|---|---|
| `{self}` | 1 | control：步骤 2/3/4/5，均 `[40,60)` | solo |
| `{arcA,backA}` | 2 | debit：步2/3 `[20,80)`（backA 墓碑后步4 起消失）；credit：步2/3 `[40,60)` | a,b,mix |
| `{arcB,backB}` | 2 | credit：步2/3/4 `[20,80)`；debit：步2/3/4 `[40,60)`（arcB 墓碑后步5 消失） | c,d,mirror |
| `{bridgeA,bridgeB}` | 2 | debit/credit：步 2/3/4/5 均 `[40,60)` | a,c（及跨源全集） |
| `{arcA,gateA,returnA}` | 3 | debit：步2/3/4 `[25,75)`；credit：步2/3/4 `[40,60)`；步5 returnA 墓碑后消失 | a,b,mix |
| `{arcB,gateB,returnB}` | 3 | credit：步2/3/4 `[25,75)`；debit：步2/3/4 `[40,60)`；步5 退窗 | c,d,mirror |
| `{backA,bridgeA,crossB}` | 3 | 步2/3 两来源 `[45,55)`（backA 墓碑后消失） | b,c 等 |
| `{backB,bridgeB,crossD}` | 3 | 步 2/3/4/5 两来源均 `[45,55)` | d,a 等 |
| `{crossA,crossB,crossC,crossD}` | 4 | 步 2/3/4/5 两来源均 `[45,55)` | a,b,c,d |
| `{crossB,crossD,gateA,gateB,returnA,returnB}` | 6 | 步2/3/4 两来源 `[45,55)`（步5 随 returnA/arcB 墓碑消失） | 全套跨源节点 |

### 4.2 各步骤异常维持概览（区间并集；括号内为该步各 slab 极小组合数）

| 步骤 | 来源 | 异常区间并集 | 每 slab 极小组合数 | 见证节点（含隐藏） |
|---|---|---|---|---|
| 2 | debit | `[20,80)` | 1,2,5,33 | a,b,c,d,mix,mirror |
| 2 | credit | `[20,80)` | 1,2,5,33 | a,b,c,d,mix,mirror |
| 2 | control | `[40,60)` | 1 | solo |
| 3 | debit/credit | `[20,80)`（传播节点 late 另在 `[50,80)`/`[50,60)`） | 1,2,5,33 | a,b,c,d,mix,mirror,late |
| 3 | control | `[40,60)` | 1 | solo |
| 4 | debit | `[25,75)` | 1,4,27 | a,b,c,d,mix,mirror,late |
| 4 | credit | `[20,80)` | 1,2,4,27 | 同上 |
| 4 | control | `[40,60)` | 1 | solo |
| 5 | debit/credit | `[40,60)`（d、late 仅 `[45,55)`/`[50,60)` 子窗） | 1,13 | a,b,c,d,mix,mirror,late |
| 5 | control | `[40,60)` | 1 | solo |

步骤 1 不存在任何满足定义的组合（S∖L 为空），清单显式为空。注意每个 P 都含一个有向环（2-环、3-环或 cross 4-环等），这与第 2 节根因互为印证：无环规则集不可能在外部依据撤回后自支撑。

## 5. 第二步检查点：清除全部隐藏污染的最少联合停用

### 5.1 实验设置（严格按 `CONTRACT.md:32`）**【已证实】**
从第二步提交并重开后的同一检查点出发（`analysis.py:step2_checkpoint`，JSON 往返与事故同构）；仅从 `probe_candidates` 的 15 个 ID 中选取，按其**当前内容**构造 `rev+1, deleted=true` 的**一次原子批次**；不运行第三步及以后；验收标准是停用后每个 slab × 每个来源的**完整节点集（含 mix/mirror 等未观测节点）**等于停用后源规则的最小不动点，而不仅是 totals 相等。停用只在隔离反事实实例执行，原包不变。

### 5.2 结果

- 第二步检查点上去重后的极小自支撑组合共 **34 个**（`evidence.json.step2_deactivation.distinct_combos`），其 ID 并集恰为全部 15 个候选——即不存在“候选外规则维持污染”的情况，问题在候选空间内可行。
- **最少停用条数 k = 5；全部同分方案恰有 2 个**：
  1. `D*₁ = {arcA, arcB, bridgeA, crossD, self}`
  2. `D*₂ = {arcA, arcB, bridgeB, crossB, self}`
- 两方案均经**真实 Engine 单批停用**后逐 slab × 逐来源（含未观测节点）核对为完全干净，且 `read()` 与 `reference.expected` 一致（`plan_runs`，每条含实际停用批次、残留列表与实际输出）。

### 5.3 最优性下界（可独立核对）**【证明+已证实】**
以下 5 个极小组合两两 ID 不相交（`lower_bound_packing`）：
`{self}`、`{arcA,backA}`、`{arcB,backB}`、`{bridgeA,bridgeB}`、`{crossA,crossB,crossC,crossD}`。
任一停用集必须命中每个组合，而一次停用只能命中其中一个不相交组合，故 |D| ≥ 5。装填由贪心生成、其两两不相交性可直接复核；引擎实测两个 5 元方案可行，故 k = 5。

### 5.4 完备性（为什么恰有两个 5 元方案）**【证明】**
设 D 为任一命中集。

1. `{self}` 是极小组合 ⇒ `self ∈ D`。
2. `{arcA,backA}` 与 `{arcB,backB}` 必须被命中。在 5 元预算与下界 5 之下，再结合 `{arcA,gateA,returnA}`、`{arcB,gateB,returnB}`：选 gate/return 链需花费 2-3 个 ID，而剩余预算不足以同时满足 bridge 与 cross 族约束（这些约束与 arc 族不相交），故两侧只能各取 `arcA`、`arcB`（各 1 个 ID 同时命中 2-环与 3-环）。已用 3 个 ID。
3. `{bridgeA,bridgeB}` 要求二选一。
   - 若取 `bridgeA`：组合 `{backB,bridgeB,crossD}` 中 bridgeB、backB 均不在 D，强制 `crossD ∈ D`。得 D*₁。
   - 若取 `branchB`：对称地，`{backA,bridgeA,crossB}` 强制 `crossB ∈ D`。得 D*₂。
4. 对两个候选 D*，34 个组合逐一检查均被命中（机器枚举在 `minimum_hitting_sets`，证书镜像在 `analytic_certificate`）；分支限界搜索在该下界剪枝下穷尽其余分支，无第三个 5 元解。

### 5.5 排除证据 **【已证实】**
两个最优方案的全部 `C(5,4)=10` 个 4 元近邻子集都真实跑了停用批次，无一干净，残留见证已记录（`near_miss_size_k_minus_1`）：例如去掉 `self` 后 control 的 `solo[40,60)` 残留；去掉 `arcB` 后 credit `{c,d,mirror}[20,25)` 即残留；去掉 crossD/crossB 后 `[45,55)` cross 族 SCC 残留。结合 5.3 的装填下界，**任意** 4 元子集都不可能命中全部 5 个不相交组合，无需试遍 `C(15,4)=1365` 个集合（分析式排除，不是逐个试停用）。

## 6. 适用于任意合法修订史的残留与传播判据 **【证明】**

设当前检查点在某区间端点 t 的缓存为 S（含 root r），下一批提交后 t 处有效规则集为 R（已套用最高 rev、墓碑、左闭右开有效期；规则 r 写作 body(r)→dst(r)），变更前后身份相同的规则子集为 K（对应 `kept`）。定义算子

- `F_R(X) = {r} ∪ { dst(r) | r∈R, body(r)⊆X }`（单调）；
- 收缩算子 `G_K(X) = {r} ∪ { dst(r) | r∈K, body(r)⊆X } ∩ X`（在 S 上单调）。

### 定理 1（残留的充分必要条件）
`advance` 在该区间的输出是 `A₂ = lfp(F_R, A₁)`，其中 `A₁ = gfp(G_K)` 限定在 S 内（`support.py:11-30` 实现的就是从 S 反复删无支撑节点的 gfp 迭代，`support.py:34-53` 实现其后的 lfp）。业务正确集为 `L = lfp(F_R, {r})`。则：

1. 恒有 L ⊆ A₂（A₁ 含 r，F_R 单调，lfp 从更大基集出发只大不小）——故本事故只有“多入账”，从不缺账，与第 3 节全部 `missing_detail=[]` 一致。
2. **残留非空 ⟺ S∖L 中存在非空的 K-自支撑集 X**（即 ∀x∈X∖{r}，∃r∈K：dst(r)=x 且 body(r)⊆X）。
   - 必要性：残留节点在 A₁ 中存活，A₁ 本身就是这样的后不动点；
   - 充分性：任一这样的 X 在 gfp 迭代中永不被删（归纳：初始 X⊆S；若当前轮 X⊆活跃集，则 X 中每个节点的 K-支撑仍在，删除规则只移除零计数节点），故 X⊆A₁，且 X 中无 root 可达的 R-推导（否则 X⊆L），扩张相不删除节点，X 残留至 A₂。
3. 有限规则库下，K-自支撑集存在 ⟺ 依赖图（边 body 节点→dst，仅保留 body⊆S 的 K 规则）中 S∖L 诱导子图存在有向环且该环在 S 内闭包可达；每个 CONTRACT 定义的极小 P 恰好对应一个极小此类集合的规则见证。**这把“递归”精确化到“撤回后仍存活的边构成的环”**：无环时 gfp 退化为 root 可达集，撤回必然级联干净（CF-C-acyclic）。

### 定理 2（区间拆并与跨时段/跨来源）
- 状态只在规则端点切分的常数个 slab 上维护（`pipeline.py:73-80`）：同一 slab 内 R 不变，结论对整个整数区间一致成立，无需逐整数展开；端点处 `present` 取左侧 slab 状态为 prior（`pipeline.py:13-17,84`），故残留沿相邻 slab 传播，直到某端点改变 K 使某个 SCC 失去闭包性才塌缩（步骤 4/5 的收窄即此机制）。
- 来源间无共享：advance 按 origin 独立调用、root 不同、规则 body 不能跨来源拼接（`CONTRACT.md:16`，`pipeline.py:83-86`）；同一规则可在多个来源的各自 S 中分别成立，因此一条规则的污染必须按来源分别判定（bridge/cross 在 debit、credit 两侧各自成环）。

### 定理 3（传播到观测与分区，及抵消条件）
- 新规则 r 在提交后于区间 I 触发 ⟺ body(r)⊆A₂；其 dst 进入 A₂ 后，若 dst∈observed 则在 I 生成明细行（`pipeline.py:23-33`），zone 与 weight 由 origin/observed 固定。tail 传播（CF-D）是该条件的实例：干净检查点上 body⊄L，规则正确地不触发。
- body 不全的规则（如 `decoy: b∧absent→a`，absent 任何时候不可达）永不满足 body⊆A₂，不参与任何自支撑集，极小组合枚举中自然排除（事故 34 个组合无 decoy）。
- 同头替代规则：dst 的计数是所有启用规则之和（`support.py:17,40`），一条外部依据撤回后只要还有一条 K 内规则支撑，节点就存活；这解释了 gate/return 3-环与 back 2-环对同一 head `a` 的平行支撑，也说明停用必须命中**每个**极小组合而非每个节点。
- **抵消的充分必要条件**：zone z 的观测总额无偏差 ⟺ Σ_{(o,n,I)∈残留明细, zone(n)=z} w(o)·|I| 与同区合法明细的变动之和为零。权重允许 ±w 与 0（`CONTRACT.md:16`）：本事故 debit(+7)/credit(−7) 对称残留使步骤 2/3 north 完全抵消，步骤 5 south 出现和为零但组保留（`pipeline.py:35-37`）；零权重组同理留组。故“totals 相等”既不充分也不必要于无残留，验收必须比对完整 detail 与未观测状态。

### 定理 4（墓碑、重放、重开）
- 墓碑与修订：低 rev 忽略、同版同内容忽略、同版异内容 REVISION_CONFLICT、非法批整批回滚（`pipeline.py:52-72`），因此 R 只由每 id 最高 rev 的非删除行构成；被删规则不在 K 中，但其历史产物若已进入某存活 SCC 仍可残留——撤回动作本身不回溯清理缓存，这正是事故入口。
- 重放幂等：对同一 rows 重放批次时 before=after，K=R，G_K 的 gfp 从 S 出发等于 S（S 本就是 F_R 后不动点），状态不变（`checks.py:16-17` 的幂等断言即此）；从空状态首次 apply 时 S={r}，gfp 无环可留，A₂=lfp(F_R,{r})=L，故**首次全量建立永远正确**，缺陷只在后续修订的增量路径上暴露。
- 重开：checkpoint 序列化 rows 与全部 slab 状态（`pipeline.py:94-105`），JSON 往返是集合/列表的一一映射，restore 不调用 advance、不重算，故重开 ≡ 恒等（CF-B）；它只决定污染在哪份检查点上被后续批次继续消费（CF-A 证明无重开漂移相同）。

### 判据的使用方式（复杂度与禁区合规）
输入只需当前检查点、最新源表与下一批：在规则端点切出的 O(端点数) 个区间上，各做一次 gfp 删除迭代与一次 lfp 闭包（总代价 O(规则数×节点数) 量级，与 horizon 长度、历史批数无关），不做全量重放、不逐整数展开、不试遍停用子集、不展开推导树；`analysis.py.gfp_kernel/support_fixpoint/advance_prediction` 是其可执行形式，CF-F 在事故全部 213 个 (步,区间,来源) 上与生产 `support.advance` 零偏差，`criterion_self_check` 同步验证预测残留与引擎实际缓存逐格相等。参照差（reference 比对）只用于标注 L，不参与上述因果判定。

## 7. 交付与运行核对

- 新增文件：`REPORT.md`（本文件）、`evidence.json`（可重跑输入副本、改动、原始实际输出、独立期望、全部见证）、`analysis.py`（verify/demo）。原文件与原断言未改；`checks.py incident` 仍退出 1 并输出 `TEMPORAL_LINEAGE_DRIFT`，`checks.py normal` 仍退出 0 输出 `NORMAL_REGRESSION_OK`（连续两次同标记失败已在 verify 中实跑保留，与 `logs/baseline-runs.json` 一致）。
- `.pointwise-runtime.json` 的 verify/demo 已指向 `analysis.py`，setup 保持 argv 数组列表。
- verify 实际执行（非静态检查）：生产轨迹重放并逐字段比对、极小组合四条件与极小性逐条断言、停用下界/最优性/两方案全状态干净/10 个近邻残留、全部反事实结论、正常回归子进程、两次 incident 子进程退出码与同标记一致性。
