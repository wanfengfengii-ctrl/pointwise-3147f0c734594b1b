# 递归质控物化事故包

本仓库用于数据转换后的合格证据物化：origin标识原始依据，节点标识加工成果，规则描述产生该成果所必需的前置成果。它不是权限系统或前端看板。源码、原始数据、原断言只读；本轮产物是诊断报告和证据，不修复实现。

## 可调用入口

- python3 checks.py normal：正常业务回归，退出0，输出NORMAL_REGRESSION_OK。
- python3 checks.py incident：运行完整事故，任何步骤明细或汇总不符即退出1，输出TEMPORAL_LINEAGE_DRIFT和JSON事故摘要（完整轨迹由incident.py输出）；此失败须保留。
- python3 checks.py demo：输出同一真实轨迹，退出0。
- python3 incident.py：输出JSON轨迹；incident.replay(package)接受同格式包，供反事实使用。
- pipeline.Engine(data).apply(batch)返回read结果；read()不改状态；checkpoint()返回可JSON往返的完整状态；Engine.restore(data,checkpoint)续接。
- reference.expected(data,latest_rows)是从空状态计算的业务参照；reference.closure返回一个来源、一个时刻的完整成立节点。它用于独立核对小实例，不提供因果核、跨时段联合干预或最优性证书。

## 输入和结果

incident.json为一个对象，包含data、batches、reopen_after和probe_candidates。data.horizon为正整数；所有时刻在[0,horizon)内。origins各有互异字符串id、非空字符串root及整数weight，观测节点到分区的映射为observed。weight允许负数和零。每个origin始终以自己的root为真，其他节点仅由规则推出，不能跨来源拼接前置。未出现在observed的节点仍属于完整计算状态。

每批为规则对象数组，每项恰有id、rev、body、dst、lo、hi、deleted。id/dst为非空字符串；body为非空且无重复的非空字符串数组；rev为正整数；lo/hi为整数且0<=lo<hi<=horizon；deleted为布尔。body为合取，规则之间为析取，节点允许递归、自引用、平行规则和暂时没有产生规则的前置。有效期左闭右开。业务语义是每来源、每时刻从root出发的最小不动点；不是任意满足规则的状态。

每id保留最高rev，高版允许换body、dst、有效期及撤销复活，墓碑保留。低版忽略；同版同内容忽略，同版异内容抛REVISION_CONFLICT；批内重复id拒绝。非法批次全部不生效。包中的data为可信固定目录，不在运行中修改。batches按数组顺序原子提交，reopen_after中的步号从1开始，该步提交后将检查点JSON往返并重开，之后继续；无隐式重建。

read和轨迹中的observed/expected为{"detail":[[origin,node,lo,hi,weight,zone],...],"totals":{"zone":integer,...}}。明细按整行排序，同来源同节点相邻成立区间最大合并；每个节点每来源只贡献一次，不按推导条数计重。汇总是各明细weight*(hi-lo)按zone求和；有明细的组即使和为零也保留，没有明细的组缺省。checkpoint另保留未观测节点和区间状态。轨迹项为{"step":1,"observed":...,"expected":...}。

完整正常样例就是事故第一批：read.totals为{"east":120,"north":300}且明细与参照完全一致。第二批的业务期望仅保留["control","steady",0,100,3,"north"]，totals为{"north":300}；其余步骤的期望由公开参照按最新源表计算。正常回归还覆盖重放、低版、冲突回滚，以及去除指定递归规则后的全部事故步骤。

## 诊断对象和反事实边界

REPORT.md需追溯实际调用链、关键源码行号和原始规则位置，区分已证实、反事实和未证明结论；不得把递归存在、总额变化或一次重开本身直接当根因。反事实应保持其他输入不变，直接调用原Engine或advance，列出更改与实际结果；允许额外构造小包和序列，但不能替换生产计算。

“极小规则组合”按每个来源、每个规则端点切分出的时段定义：令L是当时业务正确的完整节点集，S是缓存完整节点集。P是当时有效的现存规则集合的非空子集；令H为P的所有dst。要求H非空且H包含于S减L，P的每条规则的body均包含于L∪H；移去P中任意非空部分后不再满足这些条件。列出所有这样的P及其适用来源、最大连续时段和节点见证；若不存在须明确为空。此定义用于证据核对，不预设事故原因。

“最少联合停用”固定从第二步提交并重开后的同一检查点出发，仅允许从probe_candidates中选ID，以其当前内容和rev+1、deleted=true组成一次原子批次；不运行事故第三步及以后。每ID成本1，一次停用对全部来源和全部时段共同生效。目标是停用后所有缓存节点在整个[0,horizon)均等于停用后源规则的业务语义，包含未观测节点，不能只令totals相等。交付最少条数、全部同分ID集合、逐方案实际输出及可独立核对的下界/排除证据；若不可行需给不可行证据。该操作只在隔离的反事实实例执行，原包不变。

通用残留与传播判据以合法执行得到的当前检查点、最新源表及给定的下一批合法修订为输入；须说明当前及下一批哪些节点/时段可能偏离业务语义、如何到达观测明细和分区，以及何时抵消。提供充分必要条件及完整性论证，覆盖有效期拆并、body不全、同头替代规则、共享规则跨来源/时段、墓碑、重放和重开。参照的结果差不能单独充当因果判据；不要求实现通用数据库或求解器。

## 交付运行

新增REPORT.md、evidence.json及analysis.py，evidence的内部组织自定，但必须保存可重跑的输入、改动、原始实际输出、独立期望及报告所用见证。analysis.py verify须重新执行并核对报告中的事故、反事实、极小组合和最少停用证据，重跑正常回归，连续两次运行原incident入口且都确认退出1及同一标记；不能只检查文件存在或照读静态成功结果。analysis.py demo展示真实事故和反事实差异。更新.pointwise-runtime.json的verify/demo指向这两个入口，setup仍为argv数组列表。所有命令从仓库根目录运行；保留原文件和原断言。
