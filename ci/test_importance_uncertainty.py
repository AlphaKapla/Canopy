#!/usr/bin/env python3
"""Tests for model-wide importance under uncertainty (FR-37):
`quantify.py --importance-uncertainty K`, the engine's `--importance-events`
and ci/importance.py `uncertainty_for_metric`.

  * One event tree: the combination reproduces the engine's own summaries
    bit for bit (same statistics, same measure arithmetic).
  * Two event trees (a harness-generated uncertain case, ET-TEST, plus
    ET-TEST2, which uses only the first functional event and has its own
    uncertain initiator), several cases: the combined events are exactly
    each metric's model-wide top K by point Fussell–Vesely; every combined
    draw is the per-iteration sum over the trees, recomputed here from the
    per-tree draws (the metric draw standing in where a tree does not depend
    on the event, which the cases exercise); and the Monte Carlo means of
    the model-wide F(x=1) and F(x=0) lie within 6 standard errors of their
    exact expectations E[f_IE,1]·E[P_1(CD | x=v)] + E[f_IE,2]·E[P_2(CD | x=v)]
    from the harness's exact-expectation oracle.
  * Incomplete inputs are refused (a tree that depends on an event but has
    no draws for it), and misuse of the flags fails loudly.

Usage: python ci/test_importance_uncertainty.py [--engine PATH]
"""
import argparse
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import importance  # noqa: E402
import property_test as pt  # noqa: E402

N = 20000
CASES = (2, 3, 5, 9, 14, 20)


def add_second_tree(m, d, ie2):
    """ET-TEST2: FE-F1's logic alone, failure -> CD, own initiator."""
    fe1 = sorted(m["fes"])[0]
    prov = {"source": "test", "justification": "second tree"}
    doc = {"event_tree": {
        "id": "ET-TEST2", "label": "second tree",
        "initiating_event": {"id": "IE-TEST2", "label": "second initiator",
                             "frequency": {"value": ie2, "unit": "per_year",
                                           "uncertainty": {"distribution": "lognormal",
                                                           "error_factor": 3.0}},
                             "provenance": prov},
        "functional_events": {"FE-G1": {"label": "g1", "top_gate": m["fes"][fe1]}},
        "sequences": {"SEQ-T0": {"path": {"FE-G1": "success"}, "end_state": "OK"},
                      "SEQ-T1": {"path": {"FE-G1": "failure"}, "end_state": "CD"}}}}
    open(os.path.join(d, "event-trees", "second.yaml"), "w").write(yaml.safe_dump(doc, sort_keys=True))
    return fe1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", os.path.join(ROOT, "engine/target/release/canopy")))
    a = ap.parse_args()
    failures = []

    def check(cond, msg):
        (print(f"  ok  {msg}") if cond else failures.append(msg))

    # 1) one tree: bit-identical to the engine's summaries
    j = json.loads(subprocess.run(
        [a.engine, os.path.join(ROOT, "model"), "ET-SLOCA", "--json", "--samples", "3000",
         "--importance-events", "BE-CCF-ECC-PMP-FTS-1-2,BE-RHR-PMP-A-FTS,BE-ECC-PMP-A-TM,BE-RPS-LOGIC-FAIL"],
        capture_output=True, text=True, check=True).stdout)
    u = importance.uncertainty_for_metric({"ET-SLOCA": j}, "CDF")
    eng = {r["event"]: r["uncertainty"] for r in j["metrics"][0]["importance"] if "uncertainty" in r}
    keys = ("frequency_if_true_per_year", "frequency_if_false_per_year", "birnbaum_per_year",
            "fussell_vesely", "raw", "rrw", "iterations_with_zero_frequency",
            "iterations_with_zero_frequency_if_false")
    check(u is not None and sorted(u["rows"]) == sorted(eng)
          and all(u["rows"][e][k] == eng[e][k] for e in eng for k in keys),
          f"one tree: combined summaries bit-identical to the engine's ({len(eng)} events)")

    # 2) two trees
    tmp = tempfile.mkdtemp(prefix="psa-impunc-")
    n_rows = n_outside = n_means = 0
    try:
        for i in CASES:
            seed = 20260708 * 1_000_003 + i
            m = pt.gen_model(random.Random(seed))
            urng = random.Random(seed ^ 0x5EED_5EED)
            uu = pt.gen_uncertainty(m, urng)
            o = pt.Oracle(m)
            uo = pt.UncertaintyOracle(m, uu, o)
            d = os.path.join(tmp, f"case{i}")
            os.makedirs(d)
            pt.write_uncertain_model(m, uu, d)
            ie2 = 3e-3
            fe1 = add_second_tree(m, d, ie2)
            v = subprocess.run([sys.executable, os.path.join(HERE, "validate.py"), d,
                                os.path.join(ROOT, "schema", "psa-model.schema.json")],
                               capture_output=True, text=True)
            if v.returncode != 0:
                failures.append(f"case {i}: validator: {v.stdout[-400:]}")
                continue
            out = os.path.join(tmp, f"r{i}.json")
            q = subprocess.run([sys.executable, os.path.join(HERE, "quantify.py"), d, out,
                                "--engine", a.engine, "--samples", str(N),
                                "--importance-uncertainty", "4"],
                               capture_output=True, text=True)
            if q.returncode != 0:
                failures.append(f"case {i}: quantify.py failed: {q.stderr[-500:]}")
                continue
            res = json.load(open(out))
            # selection: each metric's model-wide top 4 by point FV
            top = importance.for_metric(res, "CDF")
            want = {r["event"] for r in top["importance"][:4]} if top else set()
            unc = importance.uncertainty_for_metric(res, "CDF")
            got = set(unc["rows"]) if unc else set()
            check(got == want, f"case {i}: combined events = model-wide top 4 by FV ({sorted(got)})")
            if not unc:
                continue
            # per-iteration sums, recomputed here
            trees = {t: next(mm for mm in res[t]["metrics"] if mm["id"] == "CDF")
                     for t in sorted(res)}
            fdraws = [sum(trees[t]["uncertainty"]["draws"][k] for t in trees) for k in range(N)]
            ok_sum = all(abs(x - y) <= 1e-15 * max(abs(x), 1e-300)
                         for x, y in zip(fdraws, unc["frequency_draws"]))
            for e in sorted(got):
                for key, dkey in (("draws_if_true", "draws_if_true"), ("draws_if_false", "draws_if_false")):
                    mine = [0.0] * N
                    for t, mm in trees.items():
                        row = next((r for r in mm["importance"] if r["event"] == e), None)
                        src = (row["uncertainty"][dkey] if row and "uncertainty" in row
                               else mm["uncertainty"]["draws"])
                        if row is None and t == "ET-TEST2":
                            n_outside += 1
                        for k in range(N):
                            mine[k] += src[k]
                    ok_sum &= all(abs(x - y) <= 1e-15 * max(abs(x), 1e-300)
                                  for x, y in zip(mine, unc["rows"][e][key]))
            check(ok_sum, f"case {i}: every combined draw = per-iteration sum over trees")
            # exact expectations of the model-wide conditional frequencies
            sup_all = set()
            for t in m["fes"].values():
                o.support(t, sup_all)
            cd_seqs = [s for s in m["sequences"].values() if s["end_state"] == "CD"]

            def in_cd(st):
                return any(all(out_ == "bypassed" or (out_ == "failure") == o.ev(m["fes"][fe], st)
                               for fe, out_ in s["path"].items()) for s in cd_seqs)

            def in_cd2(st):
                return o.ev(m["fes"][fe1], st)
            sup2 = o.support(m["fes"][fe1], set())
            e_ie1 = uu["ie"][1]
            for e in sorted(got):
                n_rows += 1
                for val, key in ((True, "draws_if_true"), (False, "draws_if_false")):
                    exact = (e_ie1 * uo.expect(in_cd, sup_all | {e}, fixed=(e, val))
                             + ie2 * uo.expect(in_cd2, sup2 | {e}, fixed=(e, val)))
                    ds = unc["rows"][e][key]
                    mean = math.fsum(ds) / N
                    sd = math.sqrt(math.fsum((x - mean) ** 2 for x in ds) / (N - 1))
                    se = sd / math.sqrt(N)
                    n_means += 1
                    # a constant draw (no randomness left, e.g. F(x=0) = 0)
                    # is compared exactly, to rounding on the frequency scale
                    tol = 6 * se + 1e-12 * (e_ie1 + ie2)
                    if not abs(mean - exact) <= tol:
                        failures.append(f"case {i}: {e} {key}: MC mean {mean:.6e} vs exact "
                                        f"{exact:.6e} (s.e. {se:.3e})")
        check(n_means > 0, f"two trees: {n_means} model-wide conditional-frequency means "
                           f"within 6 SE of the exact expectation ({n_rows} event rows, "
                           f"{n_outside} tree-2 rows standing in with the metric draw)")
        check(n_outside > 0, "the 'tree does not depend on the event' path is exercised")

        # 3) the PR comment (compare.py): nothing when unchanged; the moved
        #    events, with the combiner's numbers, when the second
        #    initiator changes (every event's weight across trees shifts)
        i0 = CASES[0]
        seed = 20260708 * 1_000_003 + i0
        m = pt.gen_model(random.Random(seed))
        uu = pt.gen_uncertainty(m, random.Random(seed ^ 0x5EED_5EED))
        dh = os.path.join(tmp, "head-variant")
        os.makedirs(dh)
        pt.write_uncertain_model(m, uu, dh)
        add_second_tree(m, dh, 3e-2)                  # 10x the base's IE-TEST2
        rbase, rhead = os.path.join(tmp, f"r{i0}.json"), os.path.join(tmp, "rhead.json")
        q = subprocess.run([sys.executable, os.path.join(HERE, "quantify.py"), dh, rhead,
                            "--engine", a.engine, "--samples", str(N),
                            "--importance-uncertainty", "4"], capture_output=True, text=True)
        cmp = lambda b, h: subprocess.run([sys.executable, os.path.join(HERE, "compare.py"), b, h],
                                          capture_output=True, text=True).stdout
        same = cmp(rbase, rbase)
        check(q.returncode == 0 and "Importance under uncertainty" not in same,
              "compare: unchanged results, no importance-uncertainty section")
        md = cmp(rbase, rhead)
        uh = importance.uncertainty_for_metric(json.load(open(rhead)), "CDF")
        ub = importance.uncertainty_for_metric(json.load(open(rbase)), "CDF")
        rows = [l for l in md.splitlines() if l.startswith("| BE-")
                and md.find("### Importance under uncertainty") < md.find(l)]
        cell = lambda x: f"{x['mean']:.2%} [{x['p05']:.2%}, {x['p95']:.2%}]"
        ok_rows = bool(rows) and all(
            f"| {e} | {cell(ub['rows'][e]['fussell_vesely'])} | {cell(uh['rows'][e]['fussell_vesely'])} |" in md
            for e in (r.split("|")[1].strip() for r in rows))
        check("### Importance under uncertainty — CDF" in md and ok_rows,
              f"compare: the moved events listed with the combiner's numbers ({len(rows)} rows)")

        # 4) incomplete inputs refused
        res = json.load(open(os.path.join(tmp, f"r{CASES[0]}.json")))
        mm2 = next(mm for mm in res["ET-TEST2"]["metrics"] if mm["id"] == "CDF")
        victim = next((r for r in mm2["importance"] if "draws_if_true" in r.get("uncertainty", {})), None)
        if victim:
            del victim["uncertainty"]["draws_if_true"]
            try:
                importance.uncertainty_for_metric(res, "CDF")
                failures.append("a tree depending on an event without its draws was not refused")
            except importance.IncompleteDraws as ex:
                check("ET-TEST2 depends on" in str(ex), f"incomplete draws refused ({ex})")
        plain = json.loads(subprocess.run([a.engine, os.path.join(ROOT, "model"), "ET-SLOCA",
                                           "--json", "--samples", "200"],
                                          capture_output=True, text=True, check=True).stdout)
        check(importance.uncertainty_for_metric({"ET-SLOCA": plain}, "CDF") is None,
              "no importance draws: nothing combined (None)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 5) misuse
    model = os.path.join(ROOT, "model")
    for args, frag in ((["ET-SLOCA", "--importance-events", "BE-RHR-PMP-A-FTS"], "only apply with --samples"),
                       (["FT-RHR", "--samples", "10", "--importance-events", "BE-RHR-PMP-A-FTS"], "apply to event trees"),
                       (["ET-SLOCA", "--samples", "10", "--importance-events", "BE-RHR-PMP-A-FTS",
                         "--importance-uncertainty", "2"], "not both"),
                       (["ET-SLOCA", "--samples", "10", "--importance-events", "BE-NOPE"], "not a basic event"),
                       (["ET-SLOCA", "--samples", "10", "--importance-events", ","], "comma-separated list")):
        p = subprocess.run([a.engine, model, *args], capture_output=True, text=True)
        check(p.returncode != 0 and frag in p.stderr, f"engine refuses: {' '.join(args[1:])}")
    p = subprocess.run([sys.executable, os.path.join(HERE, "quantify.py"), model, os.devnull,
                        "--importance-uncertainty", "3"], capture_output=True, text=True)
    check(p.returncode != 0 and "needs K >= 1 and --samples" in p.stderr,
          "quantify.py refuses --importance-uncertainty without --samples")

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("importance under uncertainty: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
