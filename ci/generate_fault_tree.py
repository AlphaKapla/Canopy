#!/usr/bin/env python3
"""Generate a coherent fault tree as Open-PSA MEF XML, for benchmarking
quantification at full-scope size (roadmap v0.3 exit). Two structures:

--structure random (default): a random DAG, as in published engine
benchmarks. --structure psa: the shape of a linked fault-tree PSA, built
from systems, trains and components (described below).

The parameters follow those of the random fault-tree generator used in
published engine benchmarks (e.g. the INL SAPHSOLVE/SCRAM study: average
3 children per gate, AND/OR/k-of-n weighted 1:1:1, a 0.3 share of common
basic-event occurrences and a 0.1 share of common gate occurrences, about
2 parents per common event, probabilities uniform in [0.01, 0.05]). The
construction below is Canopy's own; only the parameter set is shared.

Construction (deterministic for a given seed):
  1. Basic events: N_u used once and N_c "common" ones used about
     parents_be times each, so that common occurrences are a common_be
     share of all basic-event occurrences.
  2. Gates G1..GM, G1 the top. Each Gi (i >= 2) gets one parent drawn
     uniformly from G1..G(i-1) (a random recursive tree, depth about
     ln M). A common_gate share of gate occurrences is added as extra
     parents of "common" gates, again drawn among lower-numbered gates,
     so every edge goes from a lower to a higher number: the graph is
     acyclic by construction, with the single root G1.
  3. When --gates is 0 the gate count follows from the average number of
     children: M * avg_children = (basic-event + gate occurrences).
  4. Basic-event occurrences are dealt to gates: first so that every gate
     has at least 2 children, then uniformly; no gate receives the same
     event twice, nor the same gate twice.
  5. Gate types by weight; k-of-n only on gates with 3+ children, with k
     uniform in [2, n-1]. No NOT/XOR: truncation needs coherent logic.
  6. Probabilities uniform in [pmin, pmax] (or log-uniform with
     --log-uniform), written with 6 significant digits so that every
     importer reads the same numbers.

--structure psa builds, for about N basic events (deterministic per seed):
  * components: 1 or 2 failure-mode basic events (probability log-uniform
    in [1e-4, 1e-2]); a two-mode component is an OR gate, a one-mode
    component is its basic event;
  * support systems (power, cooling): 2-3 trains, each the OR of 3-8
    components and of the system's common-cause event (log-uniform in
    [1e-6, 1e-4]) shared by all its trains;
  * front-line systems: 2-4 trains, each the OR of 3-8 components and of
    the matching train of 1-2 support systems (shared gates); the system
    fails when all trains fail (AND), or 2 of 3 (k-of-n, 40% of 3-train
    systems), or on its own common-cause event (OR);
  * accident sequences: AND of an initiator (log-uniform in [1e-3, 1e-1])
    and of the mitigation failure: one system, two systems, or one system
    OR two others (20/30/50%); every front-line system is used at least
    once;
  * the top event: OR of all sequences.
About 30 basic events per front-line system; 40,000 basic events give
about 21,000 gates. The --gates, --avg-children, --weights, --common-*,
--parents-*, --pmin/--pmax and --log-uniform options apply to the random
structure only.

Usage: generate_fault_tree.py OUT.xml --basic-events N [--structure random|psa] [--gates M]
         [--avg-children 3.0] [--weights AND OR KN] [--common-be 0.3]
         [--common-gate 0.1] [--parents-be 2] [--parents-gate 2]
         [--pmin 0.01] [--pmax 0.05] [--log-uniform] [--seed 1]
"""
import argparse
import math
import random
import sys
from xml.sax.saxutils import quoteattr


def common_split(total, share, parents):
    """(used once, common) counts so that the common items' occurrences,
    `parents` each, are `share` of all occurrences."""
    if share <= 0:
        return total, 0
    once = total / (1 + share / ((1 - share) * parents))
    n_once = int(round(once))
    return n_once, total - n_once


def mult(rng, mean):
    """An integer >= 2 with the given mean (random rounding)."""
    lo = max(2, int(math.floor(mean)))
    return lo + (1 if rng.random() < mean - math.floor(mean) and mean >= 2 else 0)


def generate(a):
    rng = random.Random(a.seed)
    n_be = a.basic_events
    if n_be < 2:
        sys.exit("generate_fault_tree: --basic-events must be >= 2")

    # 1. basic-event occurrences
    be_once, be_common = common_split(n_be, a.common_be, a.parents_be)
    be_occ = list(range(n_be))
    for b in range(be_once, n_be):
        be_occ += [b] * (mult(rng, a.parents_be) - 1)
    rng.shuffle(be_occ)

    # 3. gate count from the average number of children, when not given
    alpha = 1.0 / ((1 - a.common_gate) + a.common_gate / a.parents_gate)
    if a.gates:
        m = a.gates
    else:
        m = max(1, int(round((len(be_occ) - alpha) / (a.avg_children - alpha))))
    # 2. gate DAG: tree edges plus extra parents of common gates
    parents = {1: []}
    children = {g: [] for g in range(1, m + 1)}
    for g in range(2, m + 1):
        p = rng.randint(1, g - 1)
        parents[g] = [p]
        children[p].append(("g", g))
    _, g_common = common_split(m - 1, a.common_gate, a.parents_gate)
    pool = list(range(2, m + 1))
    rng.shuffle(pool)
    for g in sorted(pool[:g_common]):
        if g < 3:
            continue
        extra = mult(rng, a.parents_gate) - 1
        for _ in range(extra):
            for _try in range(20):
                p = rng.randint(1, g - 1)
                if p not in parents[g]:
                    parents[g].append(p)
                    children[p].append(("g", g))
                    break

    # 4. deal basic-event occurrences: at least 2 children each, then uniform
    have = {g: set() for g in children}
    for g in children:
        have[g] = {c for c in children[g]}
    order = []
    for g in range(1, m + 1):
        order += [g] * max(0, 2 - len(children[g]))
    rest = len(be_occ) - len(order)
    if rest < 0:
        sys.exit(f"generate_fault_tree: {n_be} basic events cannot give every one of "
                 f"{m} gates two children; lower --gates or raise --basic-events")
    order += [rng.randint(1, m) for _ in range(rest)]
    leftovers = []
    for g, b in zip(order, be_occ):
        if ("b", b) in have[g]:
            leftovers.append((g, b))
            continue
        have[g].add(("b", b))
        children[g].append(("b", b))
    for g, b in leftovers:          # place a duplicate elsewhere
        for _try in range(1000):
            h = rng.randint(1, m)
            if ("b", b) not in have[h]:
                have[h].add(("b", b))
                children[h].append(("b", b))
                break
        else:
            sys.exit("generate_fault_tree: could not place a basic event")
    # every gate needs two children (a duplicate may have left one short)
    for g in range(1, m + 1):
        while len(children[g]) < 2:
            b = rng.randrange(n_be)
            if ("b", b) not in have[g]:
                have[g].add(("b", b))
                children[g].append(("b", b))

    # 5. gate types
    w_and, w_or, w_kn = a.weights
    gates = {}
    for g in range(1, m + 1):
        kids = children[g]
        rng.shuffle(kids)
        n = len(kids)
        total = w_and + w_or + (w_kn if n >= 3 else 0)
        r = rng.random() * total
        if r < w_and:
            kind, k = "and", None
        elif r < w_and + w_or:
            kind, k = "or", None
        else:
            kind, k = "atleast", rng.randint(2, n - 1)
        gates[g] = (kind, k, kids)

    # 6. probabilities
    if a.log_uniform:
        lo, hi = math.log(a.pmin), math.log(a.pmax)
        probs = [float(f"{math.exp(rng.uniform(lo, hi)):.6g}") for _ in range(n_be)]
    else:
        probs = [float(f"{rng.uniform(a.pmin, a.pmax):.6g}") for _ in range(n_be)]
    return gates, probs


def generate_psa(a):
    """Gates {id: (kind, k, children)} and probabilities, PSA-shaped."""
    rng = random.Random(a.seed)
    logu = lambda lo, hi: float(f"{math.exp(rng.uniform(math.log(lo), math.log(hi))):.6g}")
    probs, gates = [], {}

    def be(lo, hi):
        probs.append(logu(lo, hi))
        return ("b", len(probs) - 1)

    def gate(kind, kids, k=None):
        g = len(gates) + 1
        gates[g] = (kind, k, kids)
        return ("g", g)

    def component():
        modes = [be(1e-4, 1e-2) for _ in range(rng.randint(1, 2))]
        return modes[0] if len(modes) == 1 else gate("or", modes)

    top_slot = gate("or", [])             # G1, filled in at the end
    n_front = max(2, round(a.basic_events / 30))
    n_support = max(1, n_front // 6)
    support = []                          # per support system: its train gates
    for _ in range(n_support):
        ccf = be(1e-6, 1e-4)
        support.append([gate("or", [component() for _ in range(rng.randint(3, 8))] + [ccf])
                        for _ in range(rng.randint(2, 3))])
    systems = []
    for _ in range(n_front):
        sups = rng.sample(range(n_support), min(n_support, rng.randint(1, 2)))
        trains = []
        for t in range(rng.randint(2, 4)):
            kids = [component() for _ in range(rng.randint(3, 8))]
            kids += [support[s][t % len(support[s])] for s in sups]
            trains.append(gate("or", kids))
        if len(trains) == 3 and rng.random() < 0.4:
            indep = gate("atleast", trains, k=2)
        else:
            indep = gate("and", trains)
        systems.append(gate("or", [indep, be(1e-6, 1e-4)]))

    n_seq = max(2, n_front // 2)
    order = list(range(n_front))
    rng.shuffle(order)
    sequences = []
    for q in range(n_seq):
        first = order[q] if q < len(order) else rng.randrange(n_front)
        others = [s for s in range(n_front) if s != first]
        r = rng.random()
        if r < 0.2 or len(others) < 2:
            mit = [systems[first]]
        elif r < 0.5:
            mit = [systems[first], systems[rng.choice(others)]]
        else:
            b, c = rng.sample(others, 2)
            mit = [gate("or", [systems[first], gate("and", [systems[b], systems[c]])])]
        sequences.append(gate("and", [be(1e-3, 1e-1)] + mit))
    # systems not yet used (more systems than sequences) join a sequence
    used = {c for g in gates.values() for c in g[2]}
    for s in systems:
        if s not in used:
            q = sequences[rng.randrange(len(sequences))][1]
            kind, k, kids = gates[q]
            gates[q] = (kind, k, kids + [s])
    gates[top_slot[1]] = ("or", None, sequences)
    return gates, probs


def name(kind, i):
    return f"G{i}" if kind == "g" else f"B{i + 1}"


def write(path, gates, probs, a):
    out = ['<?xml version="1.0"?>',
           "<!-- generate_fault_tree.py " + " ".join(
               f"{k}={v}" for k, v in (
                   [("structure", a.structure), ("basic_events", a.basic_events),
                    ("seed", a.seed)] + ([] if a.structure == "psa" else [
                    ("gates", a.gates), ("avg_children", a.avg_children),
                    ("weights", ",".join(str(w) for w in a.weights)),
                    ("common_be", a.common_be), ("common_gate", a.common_gate),
                    ("parents_be", a.parents_be), ("parents_gate", a.parents_gate),
                    ("pmin", a.pmin), ("pmax", a.pmax),
                    ("log_uniform", a.log_uniform)]))) + " -->",
           "<opsa-mef>",
           f'  <define-fault-tree name="Synthetic-{a.structure}-{a.basic_events}-s{a.seed}">']
    for g in sorted(gates):
        kind, k, kids = gates[g]
        args = "".join(f'<{"gate" if c[0] == "g" else "basic-event"} name="{name(*c)}"/>'
                       for c in kids)
        attr = f" min={quoteattr(str(k))}" if k is not None else ""
        out.append(f'    <define-gate name="G{g}"><{kind}{attr}>{args}</{kind}></define-gate>')
    out.append("  </define-fault-tree>")
    out.append("  <model-data>")
    for i, p in enumerate(probs):
        out.append(f'    <define-basic-event name="B{i + 1}"><float value="{p!r}"/>'
                   "</define-basic-event>")
    out.append("  </model-data>")
    out.append("</opsa-mef>")
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")


def stats(gates, probs):
    occ = sum(len(k) for _, _, k in gates.values())
    kinds = {}
    for kind, _, _ in gates.values():
        kinds[kind] = kinds.get(kind, 0) + 1
    return (f"{len(probs)} basic events, {len(gates)} gates "
            f"({', '.join(f'{v} {k}' for k, v in sorted(kinds.items()))}), "
            f"{occ / len(gates):.2f} children per gate")


def parse(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out")
    ap.add_argument("--basic-events", type=int, required=True)
    ap.add_argument("--structure", choices=["random", "psa"], default="random")
    ap.add_argument("--gates", type=int, default=0, help="0: derived from --avg-children")
    ap.add_argument("--avg-children", type=float, default=3.0)
    ap.add_argument("--weights", type=float, nargs=3, default=[1.0, 1.0, 1.0],
                    metavar=("AND", "OR", "KN"))
    ap.add_argument("--common-be", type=float, default=0.3)
    ap.add_argument("--common-gate", type=float, default=0.1)
    ap.add_argument("--parents-be", type=float, default=2.0)
    ap.add_argument("--parents-gate", type=float, default=2.0)
    ap.add_argument("--pmin", type=float, default=0.01)
    ap.add_argument("--pmax", type=float, default=0.05)
    ap.add_argument("--log-uniform", action="store_true",
                    help="draw probabilities log-uniformly in [pmin, pmax]")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args(argv)
    if not (0 <= a.common_be < 1 and 0 <= a.common_gate < 1):
        ap.error("--common-be and --common-gate must be in [0, 1)")
    if a.parents_be < 2 or a.parents_gate < 2:
        ap.error("--parents-be and --parents-gate must be >= 2")
    if not 0 < a.pmin <= a.pmax < 1:
        ap.error("need 0 < pmin <= pmax < 1")
    if a.avg_children <= 2:
        ap.error("--avg-children must be > 2")
    return a


def main(argv=None):
    a = parse(argv)
    gates, probs = generate_psa(a) if a.structure == "psa" else generate(a)
    write(a.out, gates, probs, a)
    print(f"{a.out}: {stats(gates, probs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
