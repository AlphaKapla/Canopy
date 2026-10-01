#!/usr/bin/env python3
"""Import an Open-PSA MEF XML model into the YAML format.

Usage: import_mef.py <in.xml> [<in2.xml> ...] <out-model-dir>
         [--ignore-event-trees] [--mission-time HOURS]

Several input files form one model, as SCRAM reads them: their
definitions share one name space (an event tree in one file may collect
gates of another, or link to its event trees).

Scope (FR-15, FR-51, FR-52):
  * fault trees: gates with and/or/not/xor/atleast (nand/nor rewritten),
    basic events with constant probabilities, house events (a missing
    <constant> is the MEF default, false); untyped <event name=...>
    references are resolved by what the name is defined as (gate, basic
    event, house event)
  * names and roles, as SCRAM resolves them: an element defined in a
    fault tree has that tree's name as base path; a private element
    (role="private", or inherited from a private fault tree) is known
    outside its tree only by its full path "Tree.name". A reference is
    looked up in the referencing tree's own scope first, then among
    public names (a name without a dot) or full paths (a dotted name)
  * constant expressions: basic-event probabilities, parameters, CCF
    distributions and factors, initiating-event frequencies and split
    fractions may be <float>, <int>, <parameter> references,
    <system-mission-time/> (the --mission-time given, in hours; MEF
    leaves it to the analysis) and add/sub/mul/div/neg over them. Each is
    evaluated to a number: a constant parameter is not imported as an
    entity, and the provenance of every value that came from an
    expression says so
  * distributions (FR-52): lognormal-deviate (mean, error factor, level
    — converted to Canopy's error factor at 0.95 when the level differs —
    or mu, sigma), gamma-deviate, beta-deviate and uniform-deviate import
    as Canopy uncertainty blocks, the point value being the mean (as in
    MEF and Canopy), where Canopy holds a distribution: a basic-event
    probability, an exponential's rate, a CCF group's total probability,
    an initiating-event frequency (inline only). A distribution held by a
    parameter becomes a Canopy parameter, so all its uses share one
    sample per trial, as in SCRAM
  * <exponential> (rate, time) basic events import as rate-mission
    (1 - exp(-rate x time)), the rate per hour (a parameter of another
    unit is refused), the time a constant expression in hours
  * CCF groups: alpha-factor and beta-factor (both the <factors><factor
    level> and the bare <factor> forms), alpha-factor groups imported
    with testing: non-staggered — the MEF / SCRAM convention for alpha
    factors (V&V F-1); members become basic events whose own value (the
    group total, never used) is replaced at expansion by Q_1
  * event trees whose forks have exactly two paths, both collecting
    either
      - a formula X and its negation not(X): the functional event's top
        gate is X (a pass-through gate when X is not a gate reference),
        the path collecting X is its failure, not(X) its success; or
      - one constant expression each, summing to 1 (split fractions,
        <collect-expression>): the failure path's fraction becomes a
        basic event (basic-events/split-fractions.yaml) behind a
        pass-through gate, and the other path's fraction is taken as
        1 - p. The failure path is the one whose state reads as a
        failure (failure, fail, no, false, f, ...) or, failing that, the
        other of one reading as a success; otherwise the second path,
        and the conversion says so
    A functional event that collects different formulas (or fractions)
    in different branches becomes one Canopy functional event per
    distinct one (FE-X, FE-X-2, ...): every path conjoins only its own
    collections, so frequencies are unchanged. Named branches
    (<define-branch>/<branch>) are expanded in place; a sequence whose
    definition links to another event tree (<event-tree name=...>)
    becomes a transfer to it. Each path to a sequence becomes one row;
    the MEF sequence name becomes its end state, and every end state of a
    non-transfer row gets a risk metric of the same name. The initiating
    event's frequency is its expression if present, else 1 /yr (the
    SCRAM dialect has none; sequence frequencies are then probabilities),
    and the conversion says so. Functional events must be forked in
    their declaration order, as MEF requires
  * NOT imported, refused explicitly: forks of any other shape (one path,
    three paths, paths collecting nothing), collect-formula and
    collect-expression mixed in one tree (SCRAM refuses this too),
    anything collected outside a fork path, set-house-event, rules, if,
    block and test instructions, cyclic named branches or links, MGL
    groups, normal and histogram distributions, a distribution used as a
    constant (inside arithmetic, as a split fraction, a mission time or a
    CCF factor), other time-dependent models (GLM, Weibull, periodic
    test), <define-component> scoping, and every top-level element other
    than fault trees, model data, CCF groups, event trees and initiating
    events

MEF names are mapped to the YAML ID grammar (upper-case, prefixed:
BE-/GT-/HE-/FT-/ET-/FE-/IE-/CCF-); a name that already has the right
prefix and grammar is kept as is (so Canopy's own exports round-trip to
the same IDs). A private element maps from its full path. The original
name is preserved in the entity label and in `external_ids: {mef: ...}`,
and the mapping is deterministic. Nested formulas import directly (the
YAML format is recursive).
"""
import argparse
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from statistics import NormalDist

import yaml


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


class Names:
    """Deterministic name -> YAML-ID mapping, collision-safe.

    `orig` is the memo key (any hashable); `base`, when given, is the
    preferred spelling instead of `orig` (used for synthesized entities,
    whose key must be unique but whose ID should read naturally)."""

    def __init__(self):
        self.maps = {}   # prefix -> {orig: new}
        self.used = set()

    def get(self, prefix, orig, base=None):
        m = self.maps.setdefault(prefix, {})
        if orig in m:
            return m[orig]
        text = orig if base is None else base
        if re.fullmatch(rf"{prefix}-[A-Z0-9][A-Z0-9-]*", text) and text not in self.used:
            self.used.add(text)
            m[orig] = text
            return text
        stem = re.sub(r"[^A-Z0-9-]", "-", text.upper()).strip("-")
        if not stem or not re.match(r"[A-Z0-9]", stem):
            stem = "X" + stem
        cand, i = f"{prefix}-{stem}", 1
        while cand in self.used:
            i += 1
            cand = f"{prefix}-{stem}-{i}"
        self.used.add(cand)
        m[orig] = cand
        return cand


SKIP = ("label", "attributes")


def kids(el):
    """Child elements that carry meaning (labels and attributes do not)."""
    return [c for c in el if c.tag not in SKIP]


class Entity:
    def __init__(self, kind, el, base, role, src):
        self.kind, self.el, self.base, self.role, self.src = kind, el, base, role, src
        self.name = el.get("name")
        self.path = f"{base}.{self.name}" if base else self.name
        # SCRAM's id: the name when public, the full path when private
        self.id = self.name if role == "public" else self.path
        self.cid = None   # Canopy ID (gates, basic and house events)


KINDS = ("gate", "basic-event", "house-event", "parameter")
EVENTS = ("gate", "basic-event", "house-event")
WORD = {"gate": "gate", "basic-event": "basic event",
        "house-event": "house event", "parameter": "parameter"}


class Registry:
    """MEF definitions by kind: public names and full paths (SCRAM's two
    tables), in definition order."""

    def __init__(self):
        self.public = {k: {} for k in KINDS}
        self.path = {k: {} for k in KINDS}
        self.ids = {}     # event id -> kind
        self.order = []

    def add(self, kind, el, base, role, src):
        name = el.get("name")
        if not name:
            die(f"<define-{kind}> without a name ({src})")
        if role not in ("public", "private"):
            die(f"{WORD[kind]} {name}: role {role!r} is neither public nor private")
        if role == "private" and not base:
            die(f"{WORD[kind]} {name}: private at model scope (MEF forbids it)")
        e = Entity(kind, el, base, role, src)
        if e.path in self.path[kind] or (role == "public" and name in self.public[kind]):
            die(f"{WORD[kind]} {e.path} defined twice")
        if kind in EVENTS:
            # SCRAM: an event's id (name if public, full path if private)
            # is unique across gates, basic events and house events
            if e.id in self.ids:
                die(f"event {e.id} defined twice (as {WORD[self.ids[e.id]]} "
                    f"and as {WORD[kind]})")
            self.ids[e.id] = kind
        self.path[kind][e.path] = e
        if role == "public":
            self.public[kind][name] = e
        self.order.append(e)
        return e

    def lookup(self, kinds, ref, base, where):
        """SCRAM's resolution: the referencing container's own scope, then
        public names (no dot) or full paths (dotted)."""
        if not ref:
            die(f"{where}: reference without a name")
        hits = []
        if base:
            hits = [self.path[k][f"{base}.{ref}"] for k in kinds
                    if f"{base}.{ref}" in self.path[k]]
        if not hits:
            table = self.public if "." not in ref else self.path
            hits = [table[k][ref] for k in kinds if ref in table[k]]
        if len(hits) == 1:
            return hits[0]
        if len(kinds) > 1:
            die(f"<event name=\"{ref}\"/> ({where}): defined as "
                f"{[h.kind for h in hits] or 'nothing'}; cannot resolve an "
                f"untyped reference")
        die(f"{where}: {WORD[kinds[0]]} {ref} referenced but never defined")


CONNECTIVES = {"and", "or", "xor", "not", "atleast", "nand", "nor"}


def int_attr(el, attr, where):
    try:
        return int(el.get(attr))
    except (TypeError, ValueError):
        die(f"{where}: <{el.tag}> attribute {attr}={el.get(attr)!r} is not an integer")


def import_formula(el, reg, base, where):
    tag = el.tag
    if tag in ("event", "gate", "basic-event", "house-event"):
        kinds = ("gate", "basic-event", "house-event") if tag == "event" else (tag,)
        return reg.lookup(kinds, el.get("name"), base, where).cid
    if tag not in CONNECTIVES:
        die(f"{where}: unsupported formula element <{tag}>")
    args = [import_formula(c, reg, base, where) for c in kids(el)]
    if not args:
        die(f"{where}: <{tag}> without arguments")
    if tag == "not":
        if len(args) != 1:
            die(f"{where}: <not> takes one argument, got {len(args)}")
        return {"not": args[0]}
    if tag == "atleast":
        return {"atleast": {"k": int_attr(el, "min", where), "of": args}}
    if tag == "nand":
        return {"not": {"and": args}}
    if tag == "nor":
        return {"not": {"or": args}}
    if len(args) == 1:
        return args[0]                    # degenerate single-operand gate
    return {tag: args}


DEVIATES = ("lognormal-deviate", "gamma-deviate", "beta-deviate",
            "uniform-deviate", "normal-deviate", "histogram")


class Evaluator:
    """Constant MEF expressions -> float; parameters evaluated once, in
    their own scope, with cycle detection. `<system-mission-time/>` is the
    --mission-time given (hours); distributions are not constants (see
    Distributions for where they import)."""

    ARITH = ("add", "sub", "mul", "div", "neg")

    def __init__(self, reg, mission_time=None):
        self.reg, self.memo, self.active = reg, {}, []
        self.mission_time = mission_time

    def __call__(self, el, base, where):
        tag = el.tag
        if tag in ("float", "int"):
            try:
                return float(el.get("value"))
            except (TypeError, ValueError):
                die(f"{where}: <{tag}> value {el.get('value')!r} is not a number")
        if tag == "parameter":
            return self.param(self.reg.lookup(("parameter",), el.get("name"), base, where))
        if tag == "system-mission-time":
            if self.mission_time is None:
                die(f"{where}: uses <system-mission-time/>, which MEF leaves to the "
                    f"analysis: give --mission-time HOURS (SCRAM's default is 8760)")
            return self.mission_time
        if tag in DEVIATES:
            die(f"{where}: the distribution <{tag}> is used as a constant (inside "
                f"arithmetic, a split fraction, a mission time or a CCF factor); "
                f"a distribution imports only as a basic-event probability, an "
                f"exponential's rate, a CCF group's total probability or an "
                f"initiating-event frequency")
        if tag not in self.ARITH:
            die(f"{where}: unsupported expression <{tag}>: only constant "
                f"expressions import (float, int, parameter, add, sub, mul, "
                f"div, neg, system-mission-time), distributions where Canopy "
                f"holds one, and exponential failure models")
        args = [self(c, base, where) for c in kids(el)]
        if tag == "neg":
            if len(args) != 1:
                die(f"{where}: <neg> takes one argument, got {len(args)}")
            return -args[0]
        if len(args) < 2:
            die(f"{where}: <{tag}> needs at least two arguments")
        v = args[0]
        for a in args[1:]:
            if tag == "add":
                v += a
            elif tag == "sub":
                v -= a
            elif tag == "mul":
                v *= a
            else:
                if a == 0:
                    die(f"{where}: division by zero")
                v /= a
        return v

    def param(self, e):
        if e.path in self.memo:
            return self.memo[e.path]
        if e.path in self.active:
            die("parameter cycle: " + " -> ".join(self.active[self.active.index(e.path):]
                                                  + [e.path]))
        expr = kids(e.el)
        if len(expr) != 1:
            die(f"parameter {e.path}: expected one expression, got {len(expr)}")
        self.active.append(e.path)
        v = self(expr[0], e.base, f"parameter {e.path}")
        self.active.pop()
        self.memo[e.path] = v
        return v

    def single(self, el, base, where):
        """(value, came-from-an-expression) of an element holding exactly
        one expression."""
        expr = kids(el)
        if len(expr) != 1:
            die(f"{where}: expected one constant expression, got "
                f"{[c.tag for c in expr] or 'none'}")
        return self(expr[0], base, where), expr[0].tag != "float"


Z95 = NormalDist().inv_cdf(0.95)


class Distributions:
    """MEF random deviates -> Canopy quantities with an `uncertainty`
    block, the point value being the distribution's mean (Canopy's and
    MEF's convention). A deviate held by a parameter becomes a Canopy
    parameter, so every use shares its samples (state-of-knowledge
    correlation, as in SCRAM, which samples a parameter once per trial)."""

    def __init__(self, reg, names, evaluate):
        self.reg, self.names, self.evaluate = reg, names, evaluate
        self.params = {}     # PAR-id -> parameter entry
        self.unit_of = {}    # PAR-id -> unit it was first used with

    def deviate(self, el, base, where):
        """(mean, Canopy uncertainty block, conversion note)"""
        tag, args = el.tag, [self.evaluate(c, base, where) for c in kids(el)]

        def need(cond, what):
            if not cond:
                die(f"{where}: <{tag}> {what} (arguments {args})")

        if tag == "lognormal-deviate":
            need(len(args) in (2, 3), "takes (mean, error factor, level) or (mu, sigma)")
            if len(args) == 3:
                mean, ef, level = args
                need(mean > 0 and ef > 1, "needs mean > 0 and error factor > 1")
                need(0.5 < level < 1, "needs a confidence level in (0.5, 1)")
                if level == 0.95:
                    return mean, {"distribution": "lognormal", "error_factor": ef}, ""
                ef95 = math.exp(Z95 / NormalDist().inv_cdf(level) * math.log(ef))
                return mean, {"distribution": "lognormal", "error_factor": ef95}, (
                    f"lognormal error factor {ef!r} at level {level!r} converted to "
                    f"{ef95!r} at 0.95 (same mean and sigma)")
            mu, sigma = args
            need(sigma > 0, "needs sigma > 0")
            mean, ef95 = math.exp(mu + sigma * sigma / 2), math.exp(Z95 * sigma)
            return mean, {"distribution": "lognormal", "error_factor": ef95}, (
                f"lognormal (mu {mu!r}, sigma {sigma!r}) imported as mean {mean!r}, "
                f"error factor {ef95!r} at 0.95")
        if tag == "gamma-deviate":
            need(len(args) == 2 and args[0] > 0 and args[1] > 0, "takes (k > 0, theta > 0)")
            return args[0] * args[1], {"distribution": "gamma", "shape": args[0],
                                       "scale": args[1]}, ""
        if tag == "beta-deviate":
            need(len(args) == 2 and args[0] > 0 and args[1] > 0, "takes (alpha > 0, beta > 0)")
            return args[0] / (args[0] + args[1]), {
                "distribution": "beta", "alpha": args[0], "beta": args[1]}, ""
        if tag == "uniform-deviate":
            need(len(args) == 2 and 0 <= args[0] < args[1], "takes (min >= 0, max > min)")
            return (args[0] + args[1]) / 2, {"distribution": "uniform", "lower": args[0],
                                             "upper": args[1]}, ""
        die(f"{where}: <{tag}> has no Canopy equivalent (lognormal, gamma, beta "
            f"and uniform import)")

    def quantity(self, el, base, where, unit, allow_param=True):
        """(Canopy quantity or parameter reference, how it was obtained:
        'float' | 'expression' | 'distribution')"""
        if el.tag in DEVIATES:
            mean, unc, note = self.deviate(el, base, where)
            return {"value": mean, "unit": unit, "uncertainty": unc}, "distribution"
        if el.tag == "parameter":
            e = self.reg.lookup(("parameter",), el.get("name"), base, where)
            chain = [e.path]
            while True:                       # follow parameter aliases
                body = kids(e.el)
                if len(body) == 1 and body[0].tag == "parameter":
                    e = self.reg.lookup(("parameter",), body[0].get("name"), e.base,
                                        f"parameter {e.path}")
                    if e.path in chain:       # V&V D-31: looped forever
                        die("parameter cycle: " + " -> ".join(
                            chain[chain.index(e.path):] + [e.path]))
                    chain.append(e.path)
                    continue
                break
            if len(body) == 1 and body[0].tag in DEVIATES:
                if not allow_param:
                    die(f"{where}: parameter {e.path} is a distribution, and this "
                        f"quantity cannot reference a parameter in Canopy (an "
                        f"initiating-event frequency); give the distribution inline")
                return {"param": self.param(e, unit, where)}, "distribution"
        v = self.evaluate(el, base, where)
        return {"value": v, "unit": unit}, ("float" if el.tag == "float" else "expression")

    def param(self, e, unit, where):
        pid = self.names.get("PAR", e.id)
        if pid in self.unit_of:
            if self.unit_of[pid] != unit:
                die(f"{where}: parameter {e.path} is used as {unit} here and as "
                    f"{self.unit_of[pid]} elsewhere")
            return pid
        mef_unit = e.el.get("unit")
        if unit == "per_hour" and mef_unit not in (None, "hours-1"):
            die(f"{where}: rate parameter {e.path} has unit {mef_unit!r}; only "
                f"hours-1 imports, against the mission time in hours")
        mean, unc, note = self.deviate(kids(e.el)[0], e.base, f"parameter {e.path}")
        self.unit_of[pid] = unit
        self.params[pid] = {
            "label": f"imported: {e.id}", "value": mean, "unit": unit,
            "uncertainty": unc, "external_ids": {"mef": e.id},
            "provenance": {"source": f"imported from {e.src}",
                           "justification": f"MEF <{kids(e.el)[0].tag}>, point value "
                           f"its mean (ci/import_mef.py)" + (f"; {note}" if note else "")}}
        return pid


def canon(f) -> str:
    return yaml.safe_dump(f, sort_keys=True)


# Path states read as the failure (or the success) of a split-fraction
# fork, case-insensitively; anything else falls back to the second path.
FAIL_STATES = {"failure", "failed", "fail", "fails", "f", "no", "n", "false",
               "ko", "down", "unavailable", "lost"}
SUCCESS_STATES = {"success", "succeeded", "succeeds", "ok", "works", "work",
                  "w", "yes", "y", "true", "up", "available", "s"}
FRACTION_TOLERANCE = 1e-9


def failure_path(states):
    """(index of the failure path among two, decided by the state names?)"""
    norm = [str(s or "").strip().lower() for s in states]
    f = [i for i, s in enumerate(norm) if s in FAIL_STATES]
    if len(f) == 1:
        return f[0], True
    s = [i for i, s in enumerate(norm) if s in SUCCESS_STATES]
    if len(s) == 1:
        return 1 - s[0], True
    return 1, False


def import_event_tree(et_el, ctx):
    """One MEF event tree -> {ET-id: event-tree dict}, or die explaining
    which construct has no Canopy equivalent."""
    ename, src = et_el.get("name"), ctx["src"][et_el]
    etid = ctx["et_id"][ename]
    reg, names, evaluate = ctx["reg"], ctx["names"], ctx["evaluate"]
    fe_decl, branches = [], {}
    for el in kids(et_el):
        if el.tag == "define-functional-event":
            if el.get("name") in fe_decl:
                die(f"event tree {ename}: functional event {el.get('name')} defined twice")
            fe_decl.append(el.get("name"))
        elif el.tag == "define-branch":
            if el.get("name") in branches:
                die(f"event tree {ename}: branch {el.get('name')} defined twice")
            branches[el.get("name")] = el
        elif el.tag not in ("define-sequence", "initial-state"):
            die(f"event tree {ename}: <{el.tag}> has no Canopy equivalent")
    init = et_el.find("initial-state")
    if init is None:
        die(f"event tree {ename}: no <initial-state>")
    paths = []           # ([(fe, variant key, failed: bool)], MEF sequence name)
    variants = {}        # fe -> [variant key] in encounter order
    info = {}            # (fe, key) -> ("formula", f) | ("fraction", p, ...)
    collect_kinds = set()
    COLLECT = ("collect-formula", "collect-expression")
    TERMINAL = ("fork", "sequence", "branch")

    def terminal(container):
        t = [k for k in kids(container) if k.tag in TERMINAL]
        others = [k for k in kids(container) if k.tag not in TERMINAL + COLLECT]
        if others:
            die(f"event tree {ename}: <{others[0].tag}> has no Canopy equivalent")
        if len(t) != 1:
            die(f"event tree {ename}: a path must end in exactly one fork, "
                f"sequence or branch")
        return t[0]

    def collected(br, fe):
        cols = [k for k in kids(br) if k.tag in COLLECT]
        if len(cols) != 1 or len(kids(cols[0])) != 1:
            die(f"event tree {ename}: each path of the fork on {fe} must "
                f"collect exactly one formula (or one expression)")
        c, where = cols[0], f"event tree {ename}, fork on {fe}"
        if c.tag == "collect-formula":
            return "formula", import_formula(kids(c)[0], reg, "", where)
        return "expression", evaluate(kids(c)[0], "", where)

    def walk(container, steps, stack, top):
        if top:
            for k in kids(container):
                if k.tag in COLLECT:
                    what = "formula" if k.tag == "collect-formula" else "expression"
                    die(f"event tree {ename}: {what} collected outside a fork path")
        t = terminal(container)
        if t.tag == "branch":
            b = t.get("name")
            if b not in branches:
                die(f"event tree {ename}: branch {b} is not defined in this tree")
            if b in stack:
                die(f"event tree {ename}: cyclic named branches: "
                    + " -> ".join(stack[stack.index(b):] + [b]))
            walk(branches[b], steps, stack + [b], True)
            return
        if t.tag == "sequence":
            paths.append((steps, t.get("name")))
            return
        fe = t.get("functional-event")
        if fe not in fe_decl:
            die(f"event tree {ename}: fork on undeclared functional event {fe}")
        if any(f == fe for f, _, _ in steps):
            die(f"event tree {ename}: {fe} forked twice on one path")
        if steps and fe_decl.index(fe) < fe_decl.index(steps[-1][0]):
            die(f"event tree {ename}: fork on {fe} after {steps[-1][0]}: "
                f"functional events must be forked in their declaration "
                f"order (MEF)")
        others = [k.tag for k in kids(t) if k.tag != "path"]
        if others:
            die(f"event tree {ename}: <{others[0]}> inside the fork on {fe}")
        brs = [k for k in kids(t) if k.tag == "path"]
        if len(brs) != 2:
            die(f"event tree {ename}: fork on {fe} has {len(brs)} paths; "
                f"Canopy needs exactly two, collecting a formula and its "
                f"negation (or two fractions summing to 1)")
        states = [br.get("state") for br in brs]
        if states[0] == states[1]:
            die(f"event tree {ename}: both paths of the fork on {fe} have "
                f"state {states[0]!r}")
        cols = [collected(br, fe) for br in brs]
        kinds = {k for k, _ in cols}
        collect_kinds.update(kinds)
        if len(collect_kinds) > 1:
            die(f"event tree {ename}: mixes collect-formula and "
                f"collect-expression (SCRAM refuses this too)")
        if kinds == {"formula"}:
            forms = [f for _, f in cols]
            pos = [f for f in forms if not (isinstance(f, dict) and "not" in f)]
            neg = [f["not"] for f in forms if isinstance(f, dict) and "not" in f]
            if len(pos) != 1 or len(neg) != 1 or canon(pos[0]) != canon(neg[0]):
                die(f"event tree {ename}: the fork on {fe} does not collect a "
                    f"formula and its negation")
            key = ("formula", canon(pos[0]))
            item = ("formula", pos[0])
            failed = [canon(f) == key[1] for f in forms]
        else:
            vals = [v for _, v in cols]
            fi, by_name = failure_path(states)
            p, q = vals[fi], vals[1 - fi]
            for v in vals:
                if not 0.0 <= v <= 1.0:
                    die(f"event tree {ename}: fraction {v} on the fork on {fe} "
                        f"is outside [0,1]")
            if abs(p + q - 1.0) > FRACTION_TOLERANCE:
                die(f"event tree {ename}: the fractions on the fork on {fe} sum "
                    f"to {p + q!r}, not 1; a Canopy functional event splits a "
                    f"path in two complementary branches")
            key = ("fraction", p)
            item = ("fraction", p, states[fi], states[1 - fi], q, by_name)
            failed = [i == fi for i in range(2)]
        if key not in variants.setdefault(fe, []):
            variants[fe].append(key)
            info[(fe, key)] = item
        for br, fl in zip(brs, failed):
            walk(br, steps + [(fe, key, fl)], stack, False)

    walk(init, [], [], True)

    fe_ids = Names()          # functional-event IDs are local to a tree
    fid_of = {}
    fes = {}
    notes = ctx["notes"]
    for fe in fe_decl:
        vs = variants.get(fe, [])
        if not vs:
            notes.append(f"event tree {ename}: functional event {fe} is never "
                         f"forked; dropped")
            continue
        first = fe_ids.get("FE", fe)
        if len(vs) > 1:
            notes.append(f"event tree {ename}: functional event {fe} collects "
                         f"{len(vs)} different formulas or fractions in "
                         f"different branches; imported as {len(vs)} "
                         f"functional events {first}, {first}-2, ...")
        for k, key in enumerate(vs, start=1):
            fid = first if k == 1 else fe_ids.get("FE", (fe, k), base=f"{first}-{k}")
            fid_of[(fe, key)] = fid
            item = info[(fe, key)]
            gate_key = ("functional-event gate", ename, fid)
            if item[0] == "formula":
                f = item[1]
                if isinstance(f, str) and f.startswith("GT-"):
                    top = f
                else:
                    top = names.get("GT", gate_key, base=f"FE-{fid[3:]}")
                    ctx["extra_gates"][top] = (
                        f, f"collected by functional event {fe} of event tree "
                           f"{ename}", f"{ename}/{fe}", src)
            else:
                _, p, fstate, sstate, q, by_name = item
                be = names.get("BE", ("split fraction", ename, fid),
                               base=f"SF-{etid[3:]}-{fid[3:]}")
                ctx["fractions"][be] = {
                    "p": p, "src": src, "mef": f"{ename}/{fe}",
                    "label": f"split fraction of functional event {fe} in event "
                             f"tree {ename}: path '{fstate}' (failure) collects "
                             f"{p!r}, path '{sstate}' (success) {q!r}, taken as "
                             f"1 - p"}
                if not by_name:
                    notes.append(f"event tree {ename}: fork on {fe}: neither "
                                 f"state {sstate!r} nor {fstate!r} reads as a "
                                 f"failure or a success; the second path "
                                 f"({fstate!r}) is imported as the failure")
                top = names.get("GT", gate_key, base=f"FE-{fid[3:]}")
                ctx["extra_gates"][top] = (
                    be, f"split fraction of functional event {fe} of event tree "
                        f"{ename}", f"{ename}/{fe}", src)
            fes[fid] = {"label": f"imported functional event {fe}"
                                 + (f" (variant {k} of {len(vs)})" if len(vs) > 1 else ""),
                        "top_gate": top, "external_ids": {"mef": fe}}
    seqs = {}
    short = etid[3:]
    for i, (steps, seq) in enumerate(paths, start=1):
        if seq not in ctx["sequences"]:
            die(f"event tree {ename}: sequence {seq} is not defined")
        done = {fid_of[(fe, key)]: fl for fe, key, fl in steps}
        path = {fid: ("failure" if done[fid] else "success")
                if fid in done else "bypassed" for fid in fes}
        row = {"path": path, "end_state": seq, "external_ids": {"mef": seq}}
        link = ctx["sequences"][seq]
        if link is not None:
            row["transfer"] = ctx["et_id"][link]
        seqs[f"SEQ-{short}-{i:02d}"] = row
    if not fes:
        die(f"event tree {ename}: no fork; Canopy event trees need at least "
            f"one functional event")
    tree = {"id": etid, "label": f"imported event tree {ename}",
            "functional_events": fes, "sequences": seqs,
            "external_ids": {"mef": ename}}
    ie = ctx["ie_of"].get(ename)
    if ie is None:
        notes.append(f"event tree {ename}: no initiating event; imported as a "
                     f"transfer-only tree")
    else:
        iname, freq, from_expr, isrc = ie
        if freq is None:
            notes.append(f"event tree {ename}: initiating event {iname} has no "
                         f"frequency in the file; set to 1 /yr, so sequence "
                         f"frequencies are probabilities")
        tree["initiating_event"] = {
            "id": names.get("IE", iname), "label": f"imported initiator {iname}",
            "frequency": {"value": 1.0, "unit": "per_year"} if freq is None else freq,
            "external_ids": {"mef": iname},
            "provenance": {"source": f"imported from {isrc}",
                           "justification": "frequency from the MEF file"
                           + (" (evaluated from a MEF expression or "
                              "distribution, point value its mean; the "
                              "expression itself is not kept)" if from_expr else "")
                           if freq is not None else
                           "not in the MEF file (SCRAM dialect): 1 /yr placeholder"}}
    return {etid: tree}


def main():
    ap = argparse.ArgumentParser(description="Import MEF XML into the YAML format.")
    ap.add_argument("paths", nargs="+", metavar="PATH",
                    help="<in.xml> [<in2.xml> ...] <out-model-dir>")
    ap.add_argument("--ignore-event-trees", action="store_true")
    ap.add_argument("--mission-time", type=float, metavar="HOURS",
                    help="value of <system-mission-time/> (MEF leaves it to the "
                         "analysis; SCRAM's default is 8760)")
    a = ap.parse_args()
    if len(a.paths) < 2:
        die("usage: import_mef.py <in.xml> [<in2.xml> ...] <out-model-dir> "
            "[--ignore-event-trees] [--mission-time HOURS]")
    if a.mission_time is not None and not 0 <= a.mission_time < float("inf"):
        die(f"--mission-time {a.mission_time} is not a finite non-negative number")
    xml_paths, out_dir, ignore_et = a.paths[:-1], a.paths[-1], a.ignore_event_trees
    files = []
    for p in xml_paths:
        try:
            root = ET.parse(p).getroot()
        except (OSError, ET.ParseError) as e:
            die(f"{p}: {e}")
        for el in root.iter():           # SCRAM trims attribute values
            for k, v in el.attrib.items():
                el.set(k, v.strip())
        files.append((os.path.basename(p), root))
    names = Names()
    reg = Registry()
    evaluate = Evaluator(reg, a.mission_time)
    dists = Distributions(reg, names, evaluate)
    notes = []

    # ---- registration: every definition, in document order -------------
    ft_names = []
    ccf_els = []         # (group element, base path, role, file)
    ets, ies = [], []    # (element, file)
    src = {}             # event-tree element -> file
    skipped_et = False
    for fname, root in files:
        if root.tag != "opsa-mef":
            die(f"{fname}: root element <{root.tag}>, expected <opsa-mef>")
        for el in kids(root):
            if el.tag == "define-fault-tree":
                ft = el.get("name")
                if ft in ft_names:
                    die(f"fault tree {ft} defined twice")
                ft_names.append(ft)
                ft_role = el.get("role") or "public"
                for c in kids(el):
                    kind = c.tag[len("define-"):]
                    if kind in KINDS:
                        reg.add(kind, c, ft, c.get("role") or ft_role, fname)
                    elif c.tag == "define-CCF-group":
                        ccf_els.append((c, ft, c.get("role") or ft_role, fname))
                    elif c.tag == "define-component":
                        die(f"components not supported by the importer ({fname})")
                    else:
                        die(f"fault tree {ft}: unsupported element <{c.tag}>")
            elif el.tag == "model-data":
                for c in kids(el):
                    kind = c.tag[len("define-"):]
                    if kind in ("basic-event", "house-event", "parameter"):
                        reg.add(kind, c, "", c.get("role") or "public", fname)
                    else:
                        die(f"model data: unsupported element <{c.tag}>")
            elif el.tag == "define-CCF-group":
                ccf_els.append((el, "", el.get("role") or "public", fname))
            elif el.tag in ("define-event-tree", "define-initiating-event"):
                if ignore_et:
                    skipped_et = True
                elif el.tag == "define-event-tree":
                    ets.append(el)
                    src[el] = fname
                else:
                    ies.append((el, fname))
            else:
                die(f"<{el.tag}> not supported by the importer ({fname})")
    if skipped_et:
        print("note: event trees present and skipped "
              "(--ignore-event-trees)", file=sys.stderr)
    ccf_members = {}     # member entity path -> group name
    for grp, base, role, fname in ccf_els:
        members = grp.find("members")
        if members is None:
            die(f"CCF group {grp.get('name')}: no <members>")
        for be in kids(members):
            if be.tag != "basic-event":
                die(f"CCF group {grp.get('name')}: member <{be.tag}>")
            path = f"{base}.{be.get('name')}" if base else be.get("name")
            if path in reg.path["basic-event"] or (
                    role == "public" and be.get("name") in reg.public["basic-event"]):
                die(f"CCF member {be.get('name')} is also defined as a basic event")
            e = reg.add("basic-event", be, base, role, fname)
            ccf_members[e.path] = grp.get("name")

    # Canopy IDs, in definition order
    prefix = {"gate": "GT", "basic-event": "BE", "house-event": "HE"}
    for e in reg.order:
        if e.kind in prefix:
            e.cid = names.get(prefix[e.kind], e.id)

    # ---- values ----------------------------------------------------------
    bes = {}             # BE-id -> record
    house = {}           # HE-id -> (bool, entity)
    evaluated = 0
    for e in reg.order:
        where = f"{WORD[e.kind]} {e.path}"
        if e.kind == "basic-event" and e.path not in ccf_members:
            body = kids(e.el)
            if len(body) != 1:
                die(f"{where}: expected one constant expression, got "
                    f"{[c.tag for c in body] or 'none'}")
            if body[0].tag == "exponential":
                args = kids(body[0])
                if len(args) != 2:
                    die(f"{where}: <exponential> takes (rate, time), got {len(args)} "
                        f"arguments")
                rate, how = dists.quantity(args[0], e.base, where, "per_hour")
                t = evaluate(args[1], e.base, where)
                if "value" in rate and not rate["value"] >= 0:
                    die(f"{where}: rate {rate['value']} is negative")
                if not 0 <= t < float("inf"):
                    die(f"{where}: time {t} is not a finite non-negative number")
                fm = {"type": "rate-mission", "rate": rate,
                      "mission_time": {"value": t, "unit": "hour"}}
                how = "exponential" + (", distribution" if how == "distribution" else "")
            else:
                q, how = dists.quantity(body[0], e.base, where, "per_demand")
                v = q["value"] if "value" in q else dists.params[q["param"]]["value"]
                if not 0.0 <= v <= 1.0:
                    die(f"basic event {e.path}: probability {v} outside [0,1]")
                fm = {"type": "probability", "value": q}
            evaluated += how != "float"
            bes[e.cid] = {"fm": fm, "entity": e, "how": how}
        elif e.kind == "house-event":
            body = kids(e.el)
            if not body:
                house[e.cid] = (False, e)          # the MEF default
            elif len(body) == 1 and body[0].tag == "constant" and \
                    body[0].get("value") in ("true", "false"):
                house[e.cid] = (body[0].get("value") == "true", e)
            else:
                die(f"house event {e.path}: only <constant value=\"true|false\"/> "
                    f"is supported")

    # CCF groups (alpha-factor, beta-factor; MEF / SCRAM convention:
    # non-staggered alpha factors)
    ccf = {}
    for grp, base, role, fname in ccf_els:
        gname, model = grp.get("name"), grp.get("model")
        if model not in ("alpha-factor", "beta-factor"):
            die(f"CCF group {gname}: model {model!r} not supported "
                f"(alpha-factor, beta-factor)")
        where = f"CCF group {gname}"
        mpaths = [f"{base}.{be.get('name')}" if base else be.get("name")
                  for be in kids(grp.find("members"))]
        members = [reg.path["basic-event"][m].cid for m in mpaths]
        dist = grp.find("distribution")
        if dist is None:
            die(f"{where}: no <distribution>")
        body = kids(dist)
        if len(body) != 1:
            die(f"{where}: expected one expression in <distribution>, got {len(body)}")
        qt_q, how = dists.quantity(body[0], base, where, "per_demand")
        qt = qt_q["value"] if "value" in qt_q else dists.params[qt_q["param"]]["value"]
        evaluated += how != "float"
        if not 0.0 <= qt <= 1.0:
            die(f"{where}: total probability {qt} outside [0,1]")
        n = len(members)
        if n < 2:
            die(f"{where}: {n} member(s); a CCF group needs at least two")
        factors = {}
        for fac in grp.iter("factor"):
            v, from_expr = evaluate.single(fac, base, where)
            evaluated += from_expr
            if not 0.0 <= v <= 1.0:
                die(f"{where}: factor {v} outside [0,1]")
            if fac.get("level") is None:
                level = 0                  # bare <factor>: beta-factor only
            else:
                level = int_attr(fac, "level", where)
            if level in factors:
                die(f"{where}: two factors for level {level}")
            factors[level] = v
        if model == "alpha-factor":
            if sorted(factors) != list(range(1, n + 1)):
                die(f"CCF group {gname}: alpha-factor needs levels 1..{n}, "
                    f"got {sorted(factors)}")
            fmap = {f"alpha_{k}": factors[k] for k in range(1, n + 1)}
        else:
            if len(factors) != 1:
                die(f"CCF group {gname}: beta-factor needs one factor")
            if next(iter(factors)) not in (0, n):
                die(f"CCF group {gname}: the beta factor's level must be the "
                    f"number of members ({n}), got {next(iter(factors))}")
            fmap = {"beta": next(iter(factors.values()))}
        cid = names.get("CCF", gname if role == "public" else f"{base}.{gname}")
        ccf[cid] = {"label": f"imported CCF group {gname}", "model": model,
                    "members": members,
                    "total_probability": qt_q,
                    "factors": fmap, "external_ids": {"mef": gname},
                    "src": fname, "how": how}
        if model == "alpha-factor":
            ccf[cid]["testing"] = "non-staggered"   # the MEF convention
        for m, bid in zip(mpaths, members):
            bes[bid] = {"fm": {"type": "probability",
                               "value": {"value": qt, "unit": "per_demand"}},
                        "entity": reg.path["basic-event"][m], "how": "float", "ccf": gname}
        notes.append(f"CCF group {gname}: imported as {model}"
                     + (", testing non-staggered (the MEF convention)"
                        if model == "alpha-factor" else ""))

    # ---- gates -----------------------------------------------------------
    gates = {}           # GT-id -> formula
    gate_meta = {}       # GT-id -> (label, mef id)
    for e in reg.order:
        if e.kind == "gate":
            formula = kids(e.el)
            if len(formula) != 1:
                die(f"gate {e.path}: expected one formula, got {len(formula)}")
            gates[e.cid] = import_formula(formula[0], reg, e.base, f"gate {e.path}")
            gate_meta[e.cid] = (f"imported: {e.id}", e.id)

    # ---- event trees -----------------------------------------------------
    trees = {}
    ctx = {"reg": reg, "names": names, "evaluate": evaluate, "notes": notes,
           "src": src, "extra_gates": {}, "fractions": {}, "et_id": {},
           "sequences": {}, "ie_of": {}}
    for el in ets:
        ename = el.get("name")
        if ename in ctx["et_id"]:
            die(f"event tree {ename} defined twice")
        ctx["et_id"][ename] = names.get("ET", ename)
    for el in ets:
        for s in el.findall("define-sequence"):
            sname = s.get("name")
            if sname in ctx["sequences"]:
                die(f"sequence {sname} defined twice")
            body = kids(s)
            link = None
            if body:
                if len(body) != 1 or body[0].tag != "event-tree":
                    die(f"sequence {sname}: <{body[0].tag}> in a sequence "
                        f"definition has no Canopy equivalent (only one "
                        f"<event-tree> link imports)")
                link = body[0].get("name")
                if link not in ctx["et_id"]:
                    die(f"sequence {sname}: links to undefined event tree {link}")
            ctx["sequences"][sname] = link
    # links between trees must not cycle
    graph = {el.get("name"): sorted({ctx["sequences"][s.get("name")]
                                     for s in el.findall("define-sequence")
                                     if ctx["sequences"][s.get("name")]})
             for el in ets}
    state = {}

    def visit(u, stack):
        state[u] = 1
        for v in graph[u]:
            if state.get(v) == 1:
                die("cyclic event-tree links: "
                    + " -> ".join(stack[stack.index(v):] + [v]))
            if v not in state:
                visit(v, stack + [v])
        state[u] = 2

    for u in sorted(graph):
        if u not in state:
            visit(u, [u])
    for ie, fname in ies:
        iname, tree = ie.get("name"), ie.get("event-tree")
        if not tree:
            notes.append(f"initiating event {iname} has no event tree; ignored")
            continue
        if tree not in ctx["et_id"]:
            die(f"initiating event {iname}: event tree {tree} is not defined")
        if tree in ctx["ie_of"]:
            die(f"event tree {tree}: two initiating events "
                f"({ctx['ie_of'][tree][0]}, {iname}); Canopy has one per tree")
        body = kids(ie)
        if len(body) > 1:
            die(f"initiating event {iname}: expected one expression, got {len(body)}")
        if body:
            freq, how = dists.quantity(body[0], "", f"initiating event {iname}",
                                       "per_year", allow_param=False)
            from_expr = how != "float"
            evaluated += from_expr
            if not 0.0 <= freq["value"] < float("inf"):
                die(f"initiating event {iname}: frequency {freq['value']} is not a "
                    f"finite non-negative number")
        else:
            freq, from_expr = None, False
        ctx["ie_of"][tree] = (iname, freq, from_expr, fname)
    for el in ets:
        trees.update(import_event_tree(el, ctx))
    for be, rec in ctx["fractions"].items():
        bes[be] = {"p": rec["p"], "fraction": rec}
    for top, (f, label, mef, fname) in ctx["extra_gates"].items():
        gates[top] = f
        gate_meta[top] = (label, mef)
    if evaluated:
        notes.append(f"{evaluated} value(s) evaluated from MEF expressions "
                     f"(parameters, arithmetic) or taken from distributions (point "
                     f"value the mean); constant parameters are evaluated in place, "
                     f"{len(dists.params)} distribution parameter(s) imported as "
                     f"Canopy parameters")

    # referenced-but-undefined events, roots
    def refs(f, acc):
        if isinstance(f, str):
            acc.add(f)
        elif "not" in f:
            refs(f["not"], acc)
        elif "atleast" in f:
            for c in f["atleast"]["of"]:
                refs(c, acc)
        else:
            for c in next(iter(f.values())):
                refs(c, acc)
        return acc

    referenced = set()
    for f in gates.values():
        refs(f, referenced)
    if not gates:
        die("no gates and no event trees: nothing to quantify")
    roots = [g for g in gates if g not in referenced]
    if not roots:
        die("no root gate (all gates are referenced -> cycle?)")

    # ---- write the model -------------------------------------------------
    HOW = {"expression": "; value evaluated from a MEF expression (parameters, "
                         "arithmetic), the expression itself is not kept",
           "distribution": "; a MEF distribution, imported with its Canopy "
                           "equivalent, point value its mean",
           "exponential": "; MEF <exponential> imported as rate-mission "
                          "(1 - exp(-rate x time)), its time evaluated",
           "exponential, distribution": "; MEF <exponential> imported as "
                          "rate-mission (1 - exp(-rate x time)), its time evaluated, "
                          "its rate a MEF distribution with point value its mean"}

    def prov(fname, how=None):
        return {"source": f"imported from {fname}",
                "justification": "MEF import (ci/import_mef.py)" + HOW.get(how, "")}

    os.makedirs(f"{out_dir}/basic-events", exist_ok=True)
    os.makedirs(f"{out_dir}/fault-trees", exist_ok=True)
    dump = lambda p, o: open(p, "w").write(
        yaml.safe_dump(o, sort_keys=True, default_flow_style=False))
    model_id = re.sub(r"[^A-Z0-9-]", "-",
                      (ft_names[0] if ft_names else "IMPORT").upper())
    end_states = sorted({s["end_state"] for t in trees.values()
                         for s in t["sequences"].values() if "transfer" not in s})
    includes = {"parameters": ["parameters.yaml"],
                "basic_events": ["basic-events/*.yaml"],
                "fault_trees": ["fault-trees/*.yaml"],
                "house_events": ["house-events.yaml"]}
    if trees:
        includes["event_trees"] = ["event-trees/*.yaml"]
        os.makedirs(f"{out_dir}/event-trees", exist_ok=True)
        for tid, t in trees.items():
            dump(f"{out_dir}/event-trees/{tid.lower()}.yaml", {"event_tree": t})
    if ccf:
        includes["ccf_groups"] = ["ccf-groups.yaml"]
        dump(f"{out_dir}/ccf-groups.yaml", {"ccf_groups": {
            c: {**{k: v for k, v in g.items() if k not in ("src", "how")},
                "provenance": prov(g["src"], g["how"])} for c, g in ccf.items()}})
    inputs = ", ".join(f for f, _ in files)
    dump(f"{out_dir}/model.yaml", {
        "schema_version": "0.1.0",
        "model": {"id": model_id,
                  "name": f"imported from {inputs}",
                  "risk_metrics": [{"id": es, "label": f"end state {es}",
                                    "end_states": [es]} for es in end_states]},
        "includes": includes})
    dump(f"{out_dir}/parameters.yaml", {"parameters": dists.params})
    dump(f"{out_dir}/house-events.yaml", {"house_events": {
        h: {"label": f"imported house event", "default": v,
            "provenance": prov(e.src)} for h, (v, e) in house.items()}})

    def be_entry(b, r):
        if "fraction" in r:
            fm = {"type": "probability",
                  "value": {"value": r["p"], "unit": "per_demand"}}
            fr = r["fraction"]
            return {"label": fr["label"], "failure_model": fm,
                    "external_ids": {"mef": fr["mef"]},
                    "provenance": {"source": f"imported from {fr['src']}",
                                   "justification": "MEF collect-expression on "
                                   "the failure path of the fork (ci/import_mef.py)"}}
        e = r["entity"]
        return {"label": f"imported: {e.id}" + (
                    f" (member of CCF group {r['ccf']}: its own value, the "
                    f"group total, is replaced by Q_1 at expansion)"
                    if "ccf" in r else ""),
                "failure_model": r["fm"], "external_ids": {"mef": e.id},
                "provenance": prov(e.src, r["how"])}

    dump(f"{out_dir}/basic-events/imported.yaml", {"basic_events": {
        b: be_entry(b, r) for b, r in bes.items() if "fraction" not in r}})
    fractions = {b: be_entry(b, r) for b, r in bes.items() if "fraction" in r}
    if fractions:
        dump(f"{out_dir}/basic-events/split-fractions.yaml",
             {"basic_events": fractions})
    fts = {"FT-MAIN": {"label": f"imported: {ft_names[0]}" if ft_names else
                       "imported functional-event formulas",
                       "top_gate": roots[0],
                       "gates": {g: {"label": gate_meta[g][0],
                                     "formula": f,
                                     "external_ids": {"mef": gate_meta[g][1]}}
                                 for g, f in gates.items()}}}
    for i, r in enumerate(roots[1:], start=2):
        fts[f"FT-ROOT-{i}"] = {"label": f"additional root {gate_meta[r][1]}",
                               "top_gate": r, "gates": {}}
    dump(f"{out_dir}/fault-trees/imported.yaml", {"fault_trees": fts})

    for n in notes:
        print(f"note: {n}", file=sys.stderr)
    print(f"imported {', '.join(xml_paths)}: {len(gates)} gates, {len(bes)} basic "
          f"events, {len(roots)} root(s) -> {out_dir} "
          f"(top: FT-MAIN / {roots[0]}); {len(trees)} event tree(s), "
          f"{len(ccf)} CCF group(s)")


if __name__ == "__main__":
    main()
