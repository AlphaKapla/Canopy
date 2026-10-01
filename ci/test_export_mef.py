#!/usr/bin/env python3
"""Tests for ci/export_mef.py (FR-14, FR-53): distributions exported as MEF
random deviates, their round trip through ci/import_mef.py, the refusals,
and the beta-factor level (V&V D-32), on hand-built models.

Fixture (fault tree only; the CCF group non-staggered, the MEF convention,
so a raw export imports back to the same model):

    PAR-RATE  1e-4 /h, lognormal EF 3      PAR-PFAIL 0.01, beta(2, 198)
    PAR-CONST 0.05, no distribution
    BE-A   rate-mission, rate PAR-RATE, mission time 100 h
    BE-A2  rate-mission, rate PAR-RATE, mission time 200 h (shared rate)
    BE-B   probability PAR-PFAIL
    BE-C   probability 0.02, inline gamma(2, 0.01)
    BE-D   probability PAR-CONST
    BE-E   probability 0.03, event-level uniform(0.02, 0.04)
    CCF-G  alpha 0.95/0.05 non-staggered over BE-M1, BE-M2; total 0.002,
           inline lognormal EF 10
    GT-TOP = (A and A2) or B or C or D or E or (M1 and M2)

Checked: the exported XML element by element; the raw export imported
back (parameters, failure models, CCF total identical; P(top) to 1e-12;
Monte Carlo mean and standard deviation to 1e-12 on a variant without the
event-level distribution, whose samples are then the same: every key
survives); the pre-expanded export (expanded events = coefficient x a
parameter holding the total; Canopy's importer refuses it, documented);
the refusals; and a three-member beta group exported at level 3.

Usage: python ci/test_export_mef.py [--engine PATH]
"""
import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCHEMA = os.path.join(ROOT, "schema", "psa-model.schema.json")
PROV = {"source": "ci/test_export_mef.py", "justification": "hand-built fixture"}

failures = []
n_checks = [0]


def check(cond, msg):
    n_checks[0] += 1
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def close(a, b, rel=1e-12):
    return abs(a - b) <= rel * max(abs(a), abs(b), 1e-300)


def run(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=300)


def write_model(d, params=None, bes=None, ccf=None, top=None, ets=None, house=None):
    dump = lambda p, o: open(os.path.join(d, p), "w").write(
        yaml.safe_dump(o, sort_keys=False, default_flow_style=False))
    for sub in ("basic-events", "fault-trees") + (("event-trees",) if ets else ()):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    includes = {"parameters": ["parameters.yaml"],
                "basic_events": ["basic-events/*.yaml"],
                "fault_trees": ["fault-trees/*.yaml"],
                "house_events": ["house-events.yaml"]}
    metrics = []
    if ets:
        includes["event_trees"] = ["event-trees/*.yaml"]
        for et in ets:
            dump(f"event-trees/{et['id'].lower()}.yaml", {"event_tree": et})
        metrics = [{"id": "CDF", "label": "core damage", "end_states": ["CD"]}]
    if ccf:
        includes["ccf_groups"] = ["ccf-groups.yaml"]
        dump("ccf-groups.yaml", {"ccf_groups": ccf})
    dump("model.yaml", {"schema_version": "0.1",
                        "model": {"id": "EXPORT", "name": "export fixture",
                                  "risk_metrics": metrics},
                        "includes": includes})
    dump("parameters.yaml", {"parameters": params or {}})
    dump("house-events.yaml", {"house_events": {
        h: {"label": "house", "default": v, "provenance": PROV} for h, v in (house or {}).items()}})
    dump("basic-events/b.yaml", {"basic_events": bes})
    dump("fault-trees/f.yaml", {"fault_trees": {
        "FT-T": {"label": "tree", "top_gate": "GT-TOP", "gates": top}}})


def prob(value, unc=None):
    q = {"value": value, "unit": "per_demand"}
    if unc:
        q["uncertainty"] = unc
    return {"type": "probability", "value": q}


def be(fm, unc=None):
    e = {"label": "fixture event", "provenance": PROV, "failure_model": fm}
    if unc:
        e["uncertainty"] = unc
    return e


PARAMS = {
    "PAR-RATE": {"label": "rate", "value": 1e-4, "unit": "per_hour", "provenance": PROV,
                 "uncertainty": {"distribution": "lognormal", "error_factor": 3.0}},
    "PAR-PFAIL": {"label": "p", "value": 0.01, "unit": "per_demand", "provenance": PROV,
                  "uncertainty": {"distribution": "beta", "alpha": 2.0, "beta": 198.0}},
    "PAR-CONST": {"label": "c", "value": 0.05, "unit": "per_demand", "provenance": PROV}}


def bes(event_level=True):
    mission = lambda t: {"type": "rate-mission", "rate": {"param": "PAR-RATE"},
                         "mission_time": {"value": t, "unit": "hour"}}
    uni = {"distribution": "uniform", "lower": 0.02, "upper": 0.04}
    return {
        "BE-A": be(mission(100.0)), "BE-A2": be(mission(200.0)),
        "BE-B": be({"type": "probability", "value": {"param": "PAR-PFAIL"}}),
        "BE-C": be(prob(0.02, {"distribution": "gamma", "shape": 2.0, "scale": 0.01})),
        "BE-D": be({"type": "probability", "value": {"param": "PAR-CONST"}}),
        "BE-E": be(prob(0.03), uni) if event_level else be(prob(0.03, uni)),
        "BE-M1": be(prob(0.002)), "BE-M2": be(prob(0.002))}


CCF = {"CCF-G": {"label": "pair", "model": "alpha-factor", "testing": "non-staggered",
                 "members": ["BE-M1", "BE-M2"], "provenance": PROV,
                 "total_probability": {"value": 0.002, "unit": "per_demand",
                                       "uncertainty": {"distribution": "lognormal",
                                                       "error_factor": 10.0}},
                 "factors": {"alpha_1": 0.95, "alpha_2": 0.05}}}
TOP = {"GT-TOP": {"label": "top", "formula": {"or": [
           "GT-AA", "BE-B", "BE-C", "BE-D", "BE-E", "GT-MM"]}},
       "GT-AA": {"label": "both a", "formula": {"and": ["BE-A", "BE-A2"]}},
       "GT-MM": {"label": "both m", "formula": {"and": ["BE-M1", "BE-M2"]}}}


def tree(tid, rows, ie=True, fes=None):
    """An event tree over GT-AA and GT-MM; rows: id -> (path, end state[,
    extra fields])."""
    et = {"id": tid, "label": "tree",
          "functional_events": fes or {"FE-1": {"label": "one", "top_gate": "GT-AA"},
                                       "FE-2": {"label": "two", "top_gate": "GT-MM"}},
          "sequences": {sid: {"path": r[0], "end_state": r[1], **(r[2] if len(r) > 2 else {})}
                        for sid, r in rows.items()}}
    if ie:
        et["initiating_event"] = {"id": f"IE-{tid[3:]}", "label": "initiator",
                                  "provenance": PROV,
                                  "frequency": {"value": 1e-2, "unit": "per_year"}}
    return et


ROWS = {"SEQ-1": ({"FE-1": "success", "FE-2": "success"}, "OK"),
        "SEQ-2": ({"FE-1": "success", "FE-2": "failure"}, "CD"),
        "SEQ-3": ({"FE-1": "failure", "FE-2": "bypassed"}, "CD")}


def floats(el):
    """the <float> values under `el` (None if `el` is missing)"""
    return None if el is None else [float(f.get("value")) for f in el if f.tag == "float"]


MISSING = ET.Element("missing")      # stands in for an absent element


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", os.path.join(ROOT, "engine/target/release/canopy")))
    a = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="psa-mefexp-")

    def export(model, name, *flags):
        xml = os.path.join(tmp, name + ".xml")
        r = run([sys.executable, os.path.join(HERE, "export_mef.py"), model, xml, *flags])
        return r, xml

    def imp(xml, name):
        out = os.path.join(tmp, name)
        return run([sys.executable, os.path.join(HERE, "import_mef.py"), xml, out]), out

    def p_top(model, *extra):
        """the fixture's tree is FT-T; an imported model's is FT-MAIN"""
        ft = "FT-MAIN" if os.path.basename(model).startswith(("back", "beta3-back")) else "FT-T"
        r = run([a.engine, model, ft, "--json", "--prob-only", *extra])
        return json.loads(r.stdout) if r.returncode == 0 else None

    try:
        model = os.path.join(tmp, "model")
        write_model(model, PARAMS, bes(), CCF, TOP)
        v = run([sys.executable, os.path.join(HERE, "validate.py"), model, SCHEMA])
        check(v.returncode == 0 and "0 error(s), 0 warning(s)" in v.stdout,
              f"fixture validates: {v.stdout.strip()[-120:]}")

        # default: point values only
        r, xml = export(model, "point")
        text = open(xml).read() if r.returncode == 0 else ""
        check(r.returncode == 0 and "-deviate" not in text and "define-parameter" not in text,
              "without --uncertainty: point values only")

        # --uncertainty, raw CCF
        r, xml = export(model, "unc", "--uncertainty")
        check(r.returncode == 0, f"--uncertainty export: {r.stderr}")
        root = ET.parse(xml).getroot()
        md = root.find("model-data")
        pdefs = {p.get("name"): p for p in md.findall("define-parameter")}
        check(sorted(pdefs) == ["PAR-PFAIL", "PAR-RATE"],
              f"distribution parameters defined, the constant one inlined: {sorted(pdefs)}")
        rate, pfail = pdefs.get("PAR-RATE", MISSING), pdefs.get("PAR-PFAIL", MISSING)
        check(rate.get("unit") == "hours-1"
              and floats(rate.find("lognormal-deviate")) == [1e-4, 3.0, 0.95],
              "PAR-RATE: lognormal-deviate(mean, EF, 0.95), unit hours-1")
        check(pfail is not MISSING and pfail.get("unit") is None
              and floats(pfail.find("beta-deviate")) == [2.0, 198.0],
              "PAR-PFAIL: beta-deviate(2, 198), no unit")
        ev = {e.get("name"): list(e)[0] for e in md.findall("define-basic-event")}
        ex = ev["BE-A"]
        check(ex.tag == "exponential" and len(ex) and list(ex)[0].tag == "parameter"
              and list(ex)[0].get("name") == "PAR-RATE" and floats(ex) == [100.0]
              and floats(ev["BE-A2"]) == [200.0],
              "rate-mission events: <exponential> over the shared rate parameter")
        check(ev["BE-B"].tag == "parameter" and ev["BE-B"].get("name") == "PAR-PFAIL",
              "probability from a distribution parameter: the parameter")
        check(ev["BE-C"].tag == "gamma-deviate" and floats(ev["BE-C"]) == [2.0, 0.01],
              "inline gamma: a gamma-deviate in place")
        check(ev["BE-D"].tag == "float" and float(ev["BE-D"].get("value")) == 0.05,
              "constant parameter: its value")
        check(ev["BE-E"].tag == "uniform-deviate" and floats(ev["BE-E"]) == [0.02, 0.04],
              "event-level uniform: a uniform-deviate in place")
        dist = root.find("define-CCF-group").find("distribution")
        check(floats(dist.find("lognormal-deviate")) == [0.002, 10.0, 0.95],
              "CCF total: lognormal-deviate in the group's distribution")

        # round trip
        r, out = imp(xml, "back")
        check(r.returncode == 0, f"raw --uncertainty export imports back: {r.stderr[-200:]}")
        if r.returncode == 0:
            v = run([sys.executable, os.path.join(HERE, "validate.py"), out, SCHEMA])
            check(v.returncode == 0 and "0 error(s), 0 warning(s)" in v.stdout,
                  f"imported model validates: {v.stdout.strip()[-120:]}")
            pars = yaml.safe_load(open(os.path.join(out, "parameters.yaml")))["parameters"]
            check({k: (x["value"], x["unit"], x["uncertainty"]) for k, x in pars.items()} ==
                  {k: (x["value"], x["unit"], x["uncertainty"]) for k, x in PARAMS.items()
                   if "uncertainty" in x},
                  "distribution parameters come back identical (same IDs, units, values)")
            got = yaml.safe_load(open(os.path.join(out, "basic-events", "imported.yaml")))["basic_events"]
            want = bes()
            same = all(got[b]["failure_model"] == want[b]["failure_model"]
                       for b in ("BE-A", "BE-A2", "BE-B", "BE-C"))
            check(same, "failure models of A, A2, B, C come back identical")
            check(got["BE-D"]["failure_model"]["value"] == {"value": 0.05, "unit": "per_demand"},
                  "the constant parameter comes back as its value")
            check(got["BE-E"]["failure_model"] == bes(event_level=False)["BE-E"]["failure_model"]
                  and "uncertainty" not in got["BE-E"],
                  "the event-level distribution comes back on the probability itself")
            cg = yaml.safe_load(open(os.path.join(out, "ccf-groups.yaml")))["ccf_groups"]["CCF-G"]
            check(cg["total_probability"] == CCF["CCF-G"]["total_probability"]
                  and cg["testing"] == "non-staggered" and cg["factors"] == CCF["CCF-G"]["factors"],
                  "CCF group comes back identical (total with its distribution)")
            p0, p1 = p_top(model), p_top(out)
            check(p0 and p1 and close(p0["probability"], p1["probability"]),
                  f"P(top) unchanged by the round trip: {p0 and p0['probability']}")

        # Monte Carlo through the round trip, every sampling key preserved
        model2 = os.path.join(tmp, "model2")
        write_model(model2, PARAMS, bes(event_level=False), CCF, TOP)
        r, xml2 = export(model2, "unc2", "--uncertainty")
        r2, out2 = imp(xml2, "back2")
        mc = ["--samples", "4000", "--seed", "7"]
        u0, u1 = p_top(model2, *mc), p_top(out2, *mc) if r2.returncode == 0 else None
        check(u0 and u1 and close(u0["uncertainty"]["mean"], u1["uncertainty"]["mean"])
              and close(u0["uncertainty"]["std"], u1["uncertainty"]["std"]),
              f"Monte Carlo identical to rounding after the round trip (same keys, same "
              f"samples): mean {u0 and u0['uncertainty']['mean']}")

        # --uncertainty --expand-ccf: coefficient x the total's parameter
        r, xml = export(model, "exp", "--uncertainty", "--expand-ccf")
        check(r.returncode == 0, f"--uncertainty --expand-ccf export: {r.stderr}")
        md = ET.parse(xml).getroot().find("model-data")
        pdefs = {p.get("name"): p for p in md.findall("define-parameter")}
        ev = {e.get("name"): list(e)[0] for e in md.findall("define-basic-event")}
        at = 0.95 + 2 * 0.05
        c1, c2 = 0.95 / at, 2 * 0.05 / at
        m1, m12 = ev["BE-M1"], ev["BE-CCF-G-1-2"]
        check(floats(pdefs.get("CCF-G-TOTAL", MISSING).find("lognormal-deviate"))
              == [0.002, 10.0, 0.95],
              "the inline total gets a parameter of its own")
        check(m1.tag == "mul" and floats(m1) == [c1] and m12.tag == "mul" and floats(m12) == [c2]
              and len(m12) == 2 and list(m12)[1].get("name") == "CCF-G-TOTAL",
              f"expanded events: coefficient (non-staggered {c1:.6g}, {c2:.6g}) x the total")
        r, _ = imp(xml, "back-exp")
        check(r.returncode != 0 and "is used as a constant" in r.stderr,
              "Canopy's importer refuses a distribution inside arithmetic (documented)")

        # refusals
        def refused(name, params, bes_, ccf, frag, *flags):
            d = os.path.join(tmp, name)
            write_model(d, params, bes_, ccf, TOP)
            r, _ = export(d, name, "--uncertainty", *flags)
            check(r.returncode != 0 and frag in r.stderr, f"refused: {name} ({r.stderr.strip()[:100]})")

        off = json.loads(json.dumps(PARAMS))
        off["PAR-PFAIL"]["value"] = 0.0101
        refused("point value not the mean", off, bes(), CCF,
                "point value 0.0101 is not the mean 0.01 of its beta distribution")
        rr = bes()
        rr["BE-D"] = be({"type": "rate-repair", "rate": {"param": "PAR-RATE"},
                         "mttr": {"value": 8.0, "unit": "hour"}})
        refused("distribution on a rate-repair input", PARAMS, rr, CCF,
                "BE-D: a distribution on an input of a rate-repair event has no MEF export")
        fu = json.loads(json.dumps(CCF))
        fu["CCF-G"]["factor_uncertainty"] = {"distribution": "dirichlet", "concentration": 20}
        refused("CCF factor uncertainty", PARAMS, bes(), fu,
                "CCF-G: CCF factor uncertainty has no MEF equivalent")

        # D-33: per-sequence house-event overrides are refused, not dropped
        rows = {**ROWS, "SEQ-3": (*ROWS["SEQ-3"], {"house_events": {"HE-X": True}})}
        top_h = {**TOP, "GT-TOP": {"label": "top", "formula": {"or": [
            "GT-AA", "BE-B", "BE-C", "BE-D", "BE-E", "GT-MM", "HE-X"]}}}
        d = os.path.join(tmp, "override")
        write_model(d, PARAMS, bes(), CCF, top_h, [tree("ET-H", rows)], {"HE-X": False})
        v = run([sys.executable, os.path.join(HERE, "validate.py"), d, SCHEMA])
        r, _ = export(d, "override")
        check(v.returncode == 0 and r.returncode != 0 and
              "ET-H/SEQ-3: per-sequence house-event overrides are not exported" in r.stderr,
              f"refused: a per-sequence house override (D-33: was dropped) "
              f"({r.stderr.strip()[:90]})")
        d = os.path.join(tmp, "no-override")
        write_model(d, PARAMS, bes(), CCF, top_h, [tree("ET-H", ROWS)], {"HE-X": False})
        r, _ = export(d, "no-override")
        check(r.returncode == 0, f"the same tree without the override exports: {r.stderr}")

        # FR-54: a transfer to a tree of the model is a MEF link. ET-B's
        # first function shares GT-AA with ET-A's: from SEQ-A2 (GT-AA
        # works) ET-B's SEQ-B3 is impossible, which a product of marginals
        # would miss; its second (GT-BC) is independent.
        top_x = {**TOP, "GT-BC": {"label": "b or c", "formula": {"or": ["BE-B", "BE-C"]}},
                 "GT-TOP": {"label": "top", "formula": {"or": ["GT-AA", "GT-MM", "GT-BC",
                                                               "BE-D", "BE-E"]}}}
        et_a = tree("ET-A", {
            "SEQ-A1": ({"FE-1": "success", "FE-2": "success"}, "OK"),
            "SEQ-A2": ({"FE-1": "success", "FE-2": "failure"}, "XFER-B", {"transfer": "ET-B"}),
            "SEQ-A3": ({"FE-1": "failure", "FE-2": "bypassed"}, "CD")})
        et_b = tree("ET-B", {
            "SEQ-B1": ({"FE-B1": "success", "FE-B2": "success"}, "OK"),
            "SEQ-B2": ({"FE-B1": "success", "FE-B2": "failure"}, "CD"),
            "SEQ-B3": ({"FE-B1": "failure", "FE-B2": "bypassed"}, "CD")}, ie=False,
            fes={"FE-B1": {"label": "b one", "top_gate": "GT-AA"},
                 "FE-B2": {"label": "b two", "top_gate": "GT-BC"}})
        d = os.path.join(tmp, "links")
        write_model(d, PARAMS, bes(), CCF, top_x, [et_a, et_b])
        v = run([sys.executable, os.path.join(HERE, "validate.py"), d, SCHEMA])
        check(v.returncode == 0, f"transfer fixture validates: {v.stdout.strip()[-120:]}")
        r, xml = export(d, "links")
        root = ET.parse(xml).getroot() if r.returncode == 0 else ET.Element("x")
        trees = {t.get("name"): t for t in root.findall("define-event-tree")}
        seq = {q.get("name"): q for t in trees.values() for q in t.findall("define-sequence")}
        link = seq.get("SEQ-A2", MISSING).find("event-tree")
        check(link is not None and link.get("name") == "ET-B"
              and len(seq.get("SEQ-A1", MISSING)) == 0,
              "the transfer row is a MEF link to ET-B; the others are plain sequences")
        check(sorted(i.get("event-tree") for i in root.findall("define-initiating-event"))
              == ["ET-A"], "the transfer-only tree gets no initiating event")
        r, out = imp(xml, "links-back")
        check(r.returncode == 0, f"the link imports back: {r.stderr.strip()[-150:]}")
        if r.returncode == 0:
            def rows(model, scale):
                q = run([a.engine, model, "ET-A", "--json", "--prob-only"])
                if q.returncode:
                    return {}
                ss = json.loads(q.stdout)["sequences"]
                es = {x["id"]: x["end_state"] for x in ss}
                key = lambda x: (x["id"] if scale != 1 else
                                 (f"{es[x['id'].split('>')[0]]}>{x['end_state']}"
                                  if ">" in x["id"] else x["end_state"]))
                return {key(x): x["frequency_per_year"] / scale for x in ss}
            orig, back = rows(d, 1e-2), rows(out, 1)
            ok = (set(orig) == set(back) == {"SEQ-A1", "SEQ-A2", "SEQ-A3", "SEQ-A2>SEQ-B1",
                                             "SEQ-A2>SEQ-B2", "SEQ-A2>SEQ-B3"}
                  and all(close(orig[k], back[k]) or orig[k] == back[k] == 0 for k in orig))
            check(ok and orig["SEQ-A2>SEQ-B3"] == 0.0 and orig["SEQ-A2>SEQ-B1"] > 0,
                  f"every row, the followed ones included, comes back to 1e-12 (the "
                  f"impossible one at 0): {back}")
        d = os.path.join(tmp, "absent")
        write_model(d, PARAMS, bes(), CCF, top_x, [{**et_a, "sequences": {
            **et_a["sequences"], "SEQ-A2": {**et_a["sequences"]["SEQ-A2"], "transfer": "ET-NOPE"}}}])
        r, xml = export(d, "absent")
        check(r.returncode == 0 and "transfer to ET-NOPE, which is not in the model" in r.stderr
              and ET.parse(xml).getroot().find(".//define-sequence/event-tree") is None,
              "a transfer to a tree outside the model stays an ordinary sequence, with a note")

        # D-32: a beta-factor group of three members, raw export
        d = os.path.join(tmp, "beta3")
        b3 = {f"BE-P{i}": be(prob(0.01)) for i in (1, 2, 3)}
        write_model(d, {}, b3, {"CCF-B": {
            "label": "triple", "model": "beta-factor", "provenance": PROV,
            "members": ["BE-P1", "BE-P2", "BE-P3"],
            "total_probability": {"value": 0.01, "unit": "per_demand"},
            "factors": {"beta": 0.1}}},
            {"GT-TOP": {"label": "2 of 3", "formula": {"atleast": {
                "k": 2, "of": ["BE-P1", "BE-P2", "BE-P3"]}}}})
        r, xml = export(d, "beta3")
        fac = ET.parse(xml).getroot().find("define-CCF-group").find("factors").find("factor")
        check(fac.get("level") == "3", f"beta factor exported at level 3, the group size "
              f"(was 2: D-32), got {fac.get('level')}")
        r, out = imp(xml, "beta3-back")
        p0, p1 = p_top(d), (p_top(out) if r.returncode == 0 else None)
        check(r.returncode == 0 and p0 and p1 and close(p0["probability"], p1["probability"]),
              f"the three-member beta group round-trips: {r.stderr.strip()[-120:]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print(f"export_mef: {len(failures)} of {n_checks[0]} checks FAILED")
        return 1
    print(f"export_mef: all {n_checks[0]} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
