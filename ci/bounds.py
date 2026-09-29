"""Point values and truncation bounds in quantification results (FR-42).

An event tree quantified exactly carries point values (`frequency_per_year`
per sequence, `value_per_year` per metric); one quantified with
`--truncated` (method "truncated-mcs") carries rigorous bounds instead
(`frequency_lower_bound` / `frequency_upper_bound`, `value_lower_bound` /
`value_upper_bound`) and no point value at all. The reporting tools read
values only through this module: an exact value is the degenerate interval
[v, v], and a bound is never read as a value.

Sums over event trees run in sorted tree order, left to right — the order
the tools always used for point totals, so exact results keep their
historical totals bit for bit.
"""
from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal

TRUNCATED = "truncated-mcs"


def is_truncated(et_result: dict) -> bool:
    return et_result.get("method") == TRUNCATED


def any_truncated(results: dict) -> bool:
    return any(is_truncated(r) for r in results.values())


def seq_interval(seq: dict) -> tuple[float, float]:
    if "frequency_per_year" in seq:
        v = seq["frequency_per_year"]
        return (v, v)
    return (seq["frequency_lower_bound"], seq["frequency_upper_bound"])


def metric_interval(m: dict) -> tuple[float, float]:
    if "value_per_year" in m:
        v = m["value_per_year"]
        return (v, v)
    return (m["value_lower_bound"], m["value_upper_bound"])


def metric_totals(results: dict) -> dict[str, tuple[float, float]]:
    """{metric: (lower, upper)} summed over event trees (sorted by ID)."""
    tot: dict[str, tuple[float, float]] = {}
    for et_id in sorted(results):
        for m in results[et_id].get("metrics", []):
            lo, hi = metric_interval(m)
            a, b = tot.get(m["id"], (0.0, 0.0))
            tot[m["id"]] = (a + lo, b + hi)
    return tot


def method_note(results: dict) -> str | None:
    """'cut-off 1e-12, order <= 3' style description of the truncation of
    a result set (distinct settings joined), or None if it is exact."""
    settings = sorted({(r["cutoff"], r.get("order_limit")) for r in results.values()
                       if is_truncated(r)}, key=lambda s: (s[0], s[1] or 0))
    if not settings:
        return None
    return "; ".join(f"cut-off {c:g}" + (f", order ≤ {k}" if k else "") for c, k in settings)


NOISE = Decimal("1e-12")   # relative: the bounds' own floating-point error


def _round_outward(x: float, exp: int, up: bool) -> Decimal:
    """x rounded to a multiple of 10**exp: toward +inf if `up`, else toward
    -inf — unless x is within NOISE (relative) of such a multiple, which
    it then is (5e-4 and 2.5e-6 - 1e-6 print as the decimals they are, not
    one unit further out). From the shortest decimal that reads back as x
    (repr), within half an ulp of the binary value."""
    d = Decimal(repr(x))
    q = Decimal(1).scaleb(exp)
    near = d.quantize(q, rounding=ROUND_HALF_EVEN)
    if abs(near - d) <= abs(d) * NOISE:
        return near
    return d.quantize(q, rounding=ROUND_CEILING if up else ROUND_FLOOR)


def fmt_bound(x: float, digits: int, up: bool) -> str:
    """x in scientific notation with `digits` decimals (the style of
    f"{x:.4e}"), rounded outward — toward +inf for an upper bound (`up`),
    toward -inf for a lower one — so a printed interval contains the
    computed one to within NOISE."""
    if x == 0.0:
        return f"{0.0:.{digits}e}"
    r = _round_outward(x, Decimal(repr(x)).adjusted() - digits, up)
    mant = r.scaleb(-r.adjusted())
    return f"{mant:.{digits}f}e{r.adjusted():+03d}"


def fmt_ratio_bound(x: float, up: bool) -> str:
    """A ratio bound to 3 significant digits, rounded outward like
    fmt_bound, in plain notation (as f"{x:.3g}" for moderate ratios)."""
    if x == 0.0:
        return "0"
    return format(_round_outward(x, Decimal(repr(x)).adjusted() - 2, up), "f")


def fmt_interval(lo: float, hi: float, bounded: bool, digits: int = 4) -> str:
    """A point value as f"{v:.4e}", exactly as before; a bounded one as
    [lo, hi] rounded outward, bracketed even when both bounds coincide (a
    truncated result is labelled as one)."""
    if not bounded:
        return f"{lo:.{digits}e}"
    return f"[{fmt_bound(lo, digits, False)}, {fmt_bound(hi, digits, True)}]"
