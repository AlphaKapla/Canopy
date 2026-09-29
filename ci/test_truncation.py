#!/usr/bin/env python3
"""Tests for truncated quantification: `canopy --truncated CUTOFF` on fault
trees (FR-34) and event trees (FR-39).

Hand-computed fixtures, each small and coherent, with minimal cut sets,
retained sets, bounds and exact values worked out in the comments:
  * OR/AND with a cut set below the cut-off, and with an order limit;
  * a 2-of-3 vote gate;
  * house events (false: a branch vanishes; --house true: it returns);
  * a tautology (true house event in an OR: the empty cut set, P = 1);
  * an event tree over two functional events sharing an event: every
    sequence's frequency bounds (P(F) − P(F ∧ S) with each side bounded),
    the retained failure-logic cut sets, the metric bounds, exactness at
    cut-off 0;
and every refusal: non-coherent logic (fault tree, functional event),
--samples, --prime-implicants, and malformed or out-of-range cut-offs.
The JSON carries bounds and no `probability` / `frequency_per_year` /
`value_per_year` field.

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


def write_model(d, bes, gates, top, houses=None, et=None):
    """A minimal model directory with one fault tree FT-T (and optionally
    one event tree ET-T: `et` = (IE frequency, {FE: top}, {SEQ: (path,
    end state)}), metric CDF over CD)."""
    os.makedirs(os.path.join(d, "basic-events"))
    os.makedirs(os.path.join(d, "fault-trees"))
    dump = lambda p, o: open(os.path.join(d, p), "w").write(yaml.safe_dump(o, sort_keys=True))
    includes = {"parameters": ["parameters.yaml"],
                "basic_events": ["basic-events/*.yaml"],
                "fault_trees": ["fault-trees/*.yaml"],
                "house_events": ["house-events.yaml"]}
    metrics = []
    if et:
        os.makedirs(os.path.join(d, "event-trees"))
        includes["event_trees"] = ["event-trees/*.yaml"]
        metrics = [{"id": "CDF", "label": "core damage", "end_states": ["CD"]}]
        freq, fes, seqs = et
        dump("event-trees/et.yaml", {"event_tree": {
            "id": "ET-T", "label": "fixture tree",
            "initiating_event": {"id": "IE-T", "label": "initiator", "provenance": PROV,
                                 "frequency": {"value": freq, "unit": "per_year"}},
            "functional_events": {fe: {"label": fe, "top_gate": t} for fe, t in fes.items()},
            "sequences": {sid: {"path": v[0], "end_state": v[1],
                                **({"house_events": v[2]} if len(v) > 2 else {})}
                          for sid, v in seqs.items()}}})
    dump("model.yaml", {
        "schema_version": "0.1.0",
        "model": {"id": "TRUNC-TEST", "name": "truncation fixture", "risk_metrics": metrics},
        "includes": includes})
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
        j = trunc(d, "0.005", "--upper-budget", "0")
        if j:
            # retained {D}, {A,B}: lower = 0.05 + 0.02 − 0.05·0.02 = 0.069;
            # dropped {A,C}: bound 0.001, upper 0.070 (the sum bound: FR-34)
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
            check(j["upper_bound_method"] == "sum" and j["upper_bound_split"] is None,
                  "--upper-budget 0: the sum bound")
        j = trunc(d, "0.005")
        if j:
            # FR-46: the union D ∪ AB ∪ AC of the retained cut sets and the
            # lost term is the whole function: upper = exact 0.06976
            check(close(j["probability_upper_bound"], exact) and j["upper_bound_method"] == "union"
                  and close(j["probability_lower_bound"], 0.069)
                  and close(j["truncation_error_bound"], 0.001),
                  f"default: upper = P(D ∪ AB ∪ AC) = exact 0.06976, error bound still 0.001 "
                  f"({j['probability_upper_bound']}, {j['upper_bound_method']})")
        j = trunc(d, "0.02")
        if j:
            # a cut set exactly at the cut-off is kept: 0.1 · 0.2 = 0.020000000000000004 ≥ 0.02
            check(cuts(j) == [["BE-A", "BE-B"], ["BE-D"]],
                  f"cut-off 0.02: {{A,B}} (P = 0.1·0.2) kept at the cut-off: {cuts(j)}")
        j = trunc(d, "0.0201", "--upper-budget", "0")
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
        # FR-46 at 0.0201: union D ∪ AB ∪ C = 1 − 0.95·(1 − (0.02 + 0.01 −
        # 0.0002)) = 0.07831; with a budget fitting only the split at 0.02:
        # P(D ∪ AB) + P(C) = 0.069 + 0.01 = 0.079; budget 0: 0.08
        u_union = 1 - 0.95 * (1 - (0.02 + 0.01 - 0.1 * 0.2 * 0.01))
        seen = []
        for b in range(0, 40):
            j = trunc(d, "0.0201", "--upper-budget", str(b))
            if j:
                seen.append((j["probability_upper_bound"], j["upper_bound_method"],
                             j["upper_bound_split"]))
        vals = [u for u, _, _ in seen]
        kinds = {m for _, m, _ in seen}
        check(all(any(close(u, w) for w in (0.08, 0.079, u_union)) for u in vals)
              and all(x >= y for x, y in zip(vals, vals[1:]))
              and kinds == {"sum", "hybrid", "union"}
              and all(close(u, 0.079) and close(c, 0.02) for u, m, c in seen if m == "hybrid")
              and close(vals[-1], u_union),
              f"budgets 0..39 at 0.0201: upper 0.08 (sum), 0.079 (hybrid, split 0.02), "
              f"{u_union:.5f} (union), never rising: {sorted(set((round(u, 6), m) for u, m, _ in seen))}")
        j = trunc(d, "0", "--order-limit", "1")
        if j:
            # order ≤ 1: {D}; lost {A,B}, {A,C}: bound 0.021; their union
            # with {D} is the whole function: upper = exact (FR-46)
            check(close(j["probability_upper_bound"], exact) and j["upper_bound_method"] == "union",
                  f"order limit 1: upper = exact ({j['probability_upper_bound']})")
        j = trunc(d, "0", "--order-limit", "1", "--upper-budget", "0")
        if j:
            # order ≤ 1: {D}; lost {A,B}, {A,C}: bound 0.021, upper 0.071
            check(cuts(j) == [["BE-D"]] and close(j["truncation_error_bound"], 0.021)
                  and close(j["probability_upper_bound"], 0.071) and j["order_limit"] == 1,
                  f"order limit 1: {{D}}, bound 0.021: {cuts(j)}, {j['truncation_error_bound']}")
        j = trunc(d, "0")
        if j:
            check(len(cuts(j)) == 3 and j["truncation_error_bound"] == 0.0
                  and close(j["probability_lower_bound"], exact)
                  and j["probability_upper_bound"] == j["probability_lower_bound"]
                  and j["upper_bound_method"] == "exact",
                  "cut-off 0: every cut set, exact, zero bound, method exact")
        j = trunc(d, "0.9")
        if j:
            check(cuts(j) == [] and j["probability_lower_bound"] == 0.0
                  and j["probability_lower_bound"] <= exact <= j["probability_upper_bound"],
                  f"cut-off 0.9: nothing retained, lower 0, upper ≥ exact "
                  f"({j['probability_upper_bound']})")
        r = subprocess.run([a.engine, d, "FT-T", "--truncated", "0.005", "--upper-budget", "0"],
                           capture_output=True, text=True)
        check(r.returncode == 0 and "6.900000e-2 <= P(top) <= 7.000000e-2" in r.stdout,
              f"text report states the bounds: {r.stdout[:300]}")
        r = subprocess.run([a.engine, d, "FT-T", "--truncated", "0.005"],
                           capture_output=True, text=True)
        check(r.returncode == 0 and "6.900000e-2 <= P(top) <= 6.976000e-2" in r.stdout
              and "union with the lost terms" in r.stdout,
              f"text report: the union bound and how it was obtained: {r.stdout[:400]}")
        for args, msg in [(["--upper-budget", "10"], "--upper-budget applies only with --truncated"),
                          (["--truncated", "0.005", "--upper-budget", "abc"], "--upper-budget needs")]:
            r = run(d, *args)
            check(r.returncode != 0 and msg in r.stderr, f"refused: {' '.join(args)}")

        # relative cut-off (FR-47) on the same tree, P(top) = 0.06976; the
        # cut-off is min(R x L, c_est) (V&V D-23: a finer estimation pass is
        # kept rather than redone coarser):
        #  R = 0.5: pass at 0.5 retains nothing (products lost), pass at
        #  0.005 retains {D}, {A,B}: L = 0.069, R x L = 0.0345 >= 0.005, so
        #  that pass is kept: cut-off 0.005, {D}, {A,B}
        #  R = 0.1: pass at 0.1 nothing, at 0.001 all three (0.1 · 0.01 is
        #  0.0010000000000000002 >= 0.001): L = exact 0.06976 -> kept,
        #  cut-off 0.001
        #  R = 0.01: pass at 0.01 retains {D}, {A,B}: L = 0.069, R x L =
        #  6.9e-4 < 0.01 -> a second pass at 6.9e-4 retains all three
        for ratio, est, ref, cut, want_cuts, lower in (
                (0.5, 0.005, 0.069, 0.005, [["BE-A", "BE-B"], ["BE-D"]], 0.069),
                (0.1, 0.001, exact, 0.001, [["BE-A", "BE-B"], ["BE-A", "BE-C"], ["BE-D"]], exact),
                (0.01, 0.01, 0.069, 6.9e-4, [["BE-A", "BE-B"], ["BE-A", "BE-C"], ["BE-D"]], exact)):
            r = run(d, "--truncated-relative", repr(ratio))
            j = json.loads(r.stdout) if r.returncode == 0 else {}
            check(r.returncode == 0 and close(j["estimation_cutoff"], est)
                  and close(j["cutoff_reference"], ref) and close(j["cutoff"], cut)
                  and j["relative_cutoff"] == ratio and cuts(j) == want_cuts
                  and close(j["probability_lower_bound"], lower)
                  and j["probability_lower_bound"] <= exact <= j["probability_upper_bound"] * (1 + 1e-12),
                  f"relative cut-off {ratio}: estimation at {est}, reference {ref:.5f}, "
                  f"cut-off {cut}, retained {want_cuts}: {r.stderr[:200]}"
                  f"{ {k: j.get(k) for k in ('estimation_cutoff', 'cutoff_reference', 'cutoff')} }")
        r = subprocess.run([a.engine, d, "FT-T", "--truncated-relative", "0.5"],
                           capture_output=True, text=True)
        check(r.returncode == 0 and "relative cut-off: 5e-1 x P(top)" in r.stdout,
              f"relative cut-off in the text report: {r.stdout[:200]}")
        for args, msg in [(["--truncated-relative", "0"], "in (0, 1)"),
                          (["--truncated-relative", "1"], "in (0, 1)"),
                          (["--truncated-relative", "x"], "needs a ratio"),
                          (["--truncated-relative", "0.1", "--truncated", "1e-3"], "not both"),
                          (["--truncated-relative", "0.1", "--samples", "10"], "without --samples")]:
            r = run(d, *args)
            check(r.returncode != 0 and msg in r.stderr, f"refused: {' '.join(args)}")

        # 2) 2-of-3 vote, P = 0.1, 0.2, 0.3: MCS {A,B} 0.02, {A,C} 0.03,
        #    {B,C} 0.06; exact 0.02 + 0.03 + 0.06 − 2·0.006 = 0.098.
        #    cut-off 0.025: {A,C}, {B,C}; lower P(C)·P(A ∪ B) = 0.3·0.28 = 0.084;
        #    dropped {A,B}: upper 0.104
        d = os.path.join(tmp, "vote")
        write_model(d, {"BE-A": 0.1, "BE-B": 0.2, "BE-C": 0.3},
                    {"GT-TOP": {"atleast": {"k": 2, "of": ["BE-A", "BE-B", "BE-C"]}}}, "GT-TOP")
        j = trunc(d, "0.025")
        if j:
            # FR-46: the union with the lost {A,B} is the whole vote: 0.098
            check(close(j["probability_upper_bound"], 0.098) and j["upper_bound_method"] == "union",
                  f"2-of-3 at 0.025: upper = exact 0.098 ({j['probability_upper_bound']})")
        j = trunc(d, "0.025", "--upper-budget", "0")
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

        # relative cut-off on a top that is always false (A and HE-X with
        # HE-X false): nothing retained, nothing lost -> P(top) = 0, cut-off 0
        d0 = os.path.join(tmp, "never")
        write_model(d0, {"BE-A": 0.1}, {"GT-TOP": {"and": ["BE-A", "HE-X"]}}, "GT-TOP",
                    houses={"HE-X": False})
        r = run(d0, "--truncated-relative", "0.1")
        j = json.loads(r.stdout) if r.returncode == 0 else {}
        check(r.returncode == 0 and j.get("cutoff") == 0.0 and j.get("cutoff_reference") == 0.0
              and cuts(j) == [] and j["probability_upper_bound"] == 0.0,
              f"relative cut-off on a false top: cut-off 0, exact zero ({r.stderr[:200]})")

        # 4) tautology: top = HE-T or A with HE-T true: the empty cut set
        d = os.path.join(tmp, "taut")
        write_model(d, {"BE-A": 0.1}, {"GT-TOP": {"or": ["HE-T", "BE-A"]}}, "GT-TOP",
                    houses={"HE-T": True})
        j = trunc(d, "0.5")
        if j:
            check(cuts(j) == [[]] and j["probability_lower_bound"] == 1.0
                  and j["truncation_error_bound"] == 0.0,
                  "tautology: the empty cut set, P = 1, zero bound")

        # 5) event tree (FR-39): FE1 = A or B, FE2 = B or C; P(A) = 0.1,
        #    P(B) = 0.02, P(C) = 0.3; IE 1e-2 /yr.
        #    S-OK  (FE1 ok, FE2 ok): P = 0.9·0.98·0.7 = 0.6174
        #    S-F2  (FE1 ok, FE2 fails): P = 0.9·0.98·0.3 = 0.2646
        #    S-F1  (FE1 fails, FE2 bypassed): P = 1 − 0.9·0.98 = 0.118
        #    At cut-off 0.05 ({B} and every pair fall below it):
        #    S-F2: F = {C}, lost {B}: [0.3, 0.32]; G = F ∧ S retains nothing,
        #          lost {B} (S's), {A,C} (the product), {A,B} (F's lost with
        #          S's {A}, absorbed by {B}): U_G = 0.02 + 0.03 = 0.05
        #          -> P in [0.3 − 0.05, 0.32 − 0] = [0.25, 0.32]
        #    S-F1: F = {A}, lost {B}, no success branch: [0.10, 0.12]
        #    S-OK: F = {} (P 1); G = S = {A},{C}, lost {B}: [0.37, 0.39]
        #          -> P in [0.61, 0.63]
        #    CDF = S-F2 + S-F1: [3.5e-3, 4.4e-3] around 3.826e-3
        d = os.path.join(tmp, "et")
        write_model(d, {"BE-A": 0.1, "BE-B": 0.02, "BE-C": 0.3},
                    {"GT-F1": {"or": ["BE-A", "BE-B"]}, "GT-F2": {"or": ["BE-B", "BE-C"]}},
                    "GT-F1", et=(1e-2, {"FE-F1": "GT-F1", "FE-F2": "GT-F2"},
                                 {"SEQ-S-OK": ({"FE-F1": "success", "FE-F2": "success"}, "OK"),
                                  "SEQ-S-F2": ({"FE-F1": "success", "FE-F2": "failure"}, "CD"),
                                  "SEQ-S-F1": ({"FE-F1": "failure", "FE-F2": "bypassed"}, "CD")}))
        v = subprocess.run([sys.executable, os.path.join(HERE, "validate.py"), d,
                            os.path.join(ROOT, "schema", "psa-model.schema.json")],
                           capture_output=True, text=True)
        check(v.returncode == 0, f"event-tree fixture validates {v.stdout[-200:]}")
        exact = {"SEQ-S-OK": 0.9 * 0.98 * 0.7, "SEQ-S-F2": 0.9 * 0.98 * 0.3,
                 "SEQ-S-F1": 1 - 0.9 * 0.98}
        # sum bounds (FR-39, --upper-budget 0) and FR-46's union bounds:
        #    S-F2: U_F = P(C ∪ B) = 1 − 0.7·0.98 = 0.314, U_G = P(B ∪ AC)
        #          = 1 − 0.98·0.97 = 0.0494 -> [0.3 − 0.0494, 0.314] = [0.2506, 0.314]
        #    S-F1: U_F = P(A ∪ B) = 0.118 (exact) -> [0.10, 0.118]
        #    S-OK: U_G = P(A ∪ B ∪ C) = 1 − 0.6174 = 0.3826 -> [0.6174, 0.63]
        #    partition [0.968, 1.062]; CDF [3.506e-3, 4.32e-3]
        runs = [(["--upper-budget", "0"],
                 {"SEQ-S-OK": (0.61, 0.63), "SEQ-S-F2": (0.25, 0.32), "SEQ-S-F1": (0.10, 0.12)},
                 (0.96, 1.07), (3.5e-3, 4.4e-3), "sum bounds"),
                ([], {"SEQ-S-OK": (0.6174, 0.63), "SEQ-S-F2": (0.2506, 0.314),
                      "SEQ-S-F1": (0.10, 0.118)},
                 (0.968, 1.062), (3.506e-3, 4.32e-3), "union bounds (FR-46)")]
        for extra, want, (plo, phi), (clo, chi), what in runs:
            r = subprocess.run([a.engine, d, "ET-T", "--json", "--truncated", "0.05", *extra],
                               capture_output=True, text=True)
            if r.returncode != 0:
                failures.append(f"event tree: engine failed: {r.stderr}")
                continue
            j = json.loads(r.stdout)
            rows = {x["id"]: x for x in j["sequences"]}
            for sid, (lo, hi) in want.items():
                x = rows[sid]
                check(close(x["frequency_lower_bound"], 1e-2 * lo)
                      and close(x["frequency_upper_bound"], 1e-2 * hi)
                      and x["frequency_lower_bound"] <= 1e-2 * exact[sid] <= x["frequency_upper_bound"],
                      f"ET {sid} at 0.05, {what}: [{x['frequency_lower_bound']:.4e}, "
                      f"{x['frequency_upper_bound']:.4e}] = 1e-2 x [{lo}, {hi}] around "
                      f"{1e-2 * exact[sid]:.4e}")
            # partition bounds (FR-42): probability bounds = the hand bounds
            # above, their sums bracket 1
            check(all(close(rows[sid]["probability_lower_bound"], lo)
                      and close(rows[sid]["probability_upper_bound"], hi)
                      for sid, (lo, hi) in want.items())
                  and close(j["partition"]["sum_probability_lower_bound"], plo)
                  and close(j["partition"]["sum_probability_upper_bound"], phi)
                  and j["partition"]["per_sequence_house_overrides"] is False
                  and all(x["followed"] is None for x in j["sequences"]),
                  f"ET partition bounds [{plo}, {phi}] at 0.05, {what}: {j.get('partition')}")
            check(sorted(sorted(c["events"]) for c in rows["SEQ-S-F2"]["cut_sets"]) == [["BE-C"]]
                  and sorted(sorted(c["events"]) for c in rows["SEQ-S-F1"]["cut_sets"]) == [["BE-A"]]
                  and rows["SEQ-S-OK"]["cut_sets"] == []
                  and rows["SEQ-S-OK"]["failure_logic"]["retained_cut_sets"] == 1,
                  "ET retained failure-logic cut sets: {C}, {A}; the OK row retains the "
                  "empty set but lists none, like the exact path (V&V D-20)")
            m = j["metrics"][0]
            check(close(m["value_lower_bound"], clo) and close(m["value_upper_bound"], chi)
                  and "value_per_year" not in m and all("frequency_per_year" not in x for x in rows.values()),
                  f"ET CDF bounds [{clo}, {chi}], {what}; no point value reported ({m})")
            meth = rows["SEQ-S-F2"]["failure_logic"]["upper_bound_method"]
            check(meth == ("sum" if extra else "union"),
                  f"ET upper-bound method reported per side: {meth}")
        r = subprocess.run([a.engine, d, "ET-T", "--json", "--truncated", "0"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            j = json.loads(r.stdout)
            check(all(abs(x["frequency_lower_bound"] - 1e-2 * exact[x["id"]]) <= 1e-17
                      and abs(x["frequency_upper_bound"] - 1e-2 * exact[x["id"]]) <= 1e-17
                      for x in j["sequences"]), "ET at cut-off 0: every row exact")
        else:
            failures.append(f"event tree at 0: {r.stderr}")
        # per-sequence house override: FE1 = A or (B and HE-X), HE-X false
        # by default; SEQ-2 sets it true: P(SEQ-2) = 1 − 0.9·0.98 = 0.118
        # (0.1 without the override); SEQ-1 keeps the default: 0.9
        d = os.path.join(tmp, "et-house")
        write_model(d, {"BE-A": 0.1, "BE-B": 0.02},
                    {"GT-H": {"or": ["BE-A", {"and": ["BE-B", "HE-X"]}]}}, "GT-H",
                    houses={"HE-X": False},
                    et=(1e-2, {"FE-F1": "GT-H"},
                        {"SEQ-1": ({"FE-F1": "success"}, "OK"),
                         "SEQ-2": ({"FE-F1": "failure"}, "CD", {"HE-X": True})}))
        for cutoff in ("0", "0.05"):
            r = subprocess.run([a.engine, d, "ET-T", "--json", "--truncated", cutoff],
                               capture_output=True, text=True)
            if r.returncode != 0:
                failures.append(f"house-override tree: {r.stderr}")
                continue
            rows = {x["id"]: x for x in json.loads(r.stdout)["sequences"]}
            lo2, hi2 = rows["SEQ-2"]["frequency_lower_bound"], rows["SEQ-2"]["frequency_upper_bound"]
            lo1, hi1 = rows["SEQ-1"]["frequency_lower_bound"], rows["SEQ-1"]["frequency_upper_bound"]
            if cutoff == "0":
                check(close(lo2, 1.18e-3) and close(hi2, 1.18e-3) and close(lo1, 9e-3) and close(hi1, 9e-3),
                      f"ET per-sequence house override honoured at cut-off 0 "
                      f"(SEQ-2 {lo2:.4e}, SEQ-1 {lo1:.4e})")
            else:
                # SEQ-2 at 0.05: F = {A}, lost {B} ({B} under HE-X true):
                # union bound P(A ∪ B) = 0.118 (FR-46)
                check(close(lo2, 1.0e-3) and close(hi2, 1.18e-3),
                      f"ET house override at 0.05: SEQ-2 in [1.0e-3, 1.18e-3] ({lo2:.4e}, {hi2:.4e})")

        r = subprocess.run([a.engine, d, "ET-T", "--truncated-relative", "0.1"],
                           capture_output=True, text=True)
        check(r.returncode != 0 and "applies to fault trees" in r.stderr,
              "--truncated-relative refused on an event tree")

        # non-coherent functional-event logic is refused
        d = os.path.join(tmp, "et-nc")
        write_model(d, {"BE-A": 0.1, "BE-B": 0.02},
                    {"GT-F1": {"and": ["BE-A", {"not": "BE-B"}]}}, "GT-F1",
                    et=(1e-2, {"FE-F1": "GT-F1"},
                        {"SEQ-1": ({"FE-F1": "success"}, "OK"), "SEQ-2": ({"FE-F1": "failure"}, "CD")}))
        r = subprocess.run([a.engine, d, "ET-T", "--truncated", "1e-6"], capture_output=True, text=True)
        check(r.returncode != 0 and "needs coherent logic" in r.stderr and "FE-F1" in r.stderr,
              "ET with a non-coherent functional event refused, naming it")

        # 6) refusals
        d = os.path.join(tmp, "noncoh")
        write_model(d, {"BE-A": 0.1, "BE-B": 0.2},
                    {"GT-TOP": {"and": ["BE-A", {"not": "BE-B"}]}}, "GT-TOP")
        r = run(d, "--truncated", "1e-6")
        check(r.returncode != 0 and "needs coherent logic" in r.stderr, "non-coherent refused")
        model = os.path.join(ROOT, "model")
        for args, why in [(["--truncated", "1e-6", "--samples", "10"], "--samples"),
                          (["--truncated", "1e-6", "--prime-implicants"], "--prime-implicants")]:
            for tgt in ("FT-ECCS-INJECTION", "ET-SLOCA"):
                r = run(model, *args, target=tgt)
                check(r.returncode != 0 and "--truncated applies without" in r.stderr,
                      f"refused: {why} ({tgt})")
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
