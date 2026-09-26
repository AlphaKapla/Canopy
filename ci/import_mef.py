#!/usr/bin/env python3
"""Import an Open-PSA MEF XML fault-tree model into the YAML format.

Usage: import_mef.py <in.xml> <out-model-dir> [--ignore-event-trees]

Scope (v1, sized for benchmark suites like Aralia):
  * fault trees: gates with and/or/not/xor/atleast (nand/nor rewritten),
    basic events with constant float probabilities, house events with
    constant values
  * NOT imported: event trees, CCF groups, parameters/expressions beyond
    <float>, <define-component> scoping — the importer fails loudly on
    each rather than importing silently wrong

MEF names are mapped to the YAML ID grammar (upper-case, prefixed:
BE-/GT-/HE-/FT-); the original name is preserved in the entity label and
(for basic events and gates) in `external_ids: {mef: ...}`, and
the mapping is deterministic. Nested formulas import directly (the YAML
format is recursive; no auxiliary gates needed).
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


def import_formula(el, names):
    tag = el.tag
    if tag == "gate":
        return names.get("GT", el.get("name"))
    if tag == "basic-event" or tag == "event":
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


def main():
    xml_path, out_dir = sys.argv[1], sys.argv[2]
    root = ET.parse(xml_path).getroot()
    names = Names()

    ignore_et = "--ignore-event-trees" in sys.argv
    unsupported = [
        ("define-CCF-group", "CCF groups"),
        ("define-component", "components"),
        ("define-parameter", "parameters"),
    ]
    if not ignore_et:
        unsupported.append(("define-event-tree", "event trees"))
    elif root.find(".//define-event-tree") is not None:
        print("note: event trees present and skipped "
              "(--ignore-event-trees)", file=sys.stderr)
    for bad, msg in unsupported:
        if root.find(f".//{bad}") is not None:
            die(f"{msg} not supported by importer v1 ({xml_path})")

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
    dump(f"{out_dir}/model.yaml", {
        "schema_version": "0.1.0",
        "model": {"id": model_id,
                  "name": f"imported from {os.path.basename(xml_path)}",
                  "risk_metrics": []},
        "includes": {"parameters": ["parameters.yaml"],
                     "basic_events": ["basic-events/*.yaml"],
                     "fault_trees": ["fault-trees/*.yaml"],
                     "house_events": ["house-events.yaml"]}})
    dump(f"{out_dir}/parameters.yaml", {"parameters": {}})
    dump(f"{out_dir}/house-events.yaml", {"house_events": {
        h: {"label": f"imported house event", "default": v,
            "provenance": prov} for h, v in house.items()}})
    dump(f"{out_dir}/basic-events/imported.yaml", {"basic_events": {
        b: {"label": f"imported: {be_label[b]}",
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

    print(f"imported {xml_path}: {len(gates)} gates, {len(be_prob)} basic "
          f"events, {len(roots)} root(s) -> {out_dir} "
          f"(top: FT-MAIN / {roots[0]})")


if __name__ == "__main__":
    main()
