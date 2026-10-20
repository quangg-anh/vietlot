import math
import random
from statistics import mean


REVISION = "digits-bayesian-v1"
DOMAIN = 1000
CANDIDATES = (
    {"name": "uniform", "kind": "uniform", "window": 300, "strength": 200},
    {"name": "frequency_60", "kind": "frequency", "window": 60, "strength": 200},
    {"name": "frequency_300", "kind": "frequency", "window": 300, "strength": 200},
    {"name": "position_120", "kind": "position", "window": 120, "strength": 100},
)


def target_group(spec):
    return "special" if "special" in spec["digit_groups"] else "first"


def validate_rows(rows, spec):
    previous = None
    for row in rows:
        identity = (row["draw_date"], row["draw_slot"], row["draw_id"])
        if previous is not None and identity <= previous:
            raise ValueError("Digit training rows must be strictly chronological")
        previous = identity
        if set(row["digits"]) != set(spec["digit_groups"]):
            raise ValueError("Digit training prize groups do not match configuration")
        for group, count in spec["digit_groups"].items():
            values = row["digits"][group]
            if len(values) != count or any(not isinstance(value, str) or len(value) != 3
                                          or any(character not in "0123456789" for character in value)
                                          for value in values):
                raise ValueError("Digit training requires complete three-digit prize values")


def fit(rows, spec, config):
    result = {}
    for group in spec["digit_groups"]:
        values = [value for row in rows[-config["window"]:] for value in row["digits"][group]]
        strength = config["strength"]
        if config["kind"] == "uniform" or not values:
            probability = [1 / DOMAIN] * DOMAIN
        elif config["kind"] == "frequency":
            counts = [0] * DOMAIN
            for value in values:
                counts[int(value)] += 1
            probability = [(count + strength / DOMAIN) / (len(values) + strength) for count in counts]
        else:
            positions = [[0] * 10 for _ in range(3)]
            for value in values:
                for position, character in enumerate(value):
                    positions[position][int(character)] += 1
            probability = [math.prod((positions[position][int(character)] + strength / 10)
                                     / (len(values) + strength)
                                     for position, character in enumerate(f"{number:03d}"))
                           for number in range(DOMAIN)]
        result[group] = probability
    return result


def losses(probability, actual):
    groups = {}
    for group, values in actual.items():
        distribution = probability[group]
        groups[group] = {
            "logloss": -mean(math.log(max(distribution[int(value)], 1e-12)) for value in values),
            "brier": 1 + sum(value * value for value in distribution)
                     - 2 * mean(distribution[int(value)] for value in values),
        }
    return {"logloss": mean(item["logloss"] for item in groups.values()),
            "brier": mean(item["brier"] for item in groups.values()), "groups": groups}


def select_config(rows, spec):
    if len(rows) <= 5:
        return dict(CANDIDATES[0]), []
    start = max(5, len(rows) - 20)
    candidates = []
    for config in CANDIDATES:
        errors = [losses(fit(rows[:index], spec, config), rows[index]["digits"])["logloss"]
                  for index in range(start, len(rows))]
        candidates.append({"config": dict(config), "logloss": mean(errors), "evaluated_draws": len(errors)})
    best = min(candidates, key=lambda item: item["logloss"])
    return best["config"], candidates


def sample_tickets(distributions, spec, count, seed):
    if not isinstance(count, int) or not 1 <= count <= 20:
        raise ValueError("Digit candidate count must be 1..20")
    group = target_group(spec)
    width = 2 if group == "special" else 1
    generator = random.Random(seed)
    tickets, seen = [], set()
    while len(tickets) < count:
        pool = list(range(DOMAIN))
        chosen = []
        for _ in range(width):
            number = generator.choices(pool, weights=[distributions[group][value] for value in pool])[0]
            pool.remove(number)
            chosen.append(f"{number:03d}")
        key = tuple(sorted(chosen))
        if key not in seen:
            seen.add(key)
            tickets.append({"main": [], "special": None, "digits": chosen, "group": group})
    return tickets


def score(distributions, tickets, actual, spec):
    result = losses(distributions, actual)
    result["kind"] = "digits"
    result["target_group"] = target_group(spec)
    result["hits"] = [sum(value in actual[ticket["group"]] for value in ticket["digits"]) for ticket in tickets]
    result["ticket_width"] = 2 if target_group(spec) == "special" else 1
    result["baseline"] = losses(fit([], spec, CANDIDATES[0]), actual)
    result["metric_scope"] = "categorical_per_prize_value; hits_in_target_group; not_payouts"
    return result


def train(rows, spec, seed):
    if not rows:
        raise ValueError("Digit training requires eligible history")
    validate_rows(rows, spec)
    split = max(5, len(rows) - min(20, max(1, len(rows) // 4))) if len(rows) > 10 else len(rows)
    config, candidates = select_config(rows[:split], spec)
    per_draw = []
    group = target_group(spec)
    for index in range(split, len(rows)):
        distribution = fit(rows[:index], spec, config)
        tickets = sample_tickets(distribution, spec, 1, seed + index)
        baseline_tickets = sample_tickets(fit([], spec, CANDIDATES[0]), spec, 1, seed + 100000 + index)
        result = score(distribution, tickets, rows[index]["digits"], spec)
        control = score(fit([], spec, CANDIDATES[0]), baseline_tickets, rows[index]["digits"], spec)
        per_draw.append({"draw_id": rows[index]["draw_id"], "trained_through": rows[index - 1]["draw_id"],
                         "brier": result["brier"], "baseline_brier": result["baseline"]["brier"],
                         "logloss": result["logloss"], "baseline_logloss": result["baseline"]["logloss"],
                         "hits": result["hits"][0], "baseline_hits": control["hits"][0]})
    metrics = None
    if per_draw:
        metrics = {field: mean(row[field] for row in per_draw)
                   for field in ("brier", "baseline_brier", "logloss", "baseline_logloss")}
        metrics.update(evaluated_draws=len(per_draw), per_draw=per_draw, kind="digits",
                       holdout=False, exploratory=True, selection_through=rows[split - 1]["draw_id"],
                       limitations="Rolling temporal evaluation; repeated retuning is not an untouched independent holdout.",
                       conclusion="No established advantage over uniform random selection")
    distribution = fit(rows, spec, config)
    rankings = {name: [{"number": f"{number:03d}", "probability": values[number]}
                       for number in sorted(range(DOMAIN), key=lambda value: (-values[value], value))[:20]]
                for name, values in distribution.items()}
    return {"kind": "digits", "algorithm_revision": REVISION, "config": config,
            "selection": {"candidates": candidates, "trained_through": rows[split - 1]["draw_id"],
                          "metric": "categorical_logloss", "policy": "past_only_temporal_selection"},
            "digit_probabilities": distribution, "rankings": rankings, "target_group": group,
            "tickets": sample_tickets(distribution, spec, 5, seed), "metrics": metrics,
            "window": config["window"], "strength": config["strength"],
            "learning_policy": "refit_and_reselect_on_each_eligible_history_change",
            "probability_scope": "single_prize_value_not_ticket_win_probability"}
