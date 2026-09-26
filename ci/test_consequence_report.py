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
            {
                # A transfer row whose end state is CD: never counted, its
                # cut sets never pooled (FR-11, V&V anomaly D-10).
                "id": "SEQ-5",
                "end_state": "CD",
                "transfer": "ET-2",
                "frequency_per_year": 7.0e-9,
                "cut_sets": [
                    {"events": ["BE-A", "BE-B"], "frequency_per_year": 7.0e-9},
                ],
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

    # Total is the exact sum of SEQ-1, SEQ-3, SEQ-4 (SEQ-2 is OK, SEQ-5 a
    # transfer row: both excluded).
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

    # Reproducibility (NFR-1, V&V anomaly D-11): ties are ordered by content,
    # not by set iteration order, which follows per-process string hashing.
    # Run the report under several hash seeds; the output must not change.
    import json, os, subprocess, tempfile
    tie = {"ET-1": {"sequences": [{
        "id": "SEQ-1", "end_state": "CD", "frequency_per_year": 2e-9,
        # Eight disjoint pairs of equal frequency: the two events of a pair
        # tie, and meet in one frozenset, whose iteration order is the
        # hash-dependent part.
        "cut_sets": [{"events": [f"BE-{c}1", f"BE-{c}2"],
                      "frequency_per_year": 1e-10}
                     for c in "PQRSTUVW"]}]}}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(tie, f)
    outs = set()
    for seed in ("0", "1", "2", "3", "4", "5"):
        outs.add(subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "consequence_report.py"),
             f.name, "--end-state", "CD", "--json"],
            capture_output=True, text=True, check=True,
            env={**os.environ, "PYTHONHASHSEED": seed}).stdout)
    os.unlink(f.name)
    assert len(outs) == 1, f"{len(outs)} distinct outputs over 6 hash seeds"
    ranked = json.loads(outs.pop())["basic_event_importance"]
    names = [r["event"] for r in ranked]
    assert names == sorted(names), names

    # Prime implicants of non-coherent sequence logic pool like cut sets,
    # with negated events as "¬" literals that never count as failures;
    # a sequence carrying primes is not untracked.
    pi_fix = {"ET-9": {"sequences": [
        {"id": "SEQ-P1", "end_state": "CD", "frequency_per_year": 4.0e-9,
         "cut_sets": [],
         "prime_implicants": [
             {"events": ["BE-A"], "negated": ["BE-E"], "frequency_per_year": 2.5e-9},
             {"events": ["BE-C"], "negated": [], "frequency_per_year": 1.0e-9}]},
        {"id": "SEQ-P2", "end_state": "CD", "frequency_per_year": 1.0e-9,
         "cut_sets": [{"events": ["BE-C"], "frequency_per_year": 1.0e-9}]},
        {"id": "SEQ-P3", "end_state": "CD", "frequency_per_year": 5.0e-10,
         "cut_sets": []}]}}
    agg = aggregate(pi_fix, {"CD"})
    cuts = dict(agg["ranked_cuts"])
    assert approx(cuts[frozenset({"BE-A", "¬BE-E"})]["freq"], 2.5e-9)
    # {BE-C} pooled across a prime of SEQ-P1 and a cut set of SEQ-P2
    assert approx(cuts[frozenset({"BE-C"})]["freq"], 2.0e-9)
    be = dict(agg["ranked_be"])
    assert "BE-E" not in be and "¬BE-E" not in be, be
    assert approx(be["BE-A"]["freq"], 2.5e-9) and approx(be["BE-C"]["freq"], 2.0e-9)
    assert agg["untracked"] == [("ET-9", "SEQ-P3", 5.0e-10)], agg["untracked"]
    import json, os, subprocess, tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(pi_fix, f)
    out = json.loads(subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "consequence_report.py"),
         f.name, "--end-state", "CD", "--json"],
        capture_output=True, text=True, check=True).stdout)
    os.unlink(f.name)
    row = next(c for c in out["cut_sets"] if "BE-A" in c["events"])
    assert row["events"] == ["BE-A"] and row["negated"] == ["BE-E"], row

    # End to end (needs the engine binary): a generated model whose event
    # tree has non-coherent sequences, quantified with --prime-implicants,
    # leaves no sequence untracked and pools negated literals.
    here = os.path.dirname(os.path.abspath(__file__))
    engine = os.environ.get("CANOPY_BIN", os.path.join(os.path.dirname(here),
                                                       "engine/target/release/canopy"))
    if os.path.exists(engine):
        import random, shutil
        sys.path.insert(0, here)
        import property_test as pt
        tmp = tempfile.mkdtemp(prefix="psa-cr-")
        try:
            found = False
            for i in range(60):
                m = pt.gen_model(random.Random(20260708 * 1_000_003 + i))
                o = pt.Oracle(m)
                if not any(o.uses_negation(t) for t in m["fes"].values()):
                    continue
                d = os.path.join(tmp, f"c{i}")
                os.makedirs(d)
                pt.write_model(m, d)
                res = os.path.join(tmp, f"r{i}.json")
                subprocess.run([sys.executable, os.path.join(here, "quantify.py"), d, res,
                                "--engine", engine, "--prime-implicants"],
                               check=True, capture_output=True)
                r = json.load(open(res))
                if not any(s2.get("prime_implicants") for s2 in r["ET-TEST"]["sequences"]
                           if s2["end_state"] == "CD"):
                    continue
                a2 = aggregate(r, {"CD"})
                assert a2["untracked"] == [], (i, a2["untracked"])
                found = True
                break
            assert found, "no generated case with CD prime implicants"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    else:
        print("note: engine binary not found; end-to-end prime-implicant check skipped")

    print("consequence_report.aggregate: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
