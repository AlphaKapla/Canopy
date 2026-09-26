#!/usr/bin/env python3
"""Benchmark the engine against SCRAM on a directory of MEF fault trees
(e.g. the Aralia suite bundled in SCRAM's input/Aralia).

For each XML file: import to YAML (ci/import_mef.py), quantify P(top) with
our engine (--prob-only), quantify with SCRAM (--bdd --probability), and
compare (rel tol 2e-5, bounded by SCRAM's 6-significant-digit report).
Both engines run under the same timeout and a 4 GiB address-space cap;
per-case failures (timeout/memory) are reported, not fatal — an honest
scalability profile is part of the result.

With --importance, both engines also compute importance measures on
every tree they both quantify: our Birnbaum importance (P(S|e) − P(S|¬e),
FR-6) must equal SCRAM's Marginal Importance Factor (MIF, same
definition) and our RAW, derived as (P + (1 − p)·B)/P, SCRAM's RAW, per
basic event, to the same tolerance; SCRAM's names are matched through the
`external_ids: {mef: ...}` the importer writes. SCRAM reports importance
only for events occurring in its products, so its importance pass runs
separately with products limited to order 2 (`-l 2`; the order-1 limit
of the probability pass would report only single-event cut sets): events
whose smallest cut set has a higher order are not compared, and a tree
with no reported event counts as "not compared", never as agreement.

Usage: benchmark_mef.py <xml-dir> [--timeout 60] [--engine PATH]
                        [--importance]
"""
import argparse
import glob
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

import yaml

REL_TOL = 2e-5
IMP_ABS = 1e-14   # absolute floor for importance values rounded to ~0
MEM_BYTES = int(os.environ.get("PSA_BENCH_MEM_GIB", 4)) << 30


def limits():
    # The address-space cap is enforced on Linux (CI); macOS rejects
    # RLIMIT_AS, so there the run is uncapped (reported in the summary).
    try:
        resource.setrlimit(resource.RLIMIT_AS, (MEM_BYTES, MEM_BYTES))
    except (ValueError, OSError):
        pass


def cap_enforced() -> bool:
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (soft, hard))
        return sys.platform.startswith("linux")
    except (ValueError, OSError):
        return False


def run(cmd, timeout):
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, preexec_fn=limits)
        dt = time.monotonic() - t0
        if p.returncode != 0:
            return None, dt, "crash/oom"
        return p.stdout, dt, None
    except subprocess.TimeoutExpired:
        return None, timeout, "timeout"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("xml_dir")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", "engine/target/release/canopy"))
    ap.add_argument("--importance", action="store_true")
    a = ap.parse_args()
    imp_agree = imp_disagree = imp_skipped = imp_events = 0
    imp_notes = []

    files = sorted(glob.glob(os.path.join(a.xml_dir, "*.xml")))
    print(f"{'case':<12} {'BEs':>5} {'gates':>6} | "
          f"{'ours P(top)':>12} {'t(s)':>6} {'nodes':>9} | "
          f"{'SCRAM P(top)':>12} {'t(s)':>6} | verdict")
    print("-" * 96)
    agree = disagree = incomplete = 0

    for f in files:
        name = os.path.splitext(os.path.basename(f))[0]
        d = tempfile.mkdtemp(prefix="psa-bench-")
        try:
            imp = subprocess.run(
                [sys.executable, "ci/import_mef.py", f, d],
                capture_output=True, text=True)
            if imp.returncode != 0:
                print(f"{name:<12} {'—':>5} {'—':>6} | import failed: "
                      f"{imp.stderr.strip().splitlines()[-1]}")
                incomplete += 1
                continue
            nbe = imp.stdout.split(" gates, ")[1].split(" basic")[0]
            ngt = imp.stdout.split("imported ")[1].split(": ")[1].split(
                " gates")[0]

            ours, to, eo = run([a.engine, d, "FT-MAIN", "--json"]
                               + (["--mcs-limit", "0"] if a.importance
                                  else ["--prob-only"]), a.timeout)
            rep = tempfile.mktemp(suffix=".xml")
            # -l 1: probability comes from the BDD and is unaffected;
            # this only truncates the report's product listing, which on
            # large trees otherwise reaches gigabytes.
            sout, ts, es = run(["scram", "--bdd", "--probability", "-l", "1"]
                               + (["--importance"] if a.importance else [])
                               + [f, "-o", rep], a.timeout)

            po = pn = None
            if ours:
                j = json.loads(ours)
                po, nodes = j["probability"], j["bdd_nodes"]
            ps = None
            scram_imp = {}
            if es is None and os.path.exists(rep):
                root = ET.parse(rep).getroot()
                for sp in root.iter("sum-of-products"):
                    ps = float(sp.get("probability"))
                for im in root.iter("importance"):
                    for be in im.findall("basic-event"):
                        scram_imp[be.get("name")] = (
                            float(be.get("probability")), float(be.get("MIF")),
                            float(be.get("RAW")))
            if os.path.exists(rep):
                os.unlink(rep)
            if a.importance and po is not None and ps is not None:
                rep2 = tempfile.mktemp(suffix=".xml")
                _, _, ei = run(["scram", "--bdd", "--probability", "--importance",
                                "-l", "2", f, "-o", rep2], a.timeout)
                if ei is None and os.path.exists(rep2):
                    for im in ET.parse(rep2).getroot().iter("importance"):
                        for be in im.findall("basic-event"):
                            scram_imp[be.get("name")] = (
                                float(be.get("probability")), float(be.get("MIF")),
                                float(be.get("RAW")))
                if os.path.exists(rep2):
                    os.unlink(rep2)
                bes = yaml.safe_load(open(os.path.join(
                    d, "basic-events", "imported.yaml")))["basic_events"]
                ours_b = {r["event"]: r["importance"] for r in j["birnbaum"]}
                by_mef = {be["external_ids"]["mef"]: bid for bid, be in bes.items()}
                worst, bad, n = 0.0, [], 0
                for mname, (pe, mif, raw) in sorted(scram_imp.items()):
                    bid = by_mef.get(mname)
                    if bid is None:
                        bad.append(f"{mname}: not in the imported model")
                        continue
                    b = ours_b.get(bid, 0.0)
                    p_e = j["basic_event_probabilities"][bid]
                    our_raw = (po + (1.0 - p_e) * b) / po if po > 0 else None
                    n += 1
                    for label, x, y in (("MIF", b, mif), ("RAW", our_raw, raw)):
                        if x is None:
                            continue
                        scale = max(abs(x), abs(y))
                        if abs(x - y) > max(REL_TOL * scale, IMP_ABS):
                            bad.append(f"{mname} {label}: ours {x:.6e} "
                                       f"SCRAM {y:.6e}")
                        elif scale > IMP_ABS:
                            worst = max(worst, abs(x - y) / scale)
                if bad:
                    imp_disagree += 1
                    imp_notes.append(f"{name}: importance DISAGREE on "
                                     f"{len(bad)} value(s): {bad[:3]}")
                elif n == 0:
                    imp_skipped += 1
                    imp_notes.append(f"{name}: importance not compared (SCRAM "
                                     f"reported no event{'' if ei is None else ': ' + ei})")
                else:
                    imp_agree += 1
                    imp_events += n
                    imp_notes.append(f"{name}: importance agree on {n} of "
                                     f"{len(bes)} events (MIF and RAW; max rel "
                                     f"diff {worst:.1e})")

            oc = f"{po:.6e}" if po is not None else eo
            sc = f"{ps:.6e}" if ps is not None else (es or "no result")
            nn = f"{nodes}" if po is not None else "—"
            if po is not None and ps is not None:
                ok = abs(po - ps) <= max(1e-12,
                                         REL_TOL * max(abs(po), abs(ps)))
                verdict = "AGREE" if ok else "DISAGREE"
                agree += ok
                disagree += not ok
            else:
                verdict = "incomplete"
                incomplete += 1
            print(f"{name:<12} {nbe:>5} {ngt:>6} | {oc:>12} {to:>6.1f} "
                  f"{nn:>9} | {sc:>12} {ts:>6.1f} | {verdict}")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    print("-" * 96)
    print(f"{agree} agree, {disagree} disagree, {incomplete} incomplete "
          f"(timeout {a.timeout}s, mem cap "
          f"{f'{MEM_BYTES >> 30} GiB per side' if cap_enforced() else 'NOT enforced on this platform'})")
    if a.importance:
        for note in imp_notes:
            print(f"  {note}")
        print(f"importance: {imp_agree} tree(s) agree on {imp_events} events, "
              f"{imp_disagree} disagree, {imp_skipped} not compared (Birnbaum "
              f"vs SCRAM MIF, RAW vs RAW, per basic event SCRAM reports)")
    return 1 if disagree or imp_disagree else 0


if __name__ == "__main__":
    sys.exit(main())
