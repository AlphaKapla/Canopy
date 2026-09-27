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


class IncompleteDraws(ValueError):
    """A tree depends on an event but carries no per-iteration draws for
    it: the model-wide distribution cannot be formed."""


def _measure_draws(f: list, f1: list, f0: list) -> dict:
    """Per-iteration measures, summarized as the engine does
    (engine/src/main.rs `importance_unc_json`): a ratio over the iterations
    where its denominator is non-zero, the others counted."""
    from uncertainty import summarize
    fv, raw, rrw, bi = [], [], [], []
    undef_f = undef_f0 = 0
    for a, b, c in zip(f, f1, f0):
        m = measures(a, b, c)
        bi.append(m["birnbaum_per_year"])
        if m["fussell_vesely"] is None:
            undef_f += 1
        else:
            fv.append(m["fussell_vesely"])
            raw.append(m["raw"])
        if m["rrw"] is None:
            undef_f0 += 1
        else:
            rrw.append(m["rrw"])

    def summ(xs):
        if not xs:
            return None
        s = summarize(xs)
        s.pop("n")
        return s
    return {"frequency_if_true_per_year": summ(f1),
            "frequency_if_false_per_year": summ(f0),
            "birnbaum_per_year": summ(bi), "fussell_vesely": summ(fv),
            "raw": summ(raw), "rrw": summ(rrw),
            "iterations_with_zero_frequency": undef_f,
            "iterations_with_zero_frequency_if_false": undef_f0}


def uncertainty_for_metric(results: dict, metric_id: str) -> dict | None:
    """Model-wide importance under uncertainty for a risk metric (FR-37),
    from event trees quantified with the same --samples/--seed/--sampling
    and `--importance-events` (per-iteration draws of F(x=1), F(x=0)).

    Keyed random numbers make iteration i the same state of knowledge in
    every tree, so per iteration F = Σ_t F_t and F(x=v) = Σ_t F_t(x=v), with
    F_t(x=v) = F_t for a tree that does not depend on x (it does not list
    x in its point importance rows). Sums run over trees sorted by ID, left
    to right (`fold_sum` semantics), so a single tree reproduces the
    engine's own summaries bit for bit.

    Returns {"events": [event, ...], "frequency_draws": F, "rows": {event:
    {..., "draws_if_true", "draws_if_false"}}}, or None if no tree carries
    importance draws for the metric. Raises IncompleteDraws if a tree that
    depends on one of the events lacks its draws."""
    trees = []
    for et_id in sorted(results):
        for m in results[et_id].get("metrics", []):
            if m["id"] == metric_id:
                trees.append((et_id, m))
    if not trees:
        return None
    rows_by_tree = []
    events = set()
    for et_id, m in trees:
        if "draws" not in m.get("uncertainty", {}):
            return None
        point = {r["event"] for r in m.get("importance", [])}
        drawn = {r["event"]: r["uncertainty"] for r in m.get("importance", [])
                 if "draws_if_true" in r.get("uncertainty", {})}
        events |= set(drawn)
        rows_by_tree.append((et_id, m["uncertainty"]["draws"], point, drawn))
    if not events:
        return None
    for et_id, _, point, drawn in rows_by_tree:
        missing = sorted(e for e in events if e in point and e not in drawn)
        if missing:
            raise IncompleteDraws(
                f"{et_id} depends on {', '.join(missing)} but has no draws for "
                f"it (quantify every tree with the same --importance-events)")
    n = len(rows_by_tree[0][1])
    if any(len(d) != n for _, d, _, _ in rows_by_tree):
        raise IncompleteDraws("event trees carry different numbers of draws")
    f = [0.0] * n
    for _, d, _, _ in rows_by_tree:
        for i in range(n):
            f[i] += d[i]
    out = {}
    for e in sorted(events):
        f1, f0 = [0.0] * n, [0.0] * n
        for _, d, point, drawn in rows_by_tree:
            t1 = drawn[e]["draws_if_true"] if e in drawn else d
            t0 = drawn[e]["draws_if_false"] if e in drawn else d
            for i in range(n):
                f1[i] += t1[i]
                f0[i] += t0[i]
        row = _measure_draws(f, f1, f0)
        row["draws_if_true"], row["draws_if_false"] = f1, f0
        out[e] = row
    return {"events": sorted(events), "frequency_draws": f, "rows": out}
