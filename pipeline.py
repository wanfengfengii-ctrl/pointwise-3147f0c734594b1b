import copy
from support import advance


FIELDS = {'id', 'rev', 'body', 'dst', 'lo', 'hi', 'deleted'}


def edges_at(rows, t):
    return {(r['id'], tuple(sorted(r['body'])), r['dst']) for r in rows.values()
            if not r['deleted'] and r['lo'] <= t < r['hi']}


def present(slabs, t, origin, root):
    for lo, hi, state in slabs:
        if lo <= t < hi:
            return state[origin]
    return {root}


def render(slabs, data):
    detail = []
    for origin in data['origins']:
        for node, zone in sorted(data['observed'].items()):
            spans = []
            for lo, hi, state in slabs:
                if node in state[origin['id']]:
                    if spans and spans[-1][1] == lo:
                        spans[-1][1] = hi
                    else:
                        spans.append([lo, hi])
            for lo, hi in spans:
                detail.append([origin['id'], node, lo, hi,
                               origin['weight'], zone])
    detail.sort()
    totals = {}
    for _, _, lo, hi, weight, zone in detail:
        totals[zone] = totals.get(zone, 0) + (hi - lo) * weight
    return {'detail': detail, 'totals': dict(sorted(totals.items()))}


class Engine:
    def __init__(self, data):
        self.data = copy.deepcopy(data)
        self.rows = {}
        self.slabs = [(0, data['horizon'], {
            o['id']: {o['root']} for o in data['origins']})]

    def apply(self, batch):
        rows = copy.deepcopy(self.rows)
        seen = set()
        for row in batch:
            if set(row) != FIELDS or row['id'] in seen:
                raise ValueError('BAD_BATCH')
            seen.add(row['id'])
            if (not all(isinstance(row[k], str) and row[k]
                        for k in ('id', 'dst'))
                    or not isinstance(row['body'], list) or not row['body']
                    or not all(isinstance(n, str) and n for n in row['body'])
                    or len(set(row['body'])) != len(row['body'])
                    or type(row['rev']) is not int or row['rev'] < 1
                    or type(row['lo']) is not int or type(row['hi']) is not int
                    or not 0 <= row['lo'] < row['hi'] <= self.data['horizon']
                    or type(row['deleted']) is not bool):
                raise ValueError('BAD_ROW')
            old = rows.get(row['id'])
            if old and row['rev'] < old['rev']:
                continue
            if old and row['rev'] == old['rev']:
                if row != old:
                    raise ValueError('REVISION_CONFLICT')
                continue
            rows[row['id']] = copy.deepcopy(row)
        cuts = {0, self.data['horizon']}
        for row in list(self.rows.values()) + list(rows.values()):
            cuts.update((row['lo'], row['hi']))
        for lo, hi, _ in self.slabs:
            cuts.update((lo, hi))
        cuts = sorted(cuts)
        slabs = []
        for lo, hi in zip(cuts, cuts[1:]):
            before, after = edges_at(self.rows, lo), edges_at(rows, lo)
            state = {}
            for origin in self.data['origins']:
                prior = present(self.slabs, lo, origin['id'], origin['root'])
                state[origin['id']] = advance(
                    prior, before, after, origin['root'])
            slabs.append((lo, hi, state))
        self.rows, self.slabs = rows, slabs
        return self.read()

    def read(self):
        return render(self.slabs, self.data)

    def checkpoint(self):
        return {'rows': copy.deepcopy(list(self.rows.values())),
                'slabs': [[lo, hi, {k: sorted(v) for k, v in state.items()}]
                          for lo, hi, state in self.slabs]}

    @classmethod
    def restore(cls, data, checkpoint):
        engine = cls(data)
        engine.rows = {r['id']: copy.deepcopy(r) for r in checkpoint['rows']}
        engine.slabs = [(lo, hi, {k: set(v) for k, v in state.items()})
                        for lo, hi, state in checkpoint['slabs']]
        return engine
