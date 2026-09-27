#!/usr/bin/env python3
"""Tests for the generated report appendices (ci/appendix.py, FR-32).

On the demo model (point and sampled results) and on property-harness
models (CCF groups, house events, non-coherent logic, a transfer to a
transfer-only tree), the appendix must match the model and the engine
exactly:
  * every parameter, basic event, CCF group, house event, sequence and gate
    appears exactly once, and nothing else does;
  * every basic event's probability cell is the engine's value (the
    results' basic_event_probabilities, formatted), every sequence
    frequency and metric value the results' value, every CCF Q_k the
    engine's combination-event probability;
  * every provenance source and justification is copied verbatim;
  * the output is identical under different hash seeds (reproducible).

Usage: python ci/test_appendix.py [--engine PATH]
"""
import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import property_test as pt  # noqa: E402  (model generator)


def tables(md: str) -> dict:
    """{section header: [row cells]} for every markdown table."""
    out, sec = {}, None
    for line in md.splitlines():
        if line.startswith("## ") or line.startswith("### "):
            sec = line.lstrip("# ").strip()
            out.setdefault(sec, [])
        elif line.startswith("| ") and not line.startswith("|---") and sec:
            cells = [c.replace("\\|", "|").strip()
                     for c in re.split(r"(?<!\\)\|", line.strip()[1:-1])]
            out[sec].append(cells)
    return {k: v[1:] for k, v in out.items() if v}     # drop header rows


def num(x):
    return f"{x:.4e}"


def check_model(model, engine, samples, check, tag):
    tmp = tempfile.mkdtemp(prefix="psa-app-")
    try:
        res = os.path.join(tmp, "res.json")
        extra = ["--samples", str(samples)] if samples else []
        subprocess.run([sys.executable, os.path.join(HERE, "quantify.py"), model, res,
                        "--engine", engine, *extra], check=True, capture_output=True)
        outs = []
        for seed in ("0", "1", "2"):
            o = os.path.join(tmp, f"a{seed}.md")
            subprocess.run([sys.executable, os.path.join(HERE, "appendix.py"), model, res, o],
                           check=True, capture_output=True,
                           env={**os.environ, "PYTHONHASHSEED": seed})
            outs.append(open(o).read())
        check(outs[0] == outs[1] == outs[2], f"{tag}: identical under three hash seeds")
        md = outs[0]
        t = tables(md)
        results = json.load(open(res))
        probs = {}
        for r in results.values():
            probs.update(r.get("basic_event_probabilities", {}))
        load = lambda f, k: (yaml.safe_load(open(os.path.join(model, f))) or {}).get(k) or {}
        params = load("parameters.yaml", "parameters")
        house = load("house-events.yaml", "house_events")
        ccf = load("ccf-groups.yaml", "ccf_groups") if os.path.exists(
            os.path.join(model, "ccf-groups.yaml")) else {}
        bes = {}
        for f in sorted(os.listdir(os.path.join(model, "basic-events"))):
            bes.update(load(f"basic-events/{f}", "basic_events"))
        gates = {}
        for f in sorted(os.listdir(os.path.join(model, "fault-trees"))):
            for ft in load(f"fault-trees/{f}", "fault_trees").values():
                gates.update(ft.get("gates") or {})

        def ids(sec):
            return [r[0] for r in t.get(sec, [])]
        check(sorted(ids("A.3 Parameters")) == sorted(params), f"{tag}: parameters, once each")
        check(sorted(ids("A.4 Basic events")) == sorted(bes), f"{tag}: basic events, once each")
        check(sorted(ids("A.6 House events")) == sorted(house), f"{tag}: house events, once each")
        if ccf:
            check(sorted(ids("A.5 Common-cause failure groups")) == sorted(ccf),
                  f"{tag}: CCF groups, once each")
        gate_rows = [r[1] for r in t.get("A.8 Fault-tree gates", [])]
        check(sorted(gate_rows) == sorted(gates), f"{tag}: gates, once each")
        ok_p = all(r[4] == (num(probs[r[0]]) if r[0] in probs else "not quantified")
                   for r in t["A.4 Basic events"])
        check(ok_p, f"{tag}: every basic-event probability is the engine's value")
        ok_prov = all(r[6] == " ".join(str((bes[r[0]].get("provenance") or {}).get("source", "")).split())
                      and r[7] == " ".join(str((bes[r[0]].get("provenance") or {}).get("justification", "")).split())
                      for r in t["A.4 Basic events"])
        check(ok_prov, f"{tag}: basic-event provenance verbatim")
        for gid, g in ccf.items():
            row = next(r for r in t["A.5 Common-cause failure groups"] if r[0] == gid)
            for k in range(2, len(g["members"]) + 1):
                v = next(val for e, val in sorted(probs.items())
                         if e.startswith(f"BE-{gid}-") and len(e[len(f'BE-{gid}-'):].split('-')) == k)
                check(f"Q{k} {num(v)}" in row[6], f"{tag}: {gid} Q{k} = engine value")
        seq_count = 0
        for tid, r in results.items():
            sec = next(k for k in t if k.startswith(f"{tid} — "))
            rows = {row[0]: row for row in t[sec] if row[0].startswith("SEQ-")}
            own = [s for s in r["sequences"] if s["transfer_path"] is None]
            seq_count += len(own)
            check(sorted(rows) == sorted(s["id"] for s in own)
                  and all(rows[s["id"]][4] == num(s["frequency_per_year"]) for s in own),
                  f"{tag}: {tid} sequence rows and frequencies = results")
        totals = {}
        for r in results.values():
            for mt in r["metrics"]:
                totals[mt["id"]] = totals.get(mt["id"], 0.0) + mt["value_per_year"]
        check(all(row[3] == num(totals[row[0]]) for row in t["A.1 Risk metrics"]),
              f"{tag}: metric values = results")
        return seq_count
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", os.path.join(ROOT, "engine/target/release/canopy")))
    a = ap.parse_args()
    failures = []

    def check(cond, msg):
        (print(f"  ok  {msg}") if cond else failures.append(msg))

    check_model(os.path.join(ROOT, "model"), a.engine, 0, check, "demo (point)")
    check_model(os.path.join(ROOT, "model"), a.engine, 500, check, "demo (sampled)")
    tmp = tempfile.mkdtemp(prefix="psa-app-gen-")
    try:
        for i in (1, 4, 7, 12):
            m = pt.gen_model(random.Random(20260708 * 1_000_003 + i))
            x = pt.gen_transfer(m, random.Random((20260708 * 1_000_003 + i) ^ 0x7EA5_F3E5))
            d = os.path.join(tmp, f"case{i}")
            os.makedirs(d)
            pt.write_transfer_model(m, x, d)
            check_model(d, a.engine, 0, check, f"harness case {i}"
                        + (" (CCF)" if m["ccf"] else ""))
        # uncertain CCF factors (FR-36): shown in the group row and in each
        # member's distribution cell
        for i in range(60):
            m = pt.gen_model(random.Random(20260708 * 1_000_003 + i))
            if m["ccf"]:
                break
        u = pt.gen_uncertainty(m, random.Random(1))
        u["factor"] = 12.5
        d = os.path.join(tmp, "factor")
        os.makedirs(d)
        pt.write_uncertain_model(m, u, d)
        check_model(d, a.engine, 0, check, f"harness case {i} with uncertain CCF factors")
        res, out = os.path.join(tmp, "f.json"), os.path.join(tmp, "f.md")
        subprocess.run([sys.executable, os.path.join(HERE, "quantify.py"), d, res,
                        "--engine", a.engine], check=True, capture_output=True)
        subprocess.run([sys.executable, os.path.join(HERE, "appendix.py"), d, res, out],
                       check=True, capture_output=True)
        t = tables(open(out).read())
        grow = next(r for r in t["A.5 Common-cause failure groups"] if r[0] == "CCF-G1")
        mrows = [r for r in t["A.4 Basic events"] if r[0] in m["ccf"]["members"]]
        check("(Dirichlet, concentration 12.5)" in grow[5]
              and all("factors (Dirichlet, concentration 12.5)" in r[5] for r in mrows),
              "factor uncertainty shown in the group row and every member's distribution")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("appendix: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
