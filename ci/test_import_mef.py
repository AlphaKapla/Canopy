#!/usr/bin/env python3
"""Tests for ci/import_mef.py beyond fault trees (FR-15): CCF groups, event
trees, untyped references, and every refusal rule — on small hand-written
MEF files with hand-computed results, through the validator and the
engine.

Usage: python ci/test_import_mef.py [--engine PATH]
"""
import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCHEMA = os.path.join(ROOT, "schema", "psa-model.schema.json")

# Two trains, each valve OR pump; untyped <event> references to gates and
# basic events; beta-factor group on the pumps (bare <factor>), alpha-factor
# group on the valves (<factors>). Non-staggered (MEF convention):
#   pumps  Qt 0.1, beta 0.2      -> Q1 = 0.08, Q2 = 0.02
#   valves Qt 0.05, alpha 0.9/0.1 -> alpha_t = 1.1,
#          Q1 = 0.9/1.1 * 0.05, Q2 = 2 * 0.1/1.1 * 0.05
TRAINS = """<?xml version="1.0"?>
<opsa-mef>
  <define-fault-tree name="Trains">
    <define-gate name="Top"><and><event name="TrainA"/><event name="TrainB"/></and></define-gate>
    <define-gate name="TrainA"><or><event name="ValveA"/><event name="PumpA"/></or></define-gate>
    <define-gate name="TrainB"><or><event name="ValveB"/><event name="PumpB"/></or></define-gate>
  </define-fault-tree>
  <define-CCF-group name="Pumps" model="beta-factor">
    <members><basic-event name="PumpA"/><basic-event name="PumpB"/></members>
    <distribution><float value="0.1"/></distribution>
    <factor level="2"><float value="0.2"/></factor>
  </define-CCF-group>
  <define-CCF-group name="Valves" model="alpha-factor">
    <members><basic-event name="ValveA"/><basic-event name="ValveB"/></members>
    <distribution><float value="0.05"/></distribution>
    <factors>
      <factor level="1"><float value="0.9"/></factor>
      <factor level="2"><float value="0.1"/></factor>
    </factors>
  </define-CCF-group>
</opsa-mef>
"""


def trains_p_top():
    q1p, q2p = 0.8 * 0.1, 0.2 * 0.1
    at = 0.9 + 2 * 0.1
    q1v, q2v = 0.9 / at * 0.05, 2 * 0.1 / at * 0.05
    p_common = 1 - (1 - q2p) * (1 - q2v)
    p_train = 1 - (1 - q1p) * (1 - q1v)
    return p_common + (1 - p_common) * p_train * p_train


# Event tree: initiator frequency 1e-2 /yr; F1 collects gate G1 = a or b,
# F2 collects the basic event c directly (-> pass-through gate), and the
# end state "Damage" is reached by two paths. P(a) = 0.1, P(b) = 0.2,
# P(c) = 0.3. G1 = 1 - 0.9*0.8 = 0.28.
#   F1 fails                     -> Damage  0.28          (F2 bypassed)
#   F1 works, F2 fails           -> Damage  0.72 * 0.3
#   F1 works, F2 works           -> Safe    0.72 * 0.7
ET = """<?xml version="1.0"?>
<opsa-mef>
  <define-initiating-event name="Init" event-tree="Tree"><float value="0.01"/></define-initiating-event>
  <define-event-tree name="Tree">
    <define-functional-event name="F1"/>
    <define-functional-event name="F2"/>
    <define-sequence name="Safe"/>
    <define-sequence name="Damage"/>
    <initial-state>
      <fork functional-event="F1">
        <path state="works">
          <collect-formula><not><gate name="G1"/></not></collect-formula>
          <fork functional-event="F2">
            <path state="works">
              <collect-formula><not><basic-event name="c"/></not></collect-formula>
              <sequence name="Safe"/>
            </path>
            <path state="fails">
              <collect-formula><basic-event name="c"/></collect-formula>
              <sequence name="Damage"/>
            </path>
          </fork>
        </path>
        <path state="fails">
          <collect-formula><gate name="G1"/></collect-formula>
          <sequence name="Damage"/>
        </path>
      </fork>
    </initial-state>
  </define-event-tree>
  <define-fault-tree name="Support">
    <define-gate name="G1"><or><basic-event name="a"/><basic-event name="b"/></or></define-gate>
  </define-fault-tree>
  <model-data>
    <define-basic-event name="a"><float value="0.1"/></define-basic-event>
    <define-basic-event name="b"><float value="0.2"/></define-basic-event>
    <define-basic-event name="c"><float value="0.3"/></define-basic-event>
  </model-data>
</opsa-mef>
"""


def et_variant(old, new):
    assert old in ET, old
    return ET.replace(old, new, 1)


REFUSALS = [
    ("fork with one path", et_variant(
        """        <path state="fails">
          <collect-formula><gate name="G1"/></collect-formula>
          <sequence name="Damage"/>
        </path>
      </fork>
    </initial-state>""", """      </fork>
    </initial-state>"""), "has 1 paths"),
    ("paths not complementary", et_variant(
        """<collect-formula><gate name="G1"/></collect-formula>
          <sequence name="Damage"/>""",
        """<collect-formula><basic-event name="a"/></collect-formula>
          <sequence name="Damage"/>"""), "does not collect a formula and its negation"),
    ("set-house-event instruction", et_variant(
        """<sequence name="Safe"/>
            </path>""", """<set-house-event name="h"><constant value="true"/></set-house-event>
              <sequence name="Safe"/>
            </path>"""), "<set-house-event> has no Canopy equivalent"),
    ("formula collected outside a fork", et_variant(
        """    <initial-state>
      <fork""", """    <initial-state>
      <collect-formula><gate name="G1"/></collect-formula>
      <fork"""), "formula collected outside a fork path"),
    ("MGL group", TRAINS.replace('model="alpha-factor"', 'model="MGL"'),
     "model 'MGL' not supported"),
    ("parameter", ET.replace("<model-data>", '<model-data><define-parameter name="p"><float value="1"/></define-parameter>'),
     "parameters not supported"),
    ("ambiguous untyped reference", TRAINS.replace(
        '<define-gate name="TrainB">', '<define-gate name="PumpA"><or><event name="ValveA"/><event name="ValveB"/></or></define-gate>\n    <define-gate name="TrainB">'),
     "cannot resolve an untyped reference"),
]


def run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", os.path.join(ROOT, "engine/target/release/canopy")))
    a = ap.parse_args()
    failures = []

    def check(cond, msg):
        (print(f"  ok  {msg}") if cond else failures.append(msg))

    def close(x, y):
        return abs(x - y) <= 1e-14 * max(abs(x), abs(y))

    tmp = tempfile.mkdtemp(prefix="psa-mefimp-")
    try:
        def imp(name, text):
            xml, out = os.path.join(tmp, f"{name}.xml"), os.path.join(tmp, name)
            open(xml, "w").write(text)
            return run([sys.executable, os.path.join(HERE, "import_mef.py"), xml, out]), out

        # trains: untyped refs + both CCF encodings
        r, out = imp("trains", TRAINS)
        check(r.returncode == 0, f"trains import: {r.stderr}")
        v = run([sys.executable, os.path.join(HERE, "validate.py"), out, SCHEMA])
        check(v.returncode == 0, f"trains validates: {v.stdout}")
        e = run([a.engine, out, "FT-MAIN", "--json", "--prob-only"])
        if e.returncode == 0:
            p = json.loads(e.stdout)["probability"]
            check(close(p, trains_p_top()),
                  f"trains P(top) {p} = hand-computed non-staggered {trains_p_top()}")
        else:
            failures.append(f"trains engine: {e.stderr}")
        ccf = open(os.path.join(out, "ccf-groups.yaml")).read()
        check("testing: non-staggered" in ccf and "beta: 0.2" in ccf
              and "alpha_2: 0.1" in ccf, "CCF groups imported non-staggered with factors")

        # event tree
        r, out = imp("tree", ET)
        check(r.returncode == 0 and "note:" not in r.stderr.replace(
            "note: event tree Tree", ""), f"tree import: {r.stderr}")
        v = run([sys.executable, os.path.join(HERE, "validate.py"), out, SCHEMA])
        check(v.returncode == 0 and "0 error(s)" in v.stdout, f"tree validates: {v.stdout}")
        e = run([a.engine, out, "ET-TREE", "--json"])
        if e.returncode == 0:
            j = json.loads(e.stdout)
            rows = {(s["end_state"], tuple(sorted(s["cut_sets"][0]["events"]))
                     if s["cut_sets"] else ()): s["frequency_per_year"]
                    for s in j["sequences"]}
            freq = {s["id"]: (s["end_state"], s["frequency_per_year"]) for s in j["sequences"]}
            check(len(freq) == 3, f"three rows (one per path): {sorted(freq)}")
            dmg = sorted(f for es, f in freq.values() if es == "Damage")
            check(len(dmg) == 2 and close(dmg[0], 0.01 * 0.72 * 0.3)
                  and close(dmg[1], 0.01 * 0.28), f"Damage rows {dmg}")
            metrics = {m["id"]: m["value_per_year"] for m in j["metrics"]}
            check(close(metrics["Damage"], 0.01 * (0.28 + 0.72 * 0.3))
                  and close(metrics["Safe"], 0.01 * 0.72 * 0.7),
                  f"one metric per end state, summing its paths: {metrics}")
            check(close(j["initiating_event"]["frequency_per_year"], 0.01),
                  "initiator frequency taken from the file")
            check(close(j["partition"]["sum_probability"], 1.0), "partition = 1")
        else:
            failures.append(f"tree engine: {e.stderr}")
        et_yaml = open(os.path.join(out, "event-trees", "et-tree.yaml")).read()
        check("GT-FE-F2" in et_yaml, "F2 (collects a basic event) gets a pass-through gate")
        check("bypassed" in et_yaml, "F2 is bypassed on the path where F1 fails")

        for name, text, frag in REFUSALS:
            r, _ = imp("refused", text)
            check(r.returncode != 0 and frag in r.stderr,
                  f"refused: {name} ({r.stderr.strip()[:90]})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("import_mef: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
