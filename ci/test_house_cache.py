#!/usr/bin/env python3
"""Hand-computed test of selective gate reuse on house changes (FR-43, FR-49).

With one compiler per event tree (FR-38), a row whose house-event values
differ from the previous row's drops exactly the cached gates that reach a
house event whose value changed — transitively, through the gates they
reference — and keeps the others. Fixture (initiator 1e-2 /yr):

    BE-A 0.1, BE-B 0.2, BE-C 0.3; HE-X default false
    GT-P = A or GT-Q      (reaches HE-X only through GT-Q)
    GT-Q = B and HE-X
    GT-R = B or C         (reaches no house event)
    FE-1 top GT-P, FE-2 top GT-R

    row    houses          path            P(row)
    SEQ-1  X = true        FE-1 S, FE-2 S  ¬A¬B¬C          = 0.504
    SEQ-2  (none: X false) FE-1 S, FE-2 F  ¬A (B ∨ C)      = 0.396
    SEQ-3  X = false       FE-1 F, FE-2 –  A               = 0.1

SEQ-2 needs GT-P recompiled (X went back to its default: an override
removed, not added) although GT-P's own formula names no house event; a
GT-P kept from SEQ-1 would give ¬(A ∨ B)(B ∨ C) = 0.216. GT-R is kept.
SEQ-3 states X = false, its current value: nothing is dropped. Gate
compilations: GT-P, GT-Q, GT-R for SEQ-1, GT-P and GT-Q again for SEQ-2 —
5, with 1 cached gate dropped and 1 kept (clearing the cache on every
change, as before, compiles 8).

Checked: the three frequencies, CDF = 4.96e-3, the cut sets ({B}, {C} for
SEQ-2; {A} for SEQ-3), the gate statistics, and identical results with a
fresh compiler per row, with collection forced at every safe point, and
with reordering forced.

Usage: python ci/test_house_cache.py [--engine PATH]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROV = {"source": "ci/test_house_cache.py", "justification": "hand-computed fixture"}

failures = []
n_checks = [0]


def check(cond, msg):
    n_checks[0] += 1
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def close(a, b, rel=1e-12):
    return abs(a - b) <= rel * max(abs(a), abs(b))


def write_model(d):
    dump = lambda p, o: open(os.path.join(d, p), "w").write(
        yaml.safe_dump(o, sort_keys=False, default_flow_style=False))
    for sub in ("basic-events", "fault-trees", "event-trees"):
        os.makedirs(os.path.join(d, sub))
    dump("model.yaml", {
        "schema_version": "0.1",
        "model": {"id": "HOUSE-CACHE", "name": "house cache fixture",
                  "risk_metrics": [{"id": "CDF", "label": "core damage",
                                    "end_states": ["CD"]}]},
        "includes": {"parameters": ["parameters.yaml"],
                     "basic_events": ["basic-events/*.yaml"],
                     "fault_trees": ["fault-trees/*.yaml"],
                     "event_trees": ["event-trees/*.yaml"],
                     "house_events": ["house-events.yaml"]}})
    dump("parameters.yaml", {"parameters": {}})
    dump("house-events.yaml", {"house_events": {
        "HE-X": {"label": "house event x", "default": False, "provenance": PROV}}})
    be = lambda p: {"label": "fixture event", "provenance": PROV,
                    "failure_model": {"type": "probability",
                                      "value": {"value": p, "unit": "per_demand"}}}
    dump("basic-events/b.yaml", {"basic_events": {
        "BE-A": be(0.1), "BE-B": be(0.2), "BE-C": be(0.3)}})
    dump("fault-trees/f.yaml", {"fault_trees": {
        "FT-P": {"label": "tree p", "top_gate": "GT-P", "gates": {
            "GT-P": {"label": "gate p", "formula": {"or": ["BE-A", "GT-Q"]}},
            "GT-Q": {"label": "gate q", "formula": {"and": ["BE-B", "HE-X"]}}}},
        "FT-R": {"label": "tree r", "top_gate": "GT-R", "gates": {
            "GT-R": {"label": "gate r", "formula": {"or": ["BE-B", "BE-C"]}}}}}})
    dump("event-trees/e.yaml", {"event_tree": {
        "id": "ET-H", "label": "house cache tree",
        "initiating_event": {"id": "IE-H", "label": "initiator", "provenance": PROV,
                             "frequency": {"value": 1e-2, "unit": "per_year"}},
        "functional_events": {"FE-1": {"label": "function one", "top_gate": "GT-P"},
                              "FE-2": {"label": "function two", "top_gate": "GT-R"}},
        "sequences": {
            "SEQ-1": {"path": {"FE-1": "success", "FE-2": "success"}, "end_state": "OK",
                      "house_events": {"HE-X": True}},
            "SEQ-2": {"path": {"FE-1": "success", "FE-2": "failure"}, "end_state": "CD"},
            "SEQ-3": {"path": {"FE-1": "failure", "FE-2": "bypassed"}, "end_state": "CD",
                      "house_events": {"HE-X": False}}}}})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", os.path.join(ROOT, "engine", "target", "release", "canopy")))
    a = ap.parse_args()
    d = tempfile.mkdtemp(prefix="canopy-house-cache-")
    try:
        write_model(d)
        v = subprocess.run([sys.executable, os.path.join(HERE, "validate.py"), d,
                            os.path.join(ROOT, "schema", "psa-model.schema.json")],
                           capture_output=True, text=True)
        check(v.returncode == 0, f"fixture validates {v.stdout[-300:]}")

        def run(*extra):
            p = subprocess.run([a.engine, d, "ET-H", "--json", *extra],
                               capture_output=True, text=True)
            if p.returncode != 0:
                failures.append(f"engine failed ({extra}): {p.stderr}")
                return None, p.stderr
            return json.loads(p.stdout), p.stderr

        j, err = run("--gc-stats")
        if j is None:
            raise SystemExit(1)
        rows = {s["id"]: s for s in j["sequences"]}
        want = {"SEQ-1": 0.504e-2, "SEQ-2": 0.396e-2, "SEQ-3": 0.1e-2}
        for sid, f in want.items():
            check(close(rows[sid]["frequency_per_year"], f),
                  f"{sid} frequency {rows[sid]['frequency_per_year']!r} = {f!r}")
        check(close(j["metrics"][0]["value_per_year"], 4.96e-3), "CDF = 4.96e-3")
        cuts = lambda sid: sorted(sorted(c["events"]) for c in rows[sid]["cut_sets"])
        check(cuts("SEQ-2") == [["BE-B"], ["BE-C"]] and cuts("SEQ-3") == [["BE-A"]]
              and cuts("SEQ-1") == [],
              f"cut sets: SEQ-2 {cuts('SEQ-2')}, SEQ-3 {cuts('SEQ-3')}")
        stats = re.findall(r"gates: (\S+): (\d+) compiled; on house changes (\d+) dropped, (\d+) kept",
                           err)
        check(stats == [("SEQ-1", "3", "0", "0"), ("SEQ-2", "5", "1", "1"),
                        ("SEQ-3", "5", "1", "1")],
              f"gate statistics per row (compiled, dropped, kept): {stats}")
        _, err_pr = run("--compile", "per-row", "--gc-stats")
        per_row = re.findall(r"gates: \S+: (\d+) compiled", err_pr)
        check(per_row == ["3", "3", "2"],
              f"a fresh compiler per row compiles 3 + 3 + 2 = 8 gates: {per_row}")
        # truncated quantification (FR-49): the memo is keyed by the values
        # of the house events a gate reaches, so GT-R is built once and
        # GT-P/GT-Q once per value of X: 5 gates (a memo per house
        # configuration, as before, builds 3 + 3 + 2 = 8); at cut-off 0 the
        # rows are the exact values above
        p = subprocess.run([a.engine, d, "ET-H", "--json", "--truncated", "0", "--gc-stats"],
                           capture_output=True, text=True)
        tj = json.loads(p.stdout) if p.returncode == 0 else {"sequences": []}
        tr_rows = {x["id"]: x for x in tj["sequences"]}
        check(p.returncode == 0 and all(
                  close(tr_rows[sid]["frequency_lower_bound"], f)
                  and close(tr_rows[sid]["frequency_upper_bound"], f)
                  for sid, f in want.items()),
              f"truncated at cut-off 0: every row exact under its own house values ({p.stderr[-200:]})")
        check("truncation: ET-H: 5 gates built" in p.stderr,
              f"truncated: 5 gates built across the three house configurations: {p.stderr.strip()[-80:]}")
        for extra in (("--compile", "per-row"), ("--gc-threshold", "1"),
                      ("--gc-threshold", "1", "--reorder", "--reorder-threshold", "0")):
            k, _ = run(*extra)
            ok = k is not None and all(
                close(x["frequency_per_year"], y["frequency_per_year"])
                and sorted(map(sorted, (c["events"] for c in x["cut_sets"])))
                == sorted(map(sorted, (c["events"] for c in y["cut_sets"])))
                for x, y in zip(j["sequences"], k["sequences"]))
            check(ok, f"same results with {' '.join(extra)}")
    finally:
        shutil.rmtree(d, ignore_errors=True)
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print(f"house cache: all {n_checks[0]} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
