#!/usr/bin/env python3
"""Tests for named configurations (FR-31): quantify.py --configurations,
the engine's --param overrides, and compare.py's configuration section.

The central property: quantifying a configuration through overrides
(--house / --param) gives identical engine output (every value equal) to quantifying a
copy of the model edited to say the same thing (house-event defaults and
parameter values changed in the YAML). Checked for every configuration of
the demo model plus an added parameter configuration.

Usage: python ci/test_configurations.py [--engine PATH]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODEL = os.path.join(ROOT, "model")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", os.path.join(ROOT, "engine/target/release/canopy")))
    a = ap.parse_args()
    failures = []

    def check(cond, msg):
        (print(f"  ok  {msg}") if cond else failures.append(msg))

    def quantify(model, out, *extra):
        return subprocess.run([sys.executable, os.path.join(HERE, "quantify.py"),
                               model, out, "--engine", a.engine, *extra],
                              capture_output=True, text=True)

    tmp = tempfile.mkdtemp(prefix="psa-cfg-")
    try:
        head = os.path.join(tmp, "head")
        shutil.copytree(MODEL, head)
        man_path = os.path.join(head, "model.yaml")
        man = yaml.safe_load(open(man_path))
        man["configurations"]["PUMP-DATA-X2"] = {
            "label": "ECCS pump fail-to-start probability doubled",
            "parameters": {"PAR-ECC-PMP-FTS": 2.4e-3}}
        yaml.safe_dump(man, open(man_path, "w"), sort_keys=False)
        v = subprocess.run([sys.executable, os.path.join(HERE, "validate.py"), head,
                            os.path.join(ROOT, "schema", "psa-model.schema.json")],
                           capture_output=True, text=True)
        check(v.returncode == 0, f"model with a parameter configuration validates: {v.stdout}")

        res, cfg = os.path.join(tmp, "res.json"), os.path.join(tmp, "cfg.json")
        q = quantify(head, res, "--configurations", cfg)
        check(q.returncode == 0 and "configuration TRAIN-A-OOS" in q.stdout,
              f"quantify.py --configurations: {q.stdout}{q.stderr}")
        cres = json.load(open(cfg))
        check(sorted(cres) == ["BASE", "PUMP-DATA-X2", "TRAIN-A-OOS"],
              f"every configuration quantified: {sorted(cres)}")
        base = json.load(open(res))
        check(cres["BASE"] == base, "BASE (no overrides) = the base case (identical output)")

        # each configuration = the model edited to say the same thing
        for cid, c in man["configurations"].items():
            edited = os.path.join(tmp, f"edited-{cid}")
            shutil.copytree(head, edited)
            hp = os.path.join(edited, "house-events.yaml")
            h = yaml.safe_load(open(hp))
            for he, val in (c.get("house_events") or {}).items():
                h["house_events"][he]["default"] = val
            yaml.safe_dump(h, open(hp, "w"), sort_keys=False)
            pp = os.path.join(edited, "parameters.yaml")
            pdoc = yaml.safe_load(open(pp))
            for par, val in (c.get("parameters") or {}).items():
                pdoc["parameters"][par]["value"] = val
            yaml.safe_dump(pdoc, open(pp, "w"), sort_keys=False)
            out = os.path.join(tmp, f"edited-{cid}.json")
            q = quantify(edited, out)
            check(q.returncode == 0 and json.load(open(out)) == cres[cid],
                  f"configuration {cid} = the edited model (identical output)")

        x2 = cres["PUMP-DATA-X2"]["ET-SLOCA"]["metrics"][0]["value_per_year"]
        b0 = base["ET-SLOCA"]["metrics"][0]["value_per_year"]
        check(x2 > b0, f"doubled pump data raises CDF ({b0:.4e} -> {x2:.4e})")

        # compare.py: a configuration-only change is reported, not neutral
        cmp = lambda bc, hc: subprocess.run(
            [sys.executable, os.path.join(HERE, "compare.py"), res, res,
             "--configurations", bc, hc], capture_output=True, text=True).stdout
        md = cmp(cfg, cfg)
        check("Named configurations" in md and "quantitatively neutral" in md
              and "×43.5" in md, "compare: unchanged configurations, neutral, ×43.5 shown")
        changed = json.load(open(cfg))
        changed["TRAIN-A-OOS"]["ET-SLOCA"]["metrics"][0]["value_per_year"] *= 2
        cfg2 = os.path.join(tmp, "cfg2.json")
        json.dump(changed, open(cfg2, "w"))
        md = cmp(cfg, cfg2)
        check("quantitatively neutral" not in md and "🔺 +100.00%" in md,
              "compare: a configuration-only change is reported and not called neutral")

        # misuse
        e = subprocess.run([a.engine, head, "ET-SLOCA", "--param", "PAR-NOPE=1"],
                           capture_output=True, text=True)
        check(e.returncode != 0 and "no such parameter" in e.stderr, "unknown --param refused")
        e = subprocess.run([a.engine, head, "ET-SLOCA", "--param",
                            "PAR-ECC-PMP-FTS=1e-3", "--samples", "10"],
                           capture_output=True, text=True)
        check(e.returncode != 0 and "not supported" in e.stderr,
              "--param with --samples refused")
        e = subprocess.run([a.engine, head, "ET-SLOCA", "--param", "PAR-ECC-PMP-FTS=-1"],
                           capture_output=True, text=True)
        check(e.returncode != 0, "negative --param refused")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("configurations: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
