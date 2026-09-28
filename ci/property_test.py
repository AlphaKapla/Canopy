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
import re
import shutil
import subprocess
import sys
import tempfile
from math import comb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_ccf_uncertainty import coeff_moment  # noqa: E402  (Dirichlet moments)

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
                     "house_events": ["house-events.yaml"],
                     **({"ccf_groups": ["ccf-groups.yaml"]} if m["ccf"] else {})}})
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

    def ev(self, f, st, houses=None):
        """Truth value of formula f in basic-event state st; `houses`
        (default: the model's defaults) gives the house-event values."""
        h = self.houses if houses is None else houses
        if isinstance(f, str):
            if f.startswith("BE-"):
                return st[f]
            if f.startswith("HE-"):
                return h[f]
            return self.ev(self.gates[f], st, h)
        (op, a), = f.items()
        if op == "and":
            return all(self.ev(x, st, h) for x in a)
        if op == "or":
            return any(self.ev(x, st, h) for x in a)
        if op == "xor":
            return sum(self.ev(x, st, h) for x in a) % 2 == 1
        if op == "not":
            return not self.ev(a, st, h)
        return sum(self.ev(x, st, h) for x in a["of"]) >= a["k"]

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

    def primes(self, f, max_support=12):
        """Prime implicants of f by Quine–McCluskey from its true minterms
        (independent of the engine's recursion): terms are tuples over the
        sorted support with 1 (event), 0 (negated), 2 (absent); two terms
        that differ in one fixed position merge; terms never merged are
        prime. Returns {(frozenset(events), frozenset(negated))}, or None
        when the support exceeds `max_support` (not checked)."""
        sup = sorted(self.support(f, set()))
        n = len(sup)
        if n > max_support:
            return None
        current = set()
        for bits in itertools.product([0, 1], repeat=n):
            if self.ev(f, {b: bool(v) for b, v in zip(sup, bits)}):
                current.add(bits)
        primes = set()
        while current:
            merged, used = set(), set()
            for t in current:
                for i, v in enumerate(t):
                    if v == 2:
                        continue
                    u = t[:i] + (1 - v,) + t[i + 1:]
                    if u in current:
                        merged.add(t[:i] + (2,) + t[i + 1:])
                        used.add(t)
                        used.add(u)
            primes |= current - used
            current = merged
        return {(frozenset(sup[i] for i, v in enumerate(t) if v == 1),
                 frozenset(sup[i] for i, v in enumerate(t) if v == 0))
                for t in primes}

    def mcs(self, f):
        """Minimal cut sets of monotone f: minimal true subsets."""
        return self.mcs_pred(lambda st: self.ev(f, st), self.support(f, set()))

    def mcs_pred(self, pred, sup):
        """Minimal true subsets of a monotone predicate over `sup`."""
        sup = sorted(sup)
        out = set()
        # mask 0 included: a tautological f (e.g. a true house event in an
        # OR) has the EMPTY set as its one minimal cut set.
        for mask in range(0, 1 << len(sup)):
            s = {sup[i] for i in range(len(sup)) if mask >> i & 1}
            st = {b: (b in s) for b in sup}
            if not pred(st):
                continue
            minimal = True
            for x in s:
                st[x] = False
                if pred(st):
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
    # drawn last, so the earlier draws (and the variants of groups without
    # factor uncertainty) are those of previous harness versions
    factor = None
    if m["ccf"] and r.random() < 0.6:
        factor = round(r.uniform(2.0, 60.0), 6)     # Dirichlet concentration
    return dict(params=params, be=be, qt=qt, ie=ie, factor=factor)


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
        if u.get("factor"):
            c["ccf_groups"]["CCF-G1"]["factor_uncertainty"] = {
                "distribution": "dirichlet", "concentration": u["factor"]}
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
    (or constant), independent random quantities X. With uncertain CCF
    factors (FR-36) the group's events are p = c_k · Q_t with a random,
    Dirichlet-driven coefficient vector c shared by the group: those
    factors are expanded jointly as a polynomial in (Q_t, c_1..c_n), whose
    Q_t part is merged with Q_t's other uses, and E[Π c_k^m_k] comes from
    exact Dirichlet moments (staggered) or the one-dimensional integral
    (non-staggered) in ci/test_ccf_uncertainty.py."""

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
            self.grp = {}                      # BE -> multiplicity k
            if u.get("factor"):
                s_al = sum(al)
                self.grp_a = [u["factor"] * x / s_al for x in al]
                self.grp_scheme = cc["testing"]
                self.grp_qt = (qt[1] if qt[0] == "param" else
                               "CCF-G1/total_probability" if qt[0] == "inline_ccf"
                               else None)
                self.grp_qt_const = qt[1] if qt[0] == "const" else None
                self.grp_n = n
                self.grp_memo = {}
                self.grp_cache = {}
            for b in list(cc["members"]) + cids:
                k = 1 if b in cc["members"] else len(b.split("-")) - 3
                if u.get("factor"):
                    self.grp[b] = k
                    continue
                if qt[0] == "param":
                    self.var[b], self.coef[b] = qt[1], ck[k-1]
                elif qt[0] == "inline_ccf":
                    self.var[b], self.coef[b] = "CCF-G1/total_probability", ck[k-1]
                else:
                    self.const[b] = ck[k-1] * qt[1]
        if not m["ccf"] or not u.get("factor"):
            self.grp = {}
        n_ev = len(base.be_p)
        self.mom = {key: raw_moments(unc, mean, n_ev + 1)
                    for key, (mean, unc) in self.dist.items()}

    def group_term(self, nf, ns, qpoly):
        """E over (Q_t, c) of Π_k (c_k·Q_t)^nf_k (1 − c_k·Q_t)^ns_k — the
        group's events, nf_k failed and ns_k working of multiplicity k —
        times Q_t's polynomial `qpoly` from its other uses. Expanded as
        Σ over i_k ≤ ns_k of Π_k C(ns_k, i_k) (−1)^i_k (c_k Q_t)^(nf_k + i_k).
        Memoized: it depends on nothing else."""
        key = (nf, ns, qpoly)
        if key in self.grp_cache:
            return self.grp_cache[key]
        joint = {}
        for idx in itertools.product(*(range(x + 1) for x in ns)):
            c = 1.0
            for x, i in zip(ns, idx):
                c *= comb(x, i) * (-1) ** i
            e = tuple(f + i for f, i in zip(nf, idx))
            joint[e] = joint.get(e, 0.0) + c
        acc = 0.0
        for e, c in joint.items():
            j = sum(e)     # the Q_t degree of a term is its total c degree
            dm = coeff_moment(self.grp_a, list(e), self.grp_scheme, self.grp_memo)
            if self.grp_qt is None:
                acc += c * self.grp_qt_const ** j * dm
            else:
                mo = self.mom[self.grp_qt]
                acc += c * dm * sum(a * mo[i + j] for i, a in enumerate(qpoly))
        self.grp_cache[key] = acc
        return acc

    def expect(self, pred, sup, fixed=None) -> float:
        """E[P(pred)]; with fixed=(x, val), E[P(pred | x = val)]: x is held
        at val and its own probability factor is left out."""
        sup = sorted(sup)
        total = 0.0
        for bits in itertools.product([False, True], repeat=len(sup)):
            st = dict(zip(sup, bits))
            if fixed is not None and st.get(fixed[0], fixed[1]) != fixed[1]:
                continue
            if not pred(st):
                continue
            w, polys = 1.0, {}
            if self.grp:
                nf = [0] * self.grp_n       # group events failed / working,
                ns = [0] * self.grp_n       # per multiplicity
            for b, v in st.items():
                if fixed is not None and b == fixed[0]:
                    continue
                if b in self.grp:
                    if v:
                        nf[self.grp[b] - 1] += 1
                    else:
                        ns[self.grp[b] - 1] += 1
                    continue
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
            if self.grp:
                qpoly = polys.pop(self.grp_qt, [1.0]) if self.grp_qt else [1.0]
                w *= self.group_term(tuple(nf), tuple(ns), tuple(qpoly))
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


# coverage of uncertain CCF factors in the uncertainty stage (FR-36)
FACTOR_STATS = {"cases": 0, "staggered": 0, "non-staggered": 0, "sizes": {}}


def run_uncertainty_stage(m, o, urng, engine, mc_samples, problems, keep_dir):
    u = gen_uncertainty(m, urng)
    if u.get("factor"):
        FACTOR_STATS["cases"] += 1
        FACTOR_STATS[m["ccf"]["testing"]] += 1
        n = len(m["ccf"]["members"])
        FACTOR_STATS["sizes"][n] = FACTOR_STATS["sizes"].get(n, 0) + 1
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

        # Importance under uncertainty: the per-iteration conditional
        # frequencies' means must equal their exact expectations
        # E[f_IE] · E[P(CD | x = v)] for every listed event.
        eti = json.loads(run("ET-TEST", *mc, "--importance-uncertainty", "100"))
        cd_seqs = [m["sequences"][s["id"]] for s in eti["sequences"]
                   if s["end_state"] == "CD"]
        def in_cd(st):
            return any(all(out == "bypassed"
                           or (out == "failure") == o.ev(m["fes"][fe], st)
                           for fe, out in q["path"].items()) for q in cd_seqs)
        cdfi = next(x for x in eti["metrics"] if x["id"] == "CDF")
        n_rows = 0
        for r in cdfi.get("importance", []):
            ru = r.get("uncertainty")
            if ru is None:
                problems.append(f"importance uncertainty: {r['event']} has none")
                continue
            n_rows += 1
            for val, key in ((True, "frequency_if_true_per_year"),
                             (False, "frequency_if_false_per_year")):
                exact = ie_mean * uo.expect(in_cd, sup_all | {r["event"]},
                                            fixed=(r["event"], val))
                if not mc_close(ru[key], exact):
                    problems.append(f"importance uncertainty {r['event']} {key}: "
                                    f"engine {ru[key]['mean']} ± "
                                    f"{ru[key]['std_error_of_mean']} vs exact {exact}")
        if cdfi.get("importance_uncertainty_events") != n_rows:
            problems.append("importance uncertainty: event count mismatch")

        # Latin hypercube sampling: same exact expectations (LHS is
        # unbiased; the reported SRS standard error bounds its error up to
        # a factor N/(N-1)), same per-iteration partition.
        lhs = [*mc, "--sampling", "lhs"]
        ftl = json.loads(run("FT-TEST", "--prob-only", *lhs))
        if ftl["uncertainty"]["method"] != "lhs" or not mc_close(ftl["uncertainty"], e_top):
            problems.append(f"LHS E[P(top)]: engine {ftl['uncertainty']['mean']} ± "
                            f"{ftl['uncertainty']['std_error_of_mean']} vs exact {e_top}")
        etl = json.loads(run("ET-TEST", *lhs, "--keep-samples"))
        e_cdf_l = 0.0
        for s in etl["sequences"]:
            seq = m["sequences"][s["id"]]
            def match_l(st, seq=seq):
                for fe, out in seq["path"].items():
                    if out != "bypassed" and (out == "failure") != o.ev(m["fes"][fe], st):
                        return False
                return True
            e_seq = ie_mean * uo.expect(match_l, sup_all)
            if seq["end_state"] == "CD":
                e_cdf_l += e_seq
            if not mc_close(s["uncertainty"], e_seq):
                problems.append(f"LHS E[{s['id']}]: engine {s['uncertainty']['mean']} "
                                f"± {s['uncertainty']['std_error_of_mean']} vs exact {e_seq}")
        cdfl = next(x for x in etl["metrics"] if x["id"] == "CDF")["uncertainty"]
        if not mc_close(cdfl, e_cdf_l):
            problems.append(f"LHS E[CDF]: engine {cdfl['mean']} vs exact {e_cdf_l}")
        iedl = etl["uncertainty"]["initiating_event_draws"]
        worst = max(abs(sum(s["uncertainty"]["draws"][i] for s in etl["sequences"])
                        / iedl[i] - 1.0) for i in range(len(iedl)))
        if worst > 1e-9:
            problems.append(f"LHS partition: worst |sum/f_IE - 1| = {worst}")
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


def compare_importance_group(es, f, F1, F0, sup_all, o, problems, tag=""):
    """One engine end-state group `es` against the oracle's F and per-event
    F(x=1) (F1) and F(x=0) (F0): conditional frequencies to REL_TOL, the
    derived measures recomputed from the oracle's frequencies, and events
    the engine omits (no BDD dependence) irrelevant in the oracle too."""
    tol_abs = lambda a, b, scale: abs(a - b) <= 1e-9 * scale + 1e-300
    sid = es["id"]
    if not close(es["frequency_per_year"], f):
        problems.append(f"{tag}importance {sid}: F engine "
                        f"{es['frequency_per_year']} oracle {f}")
    rows = {r["event"]: r for r in es["importance"]}
    extra = set(rows) - sup_all
    if extra:
        problems.append(f"{tag}importance {sid}: events outside the tree's "
                        f"support {sorted(extra)}")
    for b in sorted(sup_all):
        f1 = F1.get(b, 0.0)
        f0 = F0.get(b, 0.0)
        scale = max(f, f1, f0)
        if b not in rows:
            if not (tol_abs(f1, f, scale) and tol_abs(f0, f, scale)):
                problems.append(f"{tag}importance {sid}/{b}: engine omits an "
                                f"event the oracle finds relevant "
                                f"(F1 {f1}, F0 {f0}, F {f})")
            continue
        r = rows[b]
        if not (close(r["frequency_if_true_per_year"], f1)
                and close(r["frequency_if_false_per_year"], f0)):
            problems.append(
                f"{tag}importance {sid}/{b}: engine F1/F0 "
                f"{r['frequency_if_true_per_year']}/"
                f"{r['frequency_if_false_per_year']} oracle {f1}/{f0}")
            continue
        if not tol_abs(r["birnbaum_per_year"], f1 - f0, scale):
            problems.append(f"{tag}importance {sid}/{b}: Birnbaum engine "
                            f"{r['birnbaum_per_year']} oracle {f1 - f0}")
        if f > 0:
            fv, raw = (f - f0) / f, f1 / f
            # FV = 1 − F0/F: its absolute error scales with F0/F.
            if (r["fussell_vesely"] is None
                    or abs(r["fussell_vesely"] - fv) > 1e-9 * max(1, f0 / f)):
                problems.append(f"{tag}importance {sid}/{b}: FV engine "
                                f"{r['fussell_vesely']} oracle {fv}")
            if r["raw"] is None or not close(r["raw"], raw):
                problems.append(f"{tag}importance {sid}/{b}: RAW engine "
                                f"{r['raw']} oracle {raw}")
        elif r["fussell_vesely"] is not None or r["raw"] is not None:
            problems.append(f"{tag}importance {sid}/{b}: F = 0 but FV/RAW "
                            f"reported")
        if f0 > 1e-300 and (r["rrw"] is None or not close(r["rrw"], f / f0)):
            problems.append(f"{tag}importance {sid}/{b}: RRW engine "
                            f"{r['rrw']} oracle {f / f0}")
        if not close(r["probability"], o.be_p[b]):
            problems.append(f"{tag}importance {sid}/{b}: probability "
                            f"{r['probability']} vs {o.be_p[b]}")


def check_consequence_importance(m, o, et, sup_all, problems):
    """Engine's per-end-state and CDF importance vs the oracle
    (compare_importance_group per end state)."""
    if "end_states" not in et:
        problems.append("importance: event tree JSON has no end_states")
        return
    F, F1, F0 = oracle_consequence_importance(m, o, sup_all)
    for es in et["end_states"]:
        sid = es["id"]
        compare_importance_group(es, F.get(sid, 0.0), F1.get(sid, {}),
                                 F0.get(sid, {}), sup_all, o, problems)
    # CDF groups exactly the CD sequences: identical rows, bit for bit.
    cdf = next(x for x in et["metrics"] if x["id"] == "CDF")
    cd = next((es for es in et["end_states"] if es["id"] == "CD"), None)
    if cd is None or cdf.get("importance") != cd["importance"]:
        problems.append("importance: CDF rows differ from the CD end state's")


# --------------------------------------------------------------------------
# transfer stage (FR-11): the case's logic, one sequence transferring to a
# second event tree over the same gates
# --------------------------------------------------------------------------
def gen_transfer(m, r: random.Random):
    """A second tree ET-TEST2 over the case's gates (1-2 functional events,
    a random exact partition with bypass), a random sequence of ET-TEST
    transferring to it (keeping its end state: when that is CD, a counted
    transfer row would double count, anomaly D-10), with or without its own
    initiating event, and — when the case has a house event — per-sequence
    overrides on the transferring row and/or a target row.

    Biased toward the conditions that expose chain defects: the target's
    tops favour gates that depend on the house event and gates already used
    by ET-TEST (one gate compiled in both hops under different house
    values), and an override always flips the house event's default."""
    gate_ids = sorted(m["gates"])

    def uses_house(g, seen=None):
        seen = seen if seen is not None else set()
        if g in seen:
            return False
        seen.add(g)
        refs = list(formula_ids(m["gates"][g]))
        return any(x.startswith("HE-") or (x.startswith("GT-") and uses_house(x, seen))
                   for x in refs)
    k = min(r.choice([1, 2, 2]), len(gate_ids))
    tops = r.sample(gate_ids, k)
    if m["houses"] and r.random() < 0.7:
        pref = [g for g in gate_ids if uses_house(g)]
        shared = [g for g in pref if g in m["fes"].values()]
        pick = shared or pref
        if pick:
            g = r.choice(pick)
            tops = [g] + [t for t in tops if t != g][:k - 1]
    fes = {f"FE-X{i+1}": t for i, t in enumerate(tops)}
    order = list(fes)
    seqs = {}

    def rec(k, path):
        if k == len(order) or (k > 0 and r.random() < 0.2):
            full = {**path, **{fe: "bypassed" for fe in order[k:]}}
            seqs[f"SEQ-X{len(seqs):02d}"] = {
                "path": full,
                "end_state": "CD" if "failure" in full.values() else "OK"}
            return
        if r.random() < 0.2:
            rec(k + 1, {**path, order[k]: "bypassed"})
        else:
            rec(k + 1, {**path, order[k]: "success"})
            rec(k + 1, {**path, order[k]: "failure"})
    rec(0, {})
    origin_house, target_house = {}, {}
    if m["houses"] and r.random() < 0.75:
        flip = not m["houses"]["HE-H1"]
        where = r.choices(["origin", "target", "both"], [0.4, 0.2, 0.4])[0]
        if where in ("origin", "both"):
            origin_house = {"HE-H1": flip}
        if where in ("target", "both"):
            # "both": the target row switches back to the default
            val = (not flip) if where == "both" else flip
            target_house = {r.choice(sorted(seqs)): {"HE-H1": val}}
    return dict(fes=fes, order=order, seqs=seqs,
                origin=r.choice(sorted(m["sequences"])),
                has_ie=r.random() < 0.5, ie=round(10 ** r.uniform(-4, -2), 12),
                origin_house=origin_house, target_house=target_house)


def formula_ids(f):
    """Every ID a structured formula references (one level)."""
    if isinstance(f, str):
        yield f
        return
    (op, a), = f.items()
    if op == "not":
        yield from formula_ids(a)
    elif op == "atleast":
        for y in a["of"]:
            yield from formula_ids(y)
    else:
        for y in a:
            yield from formula_ids(y)


def write_transfer_model(m, x, d):
    write_model(m, d)
    dump = lambda p, o: open(p, "w").write(
        yaml.safe_dump(o, sort_keys=True, default_flow_style=False))
    path = f"{d}/event-trees/gen.yaml"
    et = yaml.safe_load(open(path))
    seq = et["event_tree"]["sequences"][x["origin"]]
    seq["transfer"] = "ET-TEST2"
    if x["origin_house"]:
        seq["house_events"] = x["origin_house"]
    dump(path, et)
    seqs2 = {sid: {**s, **({"house_events": x["target_house"][sid]}
                           if sid in x["target_house"] else {})}
             for sid, s in x["seqs"].items()}
    t2 = {"id": "ET-TEST2", "label": "generated transfer target",
          "functional_events": {fe: {"label": f"generated {fe}", "top_gate": t}
                                for fe, t in x["fes"].items()},
          "sequences": seqs2}
    if x["has_ie"]:
        t2["initiating_event"] = {
            "id": "IE-TEST2", "label": "generated initiator",
            "frequency": {"value": x["ie"], "unit": "per_year"},
            "provenance": {"source": "property-test generator",
                           "justification": "randomized transfer case"}}
    dump(f"{d}/event-trees/gen2.yaml", {"event_tree": t2})


def enumerate_rows(o, rows, sup):
    """One truth-table pass over `sup`: per row (id, predicate), P(row) and
    the cofactor probabilities P(row | x=1), P(row | x=0) per event."""
    sup = sorted(sup)
    P = [0.0] * len(rows)
    C1 = [dict() for _ in rows]
    C0 = [dict() for _ in rows]
    for bits in itertools.product([False, True], repeat=len(sup)):
        st = dict(zip(sup, bits))
        w = 1.0
        for b, v in st.items():
            w *= o.be_p[b] if v else 1.0 - o.be_p[b]
        for k, (_, pred) in enumerate(rows):
            if not pred(st):
                continue
            P[k] += w
            for b, v in st.items():
                if v:
                    C1[k][b] = C1[k].get(b, 0.0) + w / o.be_p[b]
                else:
                    C0[k][b] = C0[k].get(b, 0.0) + w / (1.0 - o.be_p[b])
    return P, C1, C0


def run_transfer_stage(m, o, trng, engine, problems, keep_dir, mc_samples):
    x = gen_transfer(m, trng)
    base_h = dict(m["houses"])
    h1 = {**base_h, **x["origin_house"]}          # origin row's houses

    def hop_pred(fes, path, houses):
        def pred(st):
            return all(out == "bypassed"
                       or (out == "failure") == o.ev(fes[fe], st, houses)
                       for fe, out in path.items())
        return pred

    def fails_pred(fes, path, houses):
        return lambda st: all(o.ev(fes[fe], st, houses)
                              for fe, out in path.items() if out == "failure")

    # expected rows, in the engine's report order
    exp = []          # (id, pred, end_state, aggregated, fail_pred, noncoh, kind)
    for sid in sorted(m["sequences"]):
        seq = m["sequences"][sid]
        hs = h1 if sid == x["origin"] else base_h
        p1 = hop_pred(m["fes"], seq["path"], hs)
        f1 = fails_pred(m["fes"], seq["path"], hs)
        nc1 = any(o.uses_negation(m["fes"][fe]) for fe, out in seq["path"].items()
                  if out != "bypassed")
        is_x = sid == x["origin"]
        exp.append((sid, p1, seq["end_state"], not is_x, f1, nc1, "own"))
        if not is_x:
            continue
        for tid in sorted(x["seqs"]):
            tseq = x["seqs"][tid]
            h2 = {**h1, **x["target_house"].get(tid, {})}
            p2 = hop_pred(x["fes"], tseq["path"], h2)
            f2 = fails_pred(x["fes"], tseq["path"], h2)
            nc2 = any(o.uses_negation(x["fes"][fe])
                      for fe, out in tseq["path"].items() if out != "bypassed")
            exp.append((f"{sid}>{tid}",
                        (lambda st, p1=p1, p2=p2: p1(st) and p2(st)),
                        tseq["end_state"], True,
                        (lambda st, f1=f1, f2=f2: f1(st) and f2(st)),
                        nc1 or nc2, "expansion"))
    sup = set()
    for t in list(m["fes"].values()) + list(x["fes"].values()):
        o.support(t, sup)
    P, C1, C0 = enumerate_rows(o, [(e[0], e[1]) for e in exp], sup)
    ie = m["ie_freq"]

    d = tempfile.mkdtemp(prefix="psa-prop-xfer-")
    try:
        write_transfer_model(m, x, d)
        v = subprocess.run([sys.executable, "ci/validate.py", d,
                            "schema/psa-model.schema.json"],
                           capture_output=True, text=True)
        if v.returncode != 0:
            problems.append("transfer: validate.py rejected the variant:\n"
                            + v.stdout + v.stderr)
        run = lambda *a: subprocess.run([engine, d, *a], capture_output=True,
                                        text=True)
        p = run("ET-TEST", "--json", "--mcs-limit", "100000")
        if p.returncode != 0:
            problems.append(f"transfer: engine failed:\n{p.stderr}")
            return
        et = json.loads(p.stdout)
        rows = et["sequences"]
        if [r["id"] for r in rows] != [e[0] for e in exp]:
            problems.append(f"transfer: rows {[r['id'] for r in rows]} vs "
                            f"expected {[e[0] for e in exp]}")
            return
        for k, (r, e) in enumerate(zip(rows, exp)):
            rid = e[0]
            if not close(r["frequency_per_year"], ie * P[k]):
                problems.append(f"transfer {rid}: engine "
                                f"{r['frequency_per_year']} oracle {ie * P[k]}")
            if r["end_state"] != e[2]:
                problems.append(f"transfer {rid}: end state {r['end_state']}")
            want_path = (None if e[6] == "own" else
                         [{"event_tree": "ET-TEST", "sequence": x["origin"]},
                          {"event_tree": "ET-TEST2", "sequence": rid.split(">")[1]}])
            if r["transfer_path"] != want_path:
                problems.append(f"transfer {rid}: transfer_path {r['transfer_path']}")
            # cut sets: failure logic of every hop, delete-term convention
            if e[2] != "OK":
                if e[5]:
                    if r["cut_sets"]:
                        problems.append(f"transfer {rid}: cut sets on "
                                        f"non-coherent logic")
                else:
                    ora = o.mcs_pred(e[4], sup)
                    eng = {frozenset(c["events"]) for c in r["cut_sets"]}
                    if eng != ora:
                        problems.append(f"transfer {rid}: cut sets engine "
                                        f"{len(eng)} oracle {len(ora)}")
        # the transfer row: followed, and its expansions' sum
        k0 = next(k for k, e in enumerate(exp) if e[0] == x["origin"])
        fol = rows[k0]["followed"]
        under = [k for k, e in enumerate(exp) if e[6] == "expansion"]
        ov = bool(x["target_house"])
        if (fol is None or fol["per_sequence_house_overrides"] != ov
                or not close(fol["sum_probability"], sum(P[k] for k in under))
                or not close(fol["probability"], P[k0])):
            problems.append(f"transfer: followed {fol} vs oracle sum "
                            f"{sum(P[k] for k in under)}, P {P[k0]}, overrides {ov}")
        if not ov and not close(sum(P[k] for k in under), P[k0]):
            problems.append("transfer: oracle expansions do not sum to the "
                            "transfer row without overrides (harness defect)")
        own_sum = sum(P[k] for k, e in enumerate(exp) if e[6] == "own")
        part = et["partition"]
        if (part["per_sequence_house_overrides"] != bool(x["origin_house"])
                or not close(part["sum_probability"], own_sum)):
            problems.append(f"transfer: partition {part} vs oracle {own_sum}")
        # metrics and end-state importance over aggregated rows only
        cdf = sum(ie * P[k] for k, e in enumerate(exp) if e[3] and e[2] == "CD")
        cdf_eng = next(mm["value_per_year"] for mm in et["metrics"]
                       if mm["id"] == "CDF")
        if not close(cdf_eng, cdf):
            problems.append(f"transfer: CDF engine {cdf_eng} oracle {cdf} "
                            f"(transfer row end state "
                            f"{m['sequences'][x['origin']]['end_state']})")
        F, F1, F0 = {}, {}, {}
        for k, e in enumerate(exp):
            if not e[3]:
                continue
            es = e[2]
            F[es] = F.get(es, 0.0) + ie * P[k]
            for b in sup:
                F1.setdefault(es, {})[b] = (F1.get(es, {}).get(b, 0.0)
                                            + ie * C1[k].get(b, 0.0))
                F0.setdefault(es, {})[b] = (F0.get(es, {}).get(b, 0.0)
                                            + ie * C0[k].get(b, 0.0))
        if {g["id"] for g in et["end_states"]} != set(F):
            problems.append(f"transfer: end-state groups "
                            f"{[g['id'] for g in et['end_states']]} vs {sorted(F)}")
        for g in et["end_states"]:
            if g["id"] in F:
                compare_importance_group(g, F[g["id"]], F1[g["id"]], F0[g["id"]],
                                         sup, o, problems, "transfer ")
        # variable order through transfers and house overrides
        order_invariant(engine, d, "ET-TEST", problems, "transfer ")
        reorder_invariant(engine, d, "ET-TEST", problems, "transfer ")
        shared_invariant(engine, d, "ET-TEST", problems, "transfer ")
        et_truncation_vs_exact(engine, d, problems, "transfer ")

        # garbage collection through transfers and house overrides
        gc_invisible(engine, d, "ET-TEST", ["--mcs-limit", "100000"], problems,
                     "transfer ")

        # the target tree standalone
        p2 = run("ET-TEST2", "--json")
        if not x["has_ie"]:
            if p2.returncode == 0 or "transfer-only tree" not in p2.stderr:
                problems.append("transfer: ET-TEST2 (no initiating event) "
                                "quantified standalone")
        elif p2.returncode != 0:
            problems.append(f"transfer: ET-TEST2 standalone failed: {p2.stderr}")
        else:
            srows = json.loads(p2.stdout)["sequences"]
            preds = [(tid, hop_pred(x["fes"], x["seqs"][tid]["path"],
                                    {**base_h, **x["target_house"].get(tid, {})}))
                     for tid in sorted(x["seqs"])]
            P2, _, _ = enumerate_rows(o, preds, sup)
            for r, (tid, _), pk in zip(srows, preds, P2):
                if r["id"] != tid or not close(r["frequency_per_year"], x["ie"] * pk):
                    problems.append(f"transfer: ET-TEST2 standalone {r['id']} "
                                    f"{r['frequency_per_year']} vs {x['ie'] * pk}")
        q = subprocess.run([sys.executable, "ci/quantify.py", d, f"{d}/q.json",
                            "--engine", engine], capture_output=True, text=True)
        want = ["ET-TEST", "ET-TEST2"] if x["has_ie"] else ["ET-TEST"]
        if q.returncode != 0 or sorted(json.load(open(f"{d}/q.json"))) != want:
            problems.append(f"transfer: quantify.py {q.returncode}: {q.stderr}")
        # Monte Carlo bookkeeping through the transfer
        if mc_samples:
            pm = run("ET-TEST", "--json", "--prob-only", "--samples",
                     str(min(mc_samples, 500)), "--keep-samples")
            u = json.loads(pm.stdout)
            ied = u["uncertainty"]["initiating_event_draws"]
            draws = [r["uncertainty"]["draws"] for r in u["sequences"]]
            if not x["origin_house"]:
                own = [dr for dr, e in zip(draws, exp) if e[6] == "own"]
                worst = max(abs(sum(dd[i] for dd in own) / ied[i] - 1)
                            for i in range(len(ied)))
                if worst > 1e-9:
                    problems.append(f"transfer MC: own-row partition {worst}")
            if not ov:
                worst = max(abs(sum(draws[k][i] for k in under) - draws[k0][i])
                            / max(draws[k0][i], 1e-300) for i in range(len(ied)))
                if worst > 1e-9 and any(draws[k0]):
                    problems.append(f"transfer MC: expansions vs transfer row "
                                    f"{worst}")
            cd = [dr for dr, e in zip(draws, exp) if e[3] and e[2] == "CD"]
            fold = []
            for i in range(len(ied)):
                acc = 0.0
                for dd in cd:
                    acc += dd[i]
                fold.append(acc)
            cdf_d = next(mm for mm in u["metrics"] if mm["id"] == "CDF")
            if cdf_d["uncertainty"]["draws"] != fold:
                problems.append("transfer MC: CDF draws are not the fold of "
                                "the aggregated CD rows")
    finally:
        if problems and keep_dir:
            shutil.copytree(d, keep_dir + "-transfer", dirs_exist_ok=True)
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# MEF round trip (FR-14/FR-15): export, import, requantify
# --------------------------------------------------------------------------
def run_mef_stage(m, engine, d, et, problems):
    """Export the case to Open-PSA MEF, import it back, validate, and
    requantify the event tree: every sequence probability must equal the
    original's (1e-12 relative; the importer names each row's end state
    after the exported sequence, and sets the initiator to 1 /yr because
    the exported MEF carries no frequency). Done with the CCF groups
    pre-expanded, and — for non-staggered groups, the MEF convention —
    also with the groups exported raw and re-expanded by the engine."""
    ie = et["initiating_event"]["frequency_per_year"]
    want = {s["id"]: s["frequency_per_year"] / ie for s in et["sequences"]}
    modes = [("expanded", ["--expand-ccf"])]
    if m["ccf"] and m["ccf"]["testing"] == "non-staggered":
        modes.append(("raw CCF", []))
    for label, flags in modes:
        tmp = tempfile.mkdtemp(prefix="psa-prop-mef-")
        try:
            xml, out = f"{tmp}/m.xml", f"{tmp}/imported"
            ex = subprocess.run([sys.executable, "ci/export_mef.py", d, xml, *flags],
                                capture_output=True, text=True)
            im = subprocess.run([sys.executable, "ci/import_mef.py", xml, out],
                                capture_output=True, text=True)
            if ex.returncode or im.returncode:
                problems.append(f"MEF {label}: export/import failed:\n"
                                f"{ex.stderr}{im.stderr}")
                continue
            v = subprocess.run([sys.executable, "ci/validate.py", out,
                                "schema/psa-model.schema.json"],
                               capture_output=True, text=True)
            if v.returncode:
                problems.append(f"MEF {label}: imported model rejected:\n{v.stdout}")
                continue
            if label == "raw CCF" and not os.path.exists(f"{out}/ccf-groups.yaml"):
                problems.append("MEF raw CCF: no CCF group imported")
            r = subprocess.run([engine, out, "ET-TEST", "--json", "--prob-only"],
                               capture_output=True, text=True)
            if r.returncode:
                problems.append(f"MEF {label}: engine failed on the import:\n{r.stderr}")
                continue
            got = {}
            for s2 in json.loads(r.stdout)["sequences"]:
                got[s2["end_state"]] = got.get(s2["end_state"], 0.0) + s2["frequency_per_year"]
            if set(got) != set(want):
                problems.append(f"MEF {label}: sequences {sorted(got)} vs {sorted(want)}")
                continue
            for k in want:
                if abs(got[k] - want[k]) > 1e-12 * max(abs(want[k]), 1e-300) + 1e-300:
                    problems.append(f"MEF {label} {k}: after round trip {got[k]!r}, "
                                    f"original {want[k]!r}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# one case
# --------------------------------------------------------------------------
def gc_invisible(engine, d, target, extra, problems, tag=""):
    """GC stage: the engine's JSON with garbage collection forced at every
    safe point (--gc-threshold 1) must equal the output with collection
    disabled (--gc-threshold 0), byte for byte after dropping bdd_nodes
    (the only field that may differ: the arena size)."""
    outs = []
    for thr in ("0", "1"):
        p = subprocess.run([engine, d, target, "--json", *extra,
                            "--gc-threshold", thr], capture_output=True, text=True)
        if p.returncode != 0:
            problems.append(f"{tag}GC {target} (threshold {thr}): engine "
                            f"failed:\n{p.stderr}")
            return
        j = json.loads(p.stdout)
        j.pop("bdd_nodes", None)
        outs.append(json.dumps(j, sort_keys=True))
    if outs[0] != outs[1]:
        problems.append(f"{tag}GC {target}: output differs with collection "
                        f"forced at every safe point")


# coverage of the reorder stage, printed at the end of a run
REORDER_STATS = {"runs": 0, "reordered": 0, "shrunk": 0}


def order_invariant(engine, d, target, problems, tag="",
                    alt=("--order", "rdfs"), name="order"):
    """Order stage: the same quantification with the basic events numbered
    in reverse-operand DFS order (--order rdfs) instead of discovery order
    must give the same results — a different BDD for the same function:
    probabilities and frequencies within 1e-12 relative, identical cut-set
    and prime-implicant sets (each probability within 1e-12), Birnbaum and
    conditional frequencies within rounding. `alt` replaces the variant's
    arguments (the reorder stage passes dynamic-reordering flags)."""
    outs = []
    for args in (("--order", "dfs"), tuple(alt)):
        p = subprocess.run([engine, d, target, "--json", "--mcs-limit", "100000",
                            "--prime-implicants", *args],
                           capture_output=True, text=True)
        if p.returncode != 0:
            problems.append(f"{tag}{name} {target} ({' '.join(args)}): engine "
                            f"failed:\n{p.stderr}")
            return
        outs.append(json.loads(p.stdout))
        if "--gc-stats" in args:
            # "reorder: <id>: N reordering(s), last A -> B live nodes"
            for line in p.stderr.splitlines():
                mt = re.match(r"reorder: \S+: (\d+) reordering\(s\), last (\d+) -> (\d+)", line)
                if mt:
                    REORDER_STATS["runs"] += 1
                    REORDER_STATS["reordered"] += int(mt.group(1)) > 0
                    REORDER_STATS["shrunk"] += int(mt.group(3)) < int(mt.group(2))
    a, b = outs
    rel = lambda x, y: abs(x - y) <= 1e-12 * max(abs(x), abs(y)) + 1e-300
    near = lambda x, y, s: abs(x - y) <= 1e-12 * max(abs(x), abs(y), s) + 1e-300
    def prods(lst, key="probability"):
        return {(frozenset(x["events"]), frozenset(x.get("negated", []))): x[key]
                for x in lst}
    bad = []
    if a["type"] == "fault_tree":
        if not rel(a["probability"], b["probability"]):
            bad.append(f"P(top) {a['probability']} vs {b['probability']}")
        for k in ("minimal_cut_sets", "prime_implicants"):
            pa, pb = prods(a.get(k, [])), prods(b.get(k, []))
            if set(pa) != set(pb) or any(not rel(pa[q], pb[q]) for q in pa):
                bad.append(f"{k} differ")
        ba = {x["event"]: x["importance"] for x in a["birnbaum"]}
        bb = {x["event"]: x["importance"] for x in b["birnbaum"]}
        if set(ba) != set(bb) or any(not near(ba[e], bb[e], a["probability"]) for e in ba):
            bad.append("Birnbaum differs")
    else:
        sa = {s["id"]: s for s in a["sequences"]}
        sb = {s["id"]: s for s in b["sequences"]}
        if set(sa) != set(sb):
            bad.append("sequence sets differ")
        for sid in sa.keys() & sb.keys():
            if not rel(sa[sid]["frequency_per_year"], sb[sid]["frequency_per_year"]):
                bad.append(f"{sid} frequency")
            for k, key in (("cut_sets", "frequency_per_year"),
                           ("prime_implicants", "frequency_per_year")):
                pa, pb = prods(sa[sid].get(k) or [], key), prods(sb[sid].get(k) or [], key)
                if set(pa) != set(pb) or any(not rel(pa[q], pb[q]) for q in pa):
                    bad.append(f"{sid} {k} differ")
        for ma, mb in zip(a["metrics"], b["metrics"]):
            if not rel(ma["value_per_year"], mb["value_per_year"]):
                bad.append(f"metric {ma['id']}")
            ia = {x["event"]: x for x in ma.get("importance", [])}
            ib = {x["event"]: x for x in mb.get("importance", [])}
            if set(ia) != set(ib) or any(
                    not near(ia[e][k], ib[e][k], ma["value_per_year"])
                    for e in ia for k in ("frequency_if_true_per_year",
                                          "frequency_if_false_per_year")):
                bad.append(f"importance {ma['id']}")
    if bad:
        problems.append(f"{tag}{name} {target}: dfs vs {' '.join(alt)}: "
                        f"{'; '.join(bad[:4])}")


# coverage of the shared-compiler stage (FR-38), printed at the end
SHARED_STATS = {"runs": 0, "byte_different": 0}


def shared_invariant(engine, d, target, problems, tag=""):
    """Shared-compiler stage (FR-38): an event tree compiled with one
    compiler for all its rows (the default) and with a fresh compiler per
    row (--compile per-row, the previous behaviour) gives the order stage's
    results — per row, the shared manager's variable numbering can differ,
    so a different BDD of the same function."""
    outs = []
    for mode in ("shared", "per-row"):
        p = subprocess.run([engine, d, target, "--json", "--mcs-limit", "100000",
                            "--prime-implicants", "--compile", mode],
                           capture_output=True, text=True)
        outs.append(p.stdout)
    SHARED_STATS["runs"] += 1
    SHARED_STATS["byte_different"] += outs[0] != outs[1]
    order_invariant(engine, d, target, problems, tag,
                    alt=("--compile", "per-row"), name="shared")


def reorder_invariant(engine, d, target, problems, tag=""):
    """Reorder stage (FR-35): dynamic reordering forced at every safe point
    (collection at every safe point, sifting whenever anything is live),
    from the default and from the reverse-DFS order, must give the order
    stage's results: same function, different BDDs."""
    forced = ("--gc-threshold", "1", "--reorder-threshold", "0", "--gc-stats")
    order_invariant(engine, d, target, problems, tag, alt=forced, name="reorder")
    # deterministic: hash seeds differ per process, results must not
    # (three runs of each non-default variant; D-17 was a hash-ordered
    # variable numbering under --order rdfs on event trees)
    for args in (("--order", "rdfs"), forced[:-1], ("--order", "rdfs") + forced[:-1]):
        runs = {subprocess.run([engine, d, target, "--json", "--mcs-limit", "100000",
                                "--prime-implicants", *args],
                               capture_output=True, text=True).stdout for _ in range(3)}
        if len(runs) != 1 or not next(iter(runs)):
            problems.append(f"{tag}reorder {target}: {' '.join(args)}: "
                            f"{len(runs)} different outputs from 3 runs")
    order_invariant(engine, d, target, problems, tag,
                    alt=("--order", "rdfs") + forced, name="reorder")


# coverage of the truncation stage, printed at the end of a run
TRUNC_STATS = {"trees": 0, "runs": 0, "dropped": 0, "gap": 0, "refused": 0}


def truncation_cutoffs(probs):
    """Cut-offs for the truncation stage: 0 (keep everything), one strictly
    between each pair of adjacent distinct cut-set probabilities (geometric
    mean, only where they differ by more than a relative 1e-6, so no
    product sits within rounding of the cut-off — exact-tie behaviour is a
    unit test), and one above the largest (drop everything)."""
    ps = sorted(set(probs))
    out = [0.0]
    for a, b in zip(ps, ps[1:]):
        if a > 0 and b > a * (1 + 1e-6):
            out.append(math.sqrt(a * b))
    if ps and ps[-1] < 0.5:
        out.append(min(0.999, ps[-1] * 2))
    return out


def run_truncation_stage(m, o, engine, d, top, noncoh, p_oracle, problems):
    """Truncated quantification (FR-34) against the oracle: for every
    cut-off in `truncation_cutoffs` and order limits none / 1 / 2, the
    retained set is exactly {minimal cut set m : P(m) >= cut-off, |m| <= K},
    the lower bound is the exact probability of their union (enumerated),
    the rare-event sum is Σ P(retained), the exact P(top) lies within
    [lower, upper] with upper = min(1, lower + error bound), and the error
    bound is at least the probability of the union of the lost cut sets. Cut-off 0 and
    no order limit is exact (bound 0). Non-coherent trees are refused."""
    def trunc(cutoff, k):
        args = [engine, d, "FT-TEST", "--json", "--mcs-limit", "100000",
                "--truncated", repr(cutoff)]
        if k is not None:
            args += ["--order-limit", str(k)]
        return subprocess.run(args, capture_output=True, text=True)
    if noncoh:
        r = trunc(1e-6, None)
        if r.returncode == 0 or "coherent logic" not in r.stderr:
            problems.append(f"truncation: non-coherent tree not refused: {r.stderr[:200]}")
        TRUNC_STATS["refused"] += 1
        return
    TRUNC_STATS["trees"] += 1
    mcs = o.mcs(top)
    pm = {c: math.prod(o.be_p[b] for b in c) for c in mcs}
    sup = o.support(top, set())
    for cutoff in truncation_cutoffs(pm.values()):
        for k in (None, 1, 2):
            tag = f"truncation cut-off {cutoff:.3e}, order {k}"
            r = trunc(cutoff, k)
            if r.returncode != 0:
                problems.append(f"{tag}: engine failed:\n{r.stderr}")
                continue
            j = json.loads(r.stdout)
            got = {frozenset(c["events"]): c["probability"] for c in j["minimal_cut_sets"]}
            want = {c for c in mcs if pm[c] >= cutoff and (k is None or len(c) <= k)}
            if set(got) != want or j["retained_cut_sets"] != len(want):
                problems.append(f"{tag}: retained {sorted(map(sorted, got))[:3]} "
                                f"vs oracle {sorted(map(sorted, want))[:3]} "
                                f"({len(got)} vs {len(want)})")
                continue
            for c, pe in got.items():
                if not close(pe, pm[c]):
                    problems.append(f"{tag}: cut set {sorted(c)} probability {pe} vs {pm[c]}")
            lo, up, eb = (j["probability_lower_bound"], j["probability_upper_bound"],
                          j["truncation_error_bound"])
            TRUNC_STATS["runs"] += 1
            TRUNC_STATS["dropped"] += eb > 0
            TRUNC_STATS["gap"] += not close(lo, p_oracle)
            p_union = o.prob(lambda st: any(all(st[b] for b in c) for c in want), sup)
            if not close(lo, p_union):
                problems.append(f"{tag}: lower bound {lo} vs oracle P(union retained) {p_union}")
            if not close(j["rare_event_sum"], sum(pm[c] for c in want)):
                problems.append(f"{tag}: rare-event sum {j['rare_event_sum']}")
            if eb < 0 or up != min(1.0, lo + eb):
                problems.append(f"{tag}: upper {up} != min(1, {lo} + {eb})")
            # sharper than P(top) <= upper: every lost minimal cut set
            # contains a counted term (it cannot contain a retained one),
            # so the bound covers the union of ALL lost cut sets
            lost = mcs - want
            if lost:
                p_lost = o.prob(lambda st: any(all(st[b] for b in c) for c in lost), sup)
                if eb < p_lost and not close(eb, p_lost):
                    problems.append(f"{tag}: error bound {eb} < P(union of lost cut sets) "
                                    f"{p_lost}")
            slack = 1e-12 * max(p_oracle, 1e-300)
            if not (lo - slack <= p_oracle <= up + slack):
                problems.append(f"{tag}: exact P(top) {p_oracle} outside [{lo}, {up}]")
            if cutoff == 0.0 and k is None and (eb != 0.0 or not close(lo, p_oracle)):
                problems.append(f"{tag}: no truncation but bound {eb}, lower {lo} "
                                f"vs exact {p_oracle}")


# coverage of the event-tree truncation stage (FR-39)
ET_TRUNC_STATS = {"trees": 0, "runs": 0, "rows": 0, "wide": 0, "refused": 0,
                  "transfer_runs": 0}


def et_truncation_cutoffs(probs):
    """0, a cut-off near the middle of the distinct cut-set probabilities
    and one near the top (geometric means of adjacent distinct values, so
    no product sits within rounding of a cut-off)."""
    ps = sorted(set(p for p in probs if p > 0))
    mids = [math.sqrt(a * b) for a, b in zip(ps, ps[1:]) if b > a * (1 + 1e-6)]
    out = [0.0]
    if mids:
        out.append(mids[len(mids) // 2])
        out.append(mids[-1])
    return out


def run_et_truncation_stage(m, o, engine, d, problems):
    """Truncated event-tree quantification (FR-39) against the oracle: for
    each cut-off (and order limit 1 at cut-off 0), every row's frequency
    bounds contain the exact frequency f_IE · P(row) (enumerated); the
    retained failure-logic cut sets are exactly the oracle's minimal cut
    sets of the conjunction of the failed tops with P ≥ cut-off; the
    failure-logic lower bound is the probability of their union; the
    failure-and-success bounds contain P(F ∧ ∨ success tops); the metric
    bounds contain the exact CDF; cut-off 0 without a limit is exact (to the
    rounding of P(F) − P(G)). Rows using non-coherent tops: refused."""
    seqs = m["sequences"]
    noncoh = any(o.uses_negation(m["fes"][fe]) for q in seqs.values()
                 for fe, out in q["path"].items() if out != "bypassed")
    def trunc(cutoff, k=None):
        args = [engine, d, "ET-TEST", "--json", "--mcs-limit", "100000",
                "--truncated", repr(cutoff)] + (["--order-limit", str(k)] if k else [])
        return subprocess.run(args, capture_output=True, text=True)
    if noncoh:
        r = trunc(1e-6)
        if r.returncode == 0 or "coherent logic" not in r.stderr:
            problems.append(f"ET truncation: non-coherent tops not refused: {r.stderr[:200]}")
        ET_TRUNC_STATS["refused"] += 1
        return
    ET_TRUNC_STATS["trees"] += 1
    sup_all = set()
    for t in m["fes"].values():
        o.support(t, sup_all)
    ie = m["ie_freq"]
    info = {}
    all_p = []
    for sid, q in seqs.items():
        fails = [m["fes"][fe] for fe, out in q["path"].items() if out == "failure"]
        succ = [m["fes"][fe] for fe, out in q["path"].items() if out == "success"]
        fpred = lambda st, fails=fails: all(o.ev(f, st) for f in fails)
        gpred = lambda st, fails=fails, succ=succ: (all(o.ev(f, st) for f in fails)
                                                    and any(o.ev(x, st) for x in succ))
        rpred = lambda st, fails=fails, succ=succ: (all(o.ev(f, st) for f in fails)
                                                    and not any(o.ev(x, st) for x in succ))
        mcs = o.mcs_pred(fpred, sup_all) if fails else {frozenset()}
        pm = {c: math.prod(o.be_p[b] for b in c) for c in mcs}
        all_p += list(pm.values())
        info[sid] = dict(p_row=o.prob(rpred, sup_all), p_f=o.prob(fpred, sup_all),
                         p_g=o.prob(gpred, sup_all) if succ else 0.0, mcs=pm,
                         end=q["end_state"])
    e_cdf = sum(ie * v["p_row"] for v in info.values() if v["end"] == "CD")
    for cutoff, k in [(c, None) for c in et_truncation_cutoffs(all_p)] + [(0.0, 1)]:
        tag = f"ET truncation cut-off {cutoff:.3e}, order {k}"
        r = trunc(cutoff, k)
        if r.returncode != 0:
            problems.append(f"{tag}: engine failed:\n{r.stderr}")
            continue
        ET_TRUNC_STATS["runs"] += 1
        j = json.loads(r.stdout)
        for row in j["sequences"]:
            v = info[row["id"]]
            ET_TRUNC_STATS["rows"] += 1
            exact = ie * v["p_row"]
            lo, hi = row["frequency_lower_bound"], row["frequency_upper_bound"]
            ET_TRUNC_STATS["wide"] += hi - lo > 1e-12 * max(ie * v["p_f"], 1e-300)
            slack = 1e-12 * max(ie * v["p_f"], 1e-300)
            if not (lo - slack <= exact <= hi + slack):
                problems.append(f"{tag}: {row['id']} exact {exact:.6e} outside [{lo:.6e}, {hi:.6e}]")
            want = {c for c, p in v["mcs"].items() if p >= cutoff and (k is None or len(c) <= k)}
            got = {frozenset(c["events"]) for c in row["cut_sets"]}
            if got != want:
                problems.append(f"{tag}: {row['id']} retained {sorted(map(sorted, got))[:3]} "
                                f"vs oracle {sorted(map(sorted, want))[:3]}")
                continue
            fl = row["failure_logic"]
            p_union = o.prob(lambda st, want=want: any(all(st[b] for b in c) for c in want), sup_all)
            if not close(fl["probability_lower_bound"], p_union):
                problems.append(f"{tag}: {row['id']} failure-logic lower {fl['probability_lower_bound']} "
                                f"vs P(union retained) {p_union}")
            if not (fl["probability_lower_bound"] - 1e-12 <= v["p_f"] <= fl["probability_upper_bound"] + 1e-12):
                problems.append(f"{tag}: {row['id']} P(F) {v['p_f']} outside its bounds")
            gl = row["failure_and_success_logic"]
            if not (gl["probability_lower_bound"] - 1e-12 <= v["p_g"] <= gl["probability_upper_bound"] + 1e-12):
                problems.append(f"{tag}: {row['id']} P(F ∧ S) {v['p_g']} outside its bounds")
            if cutoff == 0.0 and k is None and not (abs(lo - exact) <= slack and abs(hi - exact) <= slack):
                problems.append(f"{tag}: {row['id']} not exact at cut-off 0: [{lo}, {hi}] vs {exact}")
        cdf = next(x for x in j["metrics"] if x["id"] == "CDF")
        slack = 1e-12 * max(ie, 1e-300)
        if not (cdf["value_lower_bound"] - slack <= e_cdf <= cdf["value_upper_bound"] + slack):
            problems.append(f"{tag}: CDF {e_cdf:.6e} outside [{cdf['value_lower_bound']:.6e}, "
                            f"{cdf['value_upper_bound']:.6e}]")
        if (any("frequency_per_year" in row for row in j["sequences"])
                or any("value_per_year" in x for x in j["metrics"])):
            problems.append(f"{tag}: a bound reported as a frequency or metric value")


def et_truncation_vs_exact(engine, d, problems, tag=""):
    """The transfer variant: truncated bounds against the engine's exact
    row frequencies (verified against the oracle by the transfer stage),
    every row followed through transfers and house overrides; exact at
    cut-off 0; refusal matches the exact path's coherence."""
    ex = subprocess.run([engine, d, "ET-TEST", "--json", "--mcs-limit", "0"],
                        capture_output=True, text=True)
    if ex.returncode != 0:
        return
    exj = json.loads(ex.stdout)
    exact = {s["id"]: s["frequency_per_year"] for s in exj["sequences"]}
    for cutoff in (0.0, 1e-6):
        r = subprocess.run([engine, d, "ET-TEST", "--json", "--mcs-limit", "0",
                            "--truncated", repr(cutoff)], capture_output=True, text=True)
        if r.returncode != 0:
            if "coherent logic" not in r.stderr:
                problems.append(f"{tag}ET truncation: engine failed:\n{r.stderr}")
            return
        ET_TRUNC_STATS["transfer_runs"] += 1
        j = json.loads(r.stdout)
        if sorted(s["id"] for s in j["sequences"]) != sorted(exact):
            problems.append(f"{tag}ET truncation: rows differ from the exact path's")
            continue
        scale = 1e-12 * exj["initiating_event"]["frequency_per_year"]
        for s in j["sequences"]:
            e = exact[s["id"]]
            lo, hi = s["frequency_lower_bound"], s["frequency_upper_bound"]
            if not (lo - scale <= e <= hi + scale) or (cutoff == 0.0 and not
                                                        (abs(lo - e) <= scale and abs(hi - e) <= scale)):
                problems.append(f"{tag}ET truncation cut-off {cutoff}: {s['id']} exact {e:.6e} "
                                f"vs [{lo:.6e}, {hi:.6e}]")


def run_case(rng, engine, keep_dir, urng=None, mc_samples=0, trng=None):
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
        # prime implicants (all cases, coherent or not): the engine's set
        # equals the oracle's Quine–McCluskey primes; with --order-limit 2
        # it equals their order <= 2 subset; on coherent trees the primes
        # are the minimal cut sets
        pr = subprocess.run([engine, d, "FT-TEST", "--json", "--mcs-limit", "100000",
                             "--prime-implicants"], capture_output=True, text=True)
        if pr.returncode != 0:
            problems.append(f"prime implicants: engine failed:\n{pr.stderr}")
        else:
            got = {(frozenset(x["events"]), frozenset(x["negated"]))
                   for x in json.loads(pr.stdout)["prime_implicants"]}
            want = o.primes(top)
            if want is not None and got != want:
                problems.append(f"prime implicants: engine {len(got)} oracle "
                                f"{len(want)}; only-engine {list(got - want)[:2]}, "
                                f"only-oracle {list(want - got)[:2]}")
            if not noncoh and got != {(frozenset(c["events"]), frozenset())
                                      for c in ft["minimal_cut_sets"]}:
                problems.append("prime implicants of a coherent tree differ "
                                "from its minimal cut sets")
            if want is not None:
                p2 = json.loads(subprocess.run(
                    [engine, d, "FT-TEST", "--json", "--mcs-limit", "100000",
                     "--prime-implicants", "--order-limit", "2"],
                    capture_output=True, text=True, check=True).stdout)
                got2 = {(frozenset(x["events"]), frozenset(x["negated"]))
                        for x in p2["prime_implicants"]}
                if got2 != {q for q in want if len(q[0]) + len(q[1]) <= 2}:
                    problems.append("prime implicants with --order-limit 2 differ "
                                    "from the oracle's order <= 2 primes")
                for x in p2["prime_implicants"]:
                    pe = math.prod([o.be_p[b] for b in x["events"]]
                                   + [1 - o.be_p[b] for b in x["negated"]])
                    if not close(x["probability"], pe):
                        problems.append(f"prime implicant probability {x}: {pe}")

        # truncated quantification: retained set, bounds (FR-34)
        run_truncation_stage(m, o, engine, d, top, noncoh, p_oracle, problems)
        # ... and of the event tree (FR-39)
        run_et_truncation_stage(m, o, engine, d, problems)

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
        et_pi = json.loads(subprocess.run(
            [engine, d, "ET-TEST", "--json", "--mcs-limit", "100000",
             "--prime-implicants"], capture_output=True, text=True, check=True).stdout)
        pi_by_id = {s["id"]: s.get("prime_implicants") for s in et_pi["sequences"]}
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
                    # prime implicants of the failure logic instead
                    conj = {"and": fails} if len(fails) > 1 else fails[0]
                    want_pi = o.primes(conj)
                    got_pi = pi_by_id.get(s["id"])
                    if got_pi is None:
                        problems.append(f"{s['id']}: no prime implicants listed "
                                        f"for non-coherent failure logic")
                    elif want_pi is not None:
                        got_set = {(frozenset(x["events"]), frozenset(x["negated"]))
                                   for x in got_pi}
                        if got_set != want_pi:
                            problems.append(f"{s['id']} prime implicants: engine "
                                            f"{len(got_set)} oracle {len(want_pi)}")
                        for x in got_pi:
                            fe = m["ie_freq"] * math.prod(
                                [o.be_p[b] for b in x["events"]]
                                + [1 - o.be_p[b] for b in x["negated"]])
                            if not close(x["frequency_per_year"], fe):
                                problems.append(f"{s['id']} prime frequency {x}")
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
        part = et.get("partition", {})
        if (part.get("per_sequence_house_overrides") is not False
                or abs(part.get("sum_probability", 0.0) - 1.0) > 1e-9):
            problems.append(f"partition: engine reports {part}")
        cdf_eng = next(x["value_per_year"] for x in et["metrics"]
                       if x["id"] == "CDF")
        cdf_ora = sum(s["frequency_per_year"] for s in et["sequences"]
                      if s["end_state"] == "CD")
        if not close(cdf_eng, cdf_ora):
            problems.append(f"CDF aggregation: {cdf_eng} vs {cdf_ora}")

        # consequence-level importance (exact conditional frequencies)
        check_consequence_importance(m, o, et, sup_all, problems)

        # MEF export -> import -> requantify reproduces every sequence
        run_mef_stage(m, engine, d, et, problems)

        # results do not depend on the variable order, static or dynamic
        for tgt in ("FT-TEST", "ET-TEST"):
            order_invariant(engine, d, tgt, problems)
            reorder_invariant(engine, d, tgt, problems)
        shared_invariant(engine, d, "ET-TEST", problems)

        # garbage collection is invisible (FT and ET, cut sets included)
        for tgt in ("FT-TEST", "ET-TEST"):
            gc_invisible(engine, d, tgt, ["--mcs-limit", "100000"], problems)

        # 3) uncertainty propagation on the same logic
        if urng is not None and mc_samples:
            run_uncertainty_stage(m, o, urng, engine, mc_samples, problems,
                                  keep_dir)

        # 4) the same logic with a transfer into a second event tree
        if trng is not None:
            run_transfer_stage(m, o, trng, engine, problems, keep_dir,
                               mc_samples)

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
        # Likewise for the transfer variant.
        trng = random.Random((a.seed * 1_000_003 + i) ^ 0x7EA5_F3E5)
        keep = f"property-failure-seed{a.seed}-case{i}"
        problems = run_case(rng, a.engine, keep, urng, a.mc_samples, trng)
        if problems:
            failures += 1
            print(f"CASE {i}: FAIL (model preserved in {keep}/)")
            for p in problems:
                print("   ", p)
        else:
            print(f"CASE {i}: ok")
    fs = FACTOR_STATS
    if a.mc_samples:
        print(f"\nuncertain CCF factors: {fs['cases']} cases ({fs['staggered']} staggered, "
              f"{fs['non-staggered']} non-staggered; group sizes "
              f"{dict(sorted(fs['sizes'].items()))})")
    sh = SHARED_STATS
    print(f"\nshared-compiler stage: {sh['runs']} event trees compiled both ways, "
          f"{sh['byte_different']} with byte-different output (different BDDs)")
    r = REORDER_STATS
    print(f"reorder stage: {r['runs']} compilations with reordering forced, "
          f"{r['reordered']} reordered, {r['shrunk']} where sifting shrank the BDD")
    et = ET_TRUNC_STATS
    print(f"event-tree truncation stage: {et['trees']} coherent trees, {et['runs']} runs, "
          f"{et['rows']} rows checked ({et['wide']} with bounds of non-zero width), "
          f"{et['refused']} refused; {et['transfer_runs']} transfer-variant runs")
    t = TRUNC_STATS
    print(f"truncation stage: {t['trees']} coherent trees, {t['runs']} runs "
          f"({t['dropped']} dropping products, {t['gap']} with lower bound < exact "
          f"P(top)); {t['refused']} non-coherent trees refused")
    print(f"{a.cases - failures}/{a.cases} cases passed "
          f"(seed {a.seed})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
