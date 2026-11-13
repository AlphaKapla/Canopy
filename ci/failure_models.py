"""The closed-form failure-model formulas, as the engine computes them.

One Python copy (engine/src/model.rs `fm_value` is the reference), shared
by every tool that needs a basic event's point probability without the
engine: the MEF exporter, the RiskSpectrum importer and the viewer. The
steps and their order are the engine's, so the values are the same
floating-point numbers (ci/test_failure_models.py checks them against the
engine and against exact rational arithmetic).

    probability          p
    rate-mission         1 - exp(-rate x mission_time)
    rate-repair          rate x mttr / (1 + rate x mttr)
    rate-periodic-test   1 - (1 - exp(-x)) / x,  x = rate x test_interval
"""
import math


def mission(rate: float, time: float) -> float:
    """1 - exp(-rate x time), as -expm1: the direct form loses the digits
    of a small rate x time to the subtraction (V&V D-40)."""
    return -math.expm1(-rate * time)


def repair(rate: float, mttr: float) -> float:
    return (rate * mttr) / (1.0 + rate * mttr)


def periodic_test(rate: float, interval: float) -> float:
    """Time-averaged unavailability between idealized periodic tests. Below
    x = 0.5 the closed form cancels (1% off at x = 1e-7, negative at 1e-10:
    V&V D-40), so the value is summed from its alternating series
    x/2 - x^2/6 + x^3/24 - ..."""
    x = rate * interval
    if x == 0.0:
        return 0.0
    if x >= 0.5:
        return 1.0 - (-math.expm1(-x)) / x
    term = x / 2.0
    total = term
    n = 2.0
    while True:
        term = -term * x / (n + 1.0)
        nxt = total + term
        if nxt == total:
            return total
        total = nxt
        n += 1.0
