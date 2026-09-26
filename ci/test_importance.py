#!/usr/bin/env python3
"""Hand-computed reference test for ci/importance.py (model-wide BDD-exact
consequence importance summed across event trees).

Usage: python ci/test_importance.py

Tree 1 is the engine's unit-test case (engine/src/main.rs,
`consequence_importance_hand_computed`): IE 1e-3 /yr, FE1 = A, FE2 = A OR B,
P(A) = 0.1, P(B) = 0.2; CD = A + ¬A·B, so F = 2.8e-4, F(A=1) = 1e-3,
F(A=0) = 2e-4, F(B=1) = 1e-3, F(B=0) = 1e-4.
Tree 2: IE 1e-3 /yr, one CD sequence = C, P(C) = 0.05; F = 5e-5,
F(C=1) = 1e-3, F(C=0) = 0.

Model-wide (tree 2 does not depend on A or B, tree 1 not on C, so each
contributes its F unchanged there):
  F        = 3.3e-4
  A: F1 = 1.05e-3, F0 = 2.5e-4  -> FV = 0.8/3.3,  RAW = 10.5/3.3, RRW = 3.3/2.5
  B: F1 = 1.05e-3, F0 = 1.5e-4  -> FV = 1.8/3.3,  RRW = 3.3/1.5
  C: F1 = 1.28e-3, F0 = 2.8e-4  -> FV = 0.5/3.3,  RAW = 12.8/3.3, RRW = 3.3/2.8
Tree 2 alone: C has F0 = 0, so RRW is infinite (None).
"""
import sys

from importance import combine, for_end_states, for_metric


def row(e, p, f1, f0):
    return {"event": e, "probability": p,
            "frequency_if_true_per_year": f1,
            "frequency_if_false_per_year": f0}


T1 = [row("BE-A", 0.1, 1e-3, 2e-4), row("BE-B", 0.2, 1e-3, 1e-4)]
T2 = [row("BE-C", 0.05, 1e-3, 0.0)]
RESULTS = {
    "ET-1": {"metrics": [{"id": "CDF", "value_per_year": 2.8e-4,
                          "importance": T1}],
             "end_states": [{"id": "CD", "frequency_per_year": 2.8e-4,
                             "importance": T1},
                            {"id": "OK", "frequency_per_year": 7.2e-4,
                             "importance": []}]},
    "ET-2": {"metrics": [{"id": "CDF", "value_per_year": 5e-5,
                          "importance": T2}],
             "end_states": [{"id": "CD", "frequency_per_year": 5e-5,
                             "importance": T2}]},
}


def approx(a, b, tol=1e-14):
    return abs(a - b) <= tol * max(abs(a), abs(b))


def main() -> int:
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    for label, got in (("metric", for_metric(RESULTS, "CDF")),
                       ("end states", for_end_states(RESULTS, {"CD"}))):
        check(approx(got["frequency_per_year"], 3.3e-4), f"{label}: F")
        by = {r["event"]: r for r in got["importance"]}
        exp = {"BE-A": (1.05e-3, 2.5e-4), "BE-B": (1.05e-3, 1.5e-4),
               "BE-C": (1.28e-3, 2.8e-4)}
        for e, (f1, f0) in exp.items():
            r = by[e]
            check(approx(r["frequency_if_true_per_year"], f1), f"{label}: {e} F1")
            check(approx(r["frequency_if_false_per_year"], f0), f"{label}: {e} F0")
            check(approx(r["fussell_vesely"], (3.3e-4 - f0) / 3.3e-4),
                  f"{label}: {e} FV")
            check(approx(r["raw"], f1 / 3.3e-4), f"{label}: {e} RAW")
            check(approx(r["rrw"], 3.3e-4 / f0), f"{label}: {e} RRW")
            check(approx(r["birnbaum_per_year"], f1 - f0), f"{label}: {e} B")
        check(approx(by["BE-A"]["fussell_vesely"], 0.8 / 3.3), f"{label}: FV_A")
        check([r["event"] for r in got["importance"]] == ["BE-B", "BE-A", "BE-C"],
              f"{label}: ranking by FV")
        check(by["BE-C"]["probability"] == 0.05, f"{label}: probability")

    only2 = combine([(5e-5, T2)])
    check(only2["importance"][0]["rrw"] is None, "infinite RRW is None")
    check(approx(only2["importance"][0]["fussell_vesely"], 1.0), "FV = 1")
    zero = combine([(0.0, [row("BE-Z", 0.1, 0.0, 0.0)])])
    r = zero["importance"][0]
    check(r["fussell_vesely"] is None and r["raw"] is None and r["rrw"] is None,
          "F = 0: all ratios undefined")

    # A tree quantified with --prob-only carries no importance: refuse to
    # report a partial (hence wrong) model-wide figure.
    partial = {**RESULTS, "ET-3": {"metrics": [{"id": "CDF",
                                                 "value_per_year": 1e-6}]}}
    check(for_metric(partial, "CDF") is None, "prob-only tree -> None (metric)")
    check(for_end_states(partial, {"CD"}) is None,
          "prob-only tree -> None (end states)")
    check(for_metric(RESULTS, "LERF") is None, "unknown metric -> None")

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("importance.combine: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
