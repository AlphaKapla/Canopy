#!/usr/bin/env python3
"""Hand-computed reference test for consequence_report.aggregate().

Usage: python ci/test_consequence_report.py
"""
import sys

from consequence_report import aggregate

# Two event trees, three qualifying "CD" sequences plus one non-CD "OK"
# sequence and one non-coherent CD sequence (freq contributes, no cut
# sets). {BE-A, BE-B} appears in two different sequences with different
# frequencies (5e-9 and 3e-9): pooling must sum them to 8e-9, not
# overwrite. BE-A itself must appear in three distinct cut sets.
FIXTURE = {
    "ET-1": {
        "sequences": [
            {
                "id": "SEQ-1",
                "end_state": "CD",
                "frequency_per_year": 6.0e-9,
                "cut_sets": [
                    {"events": ["BE-A", "BE-B"], "frequency_per_year": 5.0e-9},
                    {"events": ["BE-C"], "frequency_per_year": 1.0e-9},
                ],
            },
            {
                "id": "SEQ-2",
                "end_state": "OK",
                "frequency_per_year": 0.999,
                "cut_sets": [],
            },
            {
                "id": "SEQ-3",
                "end_state": "CD",
                "frequency_per_year": 2.0e-9,
                "cut_sets": [],  # non-coherent: contributes freq, no cut sets
            },
        ],
    },
    "ET-2": {
        "sequences": [
            {
                "id": "SEQ-4",
                "end_state": "CD",
                "frequency_per_year": 4.0e-9,
                "cut_sets": [
                    {"events": ["BE-A", "BE-B"], "frequency_per_year": 3.0e-9},
                    {"events": ["BE-A", "BE-D"], "frequency_per_year": 1.0e-9},
                ],
            },
        ],
    },
}


def approx(a: float, b: float, tol: float = 1e-15) -> bool:
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def main() -> int:
    agg = aggregate(FIXTURE, {"CD"}, mcs_limit=1000)

    # Total is the exact sum of SEQ-1, SEQ-3, SEQ-4 (SEQ-2 is OK, excluded).
    expected_total = 6.0e-9 + 2.0e-9 + 4.0e-9
    assert approx(agg["total_freq"], expected_total), agg["total_freq"]

    # {BE-A, BE-B} pooled across SEQ-1 (5e-9) and SEQ-4 (3e-9) = 8e-9.
    cuts = dict(agg["ranked_cuts"])
    ab = cuts[frozenset({"BE-A", "BE-B"})]
    assert approx(ab["freq"], 8.0e-9), ab["freq"]
    assert ab["from"] == {"ET-1/SEQ-1", "ET-2/SEQ-4"}, ab["from"]

    # {BE-C} and {BE-A, BE-D} are untouched singletons.
    assert approx(cuts[frozenset({"BE-C"})]["freq"], 1.0e-9)
    assert approx(cuts[frozenset({"BE-A", "BE-D"})]["freq"], 1.0e-9)
    assert len(agg["ranked_cuts"]) == 3, agg["ranked_cuts"]

    # Ranked descending by pooled frequency: {BE-A,BE-B} first.
    assert agg["ranked_cuts"][0][0] == frozenset({"BE-A", "BE-B"})

    # BE-A importance: sum over the two cut sets containing it (8e-9 + 1e-9).
    be = dict(agg["ranked_be"])
    assert approx(be["BE-A"]["freq"], 9.0e-9), be["BE-A"]["freq"]
    assert be["BE-A"]["n_cutsets"] == 2
    # BE-B only appears in the {BE-A,BE-B} cut set.
    assert approx(be["BE-B"]["freq"], 8.0e-9)
    assert be["BE-B"]["n_cutsets"] == 1

    # SEQ-3 is CD, has frequency, no cut sets: must be flagged untracked.
    assert agg["untracked"] == [("ET-1", "SEQ-3", 2.0e-9)], agg["untracked"]

    # Nothing hit the (default 1000) mcs_limit in this fixture.
    assert agg["truncated"] == []

    # Pooled cut-set sum (11e-9) vs exact total (12e-9): coverage < 1 here
    # because SEQ-3's 2e-9 has no cut sets at all (the untracked case),
    # which pulls coverage down rather than up.
    expected_pooled = 8.0e-9 + 1.0e-9 + 1.0e-9
    assert approx(agg["pooled_total"], expected_pooled), agg["pooled_total"]
    assert approx(agg["coverage"], expected_pooled / expected_total)

    print("consequence_report.aggregate: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
