from itertools import combinations
import numpy as np

N = 55
K = 6

def draw_matrix(rows):
    return np.array([[getattr(r, f"n{i}") for i in range(1, 7)] for r in rows], dtype=int)

def number_scores(history, method="bayesian"):
    # Scores are ranking heuristics, not calibrated winning probabilities.
    if method == "uniform" or len(history) == 0:
        return np.ones(N, dtype=float) / N
    matrix = draw_matrix(history)
    counts = np.bincount(matrix.ravel(), minlength=N + 1)[1:N + 1].astype(float)
    if method == "bayesian":
        # Symmetric Dirichlet smoothing. Expected frequency remains uniform absent evidence.
        alpha = 10.0
        return (counts + alpha * K / N) / (len(history) * K + alpha * K)
    if method == "xgboost":
        return xgb_scores(matrix)
    raise ValueError("Unknown method")

def xgb_scores(matrix):
    try:
        from xgboost import XGBClassifier
    except Exception as exc:
        raise RuntimeError("XGBoost unavailable") from exc
    if len(matrix) < 80:
        raise ValueError("Need at least 80 draws for XGBoost experiment")
    X, y = [], []
    for t in range(20, len(matrix)):
        prior = matrix[:t]
        counts = np.bincount(prior.ravel(), minlength=56)[1:]
        # Each training row is a number-level example for the next draw.
        present = set(matrix[t].tolist())
        for num in range(1, 56):
            X.append([num / 55.0, counts[num-1], t, counts[num-1] / (t * 6)])
            y.append(int(num in present))
    model = XGBClassifier(n_estimators=100, max_depth=2, learning_rate=0.03,
                          subsample=0.8, colsample_bytree=0.8, eval_metric="logloss",
                          random_state=42, n_jobs=2)
    model.fit(np.asarray(X), np.asarray(y))
    t = len(matrix)
    counts = np.bincount(matrix.ravel(), minlength=56)[1:]
    X_next = np.array([[num / 55.0, counts[num-1], t, counts[num-1] / (t * 6)]
                       for num in range(1, 56)])
    return model.predict_proba(X_next)[:, 1]

def candidate_sets(scores, count=5, seed=42):
    # Weighted sampling without replacement. This creates candidate sets, not true odds.
    rng = np.random.default_rng(seed)
    weights = np.maximum(np.asarray(scores, dtype=float), 1e-12)
    weights = weights / weights.sum()
    out = []
    for _ in range(max(1, min(count, 100))):
        picks = rng.choice(np.arange(1, N+1), size=K, replace=False, p=weights)
        out.append(sorted(int(x) for x in picks))
    return out

def backtest(rows, min_train=100, test_draws=100, method="bayesian"):
    rows = sorted(rows, key=lambda r: (r.draw_date, r.draw_id))
    if len(rows) <= min_train:
        raise ValueError(f"Need more than min_train={min_train} draws; found {len(rows)}")
    start = max(min_train, len(rows) - test_draws)
    results = []
    for t in range(start, len(rows)):
        train, actual = rows[:t], {getattr(rows[t], f"n{i}") for i in range(1, 7)}
        scores = number_scores(train, method)
        ranked = np.argsort(scores)[-K:] + 1
        hits = len(set(int(x) for x in ranked) & actual)
        results.append({"draw_id": rows[t].draw_id, "draw_date": str(rows[t].draw_date),
                        "hits": hits, "baseline_expected_hits": K*K/N})
    hits = np.array([r["hits"] for r in results], dtype=float)
    return {
        "method": method, "evaluated_draws": len(results),
        "mean_hits": float(hits.mean()),
        "random_expected_mean_hits": K*K/N,
        "hit_difference": float(hits.mean() - K*K/N),
        "hit_histogram": {str(i): int(np.sum(hits == i)) for i in range(7)},
        "warning": "Exploratory backtest only. No confidence claim; model selection and multiple testing can inflate results.",
        "per_draw": results
    }
