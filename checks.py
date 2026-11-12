import copy
import json
import sys
from incident import load, replay
from pipeline import Engine
from reference import expected


def normal():
    package = load()
    engine = Engine(package['data'])
    first = package['batches'][0]
    engine.apply(first)
    assert engine.read() == expected(package['data'], engine.rows)
    saved = engine.checkpoint()
    engine.apply(first)
    assert engine.checkpoint() == saved
    newer = copy.deepcopy(first[0])
    newer['rev'] = 2
    engine.apply([newer])
    engine.apply([first[0]])
    saved = engine.checkpoint()
    assert engine.read() == expected(package['data'], engine.rows)
    conflict = copy.deepcopy(first[0])
    conflict['rev'] = 2
    conflict['dst'] = 'elsewhere'
    try:
        engine.apply([conflict])
    except ValueError as exc:
        assert str(exc) == 'REVISION_CONFLICT'
    else:
        raise AssertionError('conflict accepted')
    assert engine.checkpoint() == saved
    acyclic = copy.deepcopy(package)
    acyclic['batches'][0] = [
        r for r in first if r['id'] not in ('backA', 'backB', 'returnA', 'returnB', 'self', 'bridgeB',
                                          'crossA', 'crossB', 'crossC', 'crossD')]
    for event in replay(acyclic):
        assert event['observed'] == event['expected']
    print('NORMAL_REGRESSION_OK')


def incident():
    trace = replay(load())
    failures = [e for e in trace if e['observed'] != e['expected']]
    if failures:
        print('TEMPORAL_LINEAGE_DRIFT')
        print(json.dumps([{
            'step': e['step'],
            'observed_totals': e['observed']['totals'],
            'expected_totals': e['expected']['totals'],
            'observed_rows': len(e['observed']['detail']),
            'expected_rows': len(e['expected']['detail'])
        } for e in trace], sort_keys=True))
        return 1
    print('INCIDENT_MATCHED')
    return 0


if __name__ == '__main__':
    command = sys.argv[1]
    if command in ('normal', 'verify'):
        normal()
    elif command == 'incident':
        raise SystemExit(incident())
    elif command == 'demo':
        print(json.dumps(replay(load()), sort_keys=True))
    else:
        raise SystemExit('unknown command')
