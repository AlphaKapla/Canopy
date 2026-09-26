#!/usr/bin/env python3
"""Quantify every event tree in a model; write merged JSON results.

Usage: quantify.py <model-dir> <out.json> [--engine PATH]
                   [--samples N [--seed S] [--sampling srs|lhs]]

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
house events.

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

PARTITION_TOL = 1e-9


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model_dir")
    ap.add_argument("out_path")
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", "engine/target/release/canopy"))
    ap.add_argument("--samples", type=int)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--sampling", choices=["srs", "lhs"])
    a = ap.parse_args()
    if (a.seed is not None or a.sampling) and a.samples is None:
        ap.error("--seed and --sampling only apply with --samples")

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
        part = r.get("partition")
        if part is None:
            continue
        dev = part["sum_probability"] - 1.0
        if part["per_sequence_house_overrides"]:
            print(f"note: {et_id} has per-sequence house-event overrides; "
                  f"sum of sequence probabilities = {part['sum_probability']:.12f} "
                  f"(not required to be 1)")
        elif abs(dev) > PARTITION_TOL:
            bad.append(f"{et_id}: sum of sequence probabilities = "
                       f"{part['sum_probability']:.15f} (|deviation| "
                       f"{abs(dev):.3e} > {PARTITION_TOL:g})")
        for s in r.get("sequences", []):
            fol = s.get("followed")
            if not fol:
                continue
            p, tot = fol["probability"], fol["sum_probability"]
            if fol["per_sequence_house_overrides"]:
                print(f"note: {et_id}/{s['id']}: transfer expansions override "
                      f"house events; they sum to {tot:.6e} vs the transfer "
                      f"row's {p:.6e} (not required to match)")
            elif abs(tot - p) > PARTITION_TOL * max(abs(p), 1e-300):
                bad.append(f"{et_id}/{s['id']}: transfer expansions sum to "
                           f"{tot:.15e}, the transfer row has {p:.15e}")
    if bad:
        print("ERROR: event-tree partition violated (sequence table does not "
              "cover the outcome space exactly once):", file=sys.stderr)
        for b in bad:
            print(f"  {b}", file=sys.stderr)
        return 1

    with open(a.out_path, "w") as f:
        json.dump(results, f, indent=2, sort_keys=True)
    print(f"quantified {len(results)} event tree(s) -> {a.out_path}")

    settings = sampling_settings(results)
    if settings:
        n, seed, method = settings
        print(f"uncertainty: {n} samples, seed {seed}, {method} "
              f"(model-wide, summed over event trees per iteration)")
        for mid, d in sorted(metric_draws(results).items()):
            s = summarize(d)
            print(f"  {mid}: mean {s['mean']:.4e}  5% {s['p05']:.4e}  "
                  f"median {s['p50']:.4e}  95% {s['p95']:.4e} /yr")
    return 0


if __name__ == "__main__":
    sys.exit(main())
