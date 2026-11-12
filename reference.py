def closure(data, rows, t, root):
    reached = {root}
    rules = [r for r in rows.values()
             if not r['deleted'] and r['lo'] <= t < r['hi']]
    while True:
        new = reached | {r['dst'] for r in rules
                         if set(r['body']) <= reached}
        if new == reached:
            return reached
        reached = new


def expected(data, rows):
    cuts = sorted({0, data['horizon']} |
                  {r[k] for r in rows.values() for k in ('lo', 'hi')})
    pieces = {}
    for lo, hi in zip(cuts, cuts[1:]):
        for origin in data['origins']:
            reached = closure(data, rows, lo, origin['root'])
            for node, zone in data['observed'].items():
                if node in reached:
                    key = (origin['id'], node, origin['weight'], zone)
                    spans = pieces.setdefault(key, [])
                    if spans and spans[-1][1] == lo:
                        spans[-1][1] = hi
                    else:
                        spans.append([lo, hi])
    detail = sorted([origin, node, lo, hi, weight, zone]
                    for (origin, node, weight, zone), spans in pieces.items()
                    for lo, hi in spans)
    totals = {}
    for _, _, lo, hi, weight, zone in detail:
        totals[zone] = totals.get(zone, 0) + (hi - lo) * weight
    return {'detail': detail, 'totals': dict(sorted(totals.items()))}
