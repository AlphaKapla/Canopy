"""Model-wide BDD-exact consequence importance from per-event-tree results.

Shared by consequence_report.py and compare.py. The engine reports, per
event tree and per group of sequences (each risk metric, each end state),
the exact group frequency F and, for every basic event x the group depends
on, the exact conditional frequencies F(x=1) and F(x=0) (docs/
quantification.md, "Consequence-level importance"). A model-wide group is
a sum over event trees, and so are its conditional frequencies:

    F       = Σ_t F_t
    F(x=v)  = Σ_t F_t(x=v),   with F_t(x=v) = F_t where tree t does not
                              depend on x

This is exact (no cut sets, no rare-event approximation); the measures are
then Birnbaum F(x=1) − F(x=0) (/yr), Fussell–Vesely (F − F(x=0))/F,
RAW F(x=1)/F and RRW F/F(x=0), each None where its denominator is zero.
"""
from __future__ import annotations

from uncertainty import fold_sum


def measures(f: float, f1: float, f0: float) -> dict:
    return {
        "birnbaum_per_year": f1 - f0,
        "fussell_vesely": (f - f0) / f if f > 0 else None,
        "raw": f1 / f if f > 0 else None,
        "rrw": f / f0 if f0 > 0 else None,
    }


def combine(groups: list[tuple[float, list[dict]]]) -> dict:
    """groups: (F_t, importance rows of tree t) per contributing tree.
    Returns {"frequency_per_year": F, "importance": rows ranked by FV}."""
    total = fold_sum(g[0] for g in groups)
    by_tree = [(f, {r["event"]: r for r in rows}) for f, rows in groups]
    events = sorted({e for _, rows in by_tree for e in rows})
    out = []
    for e in events:
        f1 = fold_sum(rows[e]["frequency_if_true_per_year"] if e in rows else f
                      for f, rows in by_tree)
        f0 = fold_sum(rows[e]["frequency_if_false_per_year"] if e in rows else f
                      for f, rows in by_tree)
        prob = next(rows[e]["probability"] for _, rows in by_tree if e in rows)
        out.append({"event": e, "probability": prob,
                    "frequency_if_true_per_year": f1,
                    "frequency_if_false_per_year": f0,
                    **measures(total, f1, f0)})
    out.sort(key=lambda r: (r["fussell_vesely"] is None,
                            -(r["fussell_vesely"] or 0.0), r["event"]))
    return {"frequency_per_year": total, "importance": out}


def for_metric(results: dict, metric_id: str) -> dict | None:
    """Model-wide importance for a risk metric, or None when any event tree
    carrying the metric was quantified without importance (--prob-only or
    an older engine)."""
    groups = []
    for et_id in sorted(results):
        for m in results[et_id].get("metrics", []):
            if m["id"] != metric_id:
                continue
            if "importance" not in m:
                return None
            groups.append((m["value_per_year"], m["importance"]))
    return combine(groups) if groups else None


def for_end_states(results: dict, end_states: set) -> dict | None:
    """Model-wide importance for a set of end states, or None when any event
    tree lacks the engine's `end_states` importance section."""
    groups = []
    for et_id in sorted(results):
        et = results[et_id]
        if "end_states" not in et:
            return None
        for es in et["end_states"]:
            if es["id"] in end_states:
                groups.append((es["frequency_per_year"], es["importance"]))
    return combine(groups) if groups else None
