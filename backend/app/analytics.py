import math
import random
from statistics import mean


DISCLAIMER = "Nghien cuu xac suat, khong dam bao trung thuong hay loi nhuan."
METHODS = {"uniform", "bayesian"}


def probabilities(history, spec, method="bayesian"):
    if method not in METHODS:
        raise ValueError("method must be uniform or bayesian")
    if spec["kind"] != "pick_k":
        raise ValueError("Candidate models currently support pick_k games only")
    size, count = spec["number_max"], spec["pick_count"]
    counts = [0] * size
    window = history[-300:]
    for row in window:
        for number in row["main"]:
            counts[number - 1] += 1
    strength = 100
    if method == "uniform" or not window:
        return [count / size] * size
    return [(hits + strength * count / size) / (len(window) + strength) for hits in counts]


def sample_tickets(probability, spec, count, seed):
    generator = random.Random(seed)
    tickets = []
    for _ in range(count):
        pool = list(range(1, spec["number_max"] + 1))
        chosen = []
        for _ in range(spec["pick_count"]):
            number = generator.choices(pool, weights=[probability[item - 1] for item in pool])[0]
            chosen.append(number)
            pool.remove(number)
        special = None
        if spec.get("special_max"):
            choices = list(range(1, spec["special_max"] + 1))
            if spec.get("special_excludes_main"):
                choices = [number for number in choices if number not in chosen]
            special = generator.choice(choices)
        tickets.append({"main": sorted(chosen), "special": special})
    return tickets


def losses(probability, actual):
    observed = set(actual)
    brier, logloss = 0.0, 0.0
    residuals = []
    for number, value in enumerate(probability, 1):
        outcome = int(number in observed)
        bounded = min(max(value, 1e-12), 1 - 1e-12)
        residuals.append(outcome - value)
        brier += (value - outcome) ** 2
        logloss -= outcome * math.log(bounded) + (1 - outcome) * math.log1p(-bounded)
    return {"brier": brier / len(probability), "logloss": logloss / len(probability), "residuals": residuals}


def backtest(rows, spec, min_train=100, test_draws=100, method="bayesian", seed=42):
    if len(rows) <= min_train:
        raise ValueError(f"Need more than {min_train} verified draws; found {len(rows)}")
    start = max(min_train, len(rows) - test_draws)
    per_draw = []
    for index in range(start, len(rows)):
        history, actual = rows[:index], rows[index]
        probability = probabilities(history, spec, method)
        model = sample_tickets(probability, spec, 1, seed + index)[0]["main"]
        baseline = sample_tickets(probabilities([], spec, "uniform"), spec, 1, seed + 100000 + index)[0]["main"]
        metrics = losses(probability, actual["main"])
        control = losses(probabilities([], spec, "uniform"), actual["main"])
        per_draw.append({"draw_id": actual["draw_id"], "hits": len(set(model) & set(actual["main"])),
                         "baseline_hits": len(set(baseline) & set(actual["main"])),
                         "brier": metrics["brier"], "baseline_brier": control["brier"],
                         "logloss": metrics["logloss"], "baseline_logloss": control["logloss"]})
    generator = random.Random(seed)
    differences = [entry["hits"] - entry["baseline_hits"] for entry in per_draw]
    bootstrap = sorted(mean(generator.choices(differences, k=len(differences))) for _ in range(1000))
    return {"method": method, "evaluated_draws": len(per_draw), "seed": seed,
            "mean_hits": mean(entry["hits"] for entry in per_draw),
            "baseline_mean_hits": mean(entry["baseline_hits"] for entry in per_draw),
            "random_expected_mean_hits": spec["pick_count"] ** 2 / spec["number_max"],
            "paired_difference_ci95": [bootstrap[24], bootstrap[974]],
            "brier": mean(entry["brier"] for entry in per_draw),
            "baseline_brier": mean(entry["baseline_brier"] for entry in per_draw),
            "logloss": mean(entry["logloss"] for entry in per_draw),
            "baseline_logloss": mean(entry["baseline_logloss"] for entry in per_draw),
            "conclusion": "Chua du bang chung vuot baseline ngau nhien",
            "holdout": False, "exploratory": True,
            "limitations": "No untouched holdout or multiple-trial correction; CI is exploratory, not an edge claim.",
            "per_draw": per_draw, "disclaimer": DISCLAIMER}


def frequency(rows, spec):
    if spec["kind"] != "pick_k":
        counts = {}
        for row in rows:
            for values in row["digits"].values():
                for value in values:
                    counts[value] = counts.get(value, 0) + 1
        return [{"number": number, "count": count} for number, count in sorted(counts.items())]
    result = []
    for number in range(1, spec["number_max"] + 1):
        positions = [index for index, row in enumerate(rows) if number in row["main"]]
        result.append({"number": number, "count": len(positions),
                       "gap": len(rows) - 1 - positions[-1] if positions else None})
    return result
