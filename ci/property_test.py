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
  * uncertainty (Monte Carlo, docs/quantification.md): the same logic is
    re-issued with random distributions — shared parameters (state-of-
    knowledge correlation), inline and event-level distributions, a random
    CCF total, a random initiator frequency — and the engine's sampled mean
    of P(top), of every sequence frequency and of CDF must equal the EXACT
    expectation within 6 standard errors. The oracle computes that
    expectation independently: each basic-event probability is c·X for at
    most one random quantity X, so every truth-table state's weight is a
    polynomial in each X whose expectation follows from the closed-form raw
    moments E[X^j] of the lognormal/beta/gamma/uniform distributions.
    Also: the validator accepts the variant, no probability was clamped,
    per-iteration partition (sum of sequence draws = initiator draw), CDF
    draws = sum of CD-sequence draws, and a rerun is byte-identical.

Usage: property_test.py [--cases N] [--seed S] [--engine PATH]
                        [--mc-samples N]
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
# uncertainty variant: generator and exact-expectation oracle
# --------------------------------------------------------------------------
Z95 = 1.6448536269514722
MC_Z = 6.0          # tolerance in standard errors of the Monte Carlo mean


def rand_dist(r: random.Random):
    """(point value = mean, uncertainty block). Ranges keep P(X > 1)
    negligible so the engine never clamps (asserted), which keeps the
    expectation exact, and keep high moments free of cancellation."""
    kind = r.choice(["lognormal", "beta", "gamma", "uniform"])
    if kind == "lognormal":
        return (10 ** r.uniform(-5, -3),
                {"distribution": "lognormal",
                 "error_factor": r.choice([2.0, 3.0, 5.0])})
    if kind == "beta":
        mean = 10 ** r.uniform(-4, -1)
        a = r.uniform(0.5, 5.0)
        b = a * (1 - mean) / mean
        return a / (a + b), {"distribution": "beta", "alpha": a, "beta": b}
    if kind == "gamma":
        k = r.uniform(0.5, 5.0)
        th = 10 ** r.uniform(-5, -2) / k
        return k * th, {"distribution": "gamma", "shape": k, "scale": th}
    lo = 10 ** r.uniform(-5, -2)
    hi = lo * r.uniform(1.5, 10.0)
    return 0.5 * (lo + hi), {"distribution": "uniform", "lower": lo, "upper": hi}


def raw_moments(unc: dict, mean: float, jmax: int) -> list[float]:
    """[E[X^0], ..., E[X^jmax]] in closed form."""
    d = unc["distribution"]
    if d == "lognormal":
        sg = math.log(unc["error_factor"]) / Z95
        mu = math.log(mean) - sg * sg / 2
        return [math.exp(j * mu + j * j * sg * sg / 2) for j in range(jmax + 1)]
    out = [1.0]
    for j in range(1, jmax + 1):
        if d == "beta":
            a, b = unc["alpha"], unc["beta"]
            out.append(out[-1] * (a + j - 1) / (a + b + j - 1))
        elif d == "gamma":
            out.append(out[-1] * unc["scale"] * (unc["shape"] + j - 1))
        else:
            lo, hi = unc["lower"], unc["upper"]
            out.append((hi ** (j + 1) - lo ** (j + 1)) / ((j + 1) * (hi - lo)))
    return out


def gen_uncertainty(m, r: random.Random):
    """Random distributions over the case's existing logic."""
    params = {f"PAR-U{k+1}": rand_dist(r) for k in range(r.randint(1, 3))}
    members = set(m["ccf"]["members"]) if m["ccf"] else set()
    be = {}
    for b, p in m["bes"].items():
        x = r.random()
        if x < 0.45:
            be[b] = ("param", r.choice(sorted(params)))
        elif x < 0.65:
            be[b] = ("inline",) + rand_dist(r)
        elif x < 0.8 and b not in members:   # event-level: refused on members
            be[b] = ("event",) + rand_dist(r)
        else:
            be[b] = ("const", p)
    qt = None
    if m["ccf"]:
        x = r.random()
        qt = (("param", r.choice(sorted(params))) if x < 0.5 else
              ("inline",) + rand_dist(r) if x < 0.8 else ("const", m["ccf"]["qt"]))
    ie = (("inline", m["ie_freq"], {"distribution": "lognormal",
                                     "error_factor": r.choice([3.0, 6.0])})
          if r.random() < 0.5 else ("const", m["ie_freq"]))
    return dict(params=params, be=be, qt=qt, ie=ie)


def write_uncertain_model(m, u, d):
    write_model(m, d)
    prov = {"source": "property-test generator",
            "justification": "randomized uncertainty case"}
    dump = lambda p, o: open(p, "w").write(
        yaml.safe_dump(o, sort_keys=True, default_flow_style=False))
    dump(f"{d}/parameters.yaml", {"parameters": {
        pid: {"label": "generated parameter", "value": mean,
              "unit": "per_demand", "uncertainty": unc, "provenance": prov}
        for pid, (mean, unc) in u["params"].items()}})

    def qty(spec):
        if spec[0] == "param":
            return {"param": spec[1]}
        if spec[0] == "inline":
            return {"value": spec[1], "unit": "per_demand",
                    "uncertainty": spec[2]}
        return {"value": spec[1], "unit": "per_demand"}

    bes = {}
    for b, spec in u["be"].items():
        e = {"label": f"generated event {b}", "provenance": prov}
        if spec[0] == "event":
            e["failure_model"] = {"type": "probability",
                                  "value": {"value": spec[1],
                                            "unit": "per_demand"}}
            e["uncertainty"] = spec[2]
        else:
            e["failure_model"] = {"type": "probability", "value": qty(spec)}
        bes[b] = e
    dump(f"{d}/basic-events/gen.yaml", {"basic_events": bes})
    if m["ccf"]:
        path = f"{d}/ccf-groups.yaml"
        c = yaml.safe_load(open(path))
        c["ccf_groups"]["CCF-G1"]["total_probability"] = qty(u["qt"])
        dump(path, c)
    path = f"{d}/event-trees/gen.yaml"
    et = yaml.safe_load(open(path))
    f = {"value": u["ie"][1], "unit": "per_year"}
    if u["ie"][0] == "inline":
        f["uncertainty"] = u["ie"][2]
    et["event_tree"]["initiating_event"]["frequency"] = f
    dump(path, et)


class UncertaintyOracle:
    """Exact E[P(pred)] for basic-event probabilities p_i = c_i X_{v(i)}
    (or constant), independent random quantities X."""

    def __init__(self, m, u, base: "Oracle"):
        self.o = base
        self.var: dict[str, str] = {}      # BE -> random quantity key
        self.coef: dict[str, float] = {}   # BE -> c
        self.const: dict[str, float] = {}  # BE -> fixed probability
        self.dist: dict[str, tuple] = {}   # key -> (mean, unc)
        for pid, (mean, unc) in u["params"].items():
            self.dist[pid] = (mean, unc)

        def bind(b, spec, c=1.0):
            if spec[0] == "param":
                self.var[b], self.coef[b] = spec[1], c
            elif spec[0] in ("inline", "event"):
                key = f"{b}/value" if spec[0] == "inline" else b
                self.dist[key] = (spec[1], spec[2])
                self.var[b], self.coef[b] = key, c
            else:
                self.const[b] = c * spec[1]

        for b, spec in u["be"].items():
            bind(b, spec)
        if m["ccf"]:
            cc = m["ccf"]
            n, al = len(cc["members"]), cc["alphas"]
            if cc["testing"] == "staggered":
                ck = [al[k-1] / comb(n-1, k-1) for k in range(1, n+1)]
            else:
                at = sum((i+1) * a for i, a in enumerate(al))
                ck = [k * al[k-1] / (at * comb(n-1, k-1)) for k in range(1, n+1)]
            qt = u["qt"]
            if qt[0] == "inline":
                qt = ("inline_ccf", qt[1], qt[2])
                self.dist["CCF-G1/total_probability"] = (qt[1], qt[2])
            for mem in cc["members"]:
                self.var.pop(mem, None); self.coef.pop(mem, None)
                self.const.pop(mem, None)
            cids = [b for b in base.be_p if b.startswith("BE-CCF-G1-")]
            for b in list(cc["members"]) + cids:
                k = 1 if b in cc["members"] else len(b.split("-")) - 3
                if qt[0] == "param":
                    self.var[b], self.coef[b] = qt[1], ck[k-1]
                elif qt[0] == "inline_ccf":
                    self.var[b], self.coef[b] = "CCF-G1/total_probability", ck[k-1]
                else:
                    self.const[b] = ck[k-1] * qt[1]
        n_ev = len(base.be_p)
        self.mom = {key: raw_moments(unc, mean, n_ev + 1)
                    for key, (mean, unc) in self.dist.items()}

    def expect(self, pred, sup) -> float:
        sup = sorted(sup)
        total = 0.0
        for bits in itertools.product([False, True], repeat=len(sup)):
            st = dict(zip(sup, bits))
            if not pred(st):
                continue
            w, polys = 1.0, {}
            for b, v in st.items():
                if b in self.var:
                    c = self.coef[b]
                    term = (0.0, c) if v else (1.0, -c)
                    old = polys.get(self.var[b], [1.0])
                    new = [0.0] * (len(old) + 1)
                    for i, a in enumerate(old):
                        new[i] += a * term[0]
                        new[i + 1] += a * term[1]
                    polys[self.var[b]] = new
                else:
                    p = self.const[b]
                    w *= p if v else 1.0 - p
            for key, poly in polys.items():
                mo = self.mom[key]
                w *= sum(a * mo[j] for j, a in enumerate(poly))
            total += w
        return total


def mc_close(u_json: dict, exact: float) -> bool:
    """Sampled mean within MC_Z standard errors of the exact expectation
    (plus a float floor for quantities with no randomness)."""
    return (abs(u_json["mean"] - exact)
            <= MC_Z * u_json["std_error_of_mean"] + 1e-12 * abs(exact) + 1e-300)


def run_uncertainty_stage(m, o, urng, engine, mc_samples, problems, keep_dir):
    u = gen_uncertainty(m, urng)
    uo = UncertaintyOracle(m, u, o)
    d = tempfile.mkdtemp(prefix="psa-prop-unc-")
    try:
        write_uncertain_model(m, u, d)
        v = subprocess.run([sys.executable, "ci/validate.py", d,
                            "schema/psa-model.schema.json"],
                           capture_output=True, text=True)
        if v.returncode != 0:
            problems.append("validate.py rejected the uncertainty variant:\n"
                            + v.stdout + v.stderr)
        mc = ["--json", "--samples", str(mc_samples), "--seed", "20260708"]
        run = lambda *a: subprocess.run([engine, d, *a], capture_output=True,
                                        text=True, check=True).stdout
        ft = json.loads(run("FT-TEST", "--prob-only", *mc))
        top = m["top"]
        e_top = uo.expect(lambda st: o.ev(top, st), o.support(top, set()))
        if ft["uncertainty"]["clamped_probabilities"]:
            problems.append("MC: probabilities clamped in a case built to "
                            "avoid it")
        if not mc_close(ft["uncertainty"], e_top):
            problems.append(f"MC E[P(top)]: engine mean "
                            f"{ft['uncertainty']['mean']} ± "
                            f"{ft['uncertainty']['std_error_of_mean']} (1 s.e.) "
                            f"vs exact {e_top}")
        raw = run("ET-TEST", *mc, "--keep-samples")
        if raw != run("ET-TEST", *mc, "--keep-samples"):
            problems.append("MC: rerun with the same seed not byte-identical")
        et = json.loads(raw)
        ie_mean = u["ie"][1]
        sup_all = set()
        for t in m["fes"].values():
            o.support(t, sup_all)
        e_cdf = 0.0
        for s in et["sequences"]:
            seq = m["sequences"][s["id"]]
            def match(st, seq=seq):
                for fe, out in seq["path"].items():
                    if out == "bypassed":
                        continue
                    if (out == "failure") != o.ev(m["fes"][fe], st):
                        return False
                return True
            e_seq = ie_mean * uo.expect(match, sup_all)
            if seq["end_state"] == "CD":
                e_cdf += e_seq
            if not mc_close(s["uncertainty"], e_seq):
                problems.append(f"MC E[{s['id']}]: engine "
                                f"{s['uncertainty']['mean']} ± "
                                f"{s['uncertainty']['std_error_of_mean']} vs "
                                f"exact {e_seq}")
        cdf = next(x for x in et["metrics"] if x["id"] == "CDF")["uncertainty"]
        if not mc_close(cdf, e_cdf):
            problems.append(f"MC E[CDF]: engine {cdf['mean']} ± "
                            f"{cdf['std_error_of_mean']} vs exact {e_cdf}")
        ied = et["uncertainty"]["initiating_event_draws"]
        worst = max(abs(sum(s["uncertainty"]["draws"][i]
                            for s in et["sequences"]) / ied[i] - 1.0)
                    for i in range(len(ied)))
        if worst > 1e-9:
            problems.append(f"MC partition: worst |sum/f_IE - 1| = {worst}")
        cd = [s["uncertainty"]["draws"] for s in et["sequences"]
              if s["end_state"] == "CD"]
        def fold(xs):            # engine sums left to right; Python >= 3.12
            acc = 0.0            # sum() of floats is compensated instead
            for x in xs:
                acc += x
            return acc
        if [fold(x) for x in zip(*cd)] != cdf["draws"]:
            problems.append("MC: CDF draws are not the sum of CD-sequence draws")
    except subprocess.CalledProcessError as e:
        problems.append(f"engine failed on the uncertainty variant:\n{e.stderr}")
    finally:
        if problems and keep_dir:
            shutil.copytree(d, keep_dir + "-uncertainty", dirs_exist_ok=True)
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# consequence-level importance oracle
# --------------------------------------------------------------------------
def oracle_consequence_importance(m, o, sup_all):
    """Per end state: exact F and, per basic event x, F(x=1) and F(x=0),
    by one truth-table pass. Each state is assigned to the ONE sequence
    whose path it satisfies (partition, checked separately); its weight
    with x's own factor removed is the probability of the other events,
    which is exactly the weight of that state in the cofactor on x."""
    sup = sorted(sup_all)
    ie = m["ie_freq"]
    F, F1, F0 = {}, {}, {}
    for bits in itertools.product([False, True], repeat=len(sup)):
        st = dict(zip(sup, bits))
        failed = {fe: o.ev(t, st) for fe, t in m["fes"].items()}
        es = None
        for seq in m["sequences"].values():
            if all(out == "bypassed" or (out == "failure") == failed[fe]
                   for fe, out in seq["path"].items()):
                es = seq["end_state"]
                break
        if es is None:
            continue
        w = 1.0
        for b, v in st.items():
            w *= o.be_p[b] if v else 1.0 - o.be_p[b]
        F[es] = F.get(es, 0.0) + ie * w
        c1, c0 = F1.setdefault(es, {}), F0.setdefault(es, {})
        for b, v in st.items():
            if v:
                c1[b] = c1.get(b, 0.0) + ie * w / o.be_p[b]
            else:
                c0[b] = c0.get(b, 0.0) + ie * w / (1.0 - o.be_p[b])
    return F, F1, F0


def check_consequence_importance(m, o, et, sup_all, problems):
    """Engine's per-end-state and CDF importance vs the oracle: conditional
    frequencies to REL_TOL, the derived measures recomputed from the
    oracle's frequencies, and events the engine omits (no BDD dependence)
    must be irrelevant in the oracle too."""
    if "end_states" not in et:
        problems.append("importance: event tree JSON has no end_states")
        return
    F, F1, F0 = oracle_consequence_importance(m, o, sup_all)
    tol_abs = lambda a, b, scale: abs(a - b) <= 1e-9 * scale + 1e-300
    for es in et["end_states"]:
        sid = es["id"]
        f = F.get(sid, 0.0)
        if not close(es["frequency_per_year"], f):
            problems.append(f"importance {sid}: F engine "
                            f"{es['frequency_per_year']} oracle {f}")
        rows = {r["event"]: r for r in es["importance"]}
        extra = set(rows) - sup_all
        if extra:
            problems.append(f"importance {sid}: events outside the tree's "
                            f"support {sorted(extra)}")
        for b in sorted(sup_all):
            f1 = F1.get(sid, {}).get(b, 0.0)
            f0 = F0.get(sid, {}).get(b, 0.0)
            scale = max(f, f1, f0)
            if b not in rows:
                if not (tol_abs(f1, f, scale) and tol_abs(f0, f, scale)):
                    problems.append(f"importance {sid}/{b}: engine omits an "
                                    f"event the oracle finds relevant "
                                    f"(F1 {f1}, F0 {f0}, F {f})")
                continue
            r = rows[b]
            if not (close(r["frequency_if_true_per_year"], f1)
                    and close(r["frequency_if_false_per_year"], f0)):
                problems.append(
                    f"importance {sid}/{b}: engine F1/F0 "
                    f"{r['frequency_if_true_per_year']}/"
                    f"{r['frequency_if_false_per_year']} oracle {f1}/{f0}")
                continue
            if not tol_abs(r["birnbaum_per_year"], f1 - f0, scale):
                problems.append(f"importance {sid}/{b}: Birnbaum engine "
                                f"{r['birnbaum_per_year']} oracle {f1 - f0}")
            if f > 0:
                fv, raw = (f - f0) / f, f1 / f
                # FV = 1 − F0/F: its absolute error scales with F0/F.
                if (r["fussell_vesely"] is None
                        or abs(r["fussell_vesely"] - fv) > 1e-9 * max(1, f0 / f)):
                    problems.append(f"importance {sid}/{b}: FV engine "
                                    f"{r['fussell_vesely']} oracle {fv}")
                if r["raw"] is None or not close(r["raw"], raw):
                    problems.append(f"importance {sid}/{b}: RAW engine "
                                    f"{r['raw']} oracle {raw}")
            elif r["fussell_vesely"] is not None or r["raw"] is not None:
                problems.append(f"importance {sid}/{b}: F = 0 but FV/RAW "
                                f"reported")
            if f0 > 1e-300 and (r["rrw"] is None or not close(r["rrw"], f / f0)):
                problems.append(f"importance {sid}/{b}: RRW engine "
                                f"{r['rrw']} oracle {f / f0}")
            if not close(r["probability"], o.be_p[b]):
                problems.append(f"importance {sid}/{b}: probability "
                                f"{r['probability']} vs {o.be_p[b]}")
    # CDF groups exactly the CD sequences: identical rows, bit for bit.
    cdf = next(x for x in et["metrics"] if x["id"] == "CDF")
    cd = next((es for es in et["end_states"] if es["id"] == "CD"), None)
    if cd is None or cdf.get("importance") != cd["importance"]:
        problems.append("importance: CDF rows differ from the CD end state's")


# --------------------------------------------------------------------------
# one case
# --------------------------------------------------------------------------
def run_case(rng, engine, keep_dir, urng=None, mc_samples=0):
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

        # consequence-level importance (exact conditional frequencies)
        check_consequence_importance(m, o, et, sup_all, problems)

        # 3) uncertainty propagation on the same logic
        if urng is not None and mc_samples:
            run_uncertainty_stage(m, o, urng, engine, mc_samples, problems,
                                  keep_dir)

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
    ap.add_argument("--mc-samples", type=int, default=20000,
                    help="Monte Carlo samples for the uncertainty stage "
                         "(0 disables it)")
    a = ap.parse_args()

    failures = 0
    for i in range(a.cases):
        rng = random.Random(a.seed * 1_000_003 + i)
        # A separate stream for the uncertainty variant: the logic of case
        # i is unchanged from earlier harness versions.
        urng = random.Random((a.seed * 1_000_003 + i) ^ 0x5EED_5EED)
        keep = f"property-failure-seed{a.seed}-case{i}"
        problems = run_case(rng, a.engine, keep, urng, a.mc_samples)
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
