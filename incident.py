import json
from pathlib import Path
from pipeline import Engine
from reference import expected


def load():
    return json.loads(Path(__file__).with_name('incident.json').read_text())


def replay(package):
    engine = Engine(package['data'])
    trace = []
    for step, batch in enumerate(package['batches'], 1):
        observed = engine.apply(batch)
        if step in package.get('reopen_after', []):
            engine = Engine.restore(package['data'],
                                    json.loads(json.dumps(engine.checkpoint())))
        trace.append({'step': step, 'observed': engine.read(),
                      'expected': expected(package['data'], engine.rows)})
    return trace


if __name__ == '__main__':
    print(json.dumps(replay(load()), sort_keys=True, ensure_ascii=False))
