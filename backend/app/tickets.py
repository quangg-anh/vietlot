"""Ticket-set construction for pick-k games.

``sample`` reproduces the historical independent sampler (weighted draws without
replacement, de-duplicated), so old payloads and reference tickets stay
reproducible and log-loss/ticket marginals remain comparable.

``portfolio`` optimises the *set* rather than each ticket separately. For a fixed
number of tickets, spreading the numbers raises the chance that at least one
ticket reaches a low match tier, because overlapping tickets cover the same
outcomes twice. Greedy maximum coverage is evaluated over draws simulated from the
model's own probabilities — the only distribution this project may assume.

So the improvement it reports is conditional on the model: "this set covers the
model's simulated outcomes better than independent sampling", never "this set is
more likely to win". Tiers are structural (``pick_count - 3`` and ``pick_count -
2``), not payout rules; no prize table is configured for these games.
"""

import random
from statistics import mean


SIMULATIONS = 300
POOL_MINIMUM = 48
POOL_MAXIMUM = 200


def sample(probabilities, spec, count, seed):
    generator = random.Random(seed)
    width = spec["pick_count"]
    tickets, seen, attempts = [], set(), 0
    while len(tickets) < count and attempts < count * 200:
        attempts += 1
        pool = list(range(1, spec["number_max"] + 1))
        chosen = []
        for _ in range(width):
            number = generator.choices(pool, weights=[max(probabilities[item - 1], 1e-12) for item in pool])[0]
            chosen.append(number)
            pool.remove(number)
        key = tuple(sorted(chosen))
        if key in seen:
            continue
        seen.add(key)
        tickets.append({"main": sorted(chosen), "special": _special(generator, spec, chosen)})
    return tickets


def _special(generator, spec, chosen):
    if not spec.get("special_max"):
        return None
    choices = list(range(1, spec["special_max"] + 1))
    if spec.get("special_excludes_main"):
        choices = [number for number in choices if number not in chosen]
    return generator.choice(choices) if choices else None


def tiers(spec):
    """Match tiers used to judge a ticket set; structural, not prize rules."""
    width = spec["pick_count"]
    return sorted({max(2, width - 3), max(2, width - 2)})


def _hits(candidate, draws):
    picks = set(candidate)
    return [len(picks & draw) for draw in draws]


def _candidates(probabilities, spec, generator, size):
    pool, seen, guard = [], set(), 0
    while len(pool) < size and guard < size * 60:
        guard += 1
        chosen = generator.choices(range(1, spec["number_max"] + 1),
                                   weights=[max(value, 1e-12) for value in probabilities], k=spec["pick_count"])
        if len(set(chosen)) < spec["pick_count"]:
            continue  # a real draw never repeats a number inside one ticket
        key = tuple(sorted(chosen))
        if key in seen:
            continue
        seen.add(key)
        pool.append(list(key))
    return pool


def portfolio(probabilities, spec, count, seed, simulations=SIMULATIONS):
    """Greedy maximum-coverage ticket set under the model's own distribution."""
    if not isinstance(count, int) or not 1 <= count <= 100:
        raise ValueError("Ticket count must be 1..100")
    generator = random.Random(seed)
    pool_size = min(POOL_MAXIMUM, max(POOL_MINIMUM, count * 10))
    candidates = _candidates(probabilities, spec, generator, pool_size)
    if not candidates:
        return sample(probabilities, spec, count, seed)
    draws = [set(generator.choices(range(1, spec["number_max"] + 1),
                                   weights=[max(value, 1e-12) for value in probabilities],
                                   k=spec["pick_count"])) for _ in range(simulations)]
    matrix = [_hits(candidate, draws) for candidate in candidates]
    threshold = tiers(spec)[0]
    best = [0] * simulations
    chosen, used = [], set()
    for _ in range(min(count, len(candidates))):
        pick, pick_value = None, None
        for position in range(len(candidates)):
            if position in used:
                continue
            value = sum(1 for index, hit in enumerate(matrix[position]) if max(hit, best[index]) >= threshold)
            if pick_value is None or value > pick_value:
                pick, pick_value = position, value
        if pick is None:
            break
        used.add(pick)
        best = [max(hit, best[index]) for index, hit in enumerate(matrix[pick])]
        chosen.append({"main": list(candidates[pick]),
                       "special": _special(generator, spec, candidates[pick])})
    return chosen


def coverage(probabilities, spec, tickets, seed=42, simulations=400):
    """Estimated set-level coverage under the model's simulated outcomes."""
    if not tickets:
        return None
    generator = random.Random(seed)
    draws = [set(generator.choices(range(1, spec["number_max"] + 1),
                                   weights=[max(value, 1e-12) for value in probabilities],
                                   k=spec["pick_count"])) for _ in range(simulations)]
    level = tiers(spec)[0]
    hits = [_hits(ticket["main"], draws) for ticket in tickets]
    best = [max(row[index] for row in hits) for index in range(simulations)]
    distinct = len({number for ticket in tickets for number in ticket["main"]})
    return {"tickets": len(tickets), "tier": level,
            "coverage": sum(1 for value in best if value >= level) / simulations,
            "mean_best_hits": mean(best), "distinct_numbers": distinct,
            "overlap": 1 - distinct / (len(tickets) * spec["pick_count"]),
            "simulations": simulations,
            "scope": "model_simulated_outcomes; not_win_probability"}


def compare(probabilities, spec, count, seed, simulations=400):
    """Portfolio versus independent sampling on shared simulated outcomes."""
    independent = sample(probabilities, spec, count, seed + 1)
    greedy = portfolio(probabilities, spec, count, seed, simulations=min(simulations, SIMULATIONS))
    shared = seed + 2
    return {"independent": coverage(probabilities, spec, independent, shared, simulations),
            "portfolio": coverage(probabilities, spec, greedy, shared, simulations)}


def efficiency(probabilities, spec, tickets):
    """Share of the model's top-ranked numbers the set actually covers."""
    if not tickets:
        return None
    order = sorted(range(1, spec["number_max"] + 1), key=lambda number: (-probabilities[number - 1], number))
    top = set(order[:spec["pick_count"]])
    covered = {number for ticket in tickets for number in ticket["main"]}
    return {"top_pick_coverage": len(top & covered) / len(top),
            "ticket_marginal_mean": sum(probabilities[number - 1] for ticket in tickets
                                        for number in ticket["main"]) / (len(tickets) * spec["pick_count"]),
            "uniform_marginal": spec["pick_count"] / spec["number_max"]}