#!/usr/bin/env python3
"""Hand-computed reference tests for event-tree transfers (FR-11, V&V
anomaly D-10), run against the engine binary and ci/quantify.py.

Fixture (all probabilities per demand, IE 1e-2 /yr):
  BE-A 0.1, BE-B 0.2, BE-C 0.3; HE-X default false
  GT-A = BE-A;  GT-AB = BE-A or BE-B;  GT-C = BE-C or HE-X
  ET-MAIN (IE-MAIN), FE-1 = GT-A
    SEQ-M1  FE-1 success                    -> OK
    SEQ-M2  FE-1 failure, transfer ET-SUB   -> end state CD (deliberately
            mapped to CDF: a transfer row must never be counted, D-10)
  ET-SUB (transfer-only: no initiating event), FE-2 = GT-AB, FE-3 = GT-C
    SEQ-T1  FE-2 success, FE-3 bypassed     -> OK
    SEQ-T2  FE-2 failure, FE-3 success      -> CD
    SEQ-T3  FE-2 failure, FE-3 failure      -> CD2
  metrics CDF = {CD}, CD2F = {CD2}

Exact expansions (A is shared between the trees):
  M2>T1 = A ∧ ¬(A ∨ B)       = 0             (independent trees: 0.072)
  M2>T2 = A ∧ (A ∨ B) ∧ ¬C   = A ∧ ¬C = 0.07 -> 7e-4 /yr
  M2>T3 = A ∧ (A ∨ B) ∧ C    = A ∧ C  = 0.03 -> 3e-4 /yr
  CDF = 7e-4 (not 1.7e-3), CD2F = 3e-4; partition 0.9 + 0.1 = 1; the
  expansions sum to P(M2) = 0.1.
  Cut sets (delete-term, failure logic of every hop): M2>T2 {A} 1e-3,
  M2>T3 {A, C} 3e-4.
  CDF importance: A: F1 = 7e-3, F0 = 0 (FV 1, RRW infinite);
  C: F1 = 0, F0 = 1e-3 (FV = −3/7, RAW 0); B not in the support.

Usage: python ci/test_transfers.py [--engine PATH]
"""
import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROV = {"source": "test", "justification": "hand-computed transfer fixture"}


def q(v, unit="per_demand"):
    return {"value": v, "unit": unit}


def base_model():
    be = {b: {"label": b, "failure_model": {"type": "probability", "value": q(p)},
              "provenance": PROV}
          for b, p in (("BE-A", 0.1), ("BE-B", 0.2), ("BE-C", 0.3))}
    gates = {"GT-A": {"label": "a", "formula": "BE-A"},
             "GT-AB": {"label": "ab", "formula": {"or": ["BE-A", "BE-B"]}},
             "GT-C": {"label": "c", "formula": {"or": ["BE-C", "HE-X"]}}}
    main = {"id": "ET-MAIN", "label": "main",
            "initiating_event": {"id": "IE-MAIN", "label": "ie",
                                 "frequency": q(1e-2, "per_year"),
                                 "provenance": PROV},
            "functional_events": {"FE-1": {"label": "f1", "top_gate": "GT-A"}},
            "sequences": {
                "SEQ-M1": {"path": {"FE-1": "success"}, "end_state": "OK"},
                "SEQ-M2": {"path": {"FE-1": "failure"}, "end_state": "CD",
                           "transfer": "ET-SUB"}}}
    sub = {"id": "ET-SUB", "label": "sub",
           "functional_events": {"FE-2": {"label": "f2", "top_gate": "GT-AB"},
                                 "FE-3": {"label": "f3", "top_gate": "GT-C"}},
           "sequences": {
               "SEQ-T1": {"path": {"FE-2": "success", "FE-3": "bypassed"},
                          "end_state": "OK"},
               "SEQ-T2": {"path": {"FE-2": "failure", "FE-3": "success"},
                          "end_state": "CD"},
               "SEQ-T3": {"path": {"FE-2": "failure", "FE-3": "failure"},
                          "end_state": "CD2"}}}
    return {"be": be, "gates": gates,
            "trees": {"ET-MAIN": main, "ET-SUB": sub},
            "metrics": [{"id": "CDF", "label": "cd", "end_states": ["CD"]},
                        {"id": "CD2F", "label": "cd2", "end_states": ["CD2"]}]}


def write(m, d):
    def dump(rel, obj):
        os.makedirs(os.path.dirname(os.path.join(d, rel)) or d, exist_ok=True)
        with open(os.path.join(d, rel), "w") as f:
            yaml.safe_dump(obj, f, sort_keys=False)
    dump("model.yaml", {
        "schema_version": "0.1.0",
        "model": {"id": "TRANSFER-TEST", "name": "transfer fixture",
                  "risk_metrics": m["metrics"]},
        "includes": {"parameters": ["parameters.yaml"],
                     "house_events": ["house-events.yaml"],
                     "basic_events": ["basic-events/*.yaml"],
                     "fault_trees": ["fault-trees/*.yaml"],
                     "event_trees": ["event-trees/*.yaml"]}})
    dump("parameters.yaml", {"parameters": {}})
    dump("house-events.yaml", {"house_events": {"HE-X": {
        "label": "x", "default": False, "provenance": PROV}}})
    dump("basic-events/be.yaml", {"basic_events": m["be"]})
    dump("fault-trees/ft.yaml", {"fault_trees": {"FT-T": {
        "label": "t", "top_gate": "GT-AB", "gates": m["gates"]}}})
    for tid, t in m["trees"].items():
        dump(f"event-trees/{tid.lower()}.yaml", {"event_tree": t})


class Case:
    def __init__(self, engine):
        self.engine = engine
        self.failures = []

    def check(self, cond, msg):
        if not cond:
            self.failures.append(msg)

    def close(self, a, b, msg, tol=1e-14):
        ok = (a is not None and b is not None
              and abs(a - b) <= tol * max(abs(a), abs(b)) + 1e-300)
        self.check(ok, f"{msg}: got {a}, expected {b}")

    def run(self, m, *args, expect_ok=True, target="ET-MAIN"):
        d = tempfile.mkdtemp(prefix="psa-xfer-")
        try:
            write(m, d)
            p = subprocess.run([self.engine, d, target, "--json", *args],
                               capture_output=True, text=True)
            v = subprocess.run([sys.executable, os.path.join(HERE, "validate.py"),
                                d, os.path.join(ROOT, "schema", "psa-model.schema.json")],
                               capture_output=True, text=True)
            qt = subprocess.run([sys.executable, os.path.join(HERE, "quantify.py"),
                                 d, os.path.join(d, "out.json"), "--engine", self.engine],
                                capture_output=True, text=True)
            qres = (json.load(open(os.path.join(d, "out.json")))
                    if qt.returncode == 0 else None)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        if expect_ok and p.returncode != 0:
            self.failures.append(f"engine failed: {p.stderr}")
            return None, v, qt, qres
        return (json.loads(p.stdout) if p.returncode == 0 else p), v, qt, qres


def rows(r):
    return {s["id"]: s for s in r["sequences"]}


def metric(r, mid):
    return next(x for x in r["metrics"] if x["id"] == mid)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", os.path.join(ROOT, "engine/target/release/canopy")))
    a = ap.parse_args()
    t = Case(a.engine)

    # ---- base fixture ------------------------------------------------------
    def section_0():
        r, v, qt, qres = t.run(base_model())
        s = rows(r)
        t.check(list(s) == ["SEQ-M1", "SEQ-M2", "SEQ-M2>SEQ-T1", "SEQ-M2>SEQ-T2",
                            "SEQ-M2>SEQ-T3"], f"row order {list(s)}")
        for sid, f in (("SEQ-M1", 9e-3), ("SEQ-M2", 1e-3), ("SEQ-M2>SEQ-T1", 0.0),
                       ("SEQ-M2>SEQ-T2", 7e-4), ("SEQ-M2>SEQ-T3", 3e-4)):
            t.close(s[sid]["frequency_per_year"], f, f"{sid} frequency")
        t.check(s["SEQ-M2>SEQ-T1"]["frequency_per_year"] == 0.0,
                "shared event: M2>T1 must be exactly 0")
        t.close(metric(r, "CDF")["value_per_year"], 7e-4,
                "CDF (transfer row with end state CD excluded, expansion counted)")
        t.close(metric(r, "CD2F")["value_per_year"], 3e-4, "CD2F")
        t.close(r["partition"]["sum_probability"], 1.0, "partition over own rows")
        fol = s["SEQ-M2"]["followed"]
        t.check(fol is not None and fol["per_sequence_house_overrides"] is False,
                f"followed info {fol}")
        t.close(fol["sum_probability"], 0.1, "expansions sum to P(M2)")
        t.close(fol["probability"], 0.1, "P(M2)")
        t.check(s["SEQ-M2>SEQ-T2"]["transfer_path"] == [
            {"event_tree": "ET-MAIN", "sequence": "SEQ-M2"},
            {"event_tree": "ET-SUB", "sequence": "SEQ-T2"}], "transfer_path")
        t.check(s["SEQ-M1"]["transfer_path"] is None and s["SEQ-M1"]["followed"] is None,
                "own non-transfer row fields")
        cs = {tuple(c["events"]): c["frequency_per_year"]
              for c in s["SEQ-M2>SEQ-T2"]["cut_sets"]}
        t.check(list(cs) == [("BE-A",)], f"M2>T2 cut sets {cs}")
        t.close(cs.get(("BE-A",)), 1e-3, "M2>T2 cut set frequency")
        cs = {tuple(c["events"]): c["frequency_per_year"]
              for c in s["SEQ-M2>SEQ-T3"]["cut_sets"]}
        t.check(list(cs) == [("BE-A", "BE-C")], f"M2>T3 cut sets {cs}")
        t.close(cs.get(("BE-A", "BE-C")), 3e-4, "M2>T3 cut set frequency")
        imp = {x["event"]: x for x in metric(r, "CDF")["importance"]}
        t.check(set(imp) == {"BE-A", "BE-C"}, f"CDF importance events {set(imp)}")
        t.close(imp["BE-A"]["frequency_if_true_per_year"], 7e-3, "A: F(A=1)")
        t.check(imp["BE-A"]["frequency_if_false_per_year"] == 0.0, "A: F(A=0) = 0")
        t.check(imp["BE-A"]["rrw"] is None, "A: RRW infinite")
        t.close(imp["BE-A"]["fussell_vesely"], 1.0, "A: FV")
        t.check(imp["BE-C"]["frequency_if_true_per_year"] == 0.0, "C: F(C=1) = 0")
        t.close(imp["BE-C"]["frequency_if_false_per_year"], 1e-3, "C: F(C=0)")
        t.close(imp["BE-C"]["fussell_vesely"], -3.0 / 7.0, "C: FV")
        t.check(imp["BE-C"]["raw"] == 0.0, "C: RAW 0")
        es = {e["id"] for e in r["end_states"]}
        t.check(es == {"CD", "CD2", "OK"}, f"end-state groups {es}")
        t.check(v.returncode == 0 and "SEQ-M2: end state CD of a transfer sequence "
                "is mapped to a risk metric" in v.stdout,
                f"validator: {v.stdout}")
        t.check(qt.returncode == 0 and qres is not None and list(qres) == ["ET-MAIN"],
                f"quantify.py skips the transfer-only tree: {qt.stdout}{qt.stderr}")

    # ---- standalone quantification of a transfer-only tree is refused ------
    def section_1():
        p, _, _, _ = t.run(base_model(), target="ET-SUB", expect_ok=False)
        t.check(isinstance(p, subprocess.CompletedProcess) and p.returncode != 0
                and "transfer-only tree" in p.stderr, "ET-SUB standalone refused")

    # ---- house overrides accumulate along the chain ------------------------
    def section_2():
        m = base_model()
        m["trees"]["ET-MAIN"]["sequences"]["SEQ-M2"]["house_events"] = {"HE-X": True}
        r, _, qt, _ = t.run(m)
        s = rows(r)
        t.close(s["SEQ-M2"]["frequency_per_year"], 1e-3, "M2 with HE-X (unaffected)")
        t.check(s["SEQ-M2>SEQ-T2"]["frequency_per_year"] == 0.0,
                "HE-X=true carried into ET-SUB: M2>T2 = 0")
        t.close(s["SEQ-M2>SEQ-T3"]["frequency_per_year"], 1e-3, "M2>T3 = A")
        t.check(metric(r, "CDF")["value_per_year"] == 0.0, "CDF = 0")
        t.check(r["partition"]["per_sequence_house_overrides"] is True,
                "own-row overrides flagged")
        t.check(s["SEQ-M2"]["followed"]["per_sequence_house_overrides"] is False,
                "no override on an expansion hop")
        t.close(s["SEQ-M2"]["followed"]["sum_probability"], 0.1,
                "expansions still sum to P(M2)")
        t.check(qt.returncode == 0, f"quantify.py: {qt.stderr}")
        # a later hop wins on conflict
        m["trees"]["ET-SUB"]["sequences"]["SEQ-T2"]["house_events"] = {"HE-X": False}
        r, _, qt, _ = t.run(m)
        s = rows(r)
        t.close(s["SEQ-M2>SEQ-T2"]["frequency_per_year"], 7e-4,
                "T2's HE-X=false wins: M2>T2 = A ∧ ¬C")
        t.close(s["SEQ-M2>SEQ-T3"]["frequency_per_year"], 1e-3,
                "T3 keeps M2's HE-X=true")
        t.check(s["SEQ-M2"]["followed"]["per_sequence_house_overrides"] is True,
                "expansion-hop override flagged")
        t.close(s["SEQ-M2"]["followed"]["sum_probability"], 0.17,
                "expansions sum to 0.17 (overrides: need not match)")
        t.check(qt.returncode == 0 and "override house events" in qt.stdout,
                f"quantify.py notes, not fails: {qt.stdout}{qt.stderr}")

    # ---- one gate compiled under two house configurations in one chain ---
    def section_gate_cache():
        # ET-MAIN FE-1 = GT-C = C or HE-X; SEQ-M2 sets HE-X true (so FE-1
        # fails with certainty) and transfers to ET-SUB, whose FE-2 is GT-C
        # again, evaluated with HE-X switched back to false by both target
        # rows. A gate cached from the first hop would give M2>T1 = 0.
        m = base_model()
        main = m["trees"]["ET-MAIN"]
        main["functional_events"]["FE-1"]["top_gate"] = "GT-C"
        main["sequences"]["SEQ-M2"]["house_events"] = {"HE-X": True}
        m["trees"]["ET-SUB"] = {
            "id": "ET-SUB", "label": "sub",
            "functional_events": {"FE-2": {"label": "f2", "top_gate": "GT-C"}},
            "sequences": {
                "SEQ-T1": {"path": {"FE-2": "success"}, "end_state": "OK",
                           "house_events": {"HE-X": False}},
                "SEQ-T2": {"path": {"FE-2": "failure"}, "end_state": "CD",
                           "house_events": {"HE-X": False}}}}
        r, _, qt, _ = t.run(m)
        s = rows(r)
        t.close(s["SEQ-M1"]["frequency_per_year"], 7e-3, "M1 = not C (HE-X false)")
        t.close(s["SEQ-M2"]["frequency_per_year"], 1e-2, "M2 = C or true = 1")
        t.close(s["SEQ-M2>SEQ-T1"]["frequency_per_year"], 7e-3,
                "M2>T1 = not C (GT-C recompiled with HE-X false)")
        t.close(s["SEQ-M2>SEQ-T2"]["frequency_per_year"], 3e-3, "M2>T2 = C")
        t.close(metric(r, "CDF")["value_per_year"], 3e-3, "CDF")
        t.check(qt.returncode == 0, f"quantify.py: {qt.stderr}")

    def section_tools():
        # Every tool that reads event trees accepts a transfer-only tree.
        d = tempfile.mkdtemp(prefix="psa-xfer-tools-")
        try:
            write(base_model(), d)
            res = os.path.join(d, "res.json")
            steps = [
                ["ci/quantify.py", d, res, "--engine", t.engine],
                ["viz/build_viz.py", d, os.path.join(d, "v.html"), "--results", res],
                ["ci/export_mef.py", d, os.path.join(d, "m.xml")],
                ["ci/consequence_report.py", res, "--metric", "CDF", "--model", d],
            ]
            for st in steps:
                p = subprocess.run([sys.executable, os.path.join(ROOT, st[0]), *st[1:]],
                                   capture_output=True, text=True, cwd=ROOT)
                t.check(p.returncode == 0 and "Traceback" not in p.stderr,
                        f"{st[0]} on a transfer-only tree: {p.stdout}{p.stderr}")
                if st[0].endswith("consequence_report.py"):
                    t.check("total frequency  : 7.0000e-04 /yr" in p.stdout,
                            f"consequence report total: {p.stdout}")
        finally:
            shutil.rmtree(d, ignore_errors=True)
    sections_extra = [(section_gate_cache,
                       "one gate compiled under two house configurations"),
                      (section_tools, "tools accept a transfer-only tree")]

    # ---- unfollowed transfer (target absent): excluded, D-10 regression ----
    def section_3():
        m = base_model()
        del m["trees"]["ET-SUB"]
        r, v, _, _ = t.run(m)
        s = rows(r)
        t.check(list(s) == ["SEQ-M1", "SEQ-M2"], f"rows {list(s)}")
        t.check(metric(r, "CDF")["value_per_year"] == 0.0,
                "D-10: an unfollowed transfer row with end state CD is not counted")
        t.check(s["SEQ-M2"]["followed"] is None and s["SEQ-M2"]["transfer"] == "ET-SUB",
                "unfollowed row fields")
        t.check("transfer target ET-SUB not defined" in v.stdout, "validator warns")

    # ---- nested transfer ---------------------------------------------------
    def section_4():
        m = base_model()
        m["trees"]["ET-SUB"]["sequences"]["SEQ-T3"]["transfer"] = "ET-SUB2"
        m["gates"]["GT-B"] = {"label": "b", "formula": "BE-B"}
        m["trees"]["ET-SUB2"] = {
            "id": "ET-SUB2", "label": "sub2",
            "functional_events": {"FE-4": {"label": "f4", "top_gate": "GT-B"}},
            "sequences": {"SEQ-U1": {"path": {"FE-4": "success"}, "end_state": "CD2"},
                          "SEQ-U2": {"path": {"FE-4": "failure"}, "end_state": "CD3"}}}
        m["metrics"].append({"id": "CD3F", "label": "cd3", "end_states": ["CD3"]})
        r, v, qt, _ = t.run(m)
        s = rows(r)
        t.check("SEQ-M2>SEQ-T3" not in s, "intermediate followed hop is not a row")
        t.close(s["SEQ-M2>SEQ-T3>SEQ-U1"]["frequency_per_year"], 2.4e-4, "M2>T3>U1")
        t.close(s["SEQ-M2>SEQ-T3>SEQ-U2"]["frequency_per_year"], 6e-5, "M2>T3>U2")
        t.close(metric(r, "CD2F")["value_per_year"], 2.4e-4, "CD2F via nested transfer")
        t.close(metric(r, "CD3F")["value_per_year"], 6e-5, "CD3F")
        t.close(s["SEQ-M2"]["followed"]["sum_probability"], 0.1, "nested sum")
        t.check(v.returncode == 0 and qt.returncode == 0, f"{v.stdout}{qt.stderr}")

    # ---- transfer cycle ----------------------------------------------------
    def section_5():
        m = base_model()
        m["trees"]["ET-SUB"]["sequences"]["SEQ-T3"]["transfer"] = "ET-MAIN"
        p, v, qt, _ = t.run(m, expect_ok=False)
        t.check(isinstance(p, subprocess.CompletedProcess)
                and "transfer cycle: ET-MAIN/SEQ-M2 -> ET-SUB/SEQ-T3 -> ET-MAIN" in p.stderr,
                f"engine refuses the cycle: {getattr(p, 'stderr', p)}")
        t.check(v.returncode == 1 and "transfer cycle: ET-MAIN -> ET-SUB -> ET-MAIN"
                in v.stdout, f"validator: {v.stdout}")
        t.check(qt.returncode != 0, "quantify.py fails on a cycle")

    # ---- transfer-only tree nobody reaches ---------------------------------
    def section_6():
        m = base_model()
        m["trees"]["ET-SUB"]["sequences"]["SEQ-T3"]["transfer"] = None
        del m["trees"]["ET-SUB"]["sequences"]["SEQ-T3"]["transfer"]
        m["trees"]["ET-MAIN"]["sequences"]["SEQ-M2"].pop("transfer")
        m["trees"]["ET-MAIN"]["sequences"]["SEQ-M2"]["end_state"] = "OK"
        _, v, _, _ = t.run(m)
        t.check("ET-SUB: transfer-only event tree (no initiating event) that no "
                "sequence transfers into" in v.stdout, f"validator: {v.stdout}")

    # ---- Monte Carlo through transfers -------------------------------------
    def section_7():
        m = base_model()
        m["trees"]["ET-MAIN"]["initiating_event"]["frequency"]["uncertainty"] = {
            "distribution": "lognormal", "error_factor": 3.0}
        m["be"]["BE-A"]["failure_model"]["value"]["uncertainty"] = {
            "distribution": "beta", "alpha": 2.0, "beta": 18.0}
        point, _, _, _ = t.run(m)
        r, _, _, _ = t.run(m, "--samples", "2000", "--seed", "7", "--keep-samples")
        t.check(r["metrics"][0]["value_per_year"] == point["metrics"][0]["value_per_year"],
                "point CDF unchanged by --samples")
        s = rows(r)
        ied = r["uncertainty"]["initiating_event_draws"]
        own = [x["uncertainty"]["draws"] for x in r["sequences"] if x["transfer_path"] is None]
        worst = max(abs(sum(d[i] for d in own) / ied[i] - 1) for i in range(len(ied)))
        t.check(worst <= 1e-12, f"per-iteration partition over own rows: {worst}")
        exp = [s[k]["uncertainty"]["draws"] for k in
               ("SEQ-M2>SEQ-T1", "SEQ-M2>SEQ-T2", "SEQ-M2>SEQ-T3")]
        m2 = s["SEQ-M2"]["uncertainty"]["draws"]
        worst = max(abs(sum(d[i] for d in exp) - m2[i]) / m2[i] for i in range(len(m2)))
        t.check(worst <= 1e-12, f"per-iteration expansions = transfer row: {worst}")
        cd = s["SEQ-M2>SEQ-T2"]["uncertainty"]["draws"]
        t.check(metric(r, "CDF")["uncertainty"]["draws"] == [0.0 + x for x in cd],
                "CDF draws = the one aggregated CD row (transfer row excluded)")
        # E[CDF] = E[f] E[A (1 - C)] = 1e-2 * 0.1 * 0.7 (A and f independent)
        u = metric(r, "CDF")["uncertainty"]
        t.check(abs(u["mean"] - 7e-4) <= 6 * u["std_error_of_mean"],
                f"E[CDF] {u['mean']} ± {u['std_error_of_mean']} vs 7e-4")


    for fn, title in sections_extra + [(section_0, 'base fixture'), (section_1, 'standalone quantification of a transfer-only tree is refused'), (section_2, 'house overrides accumulate along the chain'), (section_3, 'unfollowed transfer (target absent): excluded, D-10 regression'), (section_4, 'nested transfer'), (section_5, 'transfer cycle'), (section_6, 'transfer-only tree nobody reaches'), (section_7, 'Monte Carlo through transfers')]:
        before = len(t.failures)
        try:
            fn()
        except Exception as e:           # an engine failure in one section
            t.failures.append(f"{title}: {type(e).__name__}: {e}")
        if len(t.failures) > before:
            t.failures.insert(before, f"[section: {title}]")

    if t.failures:
        for f in t.failures:
            print("FAIL:", f)
        return 1
    print("transfers: all hand-computed checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
