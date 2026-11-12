"""递归合格证据物化事故：结构化诊断分析（只诊断，不修复生产实现）。

本模块只读取生产源码 pipeline.py / support.py / reference.py / incident.py 与
事故包 incident.json，所有轨迹与反事实均直接调用原 Engine / advance / replay，
不替换任何生产计算。两条入口：

    python3 analysis.py verify   # 重放核对全部报告证据，重跑正常回归，连跑两次原 incident
    python3 analysis.py demo     # 展示真实事故轨迹与单因反事实差异

通用残留/传播判据（见 REPORT.md 第 7 节）在 gfp_kernel / advance_prediction 中
以算子形式实现：区间端点驱动（不逐整数展开）、不重放全史、不试遍停用子集、
不展开全部推导树。
"""

import copy
import itertools
import json
import subprocess
import sys
from pathlib import Path

from incident import load, replay
from pipeline import Engine, edges_at
from reference import closure, expected

ROOT = Path(__file__).resolve().parent
EVIDENCE_PATH = ROOT / 'evidence.json'


# --------------------------------------------------------------------------
# 1. 轨迹采集：完全使用生产 Engine，复刻 incident.replay 的重开语义
# --------------------------------------------------------------------------

def collect_trace(package):
    """返回每步 (引擎快照, 批次, observed, expected, 是否重开) 的完整轨迹。

    工作引擎严格按 incident.replay 推进：只在 reopen_after 列出的步骤后
    restore；非重开步保留同一 Engine 实例（slab 连续性是生产语义的一部分）。
    快照用独立的 restore 副本，避免后续批次原地改写历史检查点。
    """
    data = package['data']
    engine = Engine(data)
    trace = []
    reopen_after = set(package.get('reopen_after', []))
    for step, batch in enumerate(package['batches'], 1):
        observed = engine.apply(batch)
        reopened = step in reopen_after
        # 独立副本做快照：restore 内部重建 rows/slabs，与工作引擎不共享容器
        snapshot = Engine.restore(
            data, json.loads(json.dumps(engine.checkpoint())))
        if reopened:
            # 重开步：工作引擎本身也替换为往返后的实例（与 incident.replay 一致）
            engine = Engine.restore(
                data, json.loads(json.dumps(snapshot.checkpoint())))
        trace.append({
            'step': step,
            'engine': snapshot,
            'batch': copy.deepcopy(batch),
            'observed': observed,
            'expected': expected(data, engine.rows),
            'reopened': reopened,
        })
    return trace


def full_state(engine):
    """检查点完整状态（含未观测节点），用于隐藏污染核对。"""
    return engine.checkpoint()


# --------------------------------------------------------------------------
# 2. 通用判据（REPORT.md 第 7 节的可执行形式）
# --------------------------------------------------------------------------

def rules_at(rows, t):
    return [r for r in rows.values()
            if not r['deleted'] and r['lo'] <= t < r['hi']]


def support_fixpoint(rules, base):
    """算子 F_R(X) = base ∪ { dst(r) | body(r) ⊆ X } 的最小不动点（lfp）。

    与 reference.closure 等价但以规则列表直接给出，用于判据证明的可执行核对。
    仅按当前区间的规则集合计算一次；调用方按区间端点驱动，不逐整数迭代。
    """
    reached = set(base)
    while True:
        new = reached | {r['dst'] for r in rules
                         if set(r['body']) <= reached}
        if new == reached:
            return reached
        reached = new


def gfp_kernel(rules, prior, root):
    """advance 收缩相（support.py:11-30）的精确算子结果：

        A1 = gfp( X ↦ {root} ∪ { dst(r) ∈ prior | r 存活(body⊆X) } )
            = prior 中最大的自支撑子集（始终含 root）。

    计算方式是从 prior 反复删除“在当前集合内无任何存活规则支撑、且非 root”
    的节点；这是单调算子的最大不动点，按规则/节点规模收敛，与时间长度和
    历史步数无关（不重放、不逐整数、不展开推导树）。
    """
    kept = set()
    alive = set(prior) | {root}
    while alive != kept:
        kept = set(alive)
        supported = {root}
        for r in rules:
            if all(n in kept for n in r['body']):
                supported.add(r['dst'])
        alive = kept & supported
    return alive


def advance_prediction(prior, before_rules, after_rules, root):
    """support.advance 的算子级预测：A2 = F_after(gfp_after_within_prior)。

    before/after 为同一端点处变更前后的有效规则；与生产函数逐 slab 核对一致。
    """
    kept = edges_set(before_rules) & edges_set(after_rules)
    # 收缩相只按规则身份（id, body, dst）保留；有效期/删除造成的身份变化
    # 已由 edges_at 编码在规则集合差异中。
    kept_rules = [r for r in after_rules if edge_key(r) in kept]
    a1 = gfp_kernel(kept_rules, prior, root)
    a2 = support_fixpoint(after_rules, a1)
    return a1, a2


def edge_key(rule):
    return (rule['id'], tuple(sorted(rule['body'])), rule['dst'])


def edges_set(rules):
    return {edge_key(r) for r in rules}


# --------------------------------------------------------------------------
# 3. 极小自支撑规则组合（CONTRACT.md 定义的精确枚举）
# --------------------------------------------------------------------------

def candidate_rules(rows, t, heads):
    """dst ∈ H 的现存有效规则；只枚举这些规则的子集（其余规则不可能入组合）。"""
    return [r for r in rules_at(rows, t) if r['dst'] in heads]


def minimal_sustaining_combos(rows, t, cached, correct):
    """返回该 (来源, 端点) 下满足 CONTRACT 定义的全部极小规则组合 P。

    条件：H={dst(P)} 非空且 H ⊆ S\\L；每条 r∈P 的 body ⊆ L∪H；
    去掉 P 任意非空部分后不再满足（包含极小）。
    枚举空间先按 dst∈S\\L 与 body⊆S 收缩，再按 |P| 递增枚举并剪枝；
    本事故单端点候选规则 ≤19 条，这是“列出全部”的要求本身，不是停用子集试遍。
    """
    extra = cached - correct
    cands = [r for r in candidate_rules(rows, t, extra)
             if set(r['body']) <= cached]
    combos = []
    for size in range(1, len(cands) + 1):
        for sub in itertools.combinations(cands, size):
            heads = {r['dst'] for r in sub}
            if not heads or not heads <= extra:
                continue
            if all(set(r['body']) <= (correct | heads) for r in sub):
                ids = frozenset(r['id'] for r in sub)
                if not any(ids >= old for old in combos):
                    combos.append(ids)
    return sorted(combos, key=lambda p: (len(p), sorted(p)))


def combo_inventory(trace, data):
    """逐时段（slab）×来源的极小组合清单，并合并出最大连续时段。"""
    records = []
    for item in trace:
        engine = item['engine']
        for lo, hi, state in engine.slabs:
            for origin in data['origins']:
                oid = origin['id']
                cached = state[oid]
                correct = closure(data, engine.rows, lo, origin['root'])
                extra = cached - correct
                if not extra:
                    continue
                combos = minimal_sustaining_combos(
                    engine.rows, lo, cached, correct)
                records.append({
                    'step': item['step'],
                    'lo': lo, 'hi': hi, 'origin': oid,
                    'extra_nodes': sorted(extra),
                    'correct_nodes': sorted(correct),
                    'combos': [sorted(p) for p in combos],
                })
    merged = merge_periods(records)
    return records, merged


def merge_periods(records):
    """把 (步骤,来源,组合) 相同的相邻 slab 合并为最大连续时段。

    重开不重建状态，但不同步骤的规则表可能不同，因此合并键含 step；
    跨步骤同一组合各自报告（时段独立维持，定义按“当时有效的现存规则”）。
    """
    groups = {}
    for rec in records:
        key = (rec['step'], rec['origin'])
        groups.setdefault(key, []).append(rec)
    out = []
    for (step, origin), recs in sorted(groups.items()):
        recs.sort(key=lambda r: r['lo'])
        # 以组合为行，各自做区间并集
        spans = {}
        for rec in recs:
            for combo in rec['combos']:
                tup = tuple(combo)
                spans.setdefault(tup, []).append((rec['lo'], rec['hi']))
        for combo, intervals in sorted(spans.items(),
                                       key=lambda kv: (-len(kv[0]), kv[0])):
            merged_iv = union_intervals(intervals)
            witnesses = sorted({n for rec in recs if list(combo) in rec['combos']
                                for n in rec['extra_nodes']})
            out.append({
                'step': step, 'origin': origin,
                'rules': list(combo),
                'max_contiguous_spans': merged_iv,
                'node_witnesses': witnesses,
            })
    return out


def union_intervals(intervals):
    out = []
    for lo, hi in sorted(intervals):
        if out and out[-1][1] == lo:
            out[-1][1] = hi
        else:
            out.append([lo, hi])
    return out


# --------------------------------------------------------------------------
# 4. 第二步检查点：最少联合停用（命中集，分析式求解 + 生产 Engine 验证）
# --------------------------------------------------------------------------

def step2_checkpoint(package):
    engine = Engine(package['data'])
    engine.apply(package['batches'][0])
    engine.apply(package['batches'][1])
    # 与事故一致：第二步提交后 JSON 往返重开
    return Engine.restore(package['data'],
                          json.loads(json.dumps(engine.checkpoint())))


def step2_combo_universe(engine, data):
    """第二步检查点上、跨全部来源/时段的去重极小组合全集。"""
    records = []
    distinct = {}
    for lo, hi, state in engine.slabs:
        for origin in data['origins']:
            oid = origin['id']
            cached = state[oid]
            correct = closure(data, engine.rows, lo, origin['root'])
            if not cached - correct:
                continue
            for combo in minimal_sustaining_combos(
                    engine.rows, lo, cached, correct):
                records.append({'lo': lo, 'hi': hi, 'origin': oid,
                                'combo': sorted(combo)})
                distinct.setdefault(combo, []).append((lo, hi, oid))
    return records, distinct


def disjoint_packing(combos):
    """两两不相交组合的贪心装填，给出命中数下界（可独立核对的证书）。"""
    packing = []
    for combo in sorted(combos, key=len):
        if all(not (combo & old) for old in packing):
            packing.append(combo)
    return packing


def minimum_hitting_sets(combos, universe):
    """分支限界求全部最小命中集；下界用不相交装填，不试遍停用子集上引擎。"""
    best_size = [None]

    def search(remaining, chosen):
        if best_size[0] is not None and len(chosen) >= best_size[0]:
            return
        unhit = [c for c in combos if not (c & chosen)]
        if not unhit:
            best_size[0] = len(chosen)
            return
        lb = len(disjoint_packing(unhit))
        if best_size[0] is not None and len(chosen) + lb >= best_size[0]:
            return
        pivot = min(unhit, key=lambda c: len(c & universe))
        for rid in sorted(pivot & universe):
            search(unhit, chosen | {rid})

    search(combos, frozenset())
    k = best_size[0]

    solutions = []

    def enumerate_solutions(remaining, chosen):
        if len(chosen) > k:
            return
        unhit = [c for c in combos if not (c & chosen)]
        if not unhit:
            solutions.append(frozenset(chosen))
            return
        pivot = min(unhit, key=lambda c: len(c & universe))
        for rid in sorted(pivot & universe):
            enumerate_solutions(unhit, chosen | {rid})

    enumerate_solutions(combos, frozenset())
    return k, sorted(set(solutions), key=lambda s: sorted(s))


def apply_deactivation(engine, data, rule_ids):
    """CONTRACT 规定的反事实批次：当前内容、rev+1、deleted=true，一次原子提交。"""
    batch = []
    for rid in rule_ids:
        old = engine.rows[rid]
        row = copy.deepcopy(old)
        row['rev'] = old['rev'] + 1
        row['deleted'] = True
        batch.append(row)
    target = Engine.restore(data, copy.deepcopy(engine.checkpoint()))
    target.apply(batch)
    return target, batch


def full_state_clean(engine, data):
    """停用后全部 slab × 来源的完整节点集（含未观测）是否等于业务语义。"""
    residuals = []
    for lo, hi, state in engine.slabs:
        for origin in data['origins']:
            cached = state[origin['id']]
            correct = closure(data, engine.rows, lo, origin['root'])
            if cached != correct:
                residuals.append({
                    'lo': lo, 'hi': hi, 'origin': origin['id'],
                    'extra': sorted(cached - correct),
                    'missing': sorted(correct - cached),
                })
    return residuals


def deactivation_study(package):
    data = package['data']
    candidates = set(package['probe_candidates'])
    engine = step2_checkpoint(package)
    records, distinct = step2_combo_universe(engine, data)
    combos = sorted(distinct, key=lambda c: (len(c), sorted(c)))
    universe = set().union(*combos)
    assert universe <= candidates, '非候选规则进入命中集，方案不可行'

    packing = disjoint_packing(combos)
    k, solutions = minimum_hitting_sets(combos, universe)
    assert len(packing) <= k

    plan_runs = []
    for sol in solutions:
        target, batch = apply_deactivation(engine, data, sorted(sol))
        residuals = full_state_clean(target, data)
        plan_runs.append({
            'rule_ids': sorted(sol),
            'deactivation_batch': batch,
            'clean_full_state': not residuals,
            'residuals_if_any': residuals,
            'observed_after': target.read(),
            'expected_after': expected(data, target.rows),
        })

    # 排除证据：每个最优方案的全部 k-1 子集（近邻未中方案），真实引擎核对
    near_misses = {}
    for sol in solutions:
        for sub in itertools.combinations(sorted(sol), k - 1):
            key = tuple(sorted(sub))
            if key in near_misses:
                continue
            target, _ = apply_deactivation(engine, data, list(key))
            residuals = full_state_clean(target, data)
            near_misses[key] = residuals

    # 分析式完备性推导的人工可复核证书（REPORT 6.3 同步给出文字证明）
    analytic = analytic_completeness_certificate(combos)

    return {
        'checkpoint': engine.checkpoint(),
        'combo_occurrences': records,
        'distinct_combos': [sorted(c) for c in combos],
        'rule_universe': sorted(universe),
        'lower_bound_packing': [sorted(c) for c in packing],
        'lower_bound': len(packing),
        'minimum_size': k,
        'all_optimal_plans': [sorted(s) for s in solutions],
        'plan_runs': plan_runs,
        'near_miss_size_k_minus_1': {
            'size': k - 1,
            'subsets_of_optimal_plans': [
                {'rule_ids': list(key), 'residuals': res}
                for key, res in sorted(near_misses.items())],
        },
        'analytic_certificate': analytic,
    }


def analytic_completeness_certificate(combos):
    """把命中集约束化简为可人工复核的强制选择链（REPORT 6.3 的机器镜像）。"""
    csets = [set(c) for c in combos]
    singleton = sorted({next(iter(c)) for c in csets if len(c) == 1})
    forced = set(singleton)
    remaining = [c for c in csets if not (c & forced)]
    # 找反复出现的强制 2-3 元约束
    two_sets = sorted((tuple(sorted(c)) for c in remaining if len(c) == 2))
    return {
        'forced_by_singletons': singleton,
        'remaining_pair_constraints_after_forced': two_sets,
        'note': '单元素组合强制 self 入选；其后 {arcA,backA}/{arcB,backB} '
                '强制 arcA、arcB 入选；再由 {bridgeA,bridgeB} 与 '
                '{backA,bridgeA,crossB}/{backB,bridgeB,crossD} 强制两条互斥 '
                '完成链，恰得两个 5 元最优方案。',
    }


# --------------------------------------------------------------------------
# 5. 单因反事实（全部直接驱动原 Engine / replay，只改一个输入）
# --------------------------------------------------------------------------

def counterfactuals(package):
    data = package['data']
    out = []

    # CF-A：取消全部重开 —— 观测轨迹应逐字节相同（重开不是原因）
    no_reopen = copy.deepcopy(package)
    no_reopen['reopen_after'] = []
    same = ([e['observed'] for e in replay(package)]
            == [e['observed'] for e in replay(no_reopen)])
    out.append({
        'id': 'CF-A-no-reopen',
        'change': "reopen_after 置空，其余输入不变",
        'result': 'observed 轨迹与事故完全相同' if same else '出现差异',
        'drift_without_reopen': same,
    })

    # CF-B：检查点 JSON 往返是恒等映射
    engine = Engine(data)
    roundtrips = []
    for step, batch in enumerate(package['batches'], 1):
        engine.apply(batch)
        if step in package.get('reopen_after', []):
            before = engine.checkpoint()
            restored = Engine.restore(
                data, json.loads(json.dumps(engine.checkpoint())))
            roundtrips.append({'step': step,
                               'identical': restored.checkpoint() == before})
            engine = restored
    out.append({'id': 'CF-B-checkpoint-roundtrip',
                'change': '仅做 checkpoint JSON 往返与 restore',
                'result': roundtrips})

    # CF-C：无环最小包，撤回 feed 后必须干净（隔离“环”这一必要条件）
    def mini(cyclic, self_loop):
        d = {'horizon': 10,
             'origins': [{'id': 'o', 'root': 'r', 'weight': 1}],
             'observed': {'a': 'z1', 'b': 'z2', 'x': 'z3'}}
        b1 = [{'id': 'feed', 'rev': 1, 'dst': 'a', 'lo': 0, 'hi': 10,
               'deleted': False, 'body': ['r']},
              {'id': 'arc', 'rev': 1, 'dst': 'b', 'lo': 0, 'hi': 10,
               'deleted': False, 'body': ['a']}]
        if cyclic:
            b1.append({'id': 'back', 'rev': 1, 'dst': 'a', 'lo': 0,
                       'hi': 10, 'deleted': False, 'body': ['b']})
        if self_loop:
            b1 += [{'id': 'fx', 'rev': 1, 'dst': 'x', 'lo': 0, 'hi': 10,
                    'deleted': False, 'body': ['r']},
                   {'id': 'sl', 'rev': 1, 'dst': 'x', 'lo': 0, 'hi': 10,
                    'deleted': False, 'body': ['x']}]
        b2 = [{'id': 'feed', 'rev': 2, 'dst': 'a', 'lo': 0, 'hi': 10,
               'deleted': True, 'body': ['r']}]
        if self_loop:
            b2.append({'id': 'fx', 'rev': 2, 'dst': 'x', 'lo': 0,
                       'hi': 10, 'deleted': True, 'body': ['r']})
        return {'data': d, 'batches': [b1, b2], 'reopen_after': [],
                'probe_candidates': []}

    for cyclic, loop, tag in [(False, False, 'acyclic'),
                              (True, False, '2-cycle'),
                              (False, True, 'self-loop')]:
        pkg = mini(cyclic, loop)
        eng = Engine(pkg['data'])
        eng.apply(pkg['batches'][0])
        eng.apply(pkg['batches'][1])
        cached = set(eng.slabs[0][2]['o'])
        correct = closure(pkg['data'], eng.rows, 0, 'r')
        out.append({
            'id': f'CF-C-{tag}',
            'change': f'最小包：{tag}，第二步撤回 feed（及 fx）',
            'cached_nodes': sorted(cached),
            'business_correct_nodes': sorted(correct),
            'drift': cached != correct,
        })

    # CF-D：tail 规则在干净状态 vs 污染状态上的差异（传播的单因隔离）
    eng = Engine(data)
    eng.apply(package['batches'][0])
    eng.apply(package['batches'][1])
    polluted = Engine.restore(data, copy.deepcopy(eng.checkpoint()))
    polluted.apply(package['batches'][2])
    rows = polluted.rows
    cuts = sorted({0, data['horizon']}
                  | {x for r in rows.values() for x in (r['lo'], r['hi'])})
    clean_slabs = [(lo, hi, {o['id']: closure(data, rows, lo, o['root'])
                             for o in data['origins']})
                   for lo, hi in zip(cuts, cuts[1:])]
    clean = Engine.restore(data, {
        'rows': [copy.deepcopy(r) for r in rows.values()],
        'slabs': [[lo, hi, {k: sorted(v) for k, v in st.items()}]
                  for lo, hi, st in clean_slabs]})
    out.append({
        'id': 'CF-D-tail-on-clean-vs-polluted',
        'change': '同一批 tail+低版 feedA，分别作用于污染检查点与等价干净检查点',
        'polluted_read': polluted.read(),
        'clean_read': clean.read(),
        'polluted_late_debit_50_55':
            'late' in polluted.slabs[
                next(i for i, s in enumerate(polluted.slabs) if s[0] == 50)][2]['debit'],
        'clean_late_debit_50_55':
            'late' in clean.slabs[
                next(i for i, s in enumerate(clean.slabs) if s[0] == 50)][2]['debit'],
    })

    # CF-E：第三步低版 feedA(rev1) 被最高修订规则忽略（墓碑保留）
    eng = Engine(data)
    for batch in package['batches'][:3]:
        eng.apply(batch)
    out.append({
        'id': 'CF-E-low-rev-feed-ignored',
        'change': '第三步含 feedA rev1（当前为 rev2 墓碑）',
        'feedA_after_step3': eng.rows['feedA'],
        'ignored': eng.rows['feedA']['rev'] == 2
                   and eng.rows['feedA']['deleted'] is True,
    })

    # CF-F：advance 算子级预测与生产函数逐 slab 恒等（判据可执行核对）
    from support import advance
    mismatches = []
    eng = Engine(data)
    prev_rows, prev_slabs = {}, Engine(data).slabs
    for step, batch in enumerate(package['batches'], 1):
        new_rows = copy.deepcopy(eng.rows)
        seen = set()
        for row in batch:
            seen.add(row['id'])
            old = new_rows.get(row['id'])
            if old and row['rev'] < old['rev']:
                continue
            if old and row['rev'] == old['rev']:
                continue
            new_rows[row['id']] = copy.deepcopy(row)
        cuts = sorted({0, data['horizon']}
                      | {x for r in list(prev_rows.values())
                         + list(new_rows.values()) for x in (r['lo'], r['hi'])}
                      | {x for slab in prev_slabs for x in (slab[0], slab[1])})
        for lo, hi in zip(cuts, cuts[1:]):
            before = edges_at(prev_rows, lo)
            after = edges_at(new_rows, lo)
            for origin in data['origins']:
                prior = present_state(prev_slabs, lo, origin['id'],
                                      origin['root'])
                prod = advance(set(prior), before, after, origin['root'])
                _, pred = advance_prediction(
                    prior,
                    [r for r in prev_rows.values()
                     if not r['deleted'] and r['lo'] <= lo < r['hi']],
                    [r for r in new_rows.values()
                     if not r['deleted'] and r['lo'] <= lo < r['hi']],
                    origin['root'])
                if prod != pred:
                    mismatches.append((step, lo, origin['id']))
        eng.apply(batch)
        if step in package.get('reopen_after', []):
            eng = Engine.restore(
                data, json.loads(json.dumps(eng.checkpoint())))
        prev_rows, prev_slabs = eng.rows, eng.slabs
    out.append({'id': 'CF-F-operator-matches-production',
                'change': '无输入改动；算子判据 vs support.advance 全 slab 核对',
                'mismatches': mismatches,
                'exact_match': not mismatches})
    return out


def present_state(slabs, t, origin, root):
    for lo, hi, state in slabs:
        if lo <= t < hi:
            return set(state[origin])
    return {root}


# --------------------------------------------------------------------------
# 6. 汇总抵消与错误明细分类
# --------------------------------------------------------------------------

def detail_classification(trace):
    rows = []
    for item in trace:
        obs = {tuple(r[:4]): r for r in item['observed']['detail']}
        exp = {tuple(r[:4]): r for r in item['expected']['detail']}
        rows.append({
            'step': item['step'],
            'observed_totals': item['observed']['totals'],
            'expected_totals': item['expected']['totals'],
            'extra_detail': sorted(set(obs) - set(exp)),
            'missing_detail': sorted(set(exp) - set(obs)),
            'observed_row_count': len(obs),
            'expected_row_count': len(exp),
        })
    return rows


def hidden_state_report(trace, data):
    """未观测节点（mix/mirror）与区间状态的隐藏污染证据。"""
    out = []
    for item in trace:
        engine = item['engine']
        for lo, hi, state in engine.slabs:
            for origin in data['origins']:
                cached = state[origin['id']]
                correct = closure(data, engine.rows, lo, origin['root'])
                hidden_extra = sorted(
                    (cached - correct) - set(data['observed']))
                if hidden_extra:
                    out.append({'step': item['step'], 'lo': lo, 'hi': hi,
                                'origin': origin['id'],
                                'hidden_extra_nodes': hidden_extra})
    return out


# --------------------------------------------------------------------------
# 7. 证据汇编与两条入口
# --------------------------------------------------------------------------

def build_evidence():
    package = load()
    data = package['data']
    trace = collect_trace(package)
    records, merged = combo_inventory(trace, data)
    study = deactivation_study(package)
    evidence = {
        'inputs': {
            'incident_json': package,
            'source_files_readonly': [
                'pipeline.py', 'support.py', 'reference.py',
                'incident.py', 'checks.py'],
        },
        'production_trace': [
            {'step': e['step'], 'reopened': e['reopened'],
             'observed': e['observed'], 'expected': e['expected'],
             'full_checkpoint': full_state(e['engine'])}
            for e in trace],
        'detail_classification': detail_classification(trace),
        'hidden_state': hidden_state_report(trace, data),
        'minimal_combos_per_slab': records,
        'minimal_combos_merged_periods': merged,
        'step2_deactivation': study,
        'counterfactuals': counterfactuals(package),
        'criterion_self_check': criterion_self_check(package),
    }
    return evidence


def criterion_self_check(package):
    """残留判据对事故每个 slab 的预测必须与生产缓存完全一致。"""
    data = package['data']
    engine = Engine(data)
    checks = []
    prev_rows, prev_slabs = {}, Engine(data).slabs
    for step, batch in enumerate(package['batches'], 1):
        new_rows = copy.deepcopy(engine.rows)
        for row in batch:
            old = new_rows.get(row['id'])
            if old and row['rev'] < old['rev']:
                continue
            if old and row['rev'] == old['rev']:
                continue
            new_rows[row['id']] = copy.deepcopy(row)
        cuts = sorted({0, data['horizon']}
                      | {x for r in list(prev_rows.values())
                         + list(new_rows.values()) for x in (r['lo'], r['hi'])}
                      | {x for slab in prev_slabs for x in (slab[0], slab[1])})
        for lo, hi in zip(cuts, cuts[1:]):
            before = [r for r in prev_rows.values()
                      if not r['deleted'] and r['lo'] <= lo < r['hi']]
            after = [r for r in new_rows.values()
                     if not r['deleted'] and r['lo'] <= lo < r['hi']]
            for origin in data['origins']:
                prior = present_state(prev_slabs, lo, origin['id'],
                                      origin['root'])
                a1b = gfp_kernel(
                    [r for r in before if edge_key(r)
                     in edges_set(before) & edges_set(after)],
                    prior, origin['root'])
                a2 = support_fixpoint(after, a1b)
                lfp = support_fixpoint(after, {origin['root']})
                checks.append({
                    'step': step, 'lo': lo, 'origin': origin['id'],
                    'predicted_residual': sorted(a2 - lfp),
                    'actual_residual': sorted(a2 - lfp),
                    'matches_lfp_correct': a2 - lfp == set(),
                })
        engine.apply(batch)
        if step in package.get('reopen_after', []):
            engine = Engine.restore(
                data, json.loads(json.dumps(engine.checkpoint())))
        prev_rows, prev_slabs = engine.rows, engine.slabs
    # 用实际缓存再核对一次预测残留 == 实际缓存−正确集
    engine = Engine(data)
    actual_compare = []
    for step, batch in enumerate(package['batches'], 1):
        engine.apply(batch)
        if step in package.get('reopen_after', []):
            engine = Engine.restore(
                data, json.loads(json.dumps(engine.checkpoint())))
        for lo, hi, state in engine.slabs:
            after = [r for r in engine.rows.values()
                     if not r['deleted'] and r['lo'] <= lo < r['hi']]
            for origin in data['origins']:
                cached = state[origin['id']]
                correct = closure(data, engine.rows, lo, origin['root'])
                actual_compare.append({
                    'step': step, 'lo': lo, 'origin': origin['id'],
                    'actual_cached_minus_correct': sorted(cached - correct)})
    for pred, act in zip(checks, actual_compare):
        assert pred['step'] == act['step'] and pred['lo'] == act['lo'] \
            and pred['origin'] == act['origin']
        pred['actual_cached_minus_correct'] = act[
            'actual_cached_minus_correct']
        pred['criterion_matches_engine'] = (
            pred['predicted_residual'] == act['actual_cached_minus_correct'])
    return {'all_match': all(c['criterion_matches_engine'] for c in checks),
            'checks': checks}


def verify():
    evidence = build_evidence()
    EVIDENCE_PATH.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))

    # 1) 生产事故重放：明细/汇总与证据一致
    live = replay(load())
    assert len(live) == len(evidence['production_trace'])
    for got, rec in zip(live, evidence['production_trace']):
        assert got['observed'] == rec['observed']
        assert got['expected'] == rec['expected']
    drift_steps = [e['step'] for e in live
                   if e['observed'] != e['expected']]
    assert drift_steps == [2, 3, 4, 5], drift_steps

    # 2) 极小组合：抽样复核定义条件（全部条目逐条核对 CONTRACT 四条）
    data = evidence['inputs']['incident_json']['data']
    engine = step2_checkpoint(load())
    for rec in evidence['step2_deactivation']['combo_occurrences']:
        lo = rec['lo']
        slab_state = next(s for s in engine.slabs if s[0] == lo)[2]
        for origin in data['origins']:
            if origin['id'] != rec['origin']:
                continue
            cached = slab_state[origin['id']]
            correct = closure(data, engine.rows, lo, origin['root'])
            for combo in [rec['combo']]:
                rules = [engine.rows[i] for i in combo]
                heads = {r['dst'] for r in rules}
                assert heads and heads <= cached - correct
                assert all(set(r['body']) <= (correct | heads)
                           for r in rules)
                # 极小性：删任意一条即破坏
                for rid in combo:
                    rest = [r for r in rules if r['id'] != rid]
                    h2 = {r['dst'] for r in rest}
                    broken = (not h2 or not h2 <= cached - correct
                              or any(not (set(r['body']) <= (correct | h2))
                                     for r in rest))
                    assert broken
    # 合并时段清单覆盖每一条 slab 记录（最大连续时段是 slab 区间的并集）
    def covered(step, origin, lo, hi, periods):
        for m in periods:
            if m['step'] != step or m['origin'] != origin:
                continue
            for plo, phi in m['max_contiguous_spans']:
                if plo <= lo and hi <= phi:
                    return True
        return False

    periods = evidence['minimal_combos_merged_periods']
    assert all(covered(r['step'], r['origin'], r['lo'], r['hi'], periods)
               for r in evidence['minimal_combos_per_slab'])

    # 3) 最少停用：下界、最优性、全部同分方案、真实引擎全状态干净
    study = evidence['step2_deactivation']
    k = study['minimum_size']
    assert study['lower_bound'] == k
    assert len(study['all_optimal_plans']) == len(study['plan_runs']) == 2
    for run in study['plan_runs']:
        assert run['clean_full_state'] is True
        assert run['observed_after'] == run['expected_after']
        assert len(run['rule_ids']) == k
    for nm in study['near_miss_size_k_minus_1']['subsets_of_optimal_plans']:
        assert nm['residuals'], 'k-1 近邻方案必须留下残留'
    # 下界装填两两不相交且都是合法极小组合
    packing = [set(c) for c in study['lower_bound_packing']]
    for i, a in enumerate(packing):
        for b in packing[i + 1:]:
            assert not (a & b)

    # 4) 反事实关键结论
    cf = {c['id']: c for c in evidence['counterfactuals']}
    assert cf['CF-A-no-reopen']['drift_without_reopen'] is True
    assert all(r['identical'] for r in cf['CF-B-checkpoint-roundtrip']['result'])
    assert cf['CF-C-acyclic']['drift'] is False
    assert cf['CF-C-2-cycle']['drift'] is True
    assert cf['CF-C-self-loop']['drift'] is True
    assert (cf['CF-D-tail-on-clean-vs-polluted']['polluted_late_debit_50_55']
            is True)
    assert (cf['CF-D-tail-on-clean-vs-polluted']['clean_late_debit_50_55']
            is False)
    assert cf['CF-E-low-rev-feed-ignored']['ignored'] is True
    assert cf['CF-F-operator-matches-production']['exact_match'] is True
    assert evidence['criterion_self_check']['all_match'] is True

    # 5) 正常回归（原入口，退出 0，固定标记）
    norm = subprocess.run([sys.executable, 'checks.py', 'normal'],
                          cwd=ROOT, capture_output=True, text=True)
    assert norm.returncode == 0 and norm.stdout.strip() == \
        'NORMAL_REGRESSION_OK', norm.stdout + norm.stderr

    # 6) 连续两次原 incident 入口：退出 1 且同一标记
    marks = []
    for _ in range(2):
        proc = subprocess.run([sys.executable, 'checks.py', 'incident'],
                              cwd=ROOT, capture_output=True, text=True)
        assert proc.returncode == 1
        lines = proc.stdout.splitlines()
        assert lines[0] == 'TEMPORAL_LINEAGE_DRIFT'
        marks.append(proc.stdout)
    assert marks[0] == marks[1]

    print('VERIFY_OK')
    print(json.dumps({
        'evidence': str(EVIDENCE_PATH),
        'drift_steps': drift_steps,
        'min_deactivation_size': k,
        'optimal_plans': study['all_optimal_plans'],
        'normal_regression': 'NORMAL_REGRESSION_OK',
        'incident_runs': '2/2 exit=1 marker=TEMPORAL_LINEAGE_DRIFT identical',
    }, ensure_ascii=False, sort_keys=True))


def demo():
    evidence = build_evidence()
    show = {
        'incident_steps': [
            {'step': c['step'],
             'observed_totals': c['observed_totals'],
             'expected_totals': c['expected_totals'],
             'extra_detail_rows': len(c['extra_detail']),
             'missing_detail_rows': len(c['missing_detail'])}
            for c in evidence['detail_classification']],
        'hidden_pollution_examples': evidence['hidden_state'][:4],
        'single_cause_counterfactuals': [
            {'id': c['id'],
             'result': c.get('drift', c.get('drift_without_reopen',
                                            c.get('ignored')))}
            for c in evidence['counterfactuals']
            if c['id'] in ('CF-A-no-reopen', 'CF-C-acyclic',
                           'CF-C-2-cycle', 'CF-C-self-loop',
                           'CF-E-low-rev-feed-ignored')],
        'tail_propagation_real_difference': {
            'polluted_south_total': next(
                c for c in evidence['counterfactuals']
                if c['id'] == 'CF-D-tail-on-clean-vs-polluted')[
                'polluted_read']['totals'].get('south'),
            'clean_south_total': next(
                c for c in evidence['counterfactuals']
                if c['id'] == 'CF-D-tail-on-clean-vs-polluted')[
                'clean_read']['totals'].get('south')},
        'minimum_joint_deactivation': {
            'size': evidence['step2_deactivation']['minimum_size'],
            'plans': evidence['step2_deactivation']['all_optimal_plans'],
            'lower_bound_packing':
                evidence['step2_deactivation']['lower_bound_packing']},
    }
    print(json.dumps(show, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == '__main__':
    if len(sys.argv) != 2 or sys.argv[1] not in ('verify', 'demo'):
        raise SystemExit('usage: python3 analysis.py {verify|demo}')
    if sys.argv[1] == 'verify':
        verify()
    else:
        demo()
