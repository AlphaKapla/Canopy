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

A value disagreement is adjudicated by definition, with SCRAM alone: the
tree is re-quantified by SCRAM with the event's probability set to 1 and
to 0 (a copy of the MEF file), and SCRAM's own P(S|e) − P(S|¬e) is
compared with both importance values. If it confirms ours, the event is
reported as a reference inconsistency (SCRAM's importance output
contradicts SCRAM's own probabilities; V&V F-6) — listed, not counted as
agreement; if it does not, the disagreement stands and the run fails.

With --primes K, on every tree containing negation (non-coherent) both
engines list the prime implicants of order <= K (SCRAM --prime-implicants
-l K; ours --prime-implicants --order-limit K) and the two sets of
products — events and negated events — must be equal.

Usage: benchmark_mef.py <xml-dir> [--timeout 60] [--engine PATH]
                        [--importance] [--primes K]
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


def scram_prob_with(xml_path, event, value, timeout):
    """SCRAM's P(top) for a copy of the MEF file with basic event `event`'s
    probability set to `value` (None when SCRAM fails)."""
    tree = ET.parse(xml_path)
    hit = False
    for be in tree.getroot().iter("define-basic-event"):
        if be.get("name") == event:
            fl = be.find("float")
            if fl is not None:
                fl.set("value", repr(float(value)))
                hit = True
    if not hit:
        return None
    src = tempfile.mktemp(suffix=".xml")
    out = tempfile.mktemp(suffix=".xml")
    tree.write(src)
    try:
        _, _, err = run(["scram", "--bdd", "--probability", "-l", "1", src,
                         "-o", out], timeout)
        if err is not None or not os.path.exists(out):
            return None
        for sp in ET.parse(out).getroot().iter("sum-of-products"):
            return float(sp.get("probability"))
        return None
    finally:
        for f in (src, out):
            if os.path.exists(f):
                os.unlink(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("xml_dir")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", "engine/target/release/canopy"))
    ap.add_argument("--importance", action="store_true")
    ap.add_argument("--primes", type=int, metavar="K")
    a = ap.parse_args()
    pi_agree = pi_disagree = pi_skipped = pi_products = 0
    pi_notes = []
    imp_agree = imp_disagree = imp_skipped = imp_events = imp_ref = 0
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
                worst, bad, n, ref_issues = 0.0, [], 0, []
                for mname, (pe, mif, raw) in sorted(scram_imp.items()):
                    bid = by_mef.get(mname)
                    if bid is None:
                        bad.append(f"{mname}: not in the imported model")
                        continue
                    b = ours_b.get(bid, 0.0)
                    p_e = j["basic_event_probabilities"][bid]
                    our_raw = (po + (1.0 - p_e) * b) / po if po > 0 else None
                    n += 1
                    close = lambda x, y: abs(x - y) <= max(
                        REL_TOL * max(abs(x), abs(y)), IMP_ABS)
                    if not (close(b, mif) and (our_raw is None or close(our_raw, raw))):
                        # adjudicate by definition, with SCRAM alone
                        p1 = scram_prob_with(f, mname, 1.0, a.timeout)
                        p0 = scram_prob_with(f, mname, 0.0, a.timeout)
                        if p1 is not None and p0 is not None and close(p1 - p0, b) \
                                and not close(p1 - p0, mif):
                            ref_issues.append(
                                f"{mname}: SCRAM MIF {mif:.6e} but SCRAM's own "
                                f"P(S|e) - P(S|not e) = {p1 - p0:.6e} = ours {b:.6e}")
                        else:
                            bad.append(f"{mname}: ours MIF {b:.6e} RAW {our_raw}, SCRAM "
                                       f"MIF {mif:.6e} RAW {raw:.6e}, SCRAM "
                                       f"requantified difference "
                                       f"{None if p1 is None or p0 is None else p1 - p0}")
                        continue
                    for x, y in ((b, mif), (our_raw, raw)):
                        if x is not None and max(abs(x), abs(y)) > IMP_ABS:
                            worst = max(worst, abs(x - y) / max(abs(x), abs(y)))
                if ref_issues:
                    imp_ref += 1
                    imp_notes.append(f"{name}: {len(ref_issues)} event(s) where "
                                     f"SCRAM's importance contradicts SCRAM's own "
                                     f"requantification, which confirms ours "
                                     f"(reference inconsistency, V&V F-6): "
                                     f"{ref_issues[:2]}")
                if bad:
                    imp_disagree += 1
                    imp_notes.append(f"{name}: importance DISAGREE on "
                                     f"{len(bad)} value(s): {bad[:3]}")
                elif n - len(ref_issues) == 0 and ref_issues:
                    pass        # only reference inconsistencies: listed above
                elif n == 0:
                    imp_skipped += 1
                    imp_notes.append(f"{name}: importance not compared (SCRAM "
                                     f"reported no event{'' if ei is None else ': ' + ei})")
                else:
                    imp_agree += 1
                    imp_events += n - len(ref_issues)
                    imp_notes.append(f"{name}: importance agree on "
                                     f"{n - len(ref_issues)} of {len(bes)} events "
                                     f"(MIF and RAW; max rel diff {worst:.1e})")

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
            if a.primes and po is not None and ps is not None:
                fts = open(os.path.join(d, "fault-trees", "imported.yaml")).read()
                if "not:" in fts:
                    bes = yaml.safe_load(open(os.path.join(
                        d, "basic-events", "imported.yaml")))["basic_events"]
                    to_mef = {bid: be["external_ids"]["mef"] for bid, be in bes.items()}
                    oj, _, oe = run([a.engine, d, "FT-MAIN", "--json", "--prime-implicants",
                                     "--order-limit", str(a.primes),
                                     "--mcs-limit", "10000000"], a.timeout)
                    rep3 = tempfile.mktemp(suffix=".xml")
                    _, _, se = run(["scram", "--bdd", "--prime-implicants", "-l",
                                    str(a.primes), f, "-o", rep3], a.timeout)
                    theirs = None
                    if se is None and os.path.exists(rep3):
                        theirs = set()
                        for prod in ET.parse(rep3).getroot().iter("product"):
                            pos, neg = set(), set()
                            for el in prod:
                                if el.tag == "basic-event":
                                    pos.add(el.get("name"))
                                elif el.tag == "not":
                                    for be in el.iter("basic-event"):
                                        neg.add(be.get("name"))
                            theirs.add((frozenset(pos), frozenset(neg)))
                    if os.path.exists(rep3):
                        os.unlink(rep3)
                    if oj is None or theirs is None:
                        pi_skipped += 1
                        pi_notes.append(f"{name}: primes not compared "
                                        f"({oe or ''} {se or ''})".rstrip())
                    else:
                        ours_pi = {(frozenset(to_mef[e] for e in x["events"]),
                                    frozenset(to_mef[e] for e in x["negated"]))
                                   for x in json.loads(oj)["prime_implicants"]}
                        if ours_pi == theirs:
                            pi_agree += 1
                            pi_products += len(ours_pi)
                            pi_notes.append(f"{name}: prime implicants of order <= "
                                            f"{a.primes} identical ({len(ours_pi)})")
                        else:
                            pi_disagree += 1
                            pi_notes.append(
                                f"{name}: prime implicants DIFFER: ours "
                                f"{len(ours_pi)}, SCRAM {len(theirs)}; only ours "
                                f"{[(sorted(p), sorted(q)) for p, q in list(ours_pi - theirs)[:2]]}, "
                                f"only SCRAM "
                                f"{[(sorted(p), sorted(q)) for p, q in list(theirs - ours_pi)[:2]]}")
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
              f"{imp_disagree} disagree, {imp_skipped} not compared, {imp_ref} "
              f"with reference inconsistencies adjudicated in our favour by "
              f"SCRAM's own requantification (Birnbaum vs SCRAM MIF, RAW vs "
              f"RAW, per basic event SCRAM reports)")
    if a.primes:
        for note in pi_notes:
            print(f"  {note}")
        print(f"prime implicants (order <= {a.primes}, non-coherent trees): "
              f"{pi_agree} identical ({pi_products} products), {pi_disagree} "
              f"differ, {pi_skipped} not compared")
    return 1 if disagree or imp_disagree or pi_disagree else 0


if __name__ == "__main__":
    sys.exit(main())
