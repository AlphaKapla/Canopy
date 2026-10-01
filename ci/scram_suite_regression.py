#!/usr/bin/env python3
"""SCRAM test-suite regression (FR-51, FR-52): the MEF importer and the engine
against the expected values SCRAM publishes for the inputs it bundles
(ci/fixtures/scram-suite-reference.json), and the importer against every
bundled input.

Usage: scram_suite_regression.py <scram-checkout> [--reference PATH]
         [--engine PATH] [--summary PATH]

The checkout needs input/ and tests/input/ at the fixture's commit (CI
sparse-checks them out; the inputs are GPL-3 and never committed here).

Checks:
  1. fault trees: P(top) of each listed input against SCRAM's published
     value, at SCRAM's own tolerance (EXPECT_DOUBLE_EQ / EXPECT_EQ: 1e-12
     relative, since the arithmetic order differs)
  2. event trees: each end state's probability (the SCRAM dialect has no
     initiator frequency; the importer uses 1 /yr) against SCRAM's value
  2b. uncertainty (FR-52): P(top), the Monte Carlo mean and standard
     deviation of P(top) and the minimal cut sets of the trees whose
     distributions SCRAM's tests publish results for; Canopy samples
     1,000,000 times so that its noise is negligible next to SCRAM's
     tolerance (SCRAM's own estimates come from 10,000 trials)
  3. the same event trees against hand-derived closed forms (below) to
     1e-12 relative, because SCRAM's own tolerance is loose (1e-5 on the
     gas leak) — plus the linked gas-leak pair (gas_leak.xml links into
     gas_leak_reactive.xml), for which SCRAM publishes no value
  4. sweep: every bundled XML file outside input/Aralia (that suite is
     aralia_regression.py's), imported with SCRAM's default mission time,
     either imports or is refused with an ERROR
     message, never a traceback; the imported set is exactly the
     fixture's; every input SCRAM's initializer tests reject is refused
     (documented deviations aside); every imported model validates with
     no error and quantifies (event trees: sequence probabilities sum
     to 1)
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCHEMA = os.path.join(ROOT, "schema", "psa-model.schema.json")
FLT_EPSILON = 2.0 ** -23


def tolerance(check, ref):
    if check in ("EXPECT_DOUBLE_EQ", "EXPECT_EQ"):
        return max(1e-12 * abs(ref), 1e-15)
    if check == "Approx":                 # Catch2 default: 100 float epsilons
        return 100 * FLT_EPSILON * abs(ref)
    if isinstance(check, list) and check[0] == "EXPECT_NEAR":
        return check[1]
    raise ValueError(f"unknown check {check!r}")


def closed_forms():
    """Hand-derived exact end-state probabilities: {(inputs, tree): {es: p}}.
    Every input is constant-probability, so each sequence is a product
    over independent events once shared ones are conditioned on."""
    out = {}
    # bcd.xml: split fractions only; D-if-B is the named branch (D 0.9/0.1)
    out[("input/EventTrees/bcd.xml",), "EventTree"] = {
        "Success": .1 * .8 * .9 + .1 * .2 * .9 + .9 * .6 * .6 + .9 * .4 * .5,
        "Failure": .1 * .8 * .1 + .1 * .2 * .1 + .9 * .6 * .4 + .9 * .4 * .5}
    # attack.xml: detection fractions L1 = 0.2, L2 = 0.7, L3 = 0.05
    l1, l2, l3 = 0.2, 0.7, 0.05
    out[("input/EventTrees/attack.xml",), "AttackTree"] = {
        "AttackFails": l1 + (1 - l1) * l2 * l3,
        "AttackSucceeds": (1 - l1) * (l2 * (1 - l3) + (1 - l2))}
    # gas_leak_reactive.xml: every basic event q = 1 - 0.95 (the file's
    # parameter arithmetic). A = IVALA | RC1 | SOLA&SOLB, B = IVALB | RC1 |
    # SOLA&SOLB, C = BDVAL | RC1 | SOLC&SOLD. RC1 fails every function
    # (S8); given RC1 works, C is independent of (A, B), which share
    # SOLA&SOLB.
    q = 1 - 0.95
    r, s = 1 - q, q * q
    c = 1 - (1 - q) * (1 - s)
    nanb = (1 - s) * r * r                # A works, B works
    anb = (1 - s) * q * r                 # A fails, B works (and symmetric)
    ab = s + (1 - s) * q * q
    reactive = {"S1": r * nanb * (1 - c), "S2": r * nanb * c,
                "S3": r * anb * (1 - c), "S4": r * anb * c,
                "S5": r * anb * (1 - c), "S6": r * anb * c,
                "S7": r * ab * (1 - c), "S8": r * ab * c + q}
    out[("input/EventTrees/gas_leak/gas_leak_reactive.xml",),
        "Gas-Leak-Event-Tree-Reactive"] = reactive
    # gas_leak.xml links its working detection path into the reactive
    # tree; detection fails when the CPU fails or 2 of 3 sensors do (all
    # 0.05), events disjoint from the reactive tree's
    p = 0.05
    det = 1 - (1 - p) * (1 - (3 * p * p * (1 - p) + p ** 3))
    pair = ("input/EventTrees/gas_leak/gas_leak_reactive.xml",
            "input/EventTrees/gas_leak/gas_leak.xml")
    out[pair, "Gas-Leak-Event-Tree"] = {"S9": det, **{
        k: (1 - det) * v for k, v in reactive.items()}}
    out[pair, "Gas-Leak-Event-Tree-Reactive"] = reactive
    # TwoTrain: both paths of the single fork reach S
    out[("input/TwoTrain/two_train.xml", "input/TwoTrain/event_tree.xml"),
        "EventTree"] = {"S": 1.0}
    return out


def run(cmd, timeout=600, **kw):
    """A hang is a failure to report, not a stuck job (V&V D-31 hung the
    importer): past the timeout the result has returncode 124."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"ERROR: timed out after "
                                           f"{timeout} s: {' '.join(cmd)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scram")
    ap.add_argument("--reference", default=os.path.join(
        HERE, "fixtures", "scram-suite-reference.json"))
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", os.path.join(ROOT, "engine/target/release/canopy")))
    ap.add_argument("--summary", help="also append the report to this file "
                    "(e.g. $GITHUB_STEP_SUMMARY)")
    a = ap.parse_args()
    ref = json.load(open(a.reference))
    env = {**os.environ, "CANOPY_BIN": a.engine}
    failures, lines = [], []
    tmp = tempfile.mkdtemp(prefix="psa-scram-suite-")
    counter = [0]

    def fail(msg):
        failures.append(msg)
        lines.append(f"- FAIL {msg}")

    def imp(inputs):
        """(model dir or None, import result)"""
        counter[0] += 1
        out = os.path.join(tmp, f"m{counter[0]}")
        r = run([sys.executable, os.path.join(HERE, "import_mef.py"),
                 *[os.path.join(a.scram, i) for i in inputs], out,
                 "--mission-time", str(ref["mission_time"]["hours"])], timeout=120)
        if r.returncode == 124:
            fail(f"{inputs}: {r.stderr}")
        return (out if r.returncode == 0 else None), r

    def end_states(model):
        """{tree MEF name: {end state: probability}} via quantify.py"""
        res = os.path.join(model, "..", os.path.basename(model) + ".json")
        q = run([sys.executable, os.path.join(HERE, "quantify.py"), model, res], env=env)
        if q.returncode != 0:
            return None, q.stderr.strip()[-300:] or q.stdout.strip()[-300:]
        mef = {}
        for f in glob.glob(os.path.join(model, "event-trees", "*.yaml")):
            t = yaml.safe_load(open(f))["event_tree"]
            mef[t["id"]] = t["external_ids"]["mef"]
        return {mef[tid]: ({e["id"]: e["frequency_per_year"] for e in t["end_states"]},
                           t.get("partition", {}).get("sum_probability"))
                for tid, t in json.load(open(res)).items()}, None

    try:
        # 1. fault trees vs SCRAM
        lines.append("### Fault trees: P(top) vs SCRAM's published values\n")
        lines.append("| input | SCRAM | Canopy | check | source |")
        lines.append("|---|---|---|---|---|")
        n_ft = 0
        for case in ref["fault_trees"]:
            model, r = imp(case["inputs"])
            if model is None:
                fail(f"{case['inputs']}: import refused: {r.stderr.strip()[-200:]}")
                continue
            e = run([a.engine, model, "FT-MAIN", "--json", "--prob-only"])
            if e.returncode != 0:
                fail(f"{case['inputs']}: engine: {e.stderr.strip()[-200:]}")
                continue
            p = json.loads(e.stdout)["probability"]
            ok = abs(p - case["p_top"]) <= tolerance(case["check"], case["p_top"])
            n_ft += ok
            lines.append(f"| {case['inputs'][0]} | {case['p_top']:.6g} | {p:.10g} | "
                         f"{case['check']} | {case['source']} |")
            if not ok:
                fail(f"{case['inputs'][0]}: P(top) {p!r}, SCRAM {case['p_top']} "
                     f"({case['check']}, {case['source']})")

        # 2 + 3. event trees vs SCRAM and vs closed forms
        exact = closed_forms()
        published = {(tuple(c["inputs"]), c["tree"]): c for c in ref["event_trees"]}
        lines.append("\n### Event trees: end-state probabilities\n")
        lines.append("| inputs | tree | end state | SCRAM | closed form | Canopy |")
        lines.append("|---|---|---|---|---|---|")
        n_es = n_pub = 0
        by_inputs = {}
        for inputs, tree in sorted(set(exact) | set(published)):
            by_inputs.setdefault(inputs, []).append(tree)
        for inputs, trees in by_inputs.items():
            model, r = imp(list(inputs))
            if model is None:
                fail(f"{list(inputs)}: import refused: {r.stderr.strip()[-200:]}")
                continue
            got, err = end_states(model)
            if got is None:
                fail(f"{list(inputs)}: quantify: {err}")
                continue
            for tree in trees:
                if tree not in got:
                    fail(f"{list(inputs)}: no results for tree {tree}")
                    continue
                vals, part = got[tree]
                if part is None or abs(part - 1.0) > 1e-9:
                    fail(f"{list(inputs)} {tree}: partition sum {part}")
                pub = published.get((inputs, tree))
                ex = exact.get((inputs, tree), {})
                states = sorted(set(vals) | set(ex) | set(pub["end_states"] if pub else {}))
                for es in states:
                    v = vals.get(es)
                    if v is None:
                        fail(f"{list(inputs)} {tree}: no end state {es}")
                        continue
                    if pub and es in pub["end_states"]:
                        sv = pub["end_states"][es]
                        n_pub += 1
                        if abs(v - sv) > tolerance(pub["check"], sv):
                            fail(f"{list(inputs)} {tree} {es}: {v!r}, SCRAM {sv} "
                                 f"({pub['check']}, {pub['source']})")
                    if es in ex and abs(v - ex[es]) > max(1e-12 * abs(ex[es]), 1e-15):
                        fail(f"{list(inputs)} {tree} {es}: {v!r}, closed form {ex[es]!r}")
                    if es not in ex:
                        fail(f"{list(inputs)} {tree}: end state {es} has no closed form")
                    n_es += 1
                    lines.append(
                        f"| {', '.join(os.path.basename(i) for i in inputs)} | {tree} | "
                        f"{es} | {pub['end_states'][es] if pub and es in pub['end_states'] else '—'} | "
                        f"{ex.get(es, float('nan')):.12g} | {v:.12g} |")

        # 2b. uncertainty: P(top), mean, sigma and cut sets vs SCRAM
        uc = ref["uncertainty"]
        lines.append(f"\n### Uncertainty: {uc['samples']:,} samples, seed {uc['seed']}\n")
        lines.append("| input | quantity | SCRAM | Canopy | check |")
        lines.append("|---|---|---|---|---|")
        n_unc = 0
        for case in uc["cases"]:
            model, r = imp(case["inputs"])
            if model is None:
                fail(f"{case['inputs']}: import refused: {r.stderr.strip()[-200:]}")
                continue
            e = run([a.engine, model, "FT-MAIN", "--json", "--samples", str(uc["samples"]),
                     "--seed", str(uc["seed"])])
            if e.returncode != 0:
                fail(f"{case['inputs']}: engine: {e.stderr.strip()[-200:]}")
                continue
            j = json.loads(e.stdout)
            got = {"p_top": j["probability"], "mean": j["uncertainty"]["mean"],
                   "sigma": j["uncertainty"]["std"]}
            for q in ("p_top", "mean", "sigma"):
                ok = abs(got[q] - case[q]) <= tolerance(case[q + "_check"], case[q])
                n_unc += ok
                lines.append(f"| {case['inputs'][0]} | {q} | {case[q]} | {got[q]:.6g} | "
                             f"{case[q + '_check']} |")
                if not ok:
                    fail(f"{case['inputs'][0]}: {q} {got[q]!r}, SCRAM {case[q]} "
                         f"({case[q + '_check']}, {case['source']})")
            mef = {}
            for f in glob.glob(os.path.join(model, "basic-events", "*.yaml")):
                for bid, b in yaml.safe_load(open(f))["basic_events"].items():
                    mef[bid] = b["external_ids"]["mef"]
            cs = sorted(sorted(mef[x] for x in c["events"]) for c in j["minimal_cut_sets"])
            want = sorted(sorted(c) for c in case["cut_sets"])
            n_unc += cs == want
            lines.append(f"| {case['inputs'][0]} | minimal cut sets | {len(want)} | "
                         f"{len(cs)}{' (same)' if cs == want else ' (DIFFERENT)'} | "
                         f"EXPECT_EQ |")
            if cs != want:
                fail(f"{case['inputs'][0]}: cut sets {cs}, SCRAM {want}")

        # 4. sweep over every bundled input
        expected_imports = set(ref["imports"]["inputs"])
        rejects = set(ref["scram_rejects"]["inputs"])
        deviations = ref["scram_rejects"]["deviations"]
        files = sorted(os.path.relpath(p, a.scram) for d in ("input", "tests/input")
                       for p in glob.glob(os.path.join(a.scram, d, "**", "*.xml"),
                                          recursive=True)
                       if not os.path.relpath(p, a.scram).startswith("input/Aralia/"))
        if not files:
            fail(f"no MEF inputs under {a.scram}/input and tests/input")
        imported, refused = set(), set()
        for f in files:
            model, r = imp([f])
            if "Traceback" in r.stderr:
                fail(f"{f}: importer crashed: {r.stderr.strip().splitlines()[-1]}")
                continue
            if model is None:
                if "ERROR:" not in r.stderr:
                    fail(f"{f}: refused without an ERROR message")
                refused.add(f)
                continue
            imported.add(f)
            v = run([sys.executable, os.path.join(HERE, "validate.py"), model, SCHEMA])
            if v.returncode != 0:
                fail(f"{f}: imported model does not validate: {v.stdout.strip()[-200:]}")
                continue
            if os.path.isdir(os.path.join(model, "event-trees")):
                got, err = end_states(model)
                if got is None:
                    fail(f"{f}: quantify: {err}")
                    continue
                for tree, (_, part) in got.items():
                    if part is None or abs(part - 1.0) > 1e-9:
                        fail(f"{f} {tree}: partition sum {part}")
            else:
                e = run([a.engine, model, "FT-MAIN", "--json", "--prob-only"])
                if e.returncode != 0:
                    fail(f"{f}: engine: {e.stderr.strip()[-200:]}")
        for f in sorted(imported - expected_imports):
            fail(f"{f}: imports, but the fixture lists it as refused")
        for f in sorted(expected_imports - imported):
            fail(f"{f}: refused (or failed), but the fixture lists it as imported")
        missing = sorted((expected_imports | rejects) - set(files))
        if missing:
            fail(f"{len(missing)} fixture input(s) absent from the checkout, "
                 f"e.g. {missing[0]}")
        for f in sorted(rejects & imported):
            if f not in deviations:
                fail(f"{f}: SCRAM rejects this input, Canopy imports it")
        lines.insert(0, f"## SCRAM suite (FR-51, FR-52): {n_ft}/{len(ref['fault_trees'])} "
                        f"fault trees, {n_pub} published end-state values and "
                        f"{n_unc}/{4 * len(uc['cases'])} uncertainty results agree; "
                        f"{n_es} end states match closed forms; {len(files)} inputs "
                        f"swept ({len(imported)} imported, {len(refused)} refused, "
                        f"{len(rejects & refused)}/{len(rejects)} SCRAM rejects refused, "
                        f"deviations: {len(rejects & imported)})\n")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    report = "\n".join(lines) + "\n"
    print(report)
    if a.summary:
        with open(a.summary, "a") as f:
            f.write(report)
    if failures:
        print(f"scram_suite_regression: {len(failures)} failure(s)", file=sys.stderr)
        return 1
    print("scram_suite_regression: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
