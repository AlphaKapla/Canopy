"""Helpers for Monte Carlo (state-of-knowledge uncertainty) results.

Shared by quantify.py and compare.py. The statistics are defined exactly as
in the engine (engine/src/uncertainty.rs `Summary`): sequential-sum mean,
two-pass sample standard deviation (n - 1), percentiles by linear
interpolation between order statistics (Hyndman & Fan type 7, NumPy's
default) — so a summary recomputed here from an engine's draws is
bit-identical to the engine's own.

Aggregation across event trees relies on the engine's keyed random numbers:
iteration i draws the same value of every quantity in every engine process
(same seed), so model-wide metric draws are the iteration-by-iteration sum
of the per-event-tree metric draws.
"""
import math


def percentile(sorted_vals: list[float], q: float) -> float:
    n = len(sorted_vals)
    if n == 0:
        return math.nan
    h = (n - 1) * q
    lo = math.floor(h)
    if lo + 1 >= n:
        return sorted_vals[-1]
    return sorted_vals[lo] + (h - lo) * (sorted_vals[lo + 1] - sorted_vals[lo])


def fold_sum(xs) -> float:
    """Plain left-to-right float sum, as the engine's `iter().sum()`.
    (Python >= 3.12's built-in sum() of floats is compensated — more
    accurate, but not bit-identical to the engine.)"""
    acc = 0.0
    for x in xs:
        acc += x
    return acc


def summarize(draws: list[float]) -> dict:
    n = len(draws)
    mean = fold_sum(draws) / n
    var = (fold_sum((x - mean) * (x - mean) for x in draws) / (n - 1)
           if n > 1 else 0.0)
    s = sorted(draws)
    std = math.sqrt(var)
    return {"mean": mean, "std": std, "std_error_of_mean": std / math.sqrt(n),
            "p05": percentile(s, 0.05), "p50": percentile(s, 0.50),
            "p95": percentile(s, 0.95), "n": n}


def sampling_settings(results: dict):
    """(samples, seed, method) shared by every event tree, None if no event
    tree was sampled; raises ValueError if the trees were sampled
    inconsistently. `method` is "srs" or "lhs" (results from engines
    predating LHS carry no method: simple random sampling)."""
    settings = {(et["uncertainty"]["samples"], et["uncertainty"]["seed"],
                 et["uncertainty"].get("method", "srs"))
                for et in results.values() if "uncertainty" in et}
    if not settings:
        return None
    if len(settings) > 1 or len(settings) == 1 and any(
            "uncertainty" not in et for et in results.values()):
        raise ValueError(f"event trees sampled inconsistently: {sorted(settings)}")
    return settings.pop()


def metric_draws(results: dict) -> dict[str, list[float]]:
    """Model-wide metric draws: per-iteration sum over event trees (sorted
    by event-tree ID, a fixed order, for reproducible floating point)."""
    out: dict[str, list[float]] = {}
    for et_id in sorted(results):
        for m in results[et_id].get("metrics", []):
            d = m.get("uncertainty", {}).get("draws")
            if d is None:
                continue
            acc = out.setdefault(m["id"], [0.0] * len(d))
            if len(acc) != len(d):
                raise ValueError(f"{et_id}: {len(d)} draws, expected {len(acc)}")
            for i, x in enumerate(d):
                acc[i] += x
    return out
