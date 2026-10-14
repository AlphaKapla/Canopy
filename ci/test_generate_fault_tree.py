#!/usr/bin/env python3
"""Tests for ci/generate_fault_tree.py (FR-57): the synthetic trees the
v0.3 exit benchmark rests on.

For both structures (`random`, `psa`) and a few seeds:
  * the output is deterministic (same seed, byte-identical file) and a
    different seed gives a different tree;
  * every gate has at least 2 operands and no operand twice; k-of-n gates
    have 3+ operands and 2 <= k <= n-1; there is no NOT/XOR (coherent);
  * the gate graph is acyclic with the single root G1, every gate and
    basic event reachable from it, every referenced event defined;
  * probabilities lie in their ranges (random: [pmin, pmax]; psa: the
    documented ranges of failure modes, common-cause events and
    initiators, recognised by their role in the tree);
  * the counts are as documented: random — the requested number of basic
    events and, with --gates 0, about M x 3 occurrences; psa — about the
    requested number of basic events (within 15%: about 30 per front-line
    system, so small trees vary most);
  * the file imports through ci/import_mef.py to one fault tree FT-MAIN
    whose model the validator accepts, and the engine quantifies the
    small trees exactly and truncated with the exact value inside the
    bounds.

Usage: python ci/test_generate_fault_tree.py [--engine PATH]
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import json
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GEN = os.path.join(HERE, "generate_fault_tree.py")


def generate(out, *args):
    r = subprocess.run([sys.executable, GEN, out, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr)
    return out


def parse(path):
    """({gate: (kind, k, [(tag, name)])}, {event: p})"""
    root = ET.parse(path).getroot()
    gates, probs = {}, {}
    for g in root.iter("define-gate"):
        (op,) = list(g)
        k = int(op.get("min")) if op.get("min") else None
        gates[g.get("name")] = (op.tag, k, [(c.tag, c.get("name")) for c in op])
    for b in root.iter("define-basic-event"):
        probs[b.get("name")] = float(b.find("float").get("value"))
    return gates, probs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", os.path.join(ROOT, "engine/target/release/canopy")))
    a = ap.parse_args()
    failures = []

    def check(cond, msg):
        (print(f"  ok  {msg}") if cond else failures.append(msg))

    tmp = tempfile.mkdtemp(prefix="psa-gen-")
    try:
        for structure, n in (("random", 150), ("psa", 600)):
            for seed in (1, 2, 3):
                tag = f"{structure} {n} seed {seed}"
                x1 = generate(os.path.join(tmp, f"{structure}-{seed}-a.xml"), "--structure",
                              structure, "--basic-events", str(n), "--seed", str(seed))
                x2 = generate(os.path.join(tmp, f"{structure}-{seed}-b.xml"), "--structure",
                              structure, "--basic-events", str(n), "--seed", str(seed))
                check(open(x1).read() == open(x2).read(), f"{tag}: deterministic")
                gates, probs = parse(x1)
                bad = [g for g, (kind, k, kids) in gates.items()
                       if len(kids) < 2 or len(set(kids)) != len(kids)
                       or kind not in ("and", "or", "atleast")
                       or (kind == "atleast" and not (len(kids) >= 3 and 2 <= k <= len(kids) - 1))]
                check(not bad, f"{tag}: every gate AND/OR/k-of-n with 2+ distinct operands, "
                               f"valid k ({bad[:3]})")
                # acyclic, single root G1, everything reachable, nothing undefined
                refs = {c for _, _, kids in gates.values() for t, c in kids if t == "gate"}
                roots = [g for g in gates if g not in refs]
                state, order_ok = {}, True

                def visit(g):
                    nonlocal order_ok
                    state[g] = 1
                    for t, c in gates[g][2]:
                        if t == "gate":
                            if state.get(c) == 1:
                                order_ok = False
                            elif c not in state:
                                visit(c)
                    state[g] = 2
                sys.setrecursionlimit(100000)
                visit("G1")
                used = {c for _, _, kids in gates.values() for t, c in kids if t == "basic-event"}
                check(roots == ["G1"] and order_ok and len(state) == len(gates)
                      and used == set(probs) and refs <= set(gates),
                      f"{tag}: acyclic, single root G1, every gate and event used and defined")
                if structure == "random":
                    occ = sum(len(kids) for _, _, kids in gates.values())
                    check(len(probs) == n and all(0.01 <= p <= 0.05 for p in probs.values())
                          and abs(occ / len(gates) - 3.0) < 0.15,
                          f"{tag}: {len(probs)} events in [0.01, 0.05], "
                          f"{occ / len(gates):.2f} children per gate")
                else:
                    # roles: initiators are the event operands of the top's
                    # sequences; common-cause events the event operands of
                    # system ORs (gates whose operands include an AND or
                    # k-of-n gate) and of support trains (the last event of an
                    # OR whose other operands are components)
                    top = gates["G1"]
                    ies = {c for _, s in top[2] for t, c in gates[s][2] if t == "basic-event"}
                    ccf = {c for kind, _, kids in gates.values() if kind == "or"
                           and any(gates[g][0] in ("and", "atleast") for t, g in kids if t == "gate")
                           for t, c in kids if t == "basic-event"}
                    ok_ie = all(1e-3 <= probs[e] <= 1e-1 for e in ies) and len(ies) >= 2
                    ok_ccf = all(1e-6 <= probs[e] <= 1e-4 for e in ccf) and ccf
                    ok_all = all(1e-6 <= p <= 1e-1 for p in probs.values())
                    check(top[0] == "or" and all(gates[s][0] == "and" for _, s in top[2])
                          and ok_ie and ok_ccf and ok_all
                          and abs(len(probs) - n) <= 0.15 * n,
                          f"{tag}: OR of sequences (AND), {len(ies)} initiators in "
                          f"[1e-3, 1e-1], {len(ccf)} system CCF events in [1e-6, 1e-4], "
                          f"{len(probs)} events (about {n})")
                # import, validate, quantify
                model = os.path.join(tmp, f"{structure}-{seed}-model")
                imp = subprocess.run([sys.executable, os.path.join(HERE, "import_mef.py"), x1, model],
                                     capture_output=True, text=True)
                val = subprocess.run([sys.executable, os.path.join(HERE, "validate.py"), model,
                                      os.path.join(ROOT, "schema", "psa-model.schema.json")],
                                     capture_output=True, text=True) if imp.returncode == 0 else None
                check(imp.returncode == 0 and "1 root(s)" in imp.stdout and val is not None
                      and val.returncode == 0,
                      f"{tag}: imports as one fault tree and validates "
                      f"({(imp.stderr or (val.stdout if val else ''))[-200:]})")
                if imp.returncode == 0:
                    ex = subprocess.run([a.engine, model, "FT-MAIN", "--json", "--prob-only"],
                                        capture_output=True, text=True, timeout=300)
                    tr = subprocess.run([a.engine, model, "FT-MAIN", "--json", "--mcs-limit", "0",
                                         "--truncated", "1e-8"],
                                        capture_output=True, text=True, timeout=300)
                    if ex.returncode == 0 and tr.returncode == 0:
                        p = json.loads(ex.stdout)["probability"]
                        j = json.loads(tr.stdout)
                        s = 1e-12 * max(p, 1e-300)
                        check(j["probability_lower_bound"] - s <= p <= j["probability_upper_bound"] + s,
                              f"{tag}: exact {p:.6e} within truncated bounds "
                              f"[{j['probability_lower_bound']:.6e}, {j['probability_upper_bound']:.6e}]")
                    else:
                        failures.append(f"{tag}: engine failed: {ex.stderr[-200:]} {tr.stderr[-200:]}")
            a1, _ = parse(os.path.join(tmp, f"{structure}-1-a.xml"))
            a2, _ = parse(os.path.join(tmp, f"{structure}-2-a.xml"))
            check(a1 != a2, f"{structure}: seeds 1 and 2 give different trees")
        # refusals
        for args, msg in [(["--basic-events", "10", "--common-be", "1.0"], "--common-be"),
                          (["--basic-events", "10", "--pmin", "0.5", "--pmax", "0.1"], "pmin"),
                          (["--basic-events", "10", "--avg-children", "2"], "--avg-children"),
                          (["--basic-events", "10", "--structure", "dag"], "invalid choice")]:
            r = subprocess.run([sys.executable, GEN, os.path.join(tmp, "x.xml"), *args],
                               capture_output=True, text=True)
            check(r.returncode != 0 and msg in r.stderr, f"refused: {' '.join(args)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("generate_fault_tree: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
