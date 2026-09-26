#!/usr/bin/env python3
"""Import an Open-PSA MEF XML model into the YAML format.

Usage: import_mef.py <in.xml> <out-model-dir> [--ignore-event-trees]

Scope (v2):
  * fault trees: gates with and/or/not/xor/atleast (nand/nor rewritten),
    basic events with constant float probabilities, house events with
    constant values; untyped <event name=...> references are resolved by
    what the name is defined as (gate, basic event, house event)
  * CCF groups: alpha-factor and beta-factor with <float> distribution and
    factors (both the <factors><factor level> and the bare <factor> forms),
    alpha-factor groups imported with testing: non-staggered — the MEF /
    SCRAM convention for alpha factors (V&V F-1); members become basic events whose own value
    (the group total, never used) is replaced at expansion by Q_1
  * event trees whose forks have exactly two paths, one collecting a
    formula X and the other its negation not(X): the functional event's
    top gate is X (a pass-through gate GT-FE-<name> when X is not a gate
    reference), the path collecting X is its failure, not(X) its success,
    and a functional event not forked on a path is bypassed. Each path to
    a <sequence> becomes one sequence row; the MEF sequence name becomes
    its end state, and every end state gets a risk metric of the same
    name. The initiating event's frequency is its <float> if present,
    else 1 /yr (the SCRAM dialect has none; sequence frequencies are then
    probabilities), and the conversion says so
  * NOT imported, refused explicitly: forks of any other shape, formulas
    collected outside a fork path, set-house-event and other instructions,
    named branches, event-tree transfers, MGL groups, parameters and
    expressions beyond <float>, <define-component> scoping

MEF names are mapped to the YAML ID grammar (upper-case, prefixed:
BE-/GT-/HE-/FT-/ET-/FE-/IE-/CCF-); a name that already has the right
prefix and grammar is kept as is (so Canopy's own exports round-trip to
the same IDs). The original name is preserved in the entity label and in
`external_ids: {mef: ...}`, and the mapping is deterministic. Nested
formulas import directly (the YAML format is recursive).
"""
import os
import re
import sys
import xml.etree.ElementTree as ET

import yaml


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


class Names:
    """Deterministic MEF-name -> YAML-ID mapping, collision-safe."""

    def __init__(self):
        self.maps = {}   # prefix -> {orig: new}
        self.used = set()

    def get(self, prefix, orig):
        m = self.maps.setdefault(prefix, {})
        if orig in m:
            return m[orig]
        if re.fullmatch(rf"{prefix}-[A-Z0-9][A-Z0-9-]*", orig) and orig not in self.used:
            self.used.add(orig)
            m[orig] = orig
            return orig
        base = re.sub(r"[^A-Z0-9-]", "-", orig.upper()).strip("-")
        if not base or not re.match(r"[A-Z0-9]", base):
            base = "X" + base
        cand, i = f"{prefix}-{base}", 1
        while cand in self.used:
            i += 1
            cand = f"{prefix}-{base}-{i}"
        self.used.add(cand)
        m[orig] = cand
        return cand


CONNECTIVES = {"and", "or", "xor", "not", "atleast", "nand", "nor"}


# Names defined in the file, by kind (filled before formulas are read), to
# resolve MEF's untyped <event name="..."/> references.
DEFINED = {"gate": set(), "basic-event": set(), "house-event": set()}


def import_formula(el, names):
    tag = el.tag
    if tag == "event":
        n = el.get("name")
        kinds = [k for k, v in DEFINED.items() if n in v]
        if len(kinds) != 1:
            die(f"<event name=\"{n}\"/>: defined as {kinds or 'nothing'}; "
                f"cannot resolve an untyped reference")
        tag = kinds[0]
    if tag == "gate":
        return names.get("GT", el.get("name"))
    if tag == "basic-event":
        return names.get("BE", el.get("name"))
    if tag == "house-event":
        return names.get("HE", el.get("name"))
    if tag not in CONNECTIVES:
        die(f"unsupported formula element <{tag}>")
    kids = [import_formula(c, names) for c in el]
    if tag == "not":
        assert len(kids) == 1
        return {"not": kids[0]}
    if tag == "atleast":
        return {"atleast": {"k": int(el.get("min")), "of": kids}}
    if tag == "nand":
        return {"not": {"and": kids}}
    if tag == "nor":
        return {"not": {"or": kids}}
    if len(kids) == 1:
        return kids[0]                    # degenerate single-operand gate
    return {tag: kids}


def canon(f) -> str:
    return yaml.safe_dump(f, sort_keys=True)


def import_event_tree(et_el, names, ie_of, extra_gates, notes):
    """One MEF event tree -> {ET-id: event-tree dict}, or die explaining
    which construct has no Canopy equivalent."""
    ename = et_el.get("name")
    etid = names.get("ET", ename)
    for bad in ("define-branch", "branch", "event-tree", "set-house-event",
                "collect-expression", "if", "block", "rule"):
        if et_el.find(f".//{bad}") is not None:
            die(f"event tree {ename}: <{bad}> has no Canopy equivalent")
    fe_names = [fe.get("name") for fe in et_el.findall("define-functional-event")]
    init = et_el.find("initial-state")
    if init is None:
        die(f"event tree {ename}: no <initial-state>")
    paths = []           # (list of (fe name, failed: bool), MEF sequence name)
    top_of = {}          # fe name -> canonical YAML formula of its failure

    def terminal(container):
        t = [k for k in container if k.tag in ("fork", "sequence")]
        others = [k for k in container if k.tag not in ("fork", "sequence",
                                                         "collect-formula")]
        if others:
            die(f"event tree {ename}: unsupported <{others[0].tag}>")
        if len(t) != 1:
            die(f"event tree {ename}: a path must end in exactly one fork or "
                f"sequence")
        return t[0]

    def walk(container, steps):
        t = terminal(container)
        if t.tag == "sequence":
            paths.append((steps, t.get("name")))
            return
        fe = t.get("functional-event")
        if fe not in fe_names:
            die(f"event tree {ename}: fork on undeclared functional event {fe}")
        if any(f == fe for f, _ in steps):
            die(f"event tree {ename}: {fe} forked twice on one path")
        branches = t.findall("path")
        if len(branches) != 2:
            die(f"event tree {ename}: fork on {fe} has {len(branches)} paths; "
                f"Canopy needs exactly two, collecting a formula and its "
                f"negation")
        forms = []
        for br in branches:
            col = [k for k in br if k.tag == "collect-formula"]
            if len(col) != 1 or len(col[0]) != 1:
                die(f"event tree {ename}: each path of the fork on {fe} must "
                    f"collect exactly one formula")
            forms.append(import_formula(col[0][0], names))
        pos = [f for f in forms if not (isinstance(f, dict) and "not" in f)]
        neg = [f["not"] for f in forms if isinstance(f, dict) and "not" in f]
        if len(pos) != 1 or len(neg) != 1 or canon(pos[0]) != canon(neg[0]):
            die(f"event tree {ename}: the fork on {fe} does not collect a "
                f"formula and its negation")
        x = canon(pos[0])
        if top_of.setdefault(fe, (x, pos[0]))[0] != x:
            die(f"event tree {ename}: {fe} collects different formulas in "
                f"different branches; a Canopy functional event has one top gate")
        for br, f in zip(branches, forms):
            walk(br, steps + [(fe, canon(f) == x)])

    if [k for k in init if k.tag == "collect-formula"]:
        die(f"event tree {ename}: formula collected outside a fork path")
    walk(init, [])

    fes = {}
    for fe in fe_names:
        fid = names.get("FE", fe)
        if fe in top_of:
            f = top_of[fe][1]
            if isinstance(f, str) and f.startswith("GT-"):
                top = f
            else:
                top = names.get("GT", f"FE-{fe}")
                extra_gates[top] = f
            fes[fid] = {"label": f"imported functional event {fe}",
                        "top_gate": top, "external_ids": {"mef": fe}}
        else:
            notes.append(f"event tree {ename}: functional event {fe} is never "
                         f"forked; dropped")
    seqs = {}
    short = etid[3:]
    for i, (steps, seq) in enumerate(paths, start=1):
        done = dict(steps)
        path = {names.get("FE", fe): ("failure" if done[fe] else "success")
                if fe in done else "bypassed"
                for fe in fe_names if fe in top_of}
        seqs[f"SEQ-{short}-{i:02d}"] = {"path": path, "end_state": seq,
                                        "external_ids": {"mef": seq}}
    tree = {"id": etid, "label": f"imported event tree {ename}",
            "functional_events": fes, "sequences": seqs,
            "external_ids": {"mef": ename}}
    ie = ie_of.get(ename)
    if ie is None:
        notes.append(f"event tree {ename}: no initiating event; imported as a "
                     f"transfer-only tree")
    else:
        iname, freq = ie
        if freq is None:
            notes.append(f"event tree {ename}: initiating event {iname} has no "
                         f"frequency in the file; set to 1 /yr, so sequence "
                         f"frequencies are probabilities")
        tree["initiating_event"] = {
            "id": names.get("IE", iname), "label": f"imported initiator {iname}",
            "frequency": {"value": 1.0 if freq is None else freq, "unit": "per_year"},
            "external_ids": {"mef": iname},
            "provenance": {"source": "MEF import",
                           "justification": "frequency from the MEF file"
                           if freq is not None else
                           "not in the MEF file (SCRAM dialect): 1 /yr placeholder"}}
    return {etid: tree}


def main():
    xml_path, out_dir = sys.argv[1], sys.argv[2]
    root = ET.parse(xml_path).getroot()
    names = Names()

    ignore_et = "--ignore-event-trees" in sys.argv
    unsupported = [
        ("define-component", "components"),
        ("define-parameter", "parameters"),
    ]
    if ignore_et and root.find(".//define-event-tree") is not None:
        print("note: event trees present and skipped "
              "(--ignore-event-trees)", file=sys.stderr)
    for bad, msg in unsupported:
        if root.find(f".//{bad}") is not None:
            die(f"{msg} not supported by the importer ({xml_path})")

    for el in root.iter("define-gate"):
        DEFINED["gate"].add(el.get("name"))
    for el in root.iter("define-basic-event"):
        DEFINED["basic-event"].add(el.get("name"))
    for el in root.iter("define-house-event"):
        DEFINED["house-event"].add(el.get("name"))
    for grp in root.iter("define-CCF-group"):
        for be in grp.iter("basic-event"):
            DEFINED["basic-event"].add(be.get("name"))

    gates = {}           # GT-id -> formula
    gate_label = {}      # GT-id -> original name
    be_prob = {}         # BE-id -> float
    be_label = {}
    house = {}           # HE-id -> bool
    ft_names = []

    def import_be(el):
        bid = names.get("BE", el.get("name"))
        be_label[bid] = el.get("name")
        expr = [c for c in el if c.tag != "label"]
        if len(expr) != 1 or expr[0].tag != "float":
            die(f"basic event {el.get('name')}: only <float> "
                f"expressions supported")
        p = float(expr[0].get("value"))
        if not 0.0 <= p <= 1.0:
            die(f"basic event {el.get('name')}: probability {p} "
                f"outside [0,1]")
        be_prob[bid] = p

    for ft in root.findall("define-fault-tree"):
        ft_names.append(ft.get("name"))
        for el in ft:
            if el.tag == "define-gate":
                gid = names.get("GT", el.get("name"))
                gate_label[gid] = el.get("name")
                formula = [c for c in el if c.tag != "label"]
                assert len(formula) == 1
                gates[gid] = import_formula(formula[0], names)
            elif el.tag == "define-basic-event":
                import_be(el)
            elif el.tag == "define-house-event":
                hid = names.get("HE", el.get("name"))
                const = el.find("constant")
                house[hid] = const.get("value") == "true"
            elif el.tag != "label":
                die(f"unsupported fault-tree element <{el.tag}>")
    md = root.find("model-data")
    if md is not None:
        for el in md:
            if el.tag == "define-basic-event":
                import_be(el)
            elif el.tag == "define-house-event":
                hid = names.get("HE", el.get("name"))
                house[hid] = el.find("constant").get("value") == "true"

    # CCF groups (alpha-factor, beta-factor; MEF / SCRAM convention:
    # non-staggered alpha factors)
    ccf = {}
    ccf_member_of = {}   # BE-id -> MEF group name
    notes = []
    for grp in root.iter("define-CCF-group"):
        gname, model = grp.get("name"), grp.get("model")
        if model not in ("alpha-factor", "beta-factor"):
            die(f"CCF group {gname}: model {model!r} not supported "
                f"(alpha-factor, beta-factor)")
        members = [names.get("BE", be.get("name"))
                   for be in grp.find("members").iter("basic-event")]
        dist = grp.find("distribution")
        fl = dist.find("float") if dist is not None else None
        if fl is None or len(dist) != 1:
            die(f"CCF group {gname}: only a <float> distribution is supported")
        qt = float(fl.get("value"))
        factors = {}
        for fac in grp.iter("factor"):
            v = fac.find("float")
            if v is None:
                die(f"CCF group {gname}: only <float> factors are supported")
            factors[int(fac.get("level", "0"))] = float(v.get("value"))
        n = len(members)
        if model == "alpha-factor":
            if sorted(factors) != list(range(1, n + 1)):
                die(f"CCF group {gname}: alpha-factor needs levels 1..{n}, "
                    f"got {sorted(factors)}")
            fmap = {f"alpha_{k}": factors[k] for k in range(1, n + 1)}
        else:
            if len(factors) != 1:
                die(f"CCF group {gname}: beta-factor needs one factor")
            fmap = {"beta": next(iter(factors.values()))}
        cid = names.get("CCF", gname)
        ccf[cid] = {"label": f"imported CCF group {gname}", "model": model,
                    "members": members,
                    "total_probability": {"value": qt, "unit": "per_demand"},
                    "factors": fmap, "external_ids": {"mef": gname}}
        if model == "alpha-factor":
            ccf[cid]["testing"] = "non-staggered"   # the MEF convention
        for mname, bid in zip([be.get("name") for be in grp.find("members")
                               .iter("basic-event")], members):
            if bid in be_prob:
                die(f"CCF member {mname} is also defined as a basic event")
            be_prob[bid] = qt
            be_label[bid] = mname
            ccf_member_of[bid] = gname
        notes.append(f"CCF group {gname}: imported as {model}"
                     + (", testing non-staggered (the MEF convention)"
                        if model == "alpha-factor" else ""))

    # event trees
    ets = {}
    extra_gates = {}
    if not ignore_et:
        ie_of = {}
        for ie in root.iter("define-initiating-event"):
            fl = ie.find("float")
            ie_of[ie.get("event-tree")] = (ie.get("name"),
                                           float(fl.get("value")) if fl is not None else None)
        for et in root.iter("define-event-tree"):
            ets.update(import_event_tree(et, names, ie_of, extra_gates, notes))

    # referenced-but-undefined events, undefined gates
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

    gates.update(extra_gates)
    gate_label.update({g: f"functional-event formula {g}" for g in extra_gates})
    referenced = set()
    for f in gates.values():
        refs(f, referenced)
    for r in referenced:
        if r.startswith("BE-") and r not in be_prob:
            die(f"basic event {r} referenced but never defined")
        if r.startswith("GT-") and r not in gates:
            die(f"gate {r} referenced but never defined")

    roots = [g for g in gates if g not in referenced]
    if not roots:
        die("no root gate (all gates are referenced -> cycle?)")

    # write the model
    prov = {"source": f"imported from {os.path.basename(xml_path)}",
            "justification": "MEF import (ci/import_mef.py)"}
    os.makedirs(f"{out_dir}/basic-events", exist_ok=True)
    os.makedirs(f"{out_dir}/fault-trees", exist_ok=True)
    dump = lambda p, o: open(p, "w").write(
        yaml.safe_dump(o, sort_keys=True, default_flow_style=False))
    model_id = re.sub(r"[^A-Z0-9-]", "-",
                      (ft_names[0] if ft_names else "IMPORT").upper())
    end_states = sorted({s["end_state"] for t in ets.values()
                         for s in t["sequences"].values()})
    includes = {"parameters": ["parameters.yaml"],
                "basic_events": ["basic-events/*.yaml"],
                "fault_trees": ["fault-trees/*.yaml"],
                "house_events": ["house-events.yaml"]}
    if ets:
        includes["event_trees"] = ["event-trees/*.yaml"]
        os.makedirs(f"{out_dir}/event-trees", exist_ok=True)
        for tid, t in ets.items():
            dump(f"{out_dir}/event-trees/{tid.lower()}.yaml", {"event_tree": t})
    if ccf:
        includes["ccf_groups"] = ["ccf-groups.yaml"]
        dump(f"{out_dir}/ccf-groups.yaml", {"ccf_groups": {
            c: {**g, "provenance": prov} for c, g in ccf.items()}})
    dump(f"{out_dir}/model.yaml", {
        "schema_version": "0.1.0",
        "model": {"id": model_id,
                  "name": f"imported from {os.path.basename(xml_path)}",
                  "risk_metrics": [{"id": es, "label": f"end state {es}",
                                    "end_states": [es]} for es in end_states]},
        "includes": includes})
    dump(f"{out_dir}/parameters.yaml", {"parameters": {}})
    dump(f"{out_dir}/house-events.yaml", {"house_events": {
        h: {"label": f"imported house event", "default": v,
            "provenance": prov} for h, v in house.items()}})
    dump(f"{out_dir}/basic-events/imported.yaml", {"basic_events": {
        b: {"label": f"imported: {be_label[b]}" + (
                f" (member of CCF group {ccf_member_of[b]}: its own value, the "
                f"group total, is replaced by Q_1 at expansion)"
                if b in ccf_member_of else ""),
            "failure_model": {"type": "probability",
                              "value": {"value": p, "unit": "per_demand"}},
            "external_ids": {"mef": be_label[b]},
            "provenance": prov} for b, p in be_prob.items()}})
    fts = {"FT-MAIN": {"label": f"imported: {ft_names[0]}",
                       "top_gate": roots[0],
                       "gates": {g: {"label": f"imported: {gate_label[g]}",
                                     "formula": f,
                                     "external_ids": {"mef": gate_label[g]}}
                                 for g, f in gates.items()}}}
    for i, r in enumerate(roots[1:], start=2):
        fts[f"FT-ROOT-{i}"] = {"label": f"additional root {gate_label[r]}",
                               "top_gate": r, "gates": {}}
    dump(f"{out_dir}/fault-trees/imported.yaml", {"fault_trees": fts})

    for n in notes:
        print(f"note: {n}", file=sys.stderr)
    print(f"imported {xml_path}: {len(gates)} gates, {len(be_prob)} basic "
          f"events, {len(roots)} root(s) -> {out_dir} "
          f"(top: FT-MAIN / {roots[0]}); {len(ets)} event tree(s), "
          f"{len(ccf)} CCF group(s)")


if __name__ == "__main__":
    main()
