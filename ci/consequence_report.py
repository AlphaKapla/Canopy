#!/usr/bin/env python3
"""Aggregate the minimal-cut-set and basic-event-importance tables for a
named consequence (e.g. core damage), pooled across every sequence in
every event tree that reaches it.

The exact consequence frequency is the sum of the (BDD-exact) per-sequence
frequencies already in the results JSON -- nothing is re-derived. The cut
set and importance tables are the standard minimal-cut-set-based PSA
report: sequence cut sets follow the delete-term convention (see
docs/model-format.md), so pooling can slightly overstate the exact total
where cut sets overlap across sequences -- this is normal industry
practice, not a bug; the `coverage` figure this script prints quantifies
it. Basic-event importance here is the minimal-cut-set Fussell-Vesely
measure (sum of the frequencies of cut sets containing the event, divided
by the total): this is a cut-set-based approximation, not the BDD-exact
Birnbaum importance the engine reports for single fault trees.

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
            if seq["end_state"] not in end_states:
                continue
            total_freq += seq["frequency_per_year"]
            cuts = seq.get("cut_sets", [])
            if not cuts and seq["frequency_per_year"] > 0:
                untracked.append((et_id, seq["id"], seq["frequency_per_year"]))
            for cs in cuts:
                key = frozenset(cs["events"])
                entry = cut_pool.setdefault(key, {"freq": 0.0, "from": set()})
                entry["freq"] += cs["frequency_per_year"]
                entry["from"].add(f"{et_id}/{seq['id']}")
            if len(cuts) == mcs_limit:
                truncated.append((et_id, seq["id"], len(cuts)))

    ranked_cuts = sorted(cut_pool.items(), key=lambda kv: -kv[1]["freq"])

    be_importance: dict[str, dict] = {}
    for key, entry in cut_pool.items():
        for be in key:
            bi = be_importance.setdefault(be, {"freq": 0.0, "n_cutsets": 0})
            bi["freq"] += entry["freq"]
            bi["n_cutsets"] += 1
    ranked_be = sorted(be_importance.items(), key=lambda kv: -kv[1]["freq"])

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
                {"events": sorted(k), "frequency_per_year": e["freq"],
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
            "untracked_sequences": [
                {"event_tree": et, "sequence": sid, "frequency_per_year": f}
                for et, sid, f in untracked
            ],
        }
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0

    print(f"consequence      : {label}  (end states: {', '.join(sorted(end_states))})")
    print(f"total frequency  : {total_freq:.4e} /yr")
    print(f"pooled cut sets  : {len(ranked_cuts)}  "
          f"(sum {pooled_total:.4e} /yr, coverage {coverage:.1%})")
    if untracked:
        print("WARNING: sequences contributing frequency with no cut sets listed "
              "(non-coherent logic, or mcs-limit 0):")
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
    print(f"basic event importance ({label}, minimal-cut-set Fussell-Vesely):")
    for be, e in ranked_be[:top]:
        frac = e["freq"] / total_freq if total_freq else 0.0
        print(f"  {frac:>6.1%}  {e['freq']:>12.4e} /yr  "
              f"(in {e['n_cutsets']} cut sets)  {be}")

    print()
    print("_Cut sets follow the delete-term convention; pooled frequency can "
          "exceed the exact total where cut sets overlap across sequences "
          "(coverage > 100%). Importance is the minimal-cut-set "
          "Fussell-Vesely measure, not BDD-exact Birnbaum._")
    return 0


if __name__ == "__main__":
    sys.exit(main())
