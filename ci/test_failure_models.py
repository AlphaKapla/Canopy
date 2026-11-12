#!/usr/bin/env python3
"""Tests for the failure-model formulas and for what the engine refuses at
load (V&V D-39, D-40, D-41).

  * ci/failure_models.py against exact rational arithmetic: the
    periodic-test unavailability Q(x) = 1 - (1 - exp(-x))/x summed as a
    series in Fractions, and 1 - exp(-x) likewise, for x from 1e-18 to 2 —
    relative error at most 1e-15, where the direct closed forms are 1% off
    (x = 1e-7) or negative (x = 1e-10);
  * the engine's basic-event probabilities equal failure_models.py bit for
    bit on those inputs, for all four models (so the MEF exporter, the
    RiskSpectrum importer and the viewer show the engine's numbers);
  * a 20-of-40 vote gate quantifies in seconds and equals the binomial tail
    (D-41: the unmemoized construction was exponential);
  * the engine refuses, with a message: a vote gate with k above its inputs,
    a basic event in two CCF groups, a member listed twice (D-39).

Usage: python ci/test_failure_models.py [--engine PATH]
"""
import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from fractions import Fraction

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import failure_models  # noqa: E402
from test_truncation import write_model  # noqa: E402

XS = [1e-18, 1e-12, 1e-10, 1e-9, 1e-8, 1e-7, 2.4e-6, 1e-5, 7.2e-4, 1e-3, 0.01, 0.1,
      0.3, 0.499999, 0.5, 0.500001, 0.9, 2.0]


def exact_periodic(x: float) -> Fraction:
    X, s, t = Fraction(x), Fraction(0), Fraction(1)
    for n in range(1, 80):
        t = t * X / (n + 1)          # x^n / (n + 1)!
        s += t if n % 2 else -t
    return s


def exact_mission(x: float) -> Fraction:
    X, s, t = Fraction(x), Fraction(0), Fraction(1)
    for n in range(1, 80):
        t = t * X / n                # x^n / n!
        s += t if n % 2 else -t
    return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", os.path.join(ROOT, "engine/target/release/canopy")))
    a = ap.parse_args()
    failures = []

    def check(cond, msg):
        (print(f"  ok  {msg}") if cond else failures.append(msg))

    worst_pt = max(abs(Fraction(failure_models.periodic_test(x, 1.0)) - exact_periodic(x))
                   / exact_periodic(x) for x in XS)
    worst_ms = max(abs(Fraction(failure_models.mission(x, 1.0)) - exact_mission(x))
                   / exact_mission(x) for x in XS)
    check(worst_pt <= 1e-15, f"periodic test vs exact series: worst relative error "
                             f"{float(worst_pt):.1e} over {len(XS)} products")
    check(worst_ms <= 1e-15, f"mission vs exact series: worst relative error {float(worst_ms):.1e}")
    direct = lambda x: 1.0 - (1.0 - math.exp(-x)) / x
    check(abs(direct(1e-7) / float(exact_periodic(1e-7)) - 1) > 1e-3 and direct(1e-10) < 0,
          "the closed form it replaces: off by more than 0.1% at 1e-7, negative at 1e-10")
    check(failure_models.periodic_test(0.0, 100.0) == 0.0
          and failure_models.periodic_test(1e-3, 0.0) == 0.0, "periodic test: zero product is 0")

    tmp = tempfile.mkdtemp(prefix="psa-fm-")
    try:
        # the engine's numbers, bit for bit
        d = os.path.join(tmp, "fm")
        bes = {f"BE-X{i:02d}": 0.5 for i in range(len(XS))}
        write_model(d, bes, {"GT-TOP": {"or": sorted(bes)}}, "GT-TOP")
        evs, want = {}, {}
        q = lambda v, u: {"value": v, "unit": u}
        prov = {"source": "test", "justification": "test"}
        for i, x in enumerate(XS):
            for kind, fm, p in (
                ("PT", {"type": "rate-periodic-test", "rate": q(x, "per_hour"),
                        "test_interval": q(1.0, "hour")}, failure_models.periodic_test(x, 1.0)),
                ("MS", {"type": "rate-mission", "rate": q(x / 8.0, "per_hour"),
                        "mission_time": q(8.0, "hour")}, failure_models.mission(x / 8.0, 8.0)),
                ("RP", {"type": "rate-repair", "rate": q(x, "per_hour"), "mttr": q(3.0, "hour")},
                 failure_models.repair(x, 3.0)),
            ):
                bid = f"BE-{kind}{i:02d}"
                evs[bid] = {"label": bid, "failure_model": fm, "provenance": prov}
                want[bid] = p
        open(os.path.join(d, "basic-events", "fm.yaml"), "w").write(
            yaml.safe_dump({"basic_events": evs}))
        ft = os.path.join(d, "fault-trees", os.listdir(os.path.join(d, "fault-trees"))[0])
        y = yaml.safe_load(open(ft))
        y["fault_trees"]["FT-T"]["gates"]["GT-TOP"]["formula"] = {"or": sorted(evs)}
        open(ft, "w").write(yaml.safe_dump(y))
        r = subprocess.run([a.engine, d, "FT-T", "--prob-only", "--json"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            failures.append(f"engine failed on the failure-model fixture: {r.stderr[-300:]}")
        else:
            got = json.loads(r.stdout)["basic_event_probabilities"]
            diff = [b for b in sorted(want) if got[b] != want[b]]
            check(not diff, f"engine = failure_models.py bit for bit on {len(want)} events "
                            f"({[(b, got[b], want[b]) for b in diff[:3]]})")

        # D-41: a large vote gate
        d = os.path.join(tmp, "vote")
        n, k, p = 40, 20, 0.3
        write_model(d, {f"BE-R{i:02d}": p for i in range(n)},
                    {"GT-TOP": {"atleast": {"k": k, "of": [f"BE-R{i:02d}" for i in range(n)]}}},
                    "GT-TOP")
        t0 = time.time()
        try:
            r = subprocess.run([a.engine, d, "FT-T", "--prob-only", "--json"],
                               capture_output=True, text=True, timeout=60)
            got = json.loads(r.stdout)["probability"]
            tail = sum(math.comb(n, j) * p ** j * (1 - p) ** (n - j) for j in range(k, n + 1))
            check(abs(got - tail) <= 1e-12 * tail,
                  f"20-of-40 vote gate: {got:.12e} = binomial tail {tail:.12e}, "
                  f"{time.time() - t0:.1f} s")
        except subprocess.TimeoutExpired:
            failures.append("20-of-40 vote gate: not quantified within 60 s")

        # D-39: refusals at load
        def refused(name, d, fragment):
            r = subprocess.run([a.engine, d, "FT-T", "--prob-only", "--json"],
                               capture_output=True, text=True)
            t = subprocess.run([a.engine, d, "FT-T", "--truncated", "1e-9", "--json"],
                               capture_output=True, text=True)
            check(r.returncode != 0 and fragment in r.stderr
                  and t.returncode != 0 and fragment in t.stderr,
                  f"refused, exact and truncated: {name} ({r.stderr.strip().splitlines()[0][:90] if r.stderr else 'no message'})")
        d = os.path.join(tmp, "kn")
        write_model(d, {"BE-A": 0.1, "BE-B": 0.1},
                    {"GT-TOP": {"or": ["BE-A", {"atleast": {"k": 3, "of": ["BE-A", "BE-B"]}}]}},
                    "GT-TOP")
        refused("vote gate with k = 3 of 2 inputs", d, "got k = 3 of 2")
        grp = lambda members: {"label": "g", "model": "beta-factor", "members": members,
                               "total_probability": q(0.01, "per_demand"),
                               "factors": {"beta": 0.1}, "provenance": prov}
        for name, groups, fragment in (
            ("a basic event in two CCF groups",
             {"CCF-1": grp(["BE-A", "BE-B"]), "CCF-2": grp(["BE-B", "BE-C"])},
             "BE-B is a member of two CCF groups, CCF-1 and CCF-2"),
            ("a CCF member listed twice", {"CCF-1": grp(["BE-A", "BE-A"])},
             "CCF-1: member BE-A is listed twice"),
        ):
            d = os.path.join(tmp, name.replace(" ", "-"))
            write_model(d, {"BE-A": 0.1, "BE-B": 0.1, "BE-C": 0.1},
                        {"GT-TOP": {"and": ["BE-A", "BE-B", "BE-C"]}}, "GT-TOP")
            open(os.path.join(d, "ccf-groups.yaml"), "w").write(
                yaml.safe_dump({"ccf_groups": groups}))
            refused(name, d, fragment)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("failure models: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
