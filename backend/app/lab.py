import random

from .analytics import DISCLAIMER, frequency


def randomness(rows, spec, simulations=1000, seed=42):
    if spec["kind"] != "pick_k":
        raise ValueError("Randomness lab currently supports pick_k games only")
    if len(rows) < 20:
        raise ValueError("Need at least 20 verified draws")
    if len(rows) > 2000:
        raise ValueError("Interactive lab limited to 2000 draws; use an offline research report for larger histories")
    generator = random.Random(seed)
    size, count = spec["number_max"], spec["pick_count"]
    expected = len(rows) * count / size
    counts = [item["count"] for item in frequency(rows, spec)]
    statistic = sum((value - expected) ** 2 / expected for value in counts)
    observed_max = max(abs(value - expected) for value in counts)
    exceed, exceed_max = 0, 0
    for _ in range(simulations):
        simulated = [0] * size
        for _ in rows:
            for number in generator.sample(range(size), count):
                simulated[number] += 1
        exceed += sum((value - expected) ** 2 / expected for value in simulated) >= statistic
        exceed_max += max(abs(value - expected) for value in simulated) >= observed_max
    raw = [(exceed + 1) / (simulations + 1), (exceed_max + 1) / (simulations + 1)]
    corrected = [min(1, value * len(raw)) for value in raw]
    return {"tests": [{"name": name, "p_value": value, "p_adjusted": adjusted}
                       for name, value, adjusted in zip(("frequency_dispersion", "maximum_frequency_deviation"), raw, corrected)],
            "tested": len(raw), "correction": "Bonferroni within this report only",
            "simulations": simulations, "seed": seed,
            "conclusion": "Phat hien sai lech can kiem tra, khong phai bang chung du doan" if min(corrected) < 0.05 else "Khong phat hien sai lech",
            "limitations": "Frequency tests only; no runs, serial-dependence or pair tests yet.", "disclaimer": DISCLAIMER}


def illusion(spec, draws=100, seed=42):
    if spec["kind"] != "pick_k":
        raise ValueError("Simulator supports pick_k games only")
    generator = random.Random(seed)
    rows = [{"main": generator.sample(range(1, spec["number_max"] + 1), spec["pick_count"])} for _ in range(draws)]
    return {"draws": draws, "seed": seed, "frequency": frequency(rows, spec),
            "message": "So nong/lanh xuat hien ngay ca trong du lieu ngau nhien; khong phai tin hieu ky tiep.",
            "disclaimer": DISCLAIMER}
