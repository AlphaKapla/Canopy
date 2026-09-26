#!/usr/bin/env python3
"""Cross-verify the Rust engine against SCRAM on MEF exports.

Runs the demo model plus N randomly generated models (the property-test
generator) through BOTH engines and compares every sequence probability.
Exports use --expand-ccf so the comparison is convention-independent
(SCRAM's alpha-factor is non-staggered; ours defaults to staggered).

Requires `scram` on PATH (see docs/ci.md for the build recipe).

Usage: crosscheck_scram.py [--cases N] [--seed S]
"""
import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(__file__))
from property_test import gen_model, write_model  # noqa: E402

REL_TOL = 2e-5   # SCRAM reports 6 significant digits


def close(a, b):
    return abs(a - b) <= max(1e-12, REL_TOL * max(abs(a), abs(b)))


def scram_sequences(xml_path):
    out = tempfile.mktemp(suffix=".xml")
    subprocess.run(["scram", "--bdd", "--probability", xml_path, "-o", out],
                   check=True, capture_output=True)
    seqs = {}
    for s in ET.parse(out).getroot().iter("sequence"):
        seqs[s.get("name")] = float(s.get("value"))
    os.unlink(out)
    return seqs


def engine_sequences(engine, model_dir, et_id):
    r = json.loads(subprocess.run(
        [engine, model_dir, et_id, "--json"],
        check=True, capture_output=True, text=True).stdout)
    ie = r["initiating_event"]["frequency_per_year"]
    # The tree's own rows only: the MEF export does not carry transfers, so
    # SCRAM sees a transfer sequence as an ordinary one and no expansions.
    return {s["id"]: s["frequency_per_year"] / ie for s in r["sequences"]
            if s.get("transfer_path") is None}


def check(engine, model_dir, et_id, label):
    xml = tempfile.mktemp(suffix=".xml")
    subprocess.run([sys.executable, "ci/export_mef.py", model_dir, xml,
                    "--expand-ccf"], check=True, capture_output=True)
    ours = engine_sequences(engine, model_dir, et_id)
    theirs = scram_sequences(xml)
    os.unlink(xml)
    problems = []
    if set(ours) != set(theirs):
        problems.append(f"sequence sets differ: {set(ours) ^ set(theirs)}")
    for sid in sorted(set(ours) & set(theirs)):
        if not close(ours[sid], theirs[sid]):
            problems.append(
                f"{sid}: engine {ours[sid]:.6e} scram {theirs[sid]:.6e}")
    status = "ok " if not problems else "FAIL"
    print(f"{status} {label}: {len(ours)} sequences compared")
    for p in problems:
        print("    ", p)
    return not problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=25)
    ap.add_argument("--seed", type=int, default=20260708)
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", "engine/target/release/canopy"))
    a = ap.parse_args()

    ok = check(a.engine, "model", "ET-SLOCA", "demo model")
    for i in range(a.cases):
        rng = random.Random(a.seed * 7_919 + i)
        d = tempfile.mkdtemp(prefix="psa-xc-")
        try:
            write_model(gen_model(rng), d)
            ok &= check(a.engine, d, "ET-TEST", f"generated case {i}")
        finally:
            shutil.rmtree(d, ignore_errors=True)
    print("\nCROSS-CHECK", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
