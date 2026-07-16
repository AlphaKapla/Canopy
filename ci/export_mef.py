#!/usr/bin/env python3
"""Export the YAML model to Open-PSA Model Exchange Format (MEF) XML.

Usage: export_mef.py <model-dir> <out.xml> [--expand-ccf]

Two modes:
  default       CCF groups are exported as MEF <define-CCF-group> elements,
                letting the consuming engine perform its own expansion —
                the strongest cross-check of our expansion, but sensitive
                to the other engine's staggered/non-staggered convention.
  --expand-ccf  CCF groups are expanded by this exporter using the same
                NUREG/CR-5485 staggered/non-staggered math as the Rust
                engine, and the expanded events are exported directly.
                Use this for exact numerical comparison.

Point values only: uncertainty distributions are not exported (the
cross-verification target is point probabilities and cut sets).
"""
import glob
import json
import math
import os
import sys
from math import comb
from xml.sax.saxutils import escape, quoteattr

import yaml


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------
def load_model(md):
    params = yaml.safe_load(open(os.path.join(md, "parameters.yaml")))[
        "parameters"]

    def rv(q):
        if isinstance(q, dict) and "param" in q:
            return params[q["param"]]["value"]
        return q["value"]

    bes = {}
    for f in sorted(glob.glob(os.path.join(md, "basic-events/*.yaml"))):
        for bid, be in yaml.safe_load(open(f))["basic_events"].items():
            fm = be["failure_model"]
            t = fm["type"]
            if t == "probability":
                p = rv(fm["value"])
            elif t == "rate-mission":
                p = 1.0 - math.exp(-rv(fm["rate"]) * rv(fm["mission_time"]))
            elif t == "rate-repair":
                rm = rv(fm["rate"]) * rv(fm["mttr"])
                p = rm / (1.0 + rm)
            elif t == "rate-periodic-test":
                x = rv(fm["rate"]) * rv(fm["test_interval"])
                p = 0.0 if x == 0.0 else 1.0 - (1.0 - math.exp(-x)) / x
            else:
                die(f"{bid}: cannot export failure model {t}")
            bes[bid] = p

    house = {h: d["default"] for h, d in yaml.safe_load(
        open(os.path.join(md, "house-events.yaml")))["house_events"].items()}

    fts, gates = {}, {}
    for f in sorted(glob.glob(os.path.join(md, "fault-trees/*.yaml"))):
        for fid, ft in yaml.safe_load(open(f))["fault_trees"].items():
            fts[fid] = ft
            gates.update({g: d["formula"] for g, d in ft["gates"].items()})

    ets = {}
    for f in sorted(glob.glob(os.path.join(md, "event-trees/*.yaml"))):
        et = yaml.safe_load(open(f))["event_tree"]
        ets[et["id"]] = et

    ccf = {}
    cpath = os.path.join(md, "ccf-groups.yaml")
    if os.path.exists(cpath):
        for gid, g in (yaml.safe_load(open(cpath)) or {}).get(
                "ccf_groups", {}).items():
            ccf[gid] = {**g, "qt": rv(g["total_probability"])}

    return bes, house, fts, gates, ets, ccf


# ---------------------------------------------------------------------------
# CCF expansion (mirrors engine/src/model.rs, NUREG/CR-5485)
# ---------------------------------------------------------------------------
def expand_ccf(ccf, bes, gates):
    for gid, g in ccf.items():
        m, n, qt = g["members"], len(g["members"]), g["qt"]
        if g["model"] == "alpha-factor":
            al = [g["factors"][f"alpha_{k}"] for k in range(1, n + 1)]
        elif g["model"] == "beta-factor":
            b = g["factors"]["beta"]
            al = [1.0 - b] + [0.0] * (n - 2) + [b]
        else:
            die(f"{gid}: model {g['model']} unsupported for expansion")
        if g.get("testing", "staggered") == "staggered":
            qk = [al[k-1] / comb(n-1, k-1) * qt for k in range(1, n+1)]
        else:
            at = sum((i+1) * a for i, a in enumerate(al))
            qk = [k * al[k-1] / (at * comb(n-1, k-1)) * qt
                  for k in range(1, n+1)]
        for x in m:
            bes[x] = qk[0]
        sub = {}
        for mask in range(1, 1 << n):
            idxs = [i for i in range(n) if mask >> i & 1]
            if len(idxs) < 2:
                continue
            cid = f"BE-{gid}-" + "-".join(str(i+1) for i in idxs)
            bes[cid] = qk[len(idxs) - 1]
            for i in idxs:
                sub.setdefault(m[i], []).append(cid)

        def rw(f):
            if isinstance(f, str):
                return {"or": [f] + sub[f]} if f in sub else f
            (op, a), = f.items()
            if op == "not":
                return {"not": rw(a)}
            if op == "atleast":
                return {"atleast": {"k": a["k"],
                                    "of": [rw(x) for x in a["of"]]}}
            return {op: [rw(x) for x in a]}
        for k in gates:
            gates[k] = rw(gates[k])


# ---------------------------------------------------------------------------
# XML emission
# ---------------------------------------------------------------------------
class Xml:
    def __init__(self):
        self.buf = ['<?xml version="1.0" encoding="UTF-8"?>']
        self.depth = 0

    def line(self, s):
        self.buf.append("  " * self.depth + s)

    def open(self, tag, **attrs):
        a = "".join(f" {k.replace('_','-')}={quoteattr(str(v))}"
                    for k, v in attrs.items())
        self.line(f"<{tag}{a}>")
        self.depth += 1

    def close(self, tag):
        self.depth -= 1
        self.line(f"</{tag}>")

    def leaf(self, tag, **attrs):
        a = "".join(f" {k.replace('_','-')}={quoteattr(str(v))}"
                    for k, v in attrs.items())
        self.line(f"<{tag}{a}/>")

    def __str__(self):
        return "\n".join(self.buf) + "\n"


def emit_operand(x, ref, gates, house):
    if ref.startswith("BE-"):
        x.leaf("basic-event", name=ref)
    elif ref.startswith("HE-"):
        x.leaf("house-event", name=ref)
    elif ref.startswith("GT-"):
        x.leaf("gate", name=ref)
    else:
        die(f"unknown reference {ref}")


def emit_formula(x, f, gates, house):
    if isinstance(f, str):
        emit_operand(x, f, gates, house)
        return
    (op, a), = f.items()
    if op in ("and", "or", "xor"):
        x.open(op)
        for c in a:
            emit_formula(x, c, gates, house)
        x.close(op)
    elif op == "not":
        x.open("not")
        emit_formula(x, a, gates, house)
        x.close("not")
    elif op == "atleast":
        x.open("atleast", min=a["k"])
        for c in a["of"]:
            emit_formula(x, c, gates, house)
        x.close("atleast")
    else:
        die(f"unknown operator {op}")


def emit_event_tree(x, et, fes_order):
    """Reconstruct nested forks from the flat sequence table."""
    seqs = [{"id": sid, **s} for sid, s in sorted(et["sequences"].items())]
    x.open("define-event-tree", name=et["id"])
    for fe in fes_order:
        x.leaf("define-functional-event", name=fe)
    for s in seqs:
        x.leaf("define-sequence", name=s["id"])
    x.open("initial-state")

    def collect(fe, failed):
        top = et["functional_events"][fe]["top_gate"]
        x.open("collect-formula")
        if failed:
            x.leaf("gate", name=top)
        else:
            x.open("not")
            x.leaf("gate", name=top)
            x.close("not")
        x.close("collect-formula")

    def branch(group, fi):
        if fi == len(fes_order):
            assert len(group) == 1, "duplicate sequence paths"
            x.leaf("sequence", name=group[0]["id"])
            return
        fe = fes_order[fi]
        outs = {s["path"][fe] for s in group}
        if outs == {"bypassed"}:
            branch(group, fi + 1)
            return
        if "bypassed" in outs:
            die(f"{fe}: mixed bypassed/questioned outcomes in one branch")
        x.open("fork", functional_event=fe)
        for state in ("success", "failure"):
            sub = [s for s in group if s["path"][fe] == state]
            if not sub:
                continue
            x.open("path", state=state)
            collect(fe, state == "failure")
            branch(sub, fi + 1)
            x.close("path")
        x.close("fork")

    branch(seqs, 0)
    x.close("initial-state")
    x.close("define-event-tree")


def main():
    md, out = sys.argv[1], sys.argv[2]
    do_expand = "--expand-ccf" in sys.argv
    bes, house, fts, gates, ets, ccf = load_model(md)
    if do_expand and ccf:
        expand_ccf(ccf, bes, gates)
        ccf = {}

    manifest = yaml.safe_load(open(os.path.join(md, "model.yaml")))
    x = Xml()
    x.open("opsa-mef", name=manifest["model"]["id"].replace(".", "-"))

    # event trees + initiating events. NOTE: SCRAM's MEF grammar takes no
    # frequency expression on an initiating event, so sequence results are
    # PROBABILITIES; multiply by the IE frequency externally when comparing.
    for et_id, et in sorted(ets.items()):
        emit_event_tree(x, et, list(et["functional_events"]))
        x.leaf("define-initiating-event",
               name=et["initiating_event"]["id"], event_tree=et_id)

    # fault trees (gates only; events live in model-data).
    # SCRAM's MEF grammar wants FLAT gates: one connective per gate with
    # reference-only operands. Flatten associative same-op nesting
    # (or-in-or, and-in-and, e.g. from CCF substitution), and hoist any
    # other composite operand into an auxiliary gate.
    aux_n = [0]

    def normalize(fid, gid, f, aux_out):
        if isinstance(f, str):
            return f
        (op, a), = f.items()
        if op == "not":
            c = normalize(fid, gid, a, aux_out)
            return {"not": hoist(fid, c, aux_out)}
        if op == "atleast":
            k, of = a["k"], a["of"]
            # SCRAM requires min < number of arguments: rewrite the
            # degenerate votes to their equivalent connectives.
            if k >= len(of):
                return normalize(fid, gid, {"and": of}, aux_out)
            if k == 1:
                return normalize(fid, gid, {"or": of}, aux_out)
            kids = [normalize(fid, gid, c, aux_out) for c in of]
            kids = [hoist(fid, c, aux_out) for c in kids]
            return {"atleast": {"k": k, "of": kids}}
        kids = []
        for c in a:
            c = normalize(fid, gid, c, aux_out)
            if isinstance(c, dict) and op in c:      # flatten same op
                kids.extend(c[op])
            elif isinstance(c, str) or (
                    "not" in c and isinstance(c["not"], str)):
                kids.append(c)
            else:
                kids.append(hoist(fid, c, aux_out))
        # Deduplicate operands (sound for the idempotent and/or; CCF
        # substitution of same-group members creates repeats, which our
        # BDD absorbs but SCRAM's MEF reader rejects). xor/atleast never
        # receive duplicates: their composite children are hoisted.
        if op in ("and", "or"):
            seen, uniq = set(), []
            for c in kids:
                key = json.dumps(c, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    uniq.append(c)
            kids = uniq
        if len(kids) == 1:
            return kids[0]
        return {op: kids}

    def hoist(fid, f, aux_out):
        if isinstance(f, str) or ("not" in f and
                                  isinstance(f["not"], str)):
            return f
        aux_n[0] += 1
        gid = f"GT-AUX-{aux_n[0]:03d}"
        aux_out[gid] = f
        return gid

    for fid, ft in sorted(fts.items()):
        x.open("define-fault-tree", name=fid)
        pending = {g: gates[g] for g in sorted(ft["gates"])}
        done = set()
        while pending:
            gid, f = next(iter(pending.items()))
            del pending[gid]
            if gid in done:
                continue
            done.add(gid)
            aux_out = {}
            nf = normalize(fid, gid, f, aux_out)
            x.open("define-gate", name=gid)
            emit_formula(x, nf, gates, house)
            x.close("define-gate")
            pending.update(aux_out)
        x.close("define-fault-tree")

    # CCF groups (raw mode)
    for gid, g in sorted(ccf.items()):
        x.open("define-CCF-group", name=gid, model=g["model"])
        x.open("members")
        for m in g["members"]:
            x.leaf("basic-event", name=m)
        x.close("members")
        x.open("distribution")
        x.leaf("float", value=repr(float(g["qt"])))
        x.close("distribution")
        x.open("factors")
        n = len(g["members"])
        for k in range(1, n + 1):
            key = f"alpha_{k}"
            if g["model"] == "beta-factor":
                continue
            x.open("factor", level=k)
            x.leaf("float", value=repr(float(g["factors"][key])))
            x.close("factor")
        if g["model"] == "beta-factor":
            x.open("factor", level=2)
            x.leaf("float", value=repr(float(g["factors"]["beta"])))
            x.close("factor")
        x.close("factors")
        x.close("define-CCF-group")

    # model data: basic events + house events. CCF group members are
    # DEFINED by their group in MEF (probability from the group's
    # distribution and factors), so they must not be re-declared here.
    ccf_members = {m for g in ccf.values() for m in g["members"]}
    x.open("model-data")
    for bid, p in sorted(bes.items()):
        if bid in ccf_members:
            continue
        x.open("define-basic-event", name=bid)
        x.leaf("float", value=repr(float(p)))
        x.close("define-basic-event")
    for hid, v in sorted(house.items()):
        x.open("define-house-event", name=hid)
        x.leaf("constant", value="true" if v else "false")
        x.close("define-house-event")
    x.close("model-data")

    x.close("opsa-mef")
    open(out, "w").write(str(x))
    print(f"exported {out}: {len(fts)} fault tree(s), {len(ets)} event "
          f"tree(s), {len(bes)} basic events, {len(ccf)} CCF group(s)"
          f"{' [CCF pre-expanded]' if do_expand else ''}")


if __name__ == "__main__":
    main()
