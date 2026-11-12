from collections import Counter, defaultdict, deque


def advance(active, before, after, root):
    active = set(active)
    active.add(root)
    kept = before & after
    uses = defaultdict(set)
    enabled = set()
    count = Counter({root: 1})
    for rule in kept:
        _, body, head = rule
        for node in body:
            uses[node].add(rule)
        if all(node in active for node in body):
            enabled.add(rule)
            count[head] += 1
    pending = deque(node for node in active if not count[node])
    while pending:
        node = pending.popleft()
        if node not in active or count[node]:
            continue
        active.remove(node)
        for rule in uses[node]:
            if rule in enabled:
                enabled.remove(rule)
                head = rule[2]
                count[head] -= 1
                if head in active and not count[head]:
                    pending.append(head)
    uses = defaultdict(set)
    enabled = set()
    count = Counter({root: 1})
    for rule in after:
        _, body, head = rule
        for node in body:
            uses[node].add(rule)
        if all(node in active for node in body):
            enabled.add(rule)
            count[head] += 1
    pending = deque(node for node in count if count[node] and node not in active)
    while pending:
        node = pending.popleft()
        if node in active or not count[node]:
            continue
        active.add(node)
        for rule in uses[node]:
            if rule not in enabled and all(n in active for n in rule[1]):
                enabled.add(rule)
                head = rule[2]
                count[head] += 1
                if head not in active:
                    pending.append(head)
    return active
