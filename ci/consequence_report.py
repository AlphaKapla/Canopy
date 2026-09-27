#!/usr/bin/env python3
"""Aggregate the minimal-cut-set and basic-event-importance tables for a
named consequence (e.g. core damage), pooled across every sequence in
every event tree that reaches it.

The exact consequence frequency is the sum of the (BDD-exact) per-sequence
frequencies already in the results JSON -- nothing is re-derived. The cut
table is the standard minimal-cut-set-based PSA report: sequence cut sets
follow the delete-term convention (see docs/model-format.md), so pooling
can slightly overstate the exact total where cut sets overlap across
sequences -- this is normal industry practice, not a bug; the `coverage`
figure this script prints quantifies it.

Basic-event importance is BDD-exact (ci/importance.py): Fussell-Vesely,
RAW, RRW and Birnbaum from the engine's exact conditional frequencies
F(x=1), F(x=0) per end state, summed across event trees -- success
branches included, no cut-set overlap. The minimal-cut-set Fussell-Vesely
(sum of the frequencies of pooled cut sets containing the event, over the
total) is still printed next to it as the familiar approximation, for
comparison. Results quantified with --prob-only carry no exact importance;
the script then says so and prints the cut-set measure only.

Usage:
  quantify.py already wrote results.json (see ci/quantify.py). Then:
    consequence_report.py results.json --end-state CD
    consequence_report.py results.json --metric CDF --model model
    consequence_report.py results.json --end-state CD --json --top 15
"""
import argparse
import json
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importance  # noqa: E402


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def resolve_end_states(args) -> tuple[set, str]:
    if args.metric:
        if not args.model:
            die("--metric requires --model to resolve risk_metrics from model.yaml")
        manifest_path = os.path.join(args.model, "model.yaml")
        manifest = yaml.safe_load(open(manifest_path))
        metrics = manifest.get("model", {}).get("risk_metrics", [])
        for m in metrics:
            if m["id"] == args.metric:
                return set(m["end_states"]), f"{args.metric} ({m.get('label', '')})"
        die(f"metric {args.metric!r} not found in {manifest_path} risk_metrics")
    if not args.end_state:
        die("pass --metric ID or one or more --end-state STATE")
    return set(args.end_state), "+".join(sorted(args.end_state))


def aggregate(results: dict, end_states: set, mcs_limit: int = 1000) -> dict:
    """Pool per-sequence cut sets (already BDD-exact, delete-term
    convention) into a single ranked cut-set table and a minimal-cut-set
    Fussell-Vesely basic-event importance table, for every sequence in
    every event tree whose end_state is in `end_states`. Pure aggregation
    of already-quantified numbers; no new quantification is done here."""
    total_freq = 0.0
    cut_pool: dict[frozenset, dict] = {}
    untracked = []   # (et_id, seq_id, freq): contributes to total, no cut sets listed
    truncated = []   # (et_id, seq_id, n): cut set count hit mcs_limit exactly -- possible cutoff

    for et_id, et in results.items():
        for seq in et.get("sequences", []):
            # A transfer row is never counted (FR-11): a followed transfer
            # is carried by its expansion rows, an unfollowed one belongs
            # to the target tree's analysis (V&V anomaly D-10).
            if seq["end_state"] not in end_states or seq.get("transfer"):
                continue
            total_freq += seq["frequency_per_year"]
            cuts = seq.get("cut_sets", [])
            # Non-coherent failure logic: its prime implicants (when the
            # results carry them, quantify.py --prime-implicants) pool like
            # cut sets; a negated event is the literal "¬BE-..." in the key.
            primes = seq.get("prime_implicants") or []
            if not cuts and not primes and seq["frequency_per_year"] > 0:
                untracked.append((et_id, seq["id"], seq["frequency_per_year"]))
            products = [(frozenset(cs["events"]), cs["frequency_per_year"]) for cs in cuts]
            products += [(frozenset(p["events"]) | frozenset("¬" + e for e in p["negated"]),
                          p["frequency_per_year"]) for p in primes]
            for key, f in products:
                entry = cut_pool.setdefault(key, {"freq": 0.0, "from": set()})
                entry["freq"] += f
                entry["from"].add(f"{et_id}/{seq['id']}")
            if len(cuts) == mcs_limit or len(primes) == mcs_limit:
                truncated.append((et_id, seq["id"], max(len(cuts), len(primes))))

    # Ties are broken by content, never by set/dict iteration order (which
    # follows per-process string hashing): output is reproducible (NFR-1,
    # V&V anomaly D-11).
    ranked_cuts = sorted(cut_pool.items(),
                         key=lambda kv: (-kv[1]["freq"], sorted(kv[0])))

    be_importance: dict[str, dict] = {}
    for key, entry in cut_pool.items():
        for be in key:
            if be.startswith("¬"):
                continue       # a working component contributes no failure
            bi = be_importance.setdefault(be, {"freq": 0.0, "n_cutsets": 0})
            bi["freq"] += entry["freq"]
            bi["n_cutsets"] += 1
    ranked_be = sorted(be_importance.items(), key=lambda kv: (-kv[1]["freq"], kv[0]))

    pooled_total = sum(e["freq"] for e in cut_pool.values())
    coverage = pooled_total / total_freq if total_freq else float("nan")

    return {
        "total_freq": total_freq,
        "pooled_total": pooled_total,
        "coverage": coverage,
        "ranked_cuts": ranked_cuts,
        "ranked_be": ranked_be,
        "untracked": untracked,
        "truncated": truncated,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", help="JSON written by ci/quantify.py")
    ap.add_argument("--metric", help="risk metric id from model.yaml, e.g. CDF")
    ap.add_argument("--end-state", action="append", default=[],
                     help="sequence end state to include (repeatable); "
                          "alternative to --metric")
    ap.add_argument("--model", help="model dir, required with --metric")
    ap.add_argument("--top", type=int, default=25,
                     help="rows to print per table (default 25; 0 = all)")
    ap.add_argument("--mcs-limit", type=int, default=1000,
                     help="the --mcs-limit the results were quantified with "
                          "(default 1000, canopy's own default); used only "
                          "to detect truncated sequences")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    end_states, label = resolve_end_states(args)
    results = json.load(open(args.results))

    agg = aggregate(results, end_states, args.mcs_limit)
    exact = importance.for_end_states(results, end_states)
    # model-wide importance under uncertainty (FR-37): per metric, when the
    # trees were sampled with --importance-events (quantify.py
    # --importance-uncertainty K)
    unc, unc_note = None, None
    if args.metric:
        try:
            unc = importance.uncertainty_for_metric(results, args.metric)
        except importance.IncompleteDraws as e:
            unc_note = f"importance under uncertainty unavailable: {e}"
    unc_order = ([r["event"] for r in exact["importance"] if unc and r["event"] in unc["rows"]]
                 if exact else sorted(unc["rows"]) if unc else [])
    total_freq = agg["total_freq"]
    pooled_total = agg["pooled_total"]
    coverage = agg["coverage"]
    ranked_cuts = agg["ranked_cuts"]
    ranked_be = agg["ranked_be"]
    untracked = agg["untracked"]
    truncated = agg["truncated"]

    top = args.top if args.top > 0 else None

    if args.json:
        out = {
            "consequence": label,
            "end_states": sorted(end_states),
            "total_frequency_per_year": total_freq,
            "pooled_cut_set_frequency_per_year": pooled_total,
            "coverage": coverage,
            "cut_sets": [
                {"events": sorted(x for x in k if not x.startswith("¬")),
                 "negated": sorted(x[1:] for x in k if x.startswith("¬")),
                 "frequency_per_year": e["freq"],
                 "fraction": e["freq"] / total_freq if total_freq else 0.0,
                 "sequences": sorted(e["from"])}
                for k, e in ranked_cuts[:top]
            ],
            "basic_event_importance": [
                {"event": be, "frequency_per_year": e["freq"],
                 "fraction": e["freq"] / total_freq if total_freq else 0.0,
                 "cut_sets": e["n_cutsets"]}
                for be, e in ranked_be[:top]
            ],
            "bdd_exact_importance": (
                {"frequency_per_year": exact["frequency_per_year"],
                 "importance": exact["importance"][:top]}
                if exact else None),
            "untracked_sequences": [
                {"event_tree": et, "sequence": sid, "frequency_per_year": f}
                for et, sid, f in untracked
            ],
            "importance_uncertainty": (
                [{"event": e, **{k: v for k, v in unc["rows"][e].items()
                                 if not k.startswith("draws")}} for e in unc_order]
                if unc else None),
        }
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0

    print(f"consequence      : {label}  (end states: {', '.join(sorted(end_states))})")
    print(f"total frequency  : {total_freq:.4e} /yr")
    print(f"pooled cut sets  : {len(ranked_cuts)}  "
          f"(sum {pooled_total:.4e} /yr, coverage {coverage:.1%})")
    if untracked:
        print("WARNING: sequences contributing frequency with no cut sets or prime "
              "implicants listed (non-coherent logic quantified without "
              "--prime-implicants, or mcs-limit 0):")
        for et_id, sid, f in untracked:
            print(f"    {et_id}/{sid}  {f:.4e} /yr")
    if truncated:
        print(f"WARNING: sequence cut-set counts hit --mcs-limit "
              f"({args.mcs_limit}) exactly -- likely truncated, re-quantify "
              f"with a higher limit and confirm the count changes:")
        for et_id, sid, n in truncated:
            print(f"    {et_id}/{sid}  {n} cut sets")

    print()
    print(f"minimal cut sets ({label}):")
    for k, e in ranked_cuts[:top]:
        frac = e["freq"] / total_freq if total_freq else 0.0
        print(f"  {e['freq']:>12.4e} /yr  {frac:>6.1%}  {{{', '.join(sorted(k))}}}")

    print()
    if exact:
        mcs_fv = {be: e["freq"] / total_freq if total_freq else 0.0
                  for be, e in ranked_be}
        print(f"basic event importance ({label}, BDD-exact, "
              f"ranked by Fussell-Vesely):")
        print(f"  {'FV':>8} {'RAW':>10} {'RRW':>10} {'Birnbaum/yr':>12} "
              f"{'MCS-FV':>8}  event")
        opt = lambda x, spec: format(x, spec) if x is not None else "inf/undef"
        for r in exact["importance"][:top]:
            fv = r["fussell_vesely"]
            print(f"  {opt(fv, '>8.2%'):>8} {opt(r['raw'], '>10.4g'):>10} "
                  f"{opt(r['rrw'], '>10.4g'):>10} "
                  f"{r['birnbaum_per_year']:>12.4e} "
                  f"{mcs_fv.get(r['event'], 0.0):>8.2%}  {r['event']}")
    else:
        print("basic event importance: BDD-exact measures unavailable (results "
              "quantified with --prob-only or by an older engine); "
              "minimal-cut-set Fussell-Vesely only:")
        for be, e in ranked_be[:top]:
            frac = e["freq"] / total_freq if total_freq else 0.0
            print(f"  {frac:>6.1%}  {e['freq']:>12.4e} /yr  "
                  f"(in {e['n_cutsets']} cut sets)  {be}")

    if unc:
        print()
        print(f"importance under uncertainty ({label}, model-wide, "
              f"{len(unc['frequency_draws'])} iterations; mean [5%, 95%]):")
        print(f"  {'FV':>34} {'RAW':>34}  event")
        cell = lambda s: (f"{s['mean']:.3e} [{s['p05']:.3e}, {s['p95']:.3e}]"
                          if s else "undefined")
        for e in unc_order:
            r = unc["rows"][e]
            print(f"  {cell(r['fussell_vesely']):>34} {cell(r['raw']):>34}  {e}")
    elif unc_note:
        print()
        print(unc_note)

    print()
    print("_Cut sets follow the delete-term convention; pooled frequency can "
          "exceed the exact total where cut sets overlap across sequences "
          "(coverage > 100%). FV/RAW/RRW/Birnbaum are BDD-exact across all "
          "qualifying sequences (success branches included); MCS-FV is the "
          "minimal-cut-set approximation, shown for comparison._")
    return 0


if __name__ == "__main__":
    sys.exit(main())
