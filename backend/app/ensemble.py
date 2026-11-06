"""Adaptive probability ensemble for pick-k games.

Why this exists
---------------
``analytics.probabilities(..., "bayesian")`` uses one window (300 draws) and one
prior strength (100) for every game. Those constants were never validated per
game or per period, and a single window cannot separate short-memory from
long-memory behaviour. This module builds several shrinkage estimators over
different windows and exponential-recency series, mixes them with weights chosen
**only on draws that strictly precede the draw being predicted** (greedy ensemble
selection with shrinkage), and reports the mixture against the uniform baseline on
a chronological segment instead of asserting an advantage.

Invariants
----------
* every component returns inclusion probabilities: ``sum == pick_count`` with all
  values strictly positive, so ledger scoring, ticket sampling and the dashboard
  stay comparable across models;
* fitting is deterministic: the same history and config always produce the same
  vector, which keeps model versions reproducible;
* the mixture and the optional advisor tilt are renormalised back to that
  invariant before leaving this module.
"""

import hashlib
import math
import random
from statistics import mean

from .db import encode


REVISION = "pick-k-adaptive-v2"
WINDOW = 300
STRENGTH = 100.0

# ``uniform`` first: with too little history the ensemble degrades to the
# theoretical distribution instead of pretending to have learned something.
COMPONENTS = (
    {"name": "uniform", "kind": "uniform"},
    {"name": "frequency_60", "kind": "window", "window": 60},
    {"name": "frequency_150", "kind": "window", "window": 150},
    {"name": "frequency_300", "kind": "window", "window": 300},
    {"name": "recency_80", "kind": "decay", "half_life": 80.0},
    {"name": "recency_250", "kind": "decay", "half_life": 250.0},
)

MINIMUM_TUNING_DRAWS = 8
MAXIMUM_TUNING_DRAWS = 48
MINIMUM_TRAINING_DRAWS = 10
EVALUATION_DRAWS = 100
ROUNDS = 6
SHRINK = 0.35
MINIMUM_WEIGHT = 0.01
CALIBRATION_BINS = 5
MINIMUM_EVALUATED = 4

_CACHE = {}
_CACHE_LIMIT = 6


def _digest(rows, spec):
    return hashlib.sha256(encode([spec["code"], spec["kind"], spec["number_max"],
                                  spec["pick_count"], rows]).encode()).hexdigest()


def _cache(key):
    return _CACHE.get(key)


def _copy(value):
    """Deep-enough copy: cached structures must never be mutated by a caller."""
    result = dict(value)
    for field, item in value.items():
        if isinstance(item, list):
            result[field] = list(item)
        elif isinstance(item, dict):
            result[field] = dict(item)
    return result


def _store(key, value):
    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = value
    return value


def _prior(spec):
    return spec["pick_count"] / spec["number_max"]


def _uniform(spec):
    return [spec["pick_count"] / spec["number_max"]] * spec["number_max"]


def _inclusion(counts, spec, effective, strength=STRENGTH):
    """Shrink inclusion counts toward the uniform inclusion probability.

    ``effective`` is how many draws the counts represent, so a short window or a
    decayed series is never over-weighted against the prior.
    """
    size, count = spec["number_max"], spec["pick_count"]
    prior = count / size
    if effective + strength <= 0:
        return [prior] * size
    return [(value + strength * prior) / (effective + strength) for value in counts]


def _prefixes(rows, spec):
    """Cumulative inclusion counts: ``result[i]`` covers ``rows[:i]``."""
    size = spec["number_max"]
    counts = [0.0] * size
    result = [list(counts)]
    for row in rows:
        counts = list(counts)
        for number in row["main"]:
            counts[number - 1] += 1.0
        result.append(counts)
    return result


def _decayed(rows, spec, half_life):
    """Exponentially decayed inclusion counts: ``result[i]`` covers ``rows[:i]``."""
    size = spec["number_max"]
    decay = 0.5 ** (1.0 / half_life)
    counts = [0.0] * size
    result = [list(counts)]
    for row in rows:
        counts = [value * decay for value in counts]
        for number in row["main"]:
            counts[number - 1] += 1.0
        result.append(list(counts))
    return result


def _window(prefix, index, window):
    if index <= window:
        return prefix[index]
    return [value - base for value, base in zip(prefix[index], prefix[index - window])]


def _state(rows, spec):
    prefix = _prefixes(rows, spec)
    decayed = {component["half_life"]: _decayed(rows, spec, component["half_life"])
               for component in COMPONENTS if component["kind"] == "decay"}
    return prefix, decayed


def _vector(component, index, prefix, decayed, spec):
    if component["kind"] == "uniform":
        return _uniform(spec)
    if component["kind"] == "window":
        return _inclusion(_window(prefix, index, component["window"]), spec,
                          min(index, component["window"]))
    # A decayed series carries about ``half_life / ln 2`` effective draws.
    return _inclusion(decayed[component["half_life"]][index], spec,
                      min(index, component["half_life"] * 1.4427))


def _mix(weights, index, prefix, decayed, spec):
    vector = [0.0] * spec["number_max"]
    for component in COMPONENTS:
        weight = weights.get(component["name"], 0.0)
        if not weight:
            continue
        for position, value in enumerate(_vector(component, index, prefix, decayed, spec)):
            vector[position] += weight * value
    return vector


def _renormalise(vector, spec):
    """Restore positivity and ``sum == pick_count`` after mixing or tilting."""
    count, size = spec["pick_count"], spec["number_max"]
    floor = count * 0.02 / size
    values = [max(value, floor) for value in vector]
    for _ in range(3):
        total = sum(values)
        if total <= 0:
            return [count / size] * size
        values = [value * count / total for value in values]
    return values


def logloss(vector, picks):
    return -mean(math.log(max(vector[number - 1], 1e-12)) for number in picks)


def brier(vector, picks):
    observed = set(picks)
    return sum((value - (number in observed)) ** 2 for number, value in enumerate(vector, 1)) / len(vector)


def select(rows, spec):
    """Greedy mixture selection on draws that strictly precede the target."""
    names = [component["name"] for component in COMPONENTS]
    start = max(1, len(rows) - MAXIMUM_TUNING_DRAWS)
    evaluated = len(rows) - start
    if evaluated < MINIMUM_TUNING_DRAWS:
        selection = {"status": "insufficient_history", "evaluated_draws": max(evaluated, 0),
                     "metric": "categorical_logloss", "policy": "past_only_temporal_selection",
                     "components_considered": len(names), "weights": {"uniform": 1.0}}
        return {"uniform": 1.0}, selection
    prefix, decayed = _state(rows, spec)
    indices = list(range(start, len(rows)))
    actual = [rows[index]["main"] for index in indices]
    vectors = {component["name"]: [_vector(component, index, prefix, decayed, spec) for index in indices]
               for component in COMPONENTS}

    def score(mix):
        return mean(logloss(vector, picks) for vector, picks in zip(mix, actual))

    singles = {name: score(vectors[name]) for name in names}
    best = min(names, key=lambda name: (singles[name], names.index(name)))
    weights = {name: float(name == best) for name in names}
    current = [list(vector) for vector in vectors[best]]
    best_score = singles[best]
    rounds = 0
    for _ in range(ROUNDS):
        choice = None
        for name in names:
            blended = [[(1 - SHRINK) * base + SHRINK * value for base, value in zip(vector, other)]
                       for vector, other in zip(current, vectors[name])]
            value = score(blended)
            if choice is None or value < choice[0] - 1e-12:
                choice = (value, name, blended)
        if choice is None or choice[0] >= best_score - 1e-12:
            break
        best_score, current = choice[0], choice[2]
        weights = {name: (1 - SHRINK) * weight for name, weight in weights.items()}
        weights[choice[1]] += SHRINK
        rounds += 1
    weights = {name: weight for name, weight in weights.items() if weight >= MINIMUM_WEIGHT}
    total = sum(weights.values()) or 1.0
    weights = {name: weight / total for name, weight in weights.items()}
    selection = {"status": "selected", "evaluated_draws": evaluated, "rounds": rounds, "shrink": SHRINK,
                 "metric": "categorical_logloss", "policy": "past_only_temporal_selection",
                 "components_considered": len(names), "weights": dict(weights),
                 "ensemble_logloss": best_score, "baseline_logloss": singles["uniform"],
                 "single_component_logloss": singles, "best_single_component": best}
    return weights, selection


def _bootstrap(values, seed=42):
    if len(values) < 2:
        return [0.0, 0.0]
    generator = random.Random(seed)
    samples = sorted(mean(generator.choices(values, k=len(values))) for _ in range(1000))
    return [samples[24], samples[974]]


def evaluate(rows, spec, test_draws=EVALUATION_DRAWS, seed=42):
    """Forward test: weights fitted on an earlier prefix only, then applied onward."""
    start = max(MINIMUM_TRAINING_DRAWS, len(rows) - test_draws)
    if len(rows) - start < MINIMUM_EVALUATED:
        return None
    train, test = rows[:start], rows[start:]
    weights, selection = select(train, spec)
    prefix, decayed = _state(rows, spec)
    uniform = _uniform(spec)
    per_draw = []
    for offset, row in enumerate(test):
        index = start + offset
        vector = _renormalise(_mix(weights, index, prefix, decayed, spec), spec)
        ranked = sorted(range(1, len(vector) + 1), key=lambda number: (-vector[number - 1], number))
        per_draw.append({"draw_id": row["draw_id"], "trained_through": rows[index - 1]["draw_id"],
                         "logloss": logloss(vector, row["main"]),
                         "baseline_logloss": logloss(uniform, row["main"]),
                         "brier": brier(vector, row["main"]),
                         "baseline_brier": brier(uniform, row["main"]),
                         "top1_hit": int(ranked[0] in row["main"]),
                         "top_k_hits": sum(number in row["main"] for number in ranked[:spec["pick_count"]])})
    differences = [entry["logloss"] - entry["baseline_logloss"] for entry in per_draw]
    interval = _bootstrap(differences, seed)
    return {"kind": "pick_k", "evaluated_draws": len(per_draw), "holdout": False, "exploratory": True,
            "selection_through": train[-1]["draw_id"], "selection_status": selection["status"],
            "logloss": mean(entry["logloss"] for entry in per_draw),
            "baseline_logloss": mean(entry["baseline_logloss"] for entry in per_draw),
            "brier": mean(entry["brier"] for entry in per_draw),
            "baseline_brier": mean(entry["baseline_brier"] for entry in per_draw),
            "logloss_difference_ci95": interval, "logloss_improvement": -mean(differences),
            "top1_accuracy": mean(entry["top1_hit"] for entry in per_draw),
            "mean_top_k_hits": mean(entry["top_k_hits"] for entry in per_draw),
            "random_top_k_hits": spec["pick_count"] ** 2 / spec["number_max"],
            "conclusion": ("Log loss thap hon baseline ngau nhien tren doan danh gia cuon chieu; "
                           "day la danh gia tham do, khong phai bang chung loi the."
                           if interval[1] < 0 else "Chua du bang chung vuot baseline ngau nhien"),
            "limitations": "Rolling temporal evaluation with re-tuned weights; not an untouched holdout.",
            "per_draw": per_draw}


def calibration(rows, spec, weights, bins=CALIBRATION_BINS):
    """Expected calibration error of the inclusion probabilities over past draws."""
    if len(rows) < 2:
        return None
    prefix, decayed = _state(rows, spec)
    buckets = [[0, 0.0, 0.0] for _ in range(bins)]
    ceiling = max(_prior(spec) * 4, 1e-9)
    for index in range(1, len(rows)):
        vector = _renormalise(_mix(weights, index, prefix, decayed, spec), spec)
        observed = set(rows[index]["main"])
        for number, value in enumerate(vector, 1):
            bucket = buckets[min(bins - 1, int(value / ceiling * bins))]
            bucket[0] += 1
            bucket[1] += value
            bucket[2] += int(number in observed)
    total = sum(bucket[0] for bucket in buckets)
    if not total:
        return None
    error = sum(abs(bucket[1] - bucket[2]) / total for bucket in buckets if bucket[0])
    return {"expected_calibration_error": error, "draws": len(rows) - 1, "bins": bins,
            "scope": "inclusion_probability_vs_observed_frequency"}


def estimate(rows, spec):
    """Cheap path used inside backtests: mixture weights and probabilities only."""
    rows = list(rows)
    key = _digest(rows, spec)
    cached = _cache(key)
    if cached is not None:
        return _copy(cached)
    if not rows:
        result = {"probabilities": _uniform(spec), "weights": {"uniform": 1.0},
                  "selection": {"status": "no_history", "evaluated_draws": 0,
                                "metric": "categorical_logloss", "policy": "past_only_temporal_selection",
                                "components_considered": len(COMPONENTS), "weights": {"uniform": 1.0}},
                  "window": WINDOW, "strength": STRENGTH, "algorithm_revision": REVISION}
        return _copy(_store(key, result))
    weights, selection = select(rows, spec)
    prefix, decayed = _state(rows, spec)
    result = {"probabilities": _renormalise(_mix(weights, len(rows), prefix, decayed, spec), spec),
              "weights": weights, "selection": selection,
              "window": WINDOW, "strength": STRENGTH, "algorithm_revision": REVISION}
    return _copy(_store(key, result))


def fit(rows, spec):
    """Full adaptive fit: probabilities, mixture weights, evaluation and calibration."""
    if spec["kind"] != "pick_k":
        raise ValueError("Adaptive ensemble supports pick_k games only")
    rows = list(rows)
    key = "fit:" + _digest(rows, spec)
    cached = _cache(key)
    if cached is not None:
        return dict(cached)
    result = estimate(rows, spec)
    if not rows:
        result = result | {"metrics": None, "calibration": None}
    else:
        result["metrics"] = evaluate(rows, spec)
        result["calibration"] = calibration(rows[-min(len(rows), WINDOW):], spec, result["weights"])
    return _copy(_store(key, result))


def tilt(probabilities, scores, confidence, maximum):
    """Bounded exponential nudge from an external advisor (e.g. an LLM).

    ``scores`` is a per-entry list in ``[-1, 1]``; effective strength is
    ``confidence * maximum``. The advisor can therefore never dominate or replace
    the statistical vector, and an empty or zero score set leaves it untouched.
    """
    if not scores or not probabilities:
        return list(probabilities), None
    strength = max(0.0, min(1.0, float(confidence))) * max(0.0, min(1.0, float(maximum)))
    adjusted = sum(1 for score in scores if abs(score) > 1e-9)
    if strength <= 0 or not adjusted:
        return list(probabilities), None
    tilted = [value * math.exp(strength * max(-1.0, min(1.0, float(score))))
              for value, score in zip(probabilities, scores)]
    total, original = sum(tilted), sum(probabilities)
    if total <= 0 or original <= 0:
        return list(probabilities), None
    return [value * original / total for value in tilted], {"strength": strength, "adjusted": adjusted,
                                                            "applied": True}