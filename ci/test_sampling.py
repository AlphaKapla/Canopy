#!/usr/bin/env python3
"""End-to-end tests of the Monte Carlo sampling layouts (--sampling
srs|lhs), through the engine binary, ci/quantify.py and ci/compare.py.

Fixture: one fault tree whose top is a single basic event with a uniform
distribution on [LO, HI], so every P(top) draw IS a sample of that
quantity and its stratum can be read off directly.

  * LHS stratifies: the N draws fall one per stratum [LO + k·w/N,
    LO + (k+1)·w/N), k = 0..N−1 (w = HI − LO); simple random sampling at
    the same N does not (all strata hit once has probability N!/N^N).
  * LHS mean is within the deterministic bound |mean − μ| ≤ w/(2N): each
    draw is within w/(2N) of its stratum's midpoint and the midpoints
    average to μ. (SRS has no such bound.)
  * reproducible: a rerun is byte-identical, for both layouts;
  * diff-stable: adding an unrelated uncertain quantity to the model
    leaves the draws of the first one unchanged, for both layouts;
  * paired comparisons: compare.py shows a paired change band only for
    base and head with the same N, seed and method.

Usage: python ci/test_sampling.py [--engine PATH]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROV = {"source": "test", "justification": "sampling-layout fixture"}
LO, HI = 1e-3, 5e-3
N = 1000


def write_model(d, extra_quantity=False):
    bes = {"BE-X": {"label": "sampled event", "provenance": PROV,
                    "failure_model": {"type": "probability", "value": {
                        "value": (LO + HI) / 2, "unit": "per_demand",
                        "uncertainty": {"distribution": "uniform",
                                        "lower": LO, "upper": HI}}}}}
    gates = {"GT-TOP": {"label": "top gate", "formula": "BE-X"}}
    if extra_quantity:
        bes["BE-Y"] = {"label": "unrelated event", "provenance": PROV,
                       "failure_model": {"type": "probability", "value": {
                           "value": 2e-3, "unit": "per_demand",
                           "uncertainty": {"distribution": "lognormal",
                                           "error_factor": 3.0}}}}
        gates["GT-OTHER"] = {"label": "other gate", "formula": "BE-Y"}
    files = {
        "model.yaml": {"schema_version": "0.1.0",
                       "model": {"id": "SAMPLING-TEST", "name": "sampling fixture",
                                 "risk_metrics": [{"id": "CDF", "label": "cd",
                                                   "end_states": ["CD"]}]},
                       "includes": {"parameters": ["parameters.yaml"],
                                    "house_events": ["house-events.yaml"],
                                    "basic_events": ["basic-events/*.yaml"],
                                    "fault_trees": ["fault-trees/*.yaml"],
                                    "event_trees": ["event-trees/*.yaml"]}},
        "parameters.yaml": {"parameters": {}},
        "house-events.yaml": {"house_events": {}},
        "basic-events/be.yaml": {"basic_events": bes},
        "fault-trees/ft.yaml": {"fault_trees": {"FT-T": {
            "label": "sampling tree", "top_gate": "GT-TOP", "gates": gates}}},
        "event-trees/et.yaml": {"event_tree": {
            "id": "ET-T", "label": "sampling tree",
            "initiating_event": {"id": "IE-T", "label": "initiator",
                                 "frequency": {"value": 1e-2, "unit": "per_year"},
                                 "provenance": PROV},
            "functional_events": {"FE-1": {"label": "top", "top_gate": "GT-TOP"}},
            "sequences": {"SEQ-1": {"path": {"FE-1": "success"}, "end_state": "OK"},
                          "SEQ-2": {"path": {"FE-1": "failure"}, "end_state": "CD"}}}},
    }
    if extra_quantity:
        files["fault-trees/ft.yaml"]["fault_trees"]["FT-O"] = {
            "label": "other tree", "top_gate": "GT-OTHER", "gates": {}}
    for rel, obj in files.items():
        os.makedirs(os.path.dirname(os.path.join(d, rel)) or d, exist_ok=True)
        with open(os.path.join(d, rel), "w") as f:
            yaml.safe_dump(obj, f, sort_keys=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", os.path.join(ROOT, "engine/target/release/canopy")))
    a = ap.parse_args()
    failures = []

    def check(cond, msg):
        (print(f"  ok  {msg}") if cond else failures.append(msg))

    tmp = tempfile.mkdtemp(prefix="psa-sampling-")
    try:
        d1, d2 = os.path.join(tmp, "m1"), os.path.join(tmp, "m2")
        write_model(d1)
        write_model(d2, extra_quantity=True)

        def draws(d, method, seed="11"):
            p = subprocess.run([a.engine, d, "FT-T", "--json", "--prob-only",
                                "--samples", str(N), "--seed", seed,
                                "--sampling", method, "--keep-samples"],
                               capture_output=True, text=True, check=True)
            return p.stdout, json.loads(p.stdout)["uncertainty"]

        w = HI - LO
        for method in ("srs", "lhs"):
            raw, u = draws(d1, method)
            x = u["draws"]
            check(u["method"] == method and len(x) == N,
                  f"{method}: {N} draws, method reported")
            strata = sorted(min(int((v - LO) / w * N), N - 1) for v in x)
            one_each = strata == list(range(N))
            if method == "lhs":
                check(one_each, "lhs: one draw in each of the N strata")
                check(abs(u["mean"] - (LO + HI) / 2) <= w / (2 * N),
                      f"lhs: |mean - mu| = {abs(u['mean'] - (LO + HI) / 2):.3e} "
                      f"<= w/(2N) = {w / (2 * N):.3e}")
            else:
                check(not one_each, "srs: not stratified (as expected)")
            check(draws(d1, method)[0] == raw, f"{method}: rerun byte-identical")
            _, u2 = draws(d2, method)
            check(u2["draws"] == x,
                  f"{method}: an unrelated added quantity leaves the draws unchanged")
            check(draws(d1, method, seed="12")[1]["draws"] != x,
                  f"{method}: another seed gives other draws")

        # compare.py pairs only identical (N, seed, method)
        res = {}
        for key, extra in (("srs", []), ("lhs", ["--sampling", "lhs"]),
                           ("lhs2", ["--sampling", "lhs"])):
            out = os.path.join(tmp, f"{key}.json")
            subprocess.run([sys.executable, os.path.join(HERE, "quantify.py"), d1, out,
                            "--engine", a.engine, "--samples", "500", "--seed", "3",
                            *extra], check=True, capture_output=True)
            res[key] = out
        cmp = lambda b, h: subprocess.run(
            [sys.executable, os.path.join(HERE, "compare.py"), res[b], res[h]],
            capture_output=True, text=True, check=True).stdout
        check("paired change head − base" in cmp("lhs", "lhs2"),
              "compare: lhs vs lhs (same N, seed) is paired")
        m = cmp("srs", "lhs")
        check("not paired" in m and "paired change" not in m,
              "compare: srs vs lhs is not paired")
        # refused without --samples
        p = subprocess.run([a.engine, d1, "FT-T", "--sampling", "lhs"],
                           capture_output=True, text=True)
        check(p.returncode != 0 and "only apply with --samples" in p.stderr,
              "--sampling without --samples refused")
        p = subprocess.run([a.engine, d1, "FT-T", "--samples", "10",
                            "--sampling", "bogus"], capture_output=True, text=True)
        check(p.returncode != 0, "unknown sampling method refused")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("sampling layouts: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
