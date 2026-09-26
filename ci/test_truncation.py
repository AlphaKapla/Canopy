#!/usr/bin/env python3
"""Tests for truncated quantification (FR-34): `canopy --truncated CUTOFF`.

Hand-computed fixtures, each a small coherent fault tree whose minimal cut
sets, retained set, bounds and exact P(top) are worked out in the comments:
  * OR/AND with a cut set below the cut-off, and with an order limit;
  * a 2-of-3 vote gate;
  * house events (false: a branch vanishes; --house true: it returns);
  * a tautology (true house event in an OR: the empty cut set, P = 1);
and every refusal: non-coherent logic, event trees, --samples,
--prime-implicants, and malformed or out-of-range cut-offs. The JSON must
carry the bounds and no `probability` field.

Usage: python ci/test_truncation.py [--engine PATH]
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
PROV = {"source": "ci/test_truncation.py", "justification": "hand-computed fixture"}


def write_model(d, bes, gates, top, houses=None):
    """A minimal model directory with one fault tree FT-T."""
    os.makedirs(os.path.join(d, "basic-events"))
    os.makedirs(os.path.join(d, "fault-trees"))
    dump = lambda p, o: open(os.path.join(d, p), "w").write(yaml.safe_dump(o, sort_keys=True))
    dump("model.yaml", {
        "schema_version": "0.1.0",
        "model": {"id": "TRUNC-TEST", "name": "truncation fixture", "risk_metrics": []},
        "includes": {"parameters": ["parameters.yaml"],
                     "basic_events": ["basic-events/*.yaml"],
                     "fault_trees": ["fault-trees/*.yaml"],
                     "house_events": ["house-events.yaml"]}})
    dump("parameters.yaml", {"parameters": {}})
    dump("house-events.yaml", {"house_events": {
        h: {"label": "fixture house event", "default": v, "provenance": PROV}
        for h, v in (houses or {}).items()}})
    dump("basic-events/be.yaml", {"basic_events": {
        b: {"label": f"fixture event {b}",
            "failure_model": {"type": "probability",
                              "value": {"value": p, "unit": "per_demand"}},
            "provenance": PROV} for b, p in bes.items()}})
    dump("fault-trees/ft.yaml", {"fault_trees": {"FT-T": {
        "label": "fixture tree", "top_gate": top,
        "gates": {g: {"label": f"fixture gate {g}", "formula": f} for g, f in gates.items()}}}})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", os.path.join(ROOT, "engine/target/release/canopy")))
    a = ap.parse_args()
    failures = []

    def check(cond, msg):
        (print(f"  ok  {msg}") if cond else failures.append(msg))

    def close(x, y, rel=1e-12):
        return abs(x - y) <= rel * max(abs(x), abs(y), 1e-300)

    def run(d, *args, target="FT-T"):
        return subprocess.run([a.engine, d, target, "--json", *args],
                              capture_output=True, text=True)

    def trunc(d, cutoff, *extra):
        r = run(d, "--truncated", cutoff, *extra)
        if r.returncode != 0:
            failures.append(f"engine failed on --truncated {cutoff} {extra}: {r.stderr}")
            return None
        return json.loads(r.stdout)

    def cuts(j):
        return sorted(sorted(c["events"]) for c in j["minimal_cut_sets"])

    tmp = tempfile.mkdtemp(prefix="psa-trunc-")
    try:
        # 1) top = D or (A and B) or (A and C); P(A)=0.1, P(B)=0.2,
        #    P(C)=0.01, P(D)=0.05. MCS {D} 0.05, {A,B} 0.02, {A,C} 0.001.
        #    exact: 1 − (1 − 0.05)(1 − 0.1·(1 − 0.8·0.99)) = 0.06976
        d = os.path.join(tmp, "orand")
        write_model(d, {"BE-A": 0.1, "BE-B": 0.2, "BE-C": 0.01, "BE-D": 0.05},
                    {"GT-TOP": {"or": ["BE-D", "GT-AB", "GT-AC"]},
                     "GT-AB": {"and": ["BE-A", "BE-B"]},
                     "GT-AC": {"and": ["BE-A", "BE-C"]}}, "GT-TOP")
        exact = 1 - 0.95 * (1 - 0.1 * (1 - 0.8 * 0.99))
        e = json.loads(run(d, "--prob-only").stdout)["probability"]
        check(close(e, exact), f"fixture 1 exact P(top) {e} = hand-computed {exact}")
        j = trunc(d, "0.005")
        if j:
            # retained {D}, {A,B}: lower = 0.05 + 0.02 − 0.05·0.02 = 0.069;
            # dropped {A,C}: bound 0.001, upper 0.070
            check("probability" not in j and j["method"] == "truncated-mcs",
                  "JSON: method truncated-mcs, no 'probability' field")
            check(cuts(j) == [["BE-A", "BE-B"], ["BE-D"]] and j["retained_cut_sets"] == 2,
                  f"cut-off 5e-3 retains {{D}}, {{A,B}}: {cuts(j)}")
            check(close(j["probability_lower_bound"], 0.069),
                  f"lower = P(D ∪ AB) = 0.069: {j['probability_lower_bound']}")
            check(close(j["truncation_error_bound"], 0.001),
                  f"error bound = P(AC) = 0.001: {j['truncation_error_bound']}")
            check(close(j["probability_upper_bound"], 0.070),
                  f"upper = 0.070: {j['probability_upper_bound']}")
            check(close(j["rare_event_sum"], 0.07), "rare-event sum 0.05 + 0.02")
            check(j["probability_lower_bound"] <= exact <= j["probability_upper_bound"],
                  "exact 0.06976 within the bounds")
            check(j["cutoff"] == 0.005 and "order_limit" not in j, "cutoff echoed")
        j = trunc(d, "0.02")
        if j:
            # a cut set exactly at the cut-off is kept: 0.1 · 0.2 = 0.020000000000000004 ≥ 0.02
            check(cuts(j) == [["BE-A", "BE-B"], ["BE-D"]],
                  f"cut-off 0.02: {{A,B}} (P = 0.1·0.2) kept at the cut-off: {cuts(j)}")
        j = trunc(d, "0.0201")
        if j:
            # {D} only. Lost terms: {A,B} (0.02) and {C} itself (0.01) —
            # C alone is below the cut-off, so it is dropped where it
            # appears and stands for every product containing it, {A,C}
            # included: bound 0.03 (valid, looser than P(AB) + P(AC))
            check(cuts(j) == [["BE-D"]] and close(j["probability_lower_bound"], 0.05)
                  and close(j["truncation_error_bound"], 0.03)
                  and close(j["probability_upper_bound"], 0.08),
                  f"cut-off 0.0201: {{D}} only, bound P(AB) + P(C) = 0.03: {cuts(j)}, "
                  f"{j['truncation_error_bound']}")
        j = trunc(d, "0", "--order-limit", "1")
        if j:
            # order ≤ 1: {D}; lost {A,B}, {A,C}: bound 0.021, upper 0.071
            check(cuts(j) == [["BE-D"]] and close(j["truncation_error_bound"], 0.021)
                  and close(j["probability_upper_bound"], 0.071) and j["order_limit"] == 1,
                  f"order limit 1: {{D}}, bound 0.021: {cuts(j)}, {j['truncation_error_bound']}")
        j = trunc(d, "0")
        if j:
            check(len(cuts(j)) == 3 and j["truncation_error_bound"] == 0.0
                  and close(j["probability_lower_bound"], exact)
                  and j["probability_upper_bound"] == j["probability_lower_bound"],
                  "cut-off 0: every cut set, exact, zero bound")
        j = trunc(d, "0.9")
        if j:
            check(cuts(j) == [] and j["probability_lower_bound"] == 0.0
                  and j["probability_lower_bound"] <= exact <= j["probability_upper_bound"],
                  f"cut-off 0.9: nothing retained, lower 0, upper ≥ exact "
                  f"({j['probability_upper_bound']})")
        r = subprocess.run([a.engine, d, "FT-T", "--truncated", "0.005"],
                           capture_output=True, text=True)
        check(r.returncode == 0 and "6.900000e-2 <= P(top) <= 7.000000e-2" in r.stdout,
              f"text report states the bounds: {r.stdout[:300]}")

        # 2) 2-of-3 vote, P = 0.1, 0.2, 0.3: MCS {A,B} 0.02, {A,C} 0.03,
        #    {B,C} 0.06; exact 0.02 + 0.03 + 0.06 − 2·0.006 = 0.098.
        #    cut-off 0.025: {A,C}, {B,C}; lower P(C)·P(A ∪ B) = 0.3·0.28 = 0.084;
        #    dropped {A,B}: upper 0.104
        d = os.path.join(tmp, "vote")
        write_model(d, {"BE-A": 0.1, "BE-B": 0.2, "BE-C": 0.3},
                    {"GT-TOP": {"atleast": {"k": 2, "of": ["BE-A", "BE-B", "BE-C"]}}}, "GT-TOP")
        j = trunc(d, "0.025")
        if j:
            check(cuts(j) == [["BE-A", "BE-C"], ["BE-B", "BE-C"]]
                  and close(j["probability_lower_bound"], 0.084)
                  and close(j["probability_upper_bound"], 0.104),
                  f"2-of-3 at 0.025: {cuts(j)}, [{j['probability_lower_bound']}, "
                  f"{j['probability_upper_bound']}] = [0.084, 0.104] ∋ 0.098")

        # 3) house events: top = B or (A and HE-X); HE-X false by default
        d = os.path.join(tmp, "house")
        write_model(d, {"BE-A": 0.1, "BE-B": 0.2},
                    {"GT-TOP": {"or": ["BE-B", "GT-AX"]},
                     "GT-AX": {"and": ["BE-A", "HE-X"]}}, "GT-TOP", houses={"HE-X": False})
        j = trunc(d, "0")
        if j:
            check(cuts(j) == [["BE-B"]] and close(j["probability_lower_bound"], 0.2),
                  "house event false: the A branch vanishes")
        j = trunc(d, "0", "--house", "HE-X=true")
        if j:
            check(cuts(j) == [["BE-A"], ["BE-B"]]
                  and close(j["probability_lower_bound"], 1 - 0.9 * 0.8),
                  "--house HE-X=true: {A}, {B}, exact 0.28")

        # 4) tautology: top = HE-T or A with HE-T true: the empty cut set
        d = os.path.join(tmp, "taut")
        write_model(d, {"BE-A": 0.1}, {"GT-TOP": {"or": ["HE-T", "BE-A"]}}, "GT-TOP",
                    houses={"HE-T": True})
        j = trunc(d, "0.5")
        if j:
            check(cuts(j) == [[]] and j["probability_lower_bound"] == 1.0
                  and j["truncation_error_bound"] == 0.0,
                  "tautology: the empty cut set, P = 1, zero bound")

        # 5) refusals
        d = os.path.join(tmp, "noncoh")
        write_model(d, {"BE-A": 0.1, "BE-B": 0.2},
                    {"GT-TOP": {"and": ["BE-A", {"not": "BE-B"}]}}, "GT-TOP")
        r = run(d, "--truncated", "1e-6")
        check(r.returncode != 0 and "needs coherent logic" in r.stderr, "non-coherent refused")
        model = os.path.join(ROOT, "model")
        for args, why in [(["--truncated", "1e-6"], "event tree"),
                          (["--truncated", "1e-6", "--samples", "10"], "--samples"),
                          (["--truncated", "1e-6", "--prime-implicants"], "--prime-implicants")]:
            tgt = "ET-SLOCA" if why == "event tree" else "FT-ECCS-INJECTION"
            r = run(model, *args, target=tgt)
            check(r.returncode != 0 and "--truncated applies to fault trees" in r.stderr,
                  f"refused: {why}")
        for bad in ("abc", "1", "1.5", "-0.1"):
            r = run(model, "--truncated", bad, target="FT-ECCS-INJECTION")
            check(r.returncode != 0 and "--truncated needs" in r.stderr,
                  f"refused: cut-off {bad!r}")
        r = subprocess.run([a.engine, model, "FT-ECCS-INJECTION", "--truncated"],
                           capture_output=True, text=True)
        check(r.returncode != 0 and "--truncated needs" in r.stderr, "refused: missing cut-off")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("truncation: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
