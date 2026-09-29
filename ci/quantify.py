#!/usr/bin/env python3
"""Quantify every event tree in a model; write merged JSON results.

Usage: quantify.py <model-dir> <out.json> [--engine PATH]
                   [--samples N [--seed S] [--sampling srs|lhs]
                    [--importance-uncertainty K]]
                   [--truncated CUTOFF [--order-limit K]]
                   [--configurations CFG.json] [--prime-implicants]

With --truncated, every event tree (and every named configuration) is
quantified by truncated minimal cut sets (FR-39): each sequence frequency
and metric is a rigorous bound interval instead of a value, and the merged
results feed compare.py, the consequence report, the appendix and the
viewer, which show the intervals (FR-42). Coherent logic only: a tree
using not/xor is refused by the engine, and so is the run. No sampling,
importance or prime implicants on this path.

With --importance-uncertainty K (and --samples), the K events with the
highest model-wide point Fussell–Vesely of each risk metric (exact, summed
over event trees: ci/importance.py) are selected first, from a point pass;
every event tree is then sampled with `--importance-events` for the union
of those events, so each tree's results carry per-iteration draws of F(x=1)
and F(x=0), which ci/importance.py combines iteration by iteration into
model-wide importance distributions (FR-37). They are printed here.

With --configurations, every named configuration of model.yaml (its
house-event and parameter overrides, applied exactly as editing the model
would) is also quantified — point values only — and written to CFG.json
as {configuration ID: {event tree ID: results}}; each metric is printed
next to the base case.

With --samples, every event tree is also propagated by Monte Carlo with the
same N and seed (see docs/quantification.md). Because the engine's random
numbers are keyed by (seed, quantity ID, iteration), iteration i sees the
same parameter values in every event tree, and the model-wide metric
distribution is the iteration-by-iteration sum of the per-tree draws; it is
printed here and recomputed by compare.py (ci/uncertainty.py).

Partition check: every event tree's sequence probabilities must sum to 1
within PARTITION_TOL (the validator checks the table is an exact cover of
the functional-event outcomes, which makes the sum 1 for any logic). A tree
with per-sequence house-event overrides is exempt (its logic differs per
sequence) and is reported instead. A violation on an exempt-free tree is an
engine defect or a table the validator did not see: exit 1. The same holds
per followed transfer: its expansions must sum to the transfer row's own
probability (relative PARTITION_TOL) unless an expansion hop overrides
house events. On truncated results the check is on the bounds: the sums of
the sequences' lower and upper probability bounds must bracket 1, and a
followed transfer's expansions' summed bounds must overlap the row's own —
so every truncated run also checks that its bounds are consistent.

Event trees without an `initiating_event` are transfer-only: they are
quantified through the trees that transfer into them, never standalone
(which would count their sequences twice or with no frequency at all).
"""
import argparse
import glob
import json
import os
import subprocess
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from uncertainty import metric_draws, sampling_settings, summarize  # noqa: E402
import importance  # noqa: E402
import bounds  # noqa: E402

PARTITION_TOL = 1e-9


def partition_problems(et_id: str, r: dict) -> tuple[list[str], list[str]]:
    """(violations, notes) of the partition check on one event tree's
    results, exact (sums) or truncated (bounds)."""
    bad, notes = [], []
    part = r.get("partition")
    if part is None:
        return bad, notes
    trunc = bounds.is_truncated(r)
    if trunc:
        lo, hi = part["sum_probability_lower_bound"], part["sum_probability_upper_bound"]
        shown = f"in [{lo:.12f}, {hi:.12f}]"
    else:
        lo = hi = part["sum_probability"]
        shown = f"= {lo:.12f}"
    if part["per_sequence_house_overrides"]:
        notes.append(f"note: {et_id} has per-sequence house-event overrides; "
                     f"sum of sequence probabilities {shown} (not required to be 1)")
    elif lo > 1.0 + PARTITION_TOL or hi < 1.0 - PARTITION_TOL:
        if trunc:
            bad.append(f"{et_id}: sequence probability bounds sum to "
                       f"[{lo:.15f}, {hi:.15f}], which does not contain 1 "
                       f"(tolerance {PARTITION_TOL:g})")
        else:
            bad.append(f"{et_id}: sum of sequence probabilities = "
                       f"{lo:.15f} (|deviation| {abs(lo - 1.0):.3e} > {PARTITION_TOL:g})")
    for s in r.get("sequences", []):
        fol = s.get("followed")
        if not fol:
            continue
        if trunc:
            p_lo, p_hi = fol["probability_lower_bound"], fol["probability_upper_bound"]
            t_lo, t_hi = fol["sum_probability_lower_bound"], fol["sum_probability_upper_bound"]
        else:
            p_lo = p_hi = fol["probability"]
            t_lo = t_hi = fol["sum_probability"]
        if fol["per_sequence_house_overrides"]:
            notes.append(f"note: {et_id}/{s['id']}: transfer expansions override "
                         f"house events; they sum to {t_lo:.6e}"
                         + (f"..{t_hi:.6e}" if trunc else "")
                         + f" vs the transfer row's {p_lo:.6e}"
                         + (f"..{p_hi:.6e}" if trunc else "")
                         + " (not required to match)")
            continue
        tol = PARTITION_TOL * max(abs(p_hi), 1e-300)
        if t_lo > p_hi + tol or p_lo > t_hi + tol:
            if trunc:
                bad.append(f"{et_id}/{s['id']}: transfer expansions' bounds "
                           f"[{t_lo:.15e}, {t_hi:.15e}] miss the transfer row's "
                           f"[{p_lo:.15e}, {p_hi:.15e}]")
            else:
                bad.append(f"{et_id}/{s['id']}: transfer expansions sum to "
                           f"{t_lo:.15e}, the transfer row has {p_lo:.15e}")
    return bad, notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model_dir")
    ap.add_argument("out_path")
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", "engine/target/release/canopy"))
    ap.add_argument("--samples", type=int)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--sampling", choices=["srs", "lhs"])
    ap.add_argument("--prime-implicants", action="store_true",
                    help="list prime implicants of non-coherent sequence logic")
    ap.add_argument("--configurations", metavar="CFG.json",
                    help="also quantify every named configuration (point values)")
    ap.add_argument("--importance-uncertainty", type=int, metavar="K",
                    help="model-wide importance distributions for each metric's "
                         "K highest-FV events (needs --samples)")
    ap.add_argument("--truncated", type=float, metavar="CUTOFF",
                    help="truncated minimal cut sets: bounds instead of values")
    ap.add_argument("--order-limit", type=int, metavar="K",
                    help="with --truncated: retain cut sets of order <= K only")
    a = ap.parse_args()
    if (a.seed is not None or a.sampling) and a.samples is None:
        ap.error("--seed and --sampling only apply with --samples")
    if a.importance_uncertainty is not None and (a.samples is None
                                                 or a.importance_uncertainty < 1):
        ap.error("--importance-uncertainty K needs K >= 1 and --samples")
    if a.order_limit is not None and a.truncated is None:
        ap.error("--order-limit applies only with --truncated")
    if a.truncated is not None and (a.samples is not None or a.prime_implicants):
        ap.error("--truncated applies without --samples or --prime-implicants")

    et_ids = []
    for p in sorted(glob.glob(os.path.join(a.model_dir, "event-trees/*.yaml"))):
        et = yaml.safe_load(open(p)).get("event_tree", {})
        if "id" in et and "initiating_event" in et:
            et_ids.append(et["id"])

    extra = []
    if a.samples is not None:
        extra = ["--samples", str(a.samples)]
        if a.seed is not None:
            extra += ["--seed", str(a.seed)]
        if a.sampling:
            extra += ["--sampling", a.sampling]
    if a.prime_implicants:
        extra += ["--prime-implicants"]
    trunc_flags = []
    if a.truncated is not None:
        trunc_flags = ["--truncated", repr(a.truncated)]
        if a.order_limit is not None:
            trunc_flags += ["--order-limit", str(a.order_limit)]
    extra += trunc_flags

    if a.importance_uncertainty:
        # point pass: model-wide exact importance, then each metric's top K
        point = {}
        for et_id in et_ids:
            proc = subprocess.run([a.engine, a.model_dir, et_id, "--json"],
                                  capture_output=True, text=True)
            if proc.returncode != 0:
                print(f"ERROR quantifying {et_id}:\n{proc.stderr}", file=sys.stderr)
                return 1
            point[et_id] = json.loads(proc.stdout)
        chosen = set()
        for mid in sorted({m["id"] for r in point.values() for m in r.get("metrics", [])}):
            imp = importance.for_metric(point, mid)
            if imp:
                chosen |= {r["event"] for r in imp["importance"][:a.importance_uncertainty]}
        if chosen:
            extra += ["--importance-events", ",".join(sorted(chosen))]

    results = {}
    for et_id in et_ids:
        proc = subprocess.run(
            [a.engine, a.model_dir, et_id, "--json", *extra],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            print(f"ERROR quantifying {et_id}:\n{proc.stderr}", file=sys.stderr)
            return 1
        results[et_id] = json.loads(proc.stdout)

    bad = []
    for et_id, r in sorted(results.items()):
        b, notes = partition_problems(et_id, r)
        bad += b
        for n in notes:
            print(n)
    if bad:
        print("ERROR: event-tree partition violated (sequence table does not "
              "cover the outcome space exactly once):", file=sys.stderr)
        for b in bad:
            print(f"  {b}", file=sys.stderr)
        return 1

    with open(a.out_path, "w") as f:
        json.dump(results, f, indent=2, sort_keys=True)
    print(f"quantified {len(results)} event tree(s) -> {a.out_path}")
    note = bounds.method_note(results)
    if note:
        print(f"truncated quantification ({note}); model-wide bounds:")
        for mid, (lo, hi) in sorted(bounds.metric_totals(results).items()):
            print(f"  {mid}: {bounds.fmt_interval(lo, hi, True)} /yr")

    if a.configurations:
        manifest = yaml.safe_load(open(os.path.join(a.model_dir, "model.yaml")))
        cfgs = manifest.get("configurations") or {}
        cres = {}
        base_m = bounds.metric_totals(results)
        for cid in sorted(cfgs):
            c = cfgs[cid] or {}
            flags = list(trunc_flags)
            for h, v in sorted((c.get("house_events") or {}).items()):
                flags += ["--house", f"{h}={'true' if v else 'false'}"]
            for q, v in sorted((c.get("parameters") or {}).items()):
                flags += ["--param", f"{q}={v!r}"]
            cres[cid] = {}
            for et_id in et_ids:
                proc = subprocess.run([a.engine, a.model_dir, et_id, "--json", *flags],
                                      capture_output=True, text=True)
                if proc.returncode != 0:
                    print(f"ERROR quantifying configuration {cid} / {et_id}:\n"
                          f"{proc.stderr}", file=sys.stderr)
                    return 1
                cres[cid][et_id] = json.loads(proc.stdout)
            b, notes = [], []
            for et_id, r in sorted(cres[cid].items()):
                pb, pn = partition_problems(et_id, r)
                b += pb
                notes += pn
            if b:
                print(f"ERROR: configuration {cid}: event-tree partition violated:",
                      file=sys.stderr)
                for x in b:
                    print(f"  {x}", file=sys.stderr)
                return 1
            if trunc_flags:
                desc = ", ".join(f"{k} {bounds.fmt_interval(lo, hi, True)} /yr"
                                 for k, (lo, hi) in sorted(bounds.metric_totals(cres[cid]).items()))
            else:
                desc = ", ".join(f"{k} {v:.4e} /yr ({'x%.3g' % (v / base_m[k][0]) if base_m.get(k, (0.0,))[0] else 'base 0'})"
                                 for k, (v, _) in sorted(bounds.metric_totals(cres[cid]).items()))
            print(f"configuration {cid}: {desc}")
        with open(a.configurations, "w") as f:
            json.dump(cres, f, indent=2, sort_keys=True)
        print(f"quantified {len(cres)} configuration(s) -> {a.configurations}")

    settings = sampling_settings(results)
    if settings:
        n, seed, method = settings
        print(f"uncertainty: {n} samples, seed {seed}, {method} "
              f"(model-wide, summed over event trees per iteration)")
        for mid, d in sorted(metric_draws(results).items()):
            s = summarize(d)
            print(f"  {mid}: mean {s['mean']:.4e}  5% {s['p05']:.4e}  "
                  f"median {s['p50']:.4e}  95% {s['p95']:.4e} /yr")
        if a.importance_uncertainty:
            for mid in sorted({m["id"] for r in results.values() for m in r.get("metrics", [])}):
                top = [r["event"] for r in (importance.for_metric(results, mid) or
                                            {"importance": []})["importance"]
                       ][:a.importance_uncertainty]
                u = importance.uncertainty_for_metric(results, mid)
                if not u:
                    continue
                print(f"  {mid} importance under uncertainty (model-wide, top "
                      f"{len(top)} by point FV): FV mean [5%, 95%]")
                for e in (e for e in top if e in u["rows"]):
                    fv = u["rows"][e]["fussell_vesely"]
                    if fv:
                        print(f"    {e}: {fv['mean']:.4e} [{fv['p05']:.4e}, {fv['p95']:.4e}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
