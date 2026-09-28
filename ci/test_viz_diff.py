#!/usr/bin/env python3
"""Tests for the viewer's base-vs-head diff (FR-40): viz/build_viz.py --base.

A small synthetic base model and a head model derived from it by a known
list of edits — every entity kind (basic event, gate, fault tree, house
event, event tree, sequence) and every status (added, removed, changed) —
are built into viewers, and the diff embedded in the page must be exactly
the expected one: the same (kind, id, status, changed fields), the base
values of changed and removed entities, no other entry. Also: sequence
frequencies and metrics base -> head from the two results files, a change
below 1e-9 relative not flagged and one above flagged, probabilities and
frequencies not compared when only one side has results (with a note),
identical models giving an empty diff, output without --base carrying no
diff, and byte-identical output under different hash seeds.

The page's JavaScript is exercised manually in a browser (V&V §4.2); this
test covers the data the page renders.

Usage: python ci/test_viz_diff.py [--engine PATH]
"""
import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUILD = os.path.join(ROOT, "viz", "build_viz.py")
PROV = {"source": "ci/test_viz_diff.py", "justification": "fixture"}


def be(p, label=None):
    return {"label": label or "event", "system": "S", "provenance": dict(PROV),
            "failure_model": {"type": "probability", "value": {"value": p, "unit": "per_demand"}}}


BASE = {
    "basic_events": {"BE-A": be(0.01, "event a"), "BE-B": be(0.02, "event b"),
                     "BE-C": be(0.03, "event c"), "BE-D": be(0.04, "event d"),
                     "BE-GONE": be(0.05, "to be removed")},
    "house_events": {"HE-X": {"label": "x", "default": False, "provenance": PROV},
                     "HE-GONE": {"label": "to be removed", "default": True, "provenance": PROV}},
    "fault_trees": {
        "FT-ONE": {"label": "one", "top_gate": "GT-T1", "gates": {
            "GT-T1": {"label": "t1", "formula": {"or": ["BE-A", "GT-G1"]}},
            "GT-G1": {"label": "g1", "formula": {"and": ["BE-B", "BE-C"]}}}},
        "FT-TWO": {"label": "two", "top_gate": "GT-T2", "gates": {
            "GT-T2": {"label": "t2", "formula": {"or": ["BE-D", "BE-GONE", "GT-OLD"]}},
            "GT-OLD": {"label": "to be removed", "formula": {"and": ["BE-A", "HE-GONE"]}}}}},
    "event_tree": {
        "id": "ET-E", "label": "tree",
        "initiating_event": {"id": "IE-E", "label": "ie", "provenance": PROV,
                             "frequency": {"value": 1e-2, "unit": "per_year"}},
        "functional_events": {"FE-1": {"label": "fe one", "top_gate": "GT-T1"},
                              "FE-2": {"label": "fe two", "top_gate": "GT-T2"}},
        "sequences": {
            "SEQ-1": {"path": {"FE-1": "success", "FE-2": "success"}, "end_state": "OK"},
            "SEQ-2": {"path": {"FE-1": "success", "FE-2": "failure"}, "end_state": "CD"},
            "SEQ-3": {"path": {"FE-1": "failure", "FE-2": "bypassed"}, "end_state": "CD"}}},
}


def head_edits(m):
    """The edits and the diff they must produce: (kind, id, status, fields)."""
    m["basic_events"]["BE-A"]["failure_model"]["value"]["value"] = 0.015    # p
    m["basic_events"]["BE-B"]["label"] = "event b renamed"                  # label
    m["basic_events"]["BE-C"]["provenance"] = {"source": "new source", "justification": "revised justification"}
    del m["basic_events"]["BE-GONE"]                                         # removed
    m["basic_events"]["BE-NEW"] = be(0.001, "new event")                     # added
    m["house_events"]["HE-X"]["default"] = True                              # default
    del m["house_events"]["HE-GONE"]
    m["house_events"]["HE-NEW"] = {"label": "new", "default": False, "provenance": PROV}
    ft2 = m["fault_trees"]["FT-TWO"]
    ft2["label"] = "two renamed"                                             # FT label
    del ft2["gates"]["GT-OLD"]
    ft2["gates"]["GT-T2"]["formula"] = {"or": ["BE-D", "GT-NEW"]}            # formula
    ft2["gates"]["GT-NEW"] = {"label": "new", "formula": {"and": ["BE-NEW", "HE-X"]}}
    et = m["event_tree"]
    et["functional_events"]["FE-2"]["label"] = "fe two renamed"              # ET field
    et["sequences"]["SEQ-3"]["end_state"] = "OK"                             # sequence
    return {
        ("basic_events", "BE-A", "changed", ("p",)),
        ("basic_events", "BE-B", "changed", ("label",)),
        ("basic_events", "BE-C", "changed", ("provenance",)),
        ("basic_events", "BE-GONE", "removed", ()),
        ("basic_events", "BE-NEW", "added", ()),
        ("house_events", "HE-X", "changed", ("default",)),
        ("house_events", "HE-GONE", "removed", ()),
        ("house_events", "HE-NEW", "added", ()),
        ("fault_trees", "FT-TWO", "changed", ("label",)),
        ("gates", "GT-T2", "changed", ("formula",)),
        ("gates", "GT-OLD", "removed", ()),
        ("gates", "GT-NEW", "added", ()),
        ("event_trees", "ET-E", "changed", ("functional_events",)),
    }


def write(m, d):
    os.makedirs(os.path.join(d, "basic-events"))
    os.makedirs(os.path.join(d, "fault-trees"))
    os.makedirs(os.path.join(d, "event-trees"))
    dump = lambda p, o: open(os.path.join(d, p), "w").write(yaml.safe_dump(o, sort_keys=True))
    dump("model.yaml", {
        "schema_version": "0.1.0",
        "model": {"id": "VIZ-DIFF", "name": "viewer diff fixture",
                  "risk_metrics": [{"id": "CDF", "label": "cd", "end_states": ["CD"]}]},
        "includes": {"parameters": ["parameters.yaml"], "basic_events": ["basic-events/*.yaml"],
                     "fault_trees": ["fault-trees/*.yaml"], "event_trees": ["event-trees/*.yaml"],
                     "house_events": ["house-events.yaml"]}})
    dump("parameters.yaml", {"parameters": {}})
    dump("house-events.yaml", {"house_events": m["house_events"]})
    dump("basic-events/be.yaml", {"basic_events": m["basic_events"]})
    dump("fault-trees/ft.yaml", {"fault_trees": m["fault_trees"]})
    dump("event-trees/et.yaml", {"event_tree": m["event_tree"]})


def page_data(path):
    for line in open(path):
        if line.startswith("const M = "):
            return json.loads(line[len("const M = "):].rstrip().rstrip(";"))
    raise AssertionError("no embedded model data")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", os.path.join(ROOT, "engine/target/release/canopy")))
    a = ap.parse_args()
    failures = []

    def check(cond, msg):
        (print(f"  ok  {msg}") if cond else failures.append(msg))

    def run(*args, env=None):
        p = subprocess.run([sys.executable, *args], capture_output=True, text=True,
                           env={**os.environ, **(env or {})})
        if p.returncode != 0:
            failures.append(f"{' '.join(os.path.basename(x) for x in args[:1])} failed: {p.stderr}")
        return p

    tmp = tempfile.mkdtemp(prefix="psa-vizdiff-")
    try:
        base_d, head_d = os.path.join(tmp, "base"), os.path.join(tmp, "head")
        head = copy.deepcopy(BASE)
        want = head_edits(head)
        write(BASE, base_d)
        write(head, head_d)
        schema = os.path.join(ROOT, "schema", "psa-model.schema.json")
        for d in (base_d, head_d):
            v = subprocess.run([sys.executable, os.path.join(HERE, "validate.py"), d, schema],
                               capture_output=True, text=True)
            check(v.returncode == 0, f"{os.path.basename(d)} fixture validates")
        rb, rh = os.path.join(tmp, "base.json"), os.path.join(tmp, "head.json")
        run(os.path.join(HERE, "quantify.py"), base_d, rb, "--engine", a.engine)
        run(os.path.join(HERE, "quantify.py"), head_d, rh, "--engine", a.engine)

        out = os.path.join(tmp, "diff.html")
        run(BUILD, head_d, out, "--results", rh, "--base", base_d, "--base-results", rb)
        data = page_data(out)
        d = data.get("diff") or {}
        got = set()
        for kind in ("basic_events", "gates", "fault_trees", "house_events", "event_trees"):
            for i, e in (d.get(kind) or {}).items():
                got.add((kind, i, e["status"], tuple(e["fields"])))
        check(got == want, "every edit reported exactly once, nothing else: "
              f"missing {sorted(want - got)}, extra {sorted(got - want)}")
        check(all(e.get("base") for kind in ("basic_events", "gates", "fault_trees", "house_events")
                  for e in d.get(kind, {}).values() if e["status"] != "added"),
              "changed and removed entities carry their base definition")
        g = lambda kind, i: (d.get(kind, {}).get(i) or {}).get("base") or {}
        check(g("basic_events", "BE-A").get("p") == 0.01
              and g("gates", "GT-T2").get("formula") == BASE["fault_trees"]["FT-TWO"]["gates"]["GT-T2"]["formula"]
              and g("basic_events", "BE-GONE").get("label") == "to be removed",
              "base values are the base model's")
        # sequences: SEQ-3's end state; frequencies from the two results files
        seqs = d["event_trees"]["ET-E"]["sequences"]
        fb = {s["id"]: s["frequency_per_year"] for s in json.load(open(rb))["ET-E"]["sequences"]}
        fh = {s["id"]: s["frequency_per_year"] for s in json.load(open(rh))["ET-E"]["sequences"]}
        want_seq = {}
        for sid in fb:
            fields = []
            if sid == "SEQ-3":
                fields.append("end_state")
            if abs(fh[sid] - fb[sid]) > 1e-9 * max(abs(fb[sid]), abs(fh[sid])):
                fields.append("freq")
            if fields:
                want_seq[sid] = fields
        check({k: v["fields"] for k, v in seqs.items()} == want_seq,
              f"sequence changes = end state + frequencies changed beyond 1e-9 ({want_seq})")
        check(all(seqs[s]["base"]["freq"] == fb[s] for s in seqs if "freq" in seqs[s]["fields"]),
              "sequence base frequencies = the base results")
        mb = sum(m["value_per_year"] for m in json.load(open(rb))["ET-E"]["metrics"])
        mh = sum(m["value_per_year"] for m in json.load(open(rh))["ET-E"]["metrics"])
        met = {m["id"]: m for m in d["metrics"]}
        check(met["CDF"]["base"] == mb and met["CDF"]["head"] == mh and met["CDF"]["changed"],
              f"metric CDF base {mb:.4e} -> head {mh:.4e}")
        s = d["summary"]
        n_status = lambda st: (sum(1 for x in want if x[2] == st)
                               + (st == "changed") * len(want_seq))
        check(s == {st: n_status(st) for st in ("added", "removed", "changed")} and not d["notes"],
              f"summary counts {s}, no notes when both sides have results")

        # the 1e-9 threshold, on the results themselves
        tweak = json.load(open(rb))
        rows = {x["id"]: x for x in tweak["ET-E"]["sequences"]}
        rt = os.path.join(tmp, "tweak.json")
        for factor, flagged in ((1 + 1e-12, False), (1 + 1e-6, True)):
            t = copy.deepcopy(tweak)
            for x in t["ET-E"]["sequences"]:
                if x["id"] == "SEQ-1":
                    x["frequency_per_year"] = rows["SEQ-1"]["frequency_per_year"] * factor
            json.dump(t, open(rt, "w"))
            o2 = os.path.join(tmp, "t.html")
            run(BUILD, base_d, o2, "--results", rt, "--base", base_d, "--base-results", rb)
            q = page_data(o2)["diff"]["event_trees"].get("ET-E", {}).get("sequences", {})
            check(("SEQ-1" in q) == flagged,
                  f"a relative frequency change of {factor - 1:.0e} {'is' if flagged else 'is not'} flagged")

        # one-sided results: probabilities and frequencies not compared
        o3 = os.path.join(tmp, "one.html")
        run(BUILD, head_d, o3, "--results", rh, "--base", base_d)
        d3 = page_data(o3)["diff"]
        check("p" not in d3["basic_events"].get("BE-A", {}).get("fields", [])
              and not any("freq" in q["fields"] for q in d3["event_trees"]["ET-E"]["sequences"].values())
              and not any(m["changed"] for m in d3["metrics"]) and len(d3["notes"]) == 2,
              "results on one side only: probabilities, frequencies and metrics not compared, two notes")
        check(("basic_events", "BE-B", "changed", ("label",)) in
              {("basic_events", i, e["status"], tuple(e["fields"])) for i, e in d3["basic_events"].items()},
              "... structural changes still reported")

        # identical models; no --base; determinism
        o4 = os.path.join(tmp, "same.html")
        run(BUILD, base_d, o4, "--results", rb, "--base", base_d, "--base-results", rb)
        d4 = page_data(o4)["diff"]
        check(d4["summary"] == {"added": 0, "removed": 0, "changed": 0}
              and not any(d4[k] for k in ("basic_events", "gates", "fault_trees", "house_events",
                                          "event_trees")),
              "identical models: empty diff")
        o5 = os.path.join(tmp, "plain.html")
        run(BUILD, head_d, o5, "--results", rh)
        d5 = page_data(o5)
        check("diff" not in d5 and {k: v for k, v in data.items() if k != "diff"} == d5,
              "without --base: no diff, and the same model data")
        outs = []
        for seed in ("0", "1", "2"):
            o6 = os.path.join(tmp, f"s{seed}.html")
            run(BUILD, head_d, o6, "--results", rh, "--base", base_d, "--base-results", rb,
                env={"PYTHONHASHSEED": seed})
            outs.append(open(o6).read())
        check(outs[0] == outs[1] == outs[2], "byte-identical under three hash seeds")
        p = subprocess.run([sys.executable, BUILD, head_d, os.path.join(tmp, "x.html"),
                            "--base-results", rb], capture_output=True, text=True)
        check(p.returncode != 0 and "--base-results needs --base" in p.stderr,
              "refused: --base-results without --base")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("viewer diff: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
