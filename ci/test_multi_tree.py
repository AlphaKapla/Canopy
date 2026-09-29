#!/usr/bin/env python3
"""Hand-computed test of several event trees in one process (FR-44).

The engine's target may list event trees, `ET-A,ET-B`: the model is loaded
once, one compiler serves every tree (use counts planned over all their
rows, house changes between trees handled as between rows, FR-43), and the
results are one JSON object keyed by tree ID. Fixture (the house-cache
fixture of test_house_cache.py plus a second tree on the same gates):

    BE-A 0.1, BE-B 0.2, BE-C 0.3; HE-X default false
    GT-P = A or GT-Q, GT-Q = B and HE-X, GT-R = B or C

    ET-1 (1e-2 /yr), FE-1 on GT-P, FE-2 on GT-R:
      SEQ-1   X = true   S S   ¬A¬B¬C      = 0.504
      SEQ-2   —          S F   ¬A(B ∨ C)   = 0.396
      SEQ-3   X = true   F –   A ∨ B       = 0.28
    ET-2 (1e-3 /yr), FE-1 on GT-P, FE-2 on GT-R:
      SEQ-T1  —          F –   A           = 0.1
      SEQ-T2  X = true   S F   ¬A¬B(B ∨ C) = ¬A¬B C = 0.216
      SEQ-T3  —          S S   ¬A¬B¬C      = 0.504

ET-1 ends with X = true and ET-2 begins with no override: its first row
must see X false again (0.1; X carried over from ET-1 would give 0.28).

Gate compilations, one process: GT-P, GT-Q, GT-R for SEQ-1, then GT-P
and GT-Q again at every row, since X changes at every row (ET-2's first
row included) — 13 in all; on house changes the cached GT-P and GT-Q
(which, once recompiled, has no use count left and stays cached) are
dropped each time and GT-R kept — 9 dropped, 5 kept. Two processes
compile 7 + 7 (ET-2 alone starts with X false, so its first row finds
nothing to drop, but compiles GT-R at SEQ-T2).

Checked: every frequency and both CDFs by hand; each tree's results in
one process equal to its own process — point results, with collection
forced, per-row and truncated byte for byte on this fixture; Monte Carlo
(the same draws: keyed by quantity ID) and importance draws to rounding
(1e-12: the flat plans evaluate in the shared compiler's variable order,
which also orders the listed quantities); the gate counts; the refusals
(text output, a fault tree in the list, a tree listed twice, an unknown
tree, a transfer-only tree); `quantify.py --one-process` equal to the
default (to rounding), with configurations, --importance-uncertainty and
--truncated.

Usage: python ci/test_multi_tree.py [--engine PATH]
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
PROV = {"source": "ci/test_multi_tree.py", "justification": "hand-computed fixture"}

failures = []
n_checks = [0]


def check(cond, msg):
    n_checks[0] += 1
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def close(a, b, rel=1e-12):
    return abs(a - b) <= rel * max(abs(a), abs(b))


def agree(x, y, path=""):
    """Where two results differ beyond rounding (numbers within 1e-12
    relative; lists of records keyed by "key", "event" or "id" compared by
    key, other lists element by element); [] when they agree."""
    if isinstance(x, bool) or isinstance(y, bool) or not (
            isinstance(x, (int, float)) and isinstance(y, (int, float))):
        if type(x) is not type(y):
            return [path]
    else:
        return [] if x == y or abs(x - y) <= 1e-12 * max(abs(x), abs(y)) + 1e-300 else [path]
    if isinstance(x, dict):
        if set(x) != set(y):
            return [path]
        return [p for k in x for p in agree(x[k], y[k], f"{path}/{k}")]
    if isinstance(x, list):
        if len(x) != len(y):
            return [path]
        for key in ("key", "event", "id"):
            if x and all(isinstance(e, dict) and key in e for e in x + y):
                mx, my = {e[key]: e for e in x}, {e[key]: e for e in y}
                return agree(mx, my, path)
        return [p for i, (a, b) in enumerate(zip(x, y)) for p in agree(a, b, f"{path}[{i}]")]
    return [] if x == y else [path]


def write_model(d):
    dump = lambda p, o: open(os.path.join(d, p), "w").write(
        yaml.safe_dump(o, sort_keys=False, default_flow_style=False))
    for sub in ("basic-events", "fault-trees", "event-trees"):
        os.makedirs(os.path.join(d, sub))
    dump("model.yaml", {
        "schema_version": "0.1",
        "model": {"id": "MULTI-TREE", "name": "multi-tree fixture",
                  "risk_metrics": [{"id": "CDF", "label": "core damage",
                                    "end_states": ["CD"]}]},
        "configurations": {"X-ON": {"label": "house x on",
                                    "house_events": {"HE-X": True}}},
        "includes": {"parameters": ["parameters.yaml"],
                     "basic_events": ["basic-events/*.yaml"],
                     "fault_trees": ["fault-trees/*.yaml"],
                     "event_trees": ["event-trees/*.yaml"],
                     "house_events": ["house-events.yaml"]}})
    dump("parameters.yaml", {"parameters": {}})
    dump("house-events.yaml", {"house_events": {
        "HE-X": {"label": "house event x", "default": False, "provenance": PROV}}})
    be = lambda p, sd: {"label": "fixture event", "provenance": PROV,
                        "failure_model": {"type": "probability",
                                          "value": {"value": p, "unit": "per_demand"}},
                        "uncertainty": {"distribution": "lognormal", "error_factor": sd}}
    dump("basic-events/b.yaml", {"basic_events": {
        "BE-A": be(0.1, 3), "BE-B": be(0.2, 2), "BE-C": be(0.3, 1.5)}})
    dump("fault-trees/f.yaml", {"fault_trees": {
        "FT-P": {"label": "tree p", "top_gate": "GT-P", "gates": {
            "GT-P": {"label": "gate p", "formula": {"or": ["BE-A", "GT-Q"]}},
            "GT-Q": {"label": "gate q", "formula": {"and": ["BE-B", "HE-X"]}}}},
        "FT-R": {"label": "tree r", "top_gate": "GT-R", "gates": {
            "GT-R": {"label": "gate r", "formula": {"or": ["BE-B", "BE-C"]}}}}}})
    ie = lambda i, f: {"id": i, "label": "initiator", "provenance": PROV,
                       "frequency": {"value": f, "unit": "per_year"}}
    dump("event-trees/e1.yaml", {"event_tree": {
        "id": "ET-1", "label": "first tree", "initiating_event": ie("IE-1", 1e-2),
        "functional_events": {"FE-1": {"label": "function one", "top_gate": "GT-P"},
                              "FE-2": {"label": "function two", "top_gate": "GT-R"}},
        "sequences": {
            "SEQ-1": {"path": {"FE-1": "success", "FE-2": "success"}, "end_state": "OK",
                      "house_events": {"HE-X": True}},
            "SEQ-2": {"path": {"FE-1": "success", "FE-2": "failure"}, "end_state": "CD"},
            "SEQ-3": {"path": {"FE-1": "failure", "FE-2": "bypassed"}, "end_state": "CD",
                      "house_events": {"HE-X": True}}}}})
    dump("event-trees/e2.yaml", {"event_tree": {
        "id": "ET-2", "label": "second tree", "initiating_event": ie("IE-2", 1e-3),
        "functional_events": {"FE-1": {"label": "function p", "top_gate": "GT-P"},
                              "FE-2": {"label": "function r", "top_gate": "GT-R"}},
        "sequences": {
            "SEQ-T1": {"path": {"FE-1": "failure", "FE-2": "bypassed"}, "end_state": "CD"},
            "SEQ-T2": {"path": {"FE-1": "success", "FE-2": "failure"}, "end_state": "CD",
                       "house_events": {"HE-X": True}},
            "SEQ-T3": {"path": {"FE-1": "success", "FE-2": "success"}, "end_state": "OK"}}}})
    dump("event-trees/e3.yaml", {"event_tree": {
        "id": "ET-3", "label": "transfer-only tree",
        "functional_events": {"FE-1": {"label": "function r", "top_gate": "GT-R"}},
        "sequences": {
            "SEQ-U1": {"path": {"FE-1": "success"}, "end_state": "OK"},
            "SEQ-U2": {"path": {"FE-1": "failure"}, "end_state": "CD"}}}})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", os.path.join(ROOT, "engine", "target", "release", "canopy")))
    a = ap.parse_args()
    d = tempfile.mkdtemp(prefix="canopy-multi-tree-")
    try:
        write_model(d)
        v = subprocess.run([sys.executable, os.path.join(HERE, "validate.py"), d,
                            os.path.join(ROOT, "schema", "psa-model.schema.json")],
                           capture_output=True, text=True)
        check(v.returncode == 0, f"fixture validates {v.stdout[-300:]}{v.stderr[-300:]}")

        def run(target, *extra):
            p = subprocess.run([a.engine, d, target, *extra], capture_output=True, text=True)
            return p, (json.loads(p.stdout) if p.returncode == 0 and "--json" in extra else None)

        p, both = run("ET-1,ET-2", "--json", "--gc-stats")
        check(p.returncode == 0 and sorted(both or {}) == ["ET-1", "ET-2"],
              f"one object keyed by tree ID: {p.stderr[-300:]}")
        if both is None:
            raise SystemExit(1)
        want = {"ET-1": {"SEQ-1": 0.504e-2, "SEQ-2": 0.396e-2, "SEQ-3": 0.28e-2},
                "ET-2": {"SEQ-T1": 0.1e-3, "SEQ-T2": 0.216e-3, "SEQ-T3": 0.504e-3}}
        for t, rows in want.items():
            got = {s["id"]: s["frequency_per_year"] for s in both[t]["sequences"]}
            check(all(close(got[k], f) for k, f in rows.items()), f"{t} frequencies by hand: {got}")
        check(close(both["ET-1"]["metrics"][0]["value_per_year"], 6.76e-3)
              and close(both["ET-2"]["metrics"][0]["value_per_year"], 3.16e-4),
              "CDF 6.76e-3 and 3.16e-4")
        stats = re.findall(r"gates: (\S+): (\d+) compiled; on house changes (\d+) dropped, (\d+) kept",
                           p.stderr)
        check(stats[-1] == ("SEQ-T3", "13", "9", "5"),
              f"one process: 13 gates compiled, 9 dropped, 5 kept: {stats}")
        sep = {}
        counts = 0
        for t in ("ET-1", "ET-2"):
            q, sep[t] = run(t, "--json", "--gc-stats")
            counts += int(re.findall(r"gates: \S+: (\d+) compiled", q.stderr)[-1])
        check(counts == 14, f"two processes compile 7 + 7 gates: {counts}")
        check(both == sep, "each tree's results in one process = its own process")
        for extra, exact in ((["--gc-threshold", "1"], True), (["--compile", "per-row"], True),
                             (["--truncated", "0.05"], True),
                             (["--samples", "500", "--seed", "11"], False),
                             (["--samples", "300", "--importance-events", "BE-A,BE-B"], False)):
            _, b2 = run("ET-1,ET-2", "--json", *extra)
            s2 = {t: run(t, "--json", *extra)[1] for t in ("ET-1", "ET-2")}
            ok = b2 is not None and (b2 == s2 if exact else not agree(b2, s2))
            check(ok, f"one process = one process per tree with {' '.join(extra)} "
                      f"({'byte for byte' if exact else 'to rounding'})"
                      + ("" if ok or b2 is None else f": {agree(b2, s2)[:3]}"))
        _, bs = run("ET-1,ET-2", "--json", "--samples", "500", "--seed", "11")
        check("uncertainty" in bs["ET-1"] and bs["ET-1"]["uncertainty"]["seed"] == 11,
              "Monte Carlo results present per tree")
        for target, extra, msg in [("ET-1,ET-2", [], "need --json"),
                                   ("ET-1,FT-P", ["--json"], "event trees only"),
                                   ("ET-1,ET-1", ["--json"], "listed twice"),
                                   ("ET-1,ET-9", ["--json"], "not found"),
                                   ("ET-1,ET-3", ["--json"], "transfer-only tree")]:
            q, _ = run(target, *extra)
            check(q.returncode != 0 and msg in q.stderr,
                  f"refused: {target} {' '.join(extra)} ({q.stderr.strip()[:80]})")
        for extra in ([], ["--configurations", "CFG"], ["--samples", "400", "--seed", "3",
                                                        "--importance-uncertainty", "2"],
                      ["--truncated", "0.05"]):
            outs = []
            for mode in ([], ["--one-process"]):
                o = os.path.join(d, f"q{len(mode)}.json")
                cfg = [x if x != "CFG" else os.path.join(d, f"c{len(mode)}.json") for x in extra]
                r = subprocess.run([sys.executable, os.path.join(HERE, "quantify.py"), d, o,
                                    "--engine", a.engine, *cfg, *mode],
                                   capture_output=True, text=True)
                res = [json.load(open(o))] if r.returncode == 0 else [r.stderr]
                if "CFG" in extra and r.returncode == 0:
                    res.append(json.load(open(os.path.join(d, f"c{len(mode)}.json"))))
                outs.append((r.returncode, res))
            ok = outs[0][0] == outs[1][0] == 0 and not agree(outs[0][1], outs[1][1])
            if "CFG" in extra and ok:
                # not vacuous: the configuration was quantified, and X on
                # everywhere changes ET-1's SEQ-2 to ¬A¬B·C = 0.216
                c = outs[0][1][1]
                x = {s["id"]: s["frequency_per_year"] for s in c.get("X-ON", {}).get("ET-1", {})
                     .get("sequences", [])}
                ok = sorted(c) == ["X-ON"] and close(x.get("SEQ-2", 0.0), 0.216e-2)
            check(ok, f"quantify.py --one-process = default {' '.join(extra)}"
                      + ("" if ok else f": {outs[1][1] if outs[1][0] else agree(outs[0][1], outs[1][1])[:3]}"))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print(f"multi-tree: all {n_checks[0]} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
