#!/usr/bin/env python3
"""Property-based validation: engine vs independent brute-force oracle.

Generates random small PSA models (random gate DAGs with and/or/atleast/
not/xor, house events, CCF groups, event trees over shared logic), runs
ci/validate.py and the Rust engine on each, and independently recomputes
every result by truth-table enumeration in Python. Any disagreement fails
and the offending model is preserved for reproduction.

Checked properties, per generated model:
  * validate.py accepts the model (generator/schema/linter agreement)
  * fault tree: exact P(top) matches the oracle (rel tol 1e-9)
  * fault tree: engine's coherence flag matches the generator's knowledge
  * coherent trees: the engine's minimal cut sets EQUAL the oracle's
    (same sets, same count), and each cut probability matches
  * Birnbaum importances match on sampled events
  * event tree: every sequence frequency matches the oracle
  * partition: sequence probabilities sum to 1 (the table covers the
    outcome space exactly once)
  * coherent sequences: failure-logic cut sets equal the oracle's;
    non-coherent sequences: engine reports none (minsol would be invalid)
  * CCF: the oracle performs its own NUREG/CR-5485 alpha-factor expansion,
    so the engine's expansion is cross-checked end to end

Usage: property_test.py [--cases N] [--seed S] [--engine PATH]
"""
import argparse
import itertools
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
from math import comb

import yaml

REL_TOL = 1e-9
ABS_TOL = 1e-15


def close(a, b):
    return abs(a - b) <= max(ABS_TOL, REL_TOL * max(abs(a), abs(b)))


# --------------------------------------------------------------------------
# random model generation
# --------------------------------------------------------------------------
def gen_model(rng: random.Random):
    nbe = rng.randint(4, 9)
    bes = {f"BE-V{i+1:02d}": round(10 ** rng.uniform(-4, math.log10(0.5)), 12)
           for i in range(nbe)}
    houses = {}
    if rng.random() < 0.4:
        houses["HE-H1"] = rng.random() < 0.5

    be_ids = list(bes)
    gate_ids = []
    gates = {}
    ngates = rng.randint(3, 7)
    for gi in range(ngates):
        pool = be_ids + gate_ids + list(houses)
        k = rng.randint(2, min(4, len(pool)))
        ops = rng.sample(pool, k)
        if rng.random() < 0.15:                      # negate one operand
            ops[0] = {"not": ops[0]}
        r = rng.random()
        if r < 0.45:
            formula = {"or": ops}
        elif r < 0.8:
            formula = {"and": ops}
        elif r < 0.92:
            formula = {"atleast": {"k": rng.randint(2, len(ops)),
                                   "of": ops}}
        else:
            formula = {"xor": ops[:2]} if len(ops) >= 2 else {"or": ops}
        gid = f"GT-G{gi+1:02d}"
        gates[gid] = formula
        gate_ids.append(gid)

    ccf = None
    if rng.random() < 0.5 and nbe >= 3:
        # The oracle checks probability by brute-force enumeration over a
        # formula's full support, which after CCF substitution can include
        # every combination event (2^n - n - 1 of them). Group sizes above
        # ~4 make that enumeration intractable, so the randomized harness
        # only samples small groups; the n=8 cap boundary itself is
        # verified by a closed-form hand-computed unit test instead
        # (engine/src/model.rs::ccf_tests::group_size_eight_beta_factor).
        size_choices = [2, 2, 3]
        if nbe >= 4:
            size_choices.append(4)
        m = rng.sample(be_ids, rng.choice(size_choices))
        n = len(m)
        raw = [rng.uniform(0.2, 1.0)] + [rng.uniform(0.001, 0.1)
                                         for _ in range(n - 1)]
        s = sum(raw)
        alphas = [round(x / s, 10) for x in raw]
        alphas[0] = round(1.0 - sum(alphas[1:]), 10)
        ccf = {
            "id": "CCF-G1",
            "members": m,
            "alphas": alphas,
            "qt": round(10 ** rng.uniform(-3, -1), 12),
            "testing": rng.choice(["staggered", "non-staggered"]),
        }

    nfe = rng.choice([2, 2, 3])
    fe_tops = rng.sample(gate_ids, min(nfe, len(gate_ids)))
    fes = {f"FE-F{i+1}": t for i, t in enumerate(fe_tops)}
    fe_order = list(fes)
    sequences = {}
    if rng.random() < 0.5 and len(fe_order) >= 2:
        # SLOCA-style: first FE failure bypasses the rest
        sequences["SEQ-S00"] = {
            "path": {fe_order[0]: "failure",
                     **{f: "bypassed" for f in fe_order[1:]}},
            "end_state": "CD"}
        rest = fe_order[1:]
        for i, combo in enumerate(
                itertools.product(["success", "failure"], repeat=len(rest))):
            path = {fe_order[0]: "success"}
            path.update(dict(zip(rest, combo)))
            sequences[f"SEQ-S{i+1:02d}"] = {
                "path": path,
                "end_state": "CD" if "failure" in combo else "OK"}
    else:
        for i, combo in enumerate(itertools.product(
                ["success", "failure"], repeat=len(fe_order))):
            sequences[f"SEQ-S{i:02d}"] = {
                "path": dict(zip(fe_order, combo)),
                "end_state": "CD" if "failure" in combo else "OK"}

    ie_freq = round(10 ** rng.uniform(-4, -2), 12)
    return dict(bes=bes, houses=houses, gates=gates,
                top=gate_ids[-1], ccf=ccf, fes=fes, fe_order=fe_order,
                sequences=sequences, ie_freq=ie_freq)


def write_model(m, d):
    prov = {"source": "property-test generator",
            "justification": "randomized validation case"}
    os.makedirs(f"{d}/basic-events"); os.makedirs(f"{d}/fault-trees")
    os.makedirs(f"{d}/event-trees")
    dump = lambda p, o: open(p, "w").write(
        yaml.safe_dump(o, sort_keys=True, default_flow_style=False))
    dump(f"{d}/model.yaml", {
        "schema_version": "0.1.0",
        "model": {"id": "PROP-TEST", "name": "generated",
                  "risk_metrics": [{"id": "CDF", "label": "CD frequency",
                                    "end_states": ["CD"]}]},
        "includes": {"parameters": ["parameters.yaml"],
                     "basic_events": ["basic-events/*.yaml"],
                     "fault_trees": ["fault-trees/*.yaml"],
                     "event_trees": ["event-trees/*.yaml"],
                     "house_events": ["house-events.yaml"]}})
    dump(f"{d}/parameters.yaml", {"parameters": {}})
    dump(f"{d}/house-events.yaml", {"house_events": {
        h: {"label": "generated house event", "default": v,
            "provenance": prov} for h, v in m["houses"].items()}})
    dump(f"{d}/basic-events/gen.yaml", {"basic_events": {
        b: {"label": f"generated event {b}",
            "failure_model": {"type": "probability",
                              "value": {"value": p, "unit": "per_demand"}},
            "provenance": prov} for b, p in m["bes"].items()}})
    dump(f"{d}/fault-trees/gen.yaml", {"fault_trees": {"FT-TEST": {
        "label": "generated tree", "top_gate": m["top"],
        "gates": {g: {"label": f"generated gate {g}", "formula": f}
                  for g, f in m["gates"].items()}}}})
    dump(f"{d}/event-trees/gen.yaml", {"event_tree": {
        "id": "ET-TEST", "label": "generated event tree",
        "initiating_event": {
            "id": "IE-TEST", "label": "generated initiator",
            "frequency": {"value": m["ie_freq"], "unit": "per_year"},
            "provenance": prov},
        "functional_events": {
            fe: {"label": f"generated fn event {fe}", "top_gate": t}
            for fe, t in m["fes"].items()},
        "sequences": m["sequences"]}})
    if m["ccf"]:
        c = m["ccf"]
        dump(f"{d}/ccf-groups.yaml", {"ccf_groups": {c["id"]: {
            "label": "generated CCF group", "model": "alpha-factor",
            "members": c["members"], "testing": c["testing"],
            "total_probability": {"value": c["qt"], "unit": "per_demand"},
            "factors": {f"alpha_{k+1}": a
                        for k, a in enumerate(c["alphas"])},
            "provenance": prov}}})


# --------------------------------------------------------------------------
# independent oracle
# --------------------------------------------------------------------------
def oracle_expand_ccf(m):
    """NUREG/CR-5485 alpha-factor expansion, implemented independently."""
    be_p = dict(m["bes"])
    gates = dict(m["gates"])
    if not m["ccf"]:
        return be_p, gates
    c = m["ccf"]
    n = len(c["members"])
    al, qt = c["alphas"], c["qt"]
    if c["testing"] == "staggered":
        qk = [al[k-1] / comb(n-1, k-1) * qt for k in range(1, n+1)]
    else:
        at = sum((i+1) * a for i, a in enumerate(al))
        qk = [k * al[k-1] / (at * comb(n-1, k-1)) * qt
              for k in range(1, n+1)]
    for mem in c["members"]:
        be_p[mem] = qk[0]
    sub = {}
    for mask in range(1, 1 << n):
        idxs = [i for i in range(n) if mask >> i & 1]
        if len(idxs) < 2:
            continue
        cid = f"BE-{c['id']}-" + "-".join(str(i+1) for i in idxs)
        be_p[cid] = qk[len(idxs) - 1]
        for i in idxs:
            sub.setdefault(c["members"][i], []).append(cid)

    def rw(f):
        if isinstance(f, str):
            return {"or": [f] + sub[f]} if f in sub else f
        (op, a), = f.items()
        if op == "not":
            return {"not": rw(a)}
        if op == "atleast":
            return {"atleast": {"k": a["k"], "of": [rw(x) for x in a["of"]]}}
        return {op: [rw(x) for x in a]}
    return be_p, {g: rw(f) for g, f in gates.items()}


class Oracle:
    def __init__(self, m):
        self.be_p, self.gates = oracle_expand_ccf(m)
        self.houses = m["houses"]

    def ev(self, f, st):
        if isinstance(f, str):
            if f.startswith("BE-"):
                return st[f]
            if f.startswith("HE-"):
                return self.houses[f]
            return self.ev(self.gates[f], st)
        (op, a), = f.items()
        if op == "and":
            return all(self.ev(x, st) for x in a)
        if op == "or":
            return any(self.ev(x, st) for x in a)
        if op == "xor":
            return sum(self.ev(x, st) for x in a) % 2 == 1
        if op == "not":
            return not self.ev(a, st)
        return sum(self.ev(x, st) for x in a["of"]) >= a["k"]

    def support(self, f, acc):
        if isinstance(f, str):
            if f.startswith("BE-"):
                acc.add(f)
            elif f.startswith("GT-"):
                self.support(self.gates[f], acc)
            return acc
        (op, a), = f.items()
        if op == "not":
            self.support(a, acc)
        elif op == "atleast":
            for x in a["of"]:
                self.support(x, acc)
        else:
            for x in a:
                self.support(x, acc)
        return acc

    def uses_negation(self, f):
        if isinstance(f, str):
            return (f.startswith("GT-")
                    and self.uses_negation(self.gates[f]))
        (op, a), = f.items()
        if op in ("not", "xor"):
            return True
        if op == "atleast":
            return any(self.uses_negation(x) for x in a["of"])
        return any(self.uses_negation(x) for x in a)

    def prob(self, pred, sup):
        """P[pred(state)] by enumeration over the support variables."""
        sup = sorted(sup)
        total = 0.0
        for bits in itertools.product([False, True], repeat=len(sup)):
            st = dict(zip(sup, bits))
            if pred(st):
                w = 1.0
                for b, v in st.items():
                    w *= self.be_p[b] if v else 1.0 - self.be_p[b]
                total += w
        return total

    def mcs(self, f):
        """Minimal cut sets of monotone f: minimal true subsets."""
        sup = sorted(self.support(f, set()))
        out = set()
        # mask 0 included: a tautological f (e.g. a true house event in an
        # OR) has the EMPTY set as its one minimal cut set.
        for mask in range(0, 1 << len(sup)):
            s = {sup[i] for i in range(len(sup)) if mask >> i & 1}
            st = {b: (b in s) for b in sup}
            if not self.ev(f, st):
                continue
            minimal = True
            for x in s:
                st[x] = False
                if self.ev(f, st):
                    minimal = False
                st[x] = True
                if not minimal:
                    break
            if minimal:
                out.add(frozenset(s))
        return out


# --------------------------------------------------------------------------
# one case
# --------------------------------------------------------------------------
def run_case(rng, engine, keep_dir):
    m = gen_model(rng)
    d = tempfile.mkdtemp(prefix="psa-prop-")
    problems = []
    try:
        write_model(m, d)
        o = Oracle(m)

        # 0) toolchain agreement: validator accepts the generated model
        v = subprocess.run(
            [sys.executable, "ci/validate.py", d,
             "schema/psa-model.schema.json"],
            capture_output=True, text=True)
        if v.returncode != 0:
            problems.append("validate.py rejected the model:\n"
                            + v.stdout + v.stderr)

        run = lambda tgt: json.loads(subprocess.run(
            [engine, d, tgt, "--json", "--mcs-limit", "100000"],
            capture_output=True, text=True, check=True).stdout)

        # 1) fault tree
        ft = run("FT-TEST")
        top = m["top"]
        p_oracle = o.prob(lambda st: o.ev(top, st), o.support(top, set()))
        if not close(ft["probability"], p_oracle):
            problems.append(
                f"P(top): engine {ft['probability']} oracle {p_oracle}")
        noncoh = o.uses_negation(top)
        if ft["coherent"] == noncoh:
            problems.append(f"coherence flag: engine {ft['coherent']}, "
                            f"oracle expects {not noncoh}")
        if not noncoh:
            eng = {frozenset(c["events"]): c["probability"]
                   for c in ft["minimal_cut_sets"]}
            ora = o.mcs(top)
            if set(eng) != ora:
                problems.append(
                    f"MCS mismatch: engine {len(eng)} oracle {len(ora)}; "
                    f"only-engine {list(set(eng)-ora)[:3]}, "
                    f"only-oracle {list(ora-set(eng))[:3]}")
            else:
                for s, pe in eng.items():
                    po = math.prod(o.be_p[b] for b in s)
                    if not close(pe, po):
                        problems.append(f"cut prob {sorted(s)}: "
                                        f"engine {pe} oracle {po}")
        # Birnbaum spot checks
        for b in random.Random(0).sample(
                [x["event"] for x in ft["birnbaum"]],
                min(2, len(ft["birnbaum"]))):
            sup = o.support(top, set()) | {b}
            p1 = o.prob(lambda st: o.ev(top, {**st, b: True}), sup - {b})
            p0 = o.prob(lambda st: o.ev(top, {**st, b: False}), sup - {b})
            be_eng = next(x["importance"] for x in ft["birnbaum"]
                          if x["event"] == b)
            if not close(be_eng, p1 - p0):
                problems.append(f"Birnbaum {b}: engine {be_eng} "
                                f"oracle {p1-p0}")

        # 2) event tree
        et = run("ET-TEST")
        total_p = 0.0
        sup_all = set()
        for fe, t in m["fes"].items():
            o.support(t, sup_all)
        for s in et["sequences"]:
            seq = m["sequences"][s["id"]]
            def match(st, seq=seq):
                for fe, out in seq["path"].items():
                    if out == "bypassed":
                        continue
                    failed = o.ev(m["fes"][fe], st)
                    if (out == "failure") != failed:
                        return False
                return True
            p_seq = o.prob(match, sup_all)
            total_p += p_seq
            if not close(s["frequency_per_year"], m["ie_freq"] * p_seq):
                problems.append(f"{s['id']}: engine "
                                f"{s['frequency_per_year']} oracle "
                                f"{m['ie_freq']*p_seq}")
            # sequence cut sets (failure logic, delete-term)
            fails = [m["fes"][fe] for fe, out in seq["path"].items()
                     if out == "failure"]
            seq_noncoh = any(o.uses_negation(m["fes"][fe])
                             for fe, out in seq["path"].items()
                             if out != "bypassed")
            if seq["end_state"] != "OK" and fails:
                if seq_noncoh:
                    if s["cut_sets"]:
                        problems.append(f"{s['id']}: cut sets emitted for "
                                        f"non-coherent sequence logic")
                else:
                    conj = {"and": fails} if len(fails) > 1 else fails[0]
                    ora = o.mcs(conj)
                    eng = {frozenset(c["events"]) for c in s["cut_sets"]}
                    if eng != ora:
                        problems.append(
                            f"{s['id']} cut sets: engine {len(eng)} "
                            f"oracle {len(ora)}")
        if abs(total_p - 1.0) > 1e-9:
            problems.append(f"partition: sum P(seq) = {total_p}")
        cdf_eng = next(x["value_per_year"] for x in et["metrics"]
                       if x["id"] == "CDF")
        cdf_ora = sum(s["frequency_per_year"] for s in et["sequences"]
                      if s["end_state"] == "CD")
        if not close(cdf_eng, cdf_ora):
            problems.append(f"CDF aggregation: {cdf_eng} vs {cdf_ora}")

    except subprocess.CalledProcessError as e:
        problems.append(f"engine failed:\n{e.stderr}")
    finally:
        if problems and keep_dir:
            dst = keep_dir
            shutil.copytree(d, dst, dirs_exist_ok=True)
        shutil.rmtree(d, ignore_errors=True)
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=40)
    ap.add_argument("--seed", type=int, default=20260708)
    ap.add_argument("--engine",
                    default=os.environ.get(
                        "CANOPY_BIN", "engine/target/release/canopy"))
    a = ap.parse_args()

    failures = 0
    for i in range(a.cases):
        rng = random.Random(a.seed * 1_000_003 + i)
        keep = f"property-failure-seed{a.seed}-case{i}"
        problems = run_case(rng, a.engine, keep)
        if problems:
            failures += 1
            print(f"CASE {i}: FAIL (model preserved in {keep}/)")
            for p in problems:
                print("   ", p)
        else:
            print(f"CASE {i}: ok")
    print(f"\n{a.cases - failures}/{a.cases} cases passed "
          f"(seed {a.seed})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
