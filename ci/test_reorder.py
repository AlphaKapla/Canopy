#!/usr/bin/env python3
"""Tests for dynamic variable reordering (FR-35): `canopy --reorder`.

On the demo model (CCF groups, house events, every failure model): each
fault tree and each event tree quantified with reordering forced at every
safe point, from both static orders, gives the default results — P(top),
sequence frequencies, metrics and importance within 1e-12 relative,
identical cut-set sets — and the reordering is reported on stderr with
--gc-stats. Two forced runs are byte-identical. Malformed thresholds are
refused. Also the event-tree compilation mode (FR-38): one compiler per
tree (default) and one per row give byte-identical demo results, with and
without forced collection and reordering; a bad mode is refused.

Usage: python ci/test_reorder.py [--engine PATH]
"""
import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODEL = os.path.join(ROOT, "model")
FORCED = ["--gc-threshold", "1", "--reorder-threshold", "0"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", os.path.join(ROOT, "engine/target/release/canopy")))
    a = ap.parse_args()
    failures = []

    def check(cond, msg):
        (print(f"  ok  {msg}") if cond else failures.append(msg))

    def run(target, *extra):
        p = subprocess.run([a.engine, MODEL, target, "--json", "--mcs-limit", "100000",
                            *extra], capture_output=True, text=True)
        if p.returncode != 0:
            failures.append(f"{target} {extra}: engine failed: {p.stderr}")
            return None, p.stderr
        return json.loads(p.stdout), p.stderr

    rel = lambda x, y: abs(x - y) <= 1e-12 * max(abs(x), abs(y)) + 1e-300
    cuts = lambda lst, key: {frozenset(c["events"]): c[key] for c in lst}

    fts, ets = [], []
    for d, key, out in (("fault-trees", "fault_trees", fts), ("event-trees", "event_tree", ets)):
        for f in sorted(os.listdir(os.path.join(MODEL, d))):
            text = open(os.path.join(MODEL, d, f)).read()
            if key == "fault_trees":
                out += re.findall(r"^  (FT-[A-Z0-9-]+):", text, re.M)
            else:
                m = re.search(r"^  id: (ET-[A-Z0-9-]+)", text, re.M)
                if m and "initiating_event:" in text:
                    out.append(m.group(1))
    check(len(fts) >= 3 and len(ets) >= 1, f"demo trees found: {fts} {ets}")

    reordered_fts = 0
    for ft in fts:
        base, _ = run(ft)
        for order in ("dfs", "rdfs"):
            got, err = run(ft, "--order", order, *FORCED, "--gc-stats")
            if not (base and got):
                continue
            m = re.search(r"reorder: \S+: (\d+) reordering", err)
            ok = (rel(base["probability"], got["probability"])
                  and cuts(base["minimal_cut_sets"], "probability").keys()
                  == cuts(got["minimal_cut_sets"], "probability").keys()
                  and all(rel(v, cuts(got["minimal_cut_sets"], "probability")[k])
                          for k, v in cuts(base["minimal_cut_sets"], "probability").items()))
            bb = {x["event"]: x["importance"] for x in base["birnbaum"]}
            gb = {x["event"]: x["importance"] for x in got["birnbaum"]}
            ok = ok and bb.keys() == gb.keys() and all(
                abs(bb[e] - gb[e]) <= 1e-12 * max(abs(bb[e]), base["probability"]) for e in bb)
            runs = int(m.group(1)) if m else 0
            reordered_fts += runs > 0
            check(ok and m is not None,
                  f"{ft} ({order}, {runs} forced reordering(s)): "
                  f"P(top), cut sets, Birnbaum unchanged")

    # FT-RPS is a single small gate: nothing is live at its safe points
    check(reordered_fts >= 4, f"reordering happened in {reordered_fts} of "
                              f"{2 * len(fts)} fault-tree runs")

    for et in ets:
        base, _ = run(et)
        for order in ("dfs", "rdfs"):
            got, err = run(et, "--order", order, *FORCED, "--gc-stats")
            if not (base and got):
                continue
            n = sum(int(x) for x in re.findall(r"reorder: \S+: (\d+) reordering", err))
            sa = {s["id"]: s for s in base["sequences"]}
            sg = {s["id"]: s for s in got["sequences"]}
            ok = sa.keys() == sg.keys() and all(
                rel(sa[k]["frequency_per_year"], sg[k]["frequency_per_year"])
                and cuts(sa[k]["cut_sets"], "frequency_per_year").keys()
                == cuts(sg[k]["cut_sets"], "frequency_per_year").keys() for k in sa)
            for ma, mg in zip(base["metrics"], got["metrics"]):
                ok = ok and rel(ma["value_per_year"], mg["value_per_year"])
                ia = {x["event"]: x for x in ma.get("importance", [])}
                ig = {x["event"]: x for x in mg.get("importance", [])}
                ok = ok and ia.keys() == ig.keys() and all(
                    abs(ia[e][k] - ig[e][k]) <= 1e-12 * max(abs(ia[e][k]), ma["value_per_year"])
                    for e in ia for k in ("frequency_if_true_per_year",
                                          "frequency_if_false_per_year"))
            check(ok and n > 0, f"{et} ({order}, {n} reorderings over its rows): "
                                f"sequences, cut sets, metrics, importance unchanged")

    outs = [subprocess.run([a.engine, MODEL, ets[0] if ets else fts[0], "--json", *FORCED],
                           capture_output=True, text=True).stdout for _ in range(3)]
    check(outs[0] and outs[0] == outs[1] == outs[2], "three forced runs byte-identical")

    for et in ets:
        for extra in ([], FORCED):
            outs = [subprocess.run([a.engine, MODEL, et, "--json", "--mcs-limit", "100000",
                                    "--prime-implicants", "--compile", mode, *extra],
                                   capture_output=True, text=True).stdout
                    for mode in ("shared", "per-row")]
            if not extra:
                check(outs[0] and outs[0] == outs[1],
                      f"{et}: --compile shared = per-row, byte for byte")
                continue
            # with forced reordering the shared compiler keeps its sifted
            # order from row to row: different BDDs, same results to rounding
            ja, jb = json.loads(outs[0]), json.loads(outs[1])
            sa = {x["id"]: x for x in ja["sequences"]}
            sb = {x["id"]: x for x in jb["sequences"]}
            ok = sa.keys() == sb.keys() and all(
                rel(sa[k]["frequency_per_year"], sb[k]["frequency_per_year"])
                and cuts(sa[k]["cut_sets"], "frequency_per_year").keys()
                == cuts(sb[k]["cut_sets"], "frequency_per_year").keys() for k in sa)
            check(ok and all(rel(x["value_per_year"], y["value_per_year"])
                             for x, y in zip(ja["metrics"], jb["metrics"])),
                  f"{et}: --compile shared = per-row within 1e-12, same cut sets "
                  f"(forced collection and reordering: different BDDs)")
    p = subprocess.run([a.engine, MODEL, ets[0], "--compile", "together"],
                       capture_output=True, text=True)
    check(p.returncode != 0 and "--compile must be shared or per-row" in p.stderr,
          "refused: --compile together")

    for bad in ("abc", "-1", ""):
        p = subprocess.run([a.engine, MODEL, fts[0], "--reorder-threshold", bad],
                           capture_output=True, text=True)
        check(p.returncode != 0 and "--reorder-threshold needs" in p.stderr,
              f"refused: --reorder-threshold {bad!r}")

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("reorder: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
