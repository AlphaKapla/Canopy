#!/usr/bin/env python3
"""Cross-verify the Rust engine against SCRAM on MEF exports.

Runs the demo model plus N randomly generated models (the property-test
generator) through BOTH engines and compares every sequence probability.
Exports use --expand-ccf so the comparison is convention-independent
(SCRAM's alpha-factor is non-staggered; ours defaults to staggered).

For every generated case whose fault tree is non-coherent (NOT/XOR), the
complete prime-implicant sets of both engines are also compared (FR-30):
an FT-only copy of the case is exported, SCRAM runs --prime-implicants,
and its products for the tree's top gate must equal ours exactly.

Each case's transfer variant (the property harness's, when it has no
per-sequence house overrides) is exported with the transfer as a MEF link
(FR-54): SCRAM's sequence probabilities under each initiating event must
equal ours — the tree's own rows, and the target's rows reached through
the link under the target's sequence names (FR-55).

Each case's uncertainty variant (the harness's, CCF factor uncertainty
dropped: MEF has none) is exported with its distributions (FR-53) and
SCRAM runs its Monte Carlo: SCRAM's mean of every sequence probability
must lie within Z standard errors (plus its 6-significant-digit report)
of the exact expectation that the harness's oracle computes (FR-55). SCRAM refuses some valid
distributions — its check that a probability's sampled range stays in
[0, 1] uses rough upper estimates (for a beta, 1/I_0.99(a, b), which
exceeds 1 unless it rounds to 1) — and such refusals are counted, not
failed.

Requires `scram` on PATH (see docs/ci.md for the build recipe).

Usage: crosscheck_scram.py [--cases N] [--seed S] [--trials N]
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
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(__file__))
from property_test import (Oracle, UncertaintyOracle, gen_model, gen_transfer,  # noqa: E402
                           gen_uncertainty, write_model, write_transfer_model,
                           write_uncertain_model)

import yaml  # noqa: E402

REL_TOL = 2e-5   # SCRAM reports 6 significant digits


def close(a, b):
    return abs(a - b) <= max(1e-12, REL_TOL * max(abs(a), abs(b)))


Z = 5.0          # standard errors allowed between SCRAM's MC mean and the exact one


def scram_report(xml_path, *flags):
    """(report root, None) or (None, SCRAM's stderr)."""
    out = tempfile.mktemp(suffix=".xml")
    r = subprocess.run(["scram", "--bdd", "--probability", *flags, xml_path, "-o", out],
                       capture_output=True, text=True)
    if r.returncode:
        return None, (r.stderr or r.stdout).strip()
    root = ET.parse(out).getroot()
    os.unlink(out)
    return root, None


def scram_sequences(xml_path):
    """{(initiating event, sequence): probability}"""
    root, err = scram_report(xml_path)
    if root is None:
        raise RuntimeError(f"SCRAM failed: {err}")
    return {(ie.get("name"), s.get("name")): float(s.get("value"))
            for ie in root.iter("initiating-event") for s in ie.iter("sequence")}


def engine_sequences(engine, model_dir, et_id):
    """{(initiating event, sequence): probability} as SCRAM names them: a
    row followed through a MEF link is reported under the target's
    sequence name, and the transferring row itself is not reported."""
    r = json.loads(subprocess.run(
        [engine, model_dir, et_id, "--json"],
        check=True, capture_output=True, text=True).stdout)
    ie, f = r["initiating_event"]["id"], r["initiating_event"]["frequency_per_year"]
    out = {}
    for s in r["sequences"]:
        tp = s.get("transfer_path")
        if tp:
            key = (ie, tp[-1]["sequence"])
        elif (s.get("followed") or {}).get("probability") is not None:
            continue            # the transferring row: SCRAM follows the link
        else:
            key = (ie, s["id"])
        out[key] = out.get(key, 0.0) + s["frequency_per_year"] / f
    return out


def check(engine, model_dir, et_ids, label):
    xml = tempfile.mktemp(suffix=".xml")
    subprocess.run([sys.executable, "ci/export_mef.py", model_dir, xml,
                    "--expand-ccf"], check=True, capture_output=True)
    ours = {}
    for et_id in ([et_ids] if isinstance(et_ids, str) else et_ids):
        ours.update(engine_sequences(engine, model_dir, et_id))
    theirs = scram_sequences(xml)
    os.unlink(xml)
    problems = []
    if set(ours) != set(theirs):
        problems.append(f"sequence sets differ: {set(ours) ^ set(theirs)}")
    for sid in sorted(set(ours) & set(theirs)):
        if not close(ours[sid], theirs[sid]):
            problems.append(
                f"{'/'.join(sid)}: engine {ours[sid]:.6e} scram {theirs[sid]:.6e}")
    status = "ok " if not problems else "FAIL"
    print(f"{status} {label}: {len(ours)} sequences compared")
    for p in problems:
        print("    ", p)
    return not problems


def check_primes(engine, model_dir, top, label):
    """Complete prime-implicant sets of the fault tree FT-TEST: ours vs
    SCRAM's products for the top gate. Returns (ok, number compared)."""
    tmp = tempfile.mkdtemp(prefix="psa-xc-pi-")
    try:
        d = os.path.join(tmp, "m")
        shutil.copytree(model_dir, d)
        shutil.rmtree(os.path.join(d, "event-trees"), ignore_errors=True)
        man = yaml.safe_load(open(os.path.join(d, "model.yaml")))
        man["includes"].pop("event_trees", None)
        yaml.safe_dump(man, open(os.path.join(d, "model.yaml"), "w"))
        xml, rep = os.path.join(tmp, "m.xml"), os.path.join(tmp, "r.xml")
        subprocess.run([sys.executable, "ci/export_mef.py", d, xml, "--expand-ccf"],
                       check=True, capture_output=True)
        subprocess.run(["scram", "--bdd", "--prime-implicants", xml, "-o", rep],
                       check=True, capture_output=True)
        theirs = None
        for sop in ET.parse(rep).getroot().iter("sum-of-products"):
            if sop.get("name") != top:
                continue
            theirs = set()
            for prod in sop.iter("product"):
                pos, neg = set(), set()
                for el in prod:
                    if el.tag == "basic-event":
                        pos.add(el.get("name"))
                    elif el.tag == "not":
                        neg |= {be.get("name") for be in el.iter("basic-event")}
                theirs.add((frozenset(pos), frozenset(neg)))
        r = json.loads(subprocess.run(
            [engine, model_dir, "FT-TEST", "--json", "--prime-implicants",
             "--mcs-limit", "1000000"], check=True, capture_output=True, text=True).stdout)
        ours = {(frozenset(x["events"]), frozenset(x["negated"]))
                for x in r["prime_implicants"]}
        if theirs is None:
            print(f"FAIL {label}: SCRAM reported no products for {top}")
            return False, 0
        if ours != theirs:
            print(f"FAIL {label}: prime implicants ours {len(ours)}, SCRAM "
                  f"{len(theirs)}; only ours {list(ours - theirs)[:2]}, only SCRAM "
                  f"{list(theirs - ours)[:2]}")
            return False, 0
        print(f"ok  {label}: {len(ours)} prime implicants identical")
        return True, len(ours)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_uncertainty(m, u, d, label, trials, seed):
    """SCRAM's Monte Carlo mean of every sequence probability vs the exact
    expectation. Returns 'ok', 'refused' (SCRAM's sample-domain check) or
    'fail'."""
    u = {**u, "factor": None}                  # no MEF form for factor uncertainty
    write_uncertain_model(m, u, d)
    xml = os.path.join(d, "m.xml")
    subprocess.run([sys.executable, "ci/export_mef.py", d, xml, "--uncertainty",
                    "--expand-ccf"], check=True, capture_output=True)
    root, err = scram_report(xml, "--uncertainty", "--num-trials", str(trials),
                             "--seed", str(seed))
    if root is None:
        if "sample domain" in err:
            print(f"ref  {label}: SCRAM refuses a distribution's sample domain")
            return "refused"
        print(f"FAIL {label}: SCRAM failed: {err[-300:]}")
        return "fail"
    o = Oracle(m)
    uo = UncertaintyOracle(m, u, o)
    sup = set()
    for t in m["fes"].values():
        o.support(t, sup)
    got = {me.get("name"): (float(me.find("mean").get("value")),
                            float(me.find("standard-deviation").get("value")))
           for me in root.iter("measure") if me.get("initiating-event") == "IE-TEST"}
    problems, n = [], 0
    for sid, seq in sorted(m["sequences"].items()):
        def match(st, seq=seq):
            return all(out == "bypassed" or (out == "failure") == o.ev(m["fes"][fe], st)
                       for fe, out in seq["path"].items())
        e = uo.expect(match, sup)
        if sid not in got:
            if e > 0:
                problems.append(f"{sid}: no SCRAM uncertainty result (exact mean {e:.6e})")
            continue
        mean, sd = got[sid]
        n += 1
        # Z standard errors, plus SCRAM's 6-significant-digit report (a
        # nearly deterministic sequence has a tiny standard error)
        if abs(mean - e) > Z * sd / math.sqrt(trials) + REL_TOL * abs(e):
            problems.append(f"{sid}: SCRAM mean {mean:.6e} (sd {sd:.3e}, {trials} trials) "
                            f"vs exact {e:.6e}")
    print(f"{'ok ' if not problems else 'FAIL'} {label}: {n} sequence means vs exact")
    for p in problems:
        print("    ", p)
    return "ok" if not problems else "fail"



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=25)
    ap.add_argument("--seed", type=int, default=20260708)
    ap.add_argument("--trials", type=int, default=10000,
                    help="SCRAM Monte Carlo trials for the uncertainty variants")
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", "engine/target/release/canopy"))
    a = ap.parse_args()

    ok = check(a.engine, "model", "ET-SLOCA", "demo model")
    pi_cases = pi_products = 0
    xfer = {"ok": 0, "overrides": 0}
    unc = {"ok": 0, "refused": 0, "fail": 0}
    for i in range(a.cases):
        rng = random.Random(a.seed * 7_919 + i)
        d = tempfile.mkdtemp(prefix="psa-xc-")
        try:
            m = gen_model(rng)
            write_model(m, d)
            ok &= check(a.engine, d, "ET-TEST", f"generated case {i}")
            if Oracle(m).uses_negation(m["top"]):
                good, n = check_primes(a.engine, d, m["top"],
                                       f"generated case {i} (non-coherent tree)")
                ok &= good
                pi_cases += good
                pi_products += n
            # FR-55: the transfer variant through a MEF link
            x = gen_transfer(m, random.Random(a.seed * 7_919 + i + 1_000_003))
            if x["origin_house"] or x["target_house"]:
                xfer["overrides"] += 1
            else:
                dx = os.path.join(d, "transfer")
                write_transfer_model(m, x, dx)
                good = check(a.engine, dx, ["ET-TEST"] + (["ET-TEST2"] if x["has_ie"] else []),
                             f"generated case {i} (transfer as a MEF link)")
                ok &= good
                xfer["ok"] += good
            # FR-55: the uncertainty variant, SCRAM's Monte Carlo vs exact
            u = gen_uncertainty(m, random.Random(a.seed * 7_919 + i + 2_000_003))
            r = check_uncertainty(m, u, os.path.join(d, "uncertainty"),
                                  f"generated case {i} (uncertainty, SCRAM Monte Carlo)",
                                  a.trials, a.seed + i)
            unc[r] += 1
            ok &= r != "fail"
        finally:
            shutil.rmtree(d, ignore_errors=True)
    print(f"\nprime implicants: {pi_cases} non-coherent trees identical "
          f"({pi_products} products)")
    print(f"transfers as MEF links: {xfer['ok']} variants agree "
          f"({xfer['overrides']} with per-sequence house overrides, not exportable)")
    print(f"uncertainty: {unc['ok']} variants with SCRAM's Monte Carlo means within "
          f"{Z} standard errors of the exact ones, {unc['refused']} refused by SCRAM's "
          f"sample-domain check, {unc['fail']} failed")
    print("\nCROSS-CHECK", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
