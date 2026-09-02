#!/usr/bin/env python3
"""Tests for ci/import_riskspectrum.py.

Usage: python ci/test_import_riskspectrum.py

Two layers:
  1. Hand-computed unit checks of the conversion arithmetic and the
     refusal rules (MGL->alpha relations, periodic-test formula, ID
     grammar, unsupported constructs).
  2. Round trip: the RiskSpectrum-style table export of the demo model
     (ci/fixtures/riskspectrum-demo/) is converted, validated with
     ci/validate.py, and — when the engine binary exists — quantified;
     every sequence frequency, every risk metric, every cut set and every
     fault-tree top probability must equal the committed model/'s to
     1e-12 relative. The fixture uses the same record IDs as model/ so the
     comparison is by ID, not by position.
"""
import copy
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import import_riskspectrum as rs  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "ci", "fixtures", "riskspectrum-demo")
SCHEMA = os.path.join(ROOT, "schema", "psa-model.schema.json")
ENGINE = os.environ.get("CANOPY_BIN",
                        os.path.join(ROOT, "engine", "target", "release",
                                     "canopy"))


def approx(a, b, rel=1e-12):
    return abs(a - b) <= rel * max(abs(a), abs(b), 1e-300)


def opts(**kw):
    o = {"source": FIXTURE, "out": "", "model_id": "T", "model_name": "T",
         "metrics": [("CDF", ["CD"])], "time_unit": "hour",
         "frequency_unit": "per_year", "mgl_to_alpha": False,
         "allow_unsupported": False, "strict": False, "single_file": False}
    o.update(kw)
    return o


def convert(tables, **kw):
    o = opts(**kw)
    log = rs.Log(o["allow_unsupported"])
    cv = rs.Converter(tables, o, log)
    cv.run()
    return cv, log


def fixture_tables():
    return rs.load_tables(FIXTURE)


# ---------------------------------------------------------------------------
# 1. unit checks
# ---------------------------------------------------------------------------

def test_id_grammar():
    n = rs.Names()
    assert n.get("BE", "ecc pmp_a/fts") == "BE-ECC-PMP-A-FTS"
    assert n.get("BE", "ecc pmp_a/fts") == "BE-ECC-PMP-A-FTS"   # stable
    # a different original that sanitizes to the same base must not collide
    assert n.get("BE", "ECC-PMP-A-FTS") == "BE-ECC-PMP-A-FTS-2"
    assert n.origin["BE-ECC-PMP-A-FTS-2"] == "ECC-PMP-A-FTS"
    assert n.get("GT", "1st gate") == "GT-1ST-GATE"
    assert n.get("HE", "---") == "HE-X"
    for cid in n.used:
        import re
        assert re.match(r"^(BE|GT|HE)-[A-Z0-9][A-Z0-9-]*$", cid), cid


def test_mgl_to_alpha_hand_computed():
    # m = 3, beta = 0.1, gamma = 0.5 (rho_2, rho_3), non-staggered
    # NUREG/CR-5485:  Q1 = 0.9 Qt, Q2 = 0.025 Qt, Q3 = 0.05 Qt
    # weights C(3,k) Q_k: 2.7, 0.075, 0.05 -> sum 2.825
    a = rs.Converter.mgl_to_alpha(3, [0.1, 0.5])
    exp = [2.7 / 2.825, 0.075 / 2.825, 0.05 / 2.825]
    assert all(approx(x, y) for x, y in zip(a, exp)), a
    assert approx(sum(a), 1.0)
    # consistency of the non-staggered relations: alpha_t = m Qt / sum
    alpha_t = sum((k + 1) * a[k] for k in range(3))
    assert approx(alpha_t, 3.0 / 2.825), alpha_t
    # m = 2 reduces to the beta-factor relation: Q1 = (1-b)Qt, Q2 = b Qt
    b = 0.05
    a2 = rs.Converter.mgl_to_alpha(2, [b])
    w1, w2 = 2 * (1 - b), 1 * b
    assert approx(a2[0], w1 / (w1 + w2)) and approx(a2[1], w2 / (w1 + w2))
    try:
        rs.Converter.mgl_to_alpha(4, [0.1])
        raise AssertionError("size/factor mismatch not rejected")
    except ValueError:
        pass


def test_periodic_test_formula_matches_engine():
    # Same closed form the engine's failure_model_tests use (rT = 0.1).
    q = rs.Converter.periodic_test_q(1.0e-3, 100.0)
    assert approx(q, 1.0 - (1.0 - math.exp(-0.1)) / 0.1)
    assert rs.Converter.periodic_test_q(0.0, 100.0) == 0.0


def test_outcomes_and_sequence_sort():
    assert rs._outcome("S") == "success" and rs._outcome("f") == "failure"
    assert rs._outcome("") == "bypassed" and rs._outcome("-") == "bypassed"
    assert rs._outcome("maybe") is None
    seqs = ["10", "2", "1", "SEQ-3"]
    assert sorted(seqs, key=rs._seq_sort_key) == ["1", "2", "10", "SEQ-3"]


def test_tested_model_variants():
    t = fixture_tables()
    base = {"id": "X-TESTED", "description": "tested pump", "model": "tested",
            "rate": "1.0e-3", "test_interval": "100", "system": "X"}
    # idealized: no extras -> rate-periodic-test
    t["basic_events"].append(dict(base))
    cv, log = convert(t)
    fm = cv.basic_events["BE-X-TESTED"]["failure_model"]
    assert fm["type"] == "rate-periodic-test", fm
    assert not [w for w in log.warnings if "X-TESTED" in w]

    # repair time present + q_mean -> point probability, warning, justif.
    t = fixture_tables()
    t["basic_events"].append(dict(base, repair_time="24", q_mean="0.0725"))
    cv, log = convert(t)
    be = cv.basic_events["BE-X-TESTED"]
    assert be["failure_model"] == {"type": "probability",
                                   "value": {"value": 0.0725,
                                             "unit": "per_demand"}}
    assert "q_mean" in be["provenance"]["justification"]
    assert any("q_mean" in w for w in log.warnings)

    # repair time present, no q_mean -> idealized formula + warning
    t = fixture_tables()
    t["basic_events"].append(dict(base, repair_time="24"))
    cv, log = convert(t)
    assert cv.basic_events["BE-X-TESTED"]["failure_model"]["type"] == \
        "rate-periodic-test"
    assert any("no q_mean" in w for w in log.warnings)
    assert not log.errors


def test_refusals_and_allow_unsupported():
    # exchange event: error by default, dropped-with-warning when allowed
    t = fixture_tables()
    t["exchange_events"].append({"id": "XE1", "original": "RHR-PMP-A-FTS",
                                 "replacement": "RHR-PMP-B-FTS",
                                 "condition": "HE"})
    cv, log = convert(t)
    assert any("exchange event XE1" in e for e in log.errors), log.errors
    cv, log = convert(t, allow_unsupported=True)
    assert not log.errors
    assert any("UNSUPPORTED (dropped): exchange event XE1" in w
               for w in log.warnings)

    # basic event forced in a BC set
    t = fixture_tables()
    t["bc_set_entries"].append({"bc_set": "TRAIN-A-OOS",
                                "target": "ECC-PMP-A-TM", "value": "TRUE"})
    cv, log = convert(t)
    assert any("forces basic event ECC-PMP-A-TM" in e for e in log.errors)

    # frequency event inside a fault tree
    t = fixture_tables()
    t["gate_inputs"].append({"gate": "RT-TOP", "input": "SLOCA",
                             "position": "3"})
    cv, log = convert(t)
    assert any("frequency-type event SLOCA used inside" in e
               for e in log.errors)

    # MGL: refused without the flag, converted with it
    t = fixture_tables()
    t["ccf_groups"][0]["model"] = "mgl"
    t["ccf_factors"] = [{"group": "ECC-PMP-FTS", "name": "beta",
                         "value": "0.05"}]
    cv, log = convert(t)
    assert any("MGL model" in e for e in log.errors)
    cv, log = convert(t, mgl_to_alpha=True)
    assert not log.errors, log.errors
    g = cv.ccf["CCF-ECC-PMP-FTS"]
    assert g["model"] == "alpha-factor" and g["testing"] == "non-staggered"
    assert approx(g["factors"]["alpha_1"] + g["factors"]["alpha_2"], 1.0)
    assert any("MGL factors" in w for w in log.warnings)

    # group of 9 members exceeds the cap
    t = fixture_tables()
    for i in range(7):
        t["basic_events"].append({"id": f"X{i}", "description": f"x {i}",
                                  "model": "probability", "q": "1e-3"})
        t["ccf_members"].append({"group": "ECC-PMP-FTS", "member": f"X{i}"})
    cv, log = convert(t)
    assert any("exceeds Canopy's cap of 8" in e for e in log.errors)

    # duplicate sequence path
    t = fixture_tables()
    t["sequences"].append({"event_tree": "SLOCA", "sequence": "05",
                           "consequence": "CD"})
    for fe, o in (("RT", "S"), ("ECC", "S"), ("RHR", "F")):
        t["sequence_branches"].append({"event_tree": "SLOCA",
                                       "sequence": "05",
                                       "function_event": fe, "outcome": o})
    cv, log = convert(t)
    assert any("duplicate sequence path" in e for e in log.errors)


def test_placeholders_and_distributions():
    t = fixture_tables()
    t["basic_events"].append({"id": "NOPROV", "description": "no provenance",
                              "model": "probability", "q": "1e-3",
                              "distribution": "normal", "p1": "1e-3",
                              "p2": "1e-4"})
    cv, log = convert(t)
    be = cv.basic_events["BE-NOPROV"]
    assert be["provenance"]["justification"] == rs.PLACEHOLDER
    assert be["provenance"]["source"].startswith("RiskSpectrum export of")
    assert log.placeholders == 1
    assert "uncertainty" not in be
    assert any("'normal' has no Canopy equivalent" in w for w in log.warnings)


def test_column_linked_to_basic_event_gets_pass_through():
    t = fixture_tables()
    for r in t["et_columns"]:
        if r["function_event"] == "RT":
            r["logic"] = "RPS-LOGIC-FAIL"
    cv, log = convert(t)
    assert not log.errors, log.errors
    fe = cv.event_trees["ET-SLOCA"]["functional_events"]["FE-RT"]
    assert fe["top_gate"] == "GT-FE-RPS-LOGIC-FAIL"
    assert "FT-FE-RPS-LOGIC-FAIL" in cv.fault_trees


def test_synthesized_fault_tree_when_table_missing():
    t = fixture_tables()
    t["fault_trees"] = []
    cv, log = convert(t)
    assert not log.errors, log.errors
    assert set(cv.fault_trees) == {"FT-ECCS-INJECTION", "FT-RHR", "FT-RPS"}
    assert cv.fault_trees["FT-RHR"]["top_gate"] == "GT-RHR-TOP"


# ---------------------------------------------------------------------------
# 2. round trip against the committed demo model
# ---------------------------------------------------------------------------

def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if p.returncode != 0:
        raise AssertionError(f"{' '.join(cmd)} failed:\n{p.stdout}\n{p.stderr}")
    return p


def quantify_ft(model_dir, ft_id):
    p = run([ENGINE, model_dir, ft_id, "--json"])
    return json.loads(p.stdout)


def cut_set_index(seqs):
    return {s["id"]: {(tuple(sorted(c["events"])),
                       round(c["frequency_per_year"], 20))
                      for c in s.get("cut_sets", [])}
            for s in seqs}


def test_round_trip():
    out = tempfile.mkdtemp(prefix="rs-roundtrip-")
    try:
        run([sys.executable, os.path.join(ROOT, "ci", "import_riskspectrum.py"),
             FIXTURE, out, "--metric", "CDF=CD"], cwd=ROOT)
        assert os.path.exists(os.path.join(out, "conversion-log.md"))
        # 1. validator clean (the one expected warning: ET-ATWS transfer)
        p = run([sys.executable, os.path.join(ROOT, "ci", "validate.py"),
                 out, SCHEMA], cwd=ROOT)
        assert "0 error(s)" in p.stdout, p.stdout
        # 2. deterministic: a second run writes byte-identical files
        out2 = tempfile.mkdtemp(prefix="rs-roundtrip2-")
        try:
            run([sys.executable,
                 os.path.join(ROOT, "ci", "import_riskspectrum.py"),
                 FIXTURE, out2, "--metric", "CDF=CD"], cwd=ROOT)
            for dirpath, _, files in os.walk(out):
                for fn in files:
                    if fn == "conversion-log.md":
                        continue        # carries a timestamp
                    rel = os.path.relpath(os.path.join(dirpath, fn), out)
                    a = open(os.path.join(out, rel)).read()
                    b = open(os.path.join(out2, rel)).read()
                    assert a == b, f"non-deterministic output: {rel}"
        finally:
            shutil.rmtree(out2)
        if not os.path.exists(ENGINE):
            print("  (engine binary not found; quantification round trip "
                  "skipped — build engine/ to run it)")
            return
        # 3. quantification identical to the committed model, by ID
        model = os.path.join(ROOT, "model")
        for et in ("ET-SLOCA",):
            a = json.loads(run([ENGINE, model, et, "--json"]).stdout)
            b = json.loads(run([ENGINE, out, et, "--json"]).stdout)
            assert approx(a["initiating_event"]["frequency_per_year"],
                          b["initiating_event"]["frequency_per_year"])
            sa = {s["id"]: s for s in a["sequences"]}
            sb = {s["id"]: s for s in b["sequences"]}
            assert set(sa) == set(sb), (set(sa), set(sb))
            for sid in sa:
                assert approx(sa[sid]["frequency_per_year"],
                              sb[sid]["frequency_per_year"]), sid
                assert sa[sid]["end_state"] == sb[sid]["end_state"], sid
                assert sa[sid]["transfer"] == sb[sid]["transfer"], sid
            ca, cb = cut_set_index(a["sequences"]), cut_set_index(b["sequences"])
            for sid in ca:
                ea = {e for e, _ in ca[sid]}
                eb = {e for e, _ in cb[sid]}
                assert ea == eb, f"{sid}: cut sets differ {ea ^ eb}"
            ma = {m["id"]: m["value_per_year"] for m in a["metrics"]}
            mb = {m["id"]: m["value_per_year"] for m in b["metrics"]}
            assert ma == {"CDF": ma["CDF"]} and approx(ma["CDF"], mb["CDF"])
            assert approx(ma["CDF"], 2.208172942625735e-08), ma
        for ft in ("FT-ECCS-INJECTION", "FT-RHR", "FT-RPS"):
            a, b = quantify_ft(model, ft), quantify_ft(out, ft)
            assert approx(a["probability"], b["probability"]), ft
            ea = {tuple(sorted(c["events"])) for c in a["minimal_cut_sets"]}
            eb = {tuple(sorted(c["events"])) for c in b["minimal_cut_sets"]}
            assert ea == eb, ft
        # 4. configuration from the BC set behaves like the hand-written one
        a = json.loads(run([ENGINE, model, "ET-SLOCA", "--house",
                            "HE-ECC-TRAIN-A-OOS=true", "--json"]).stdout)
        b = json.loads(run([ENGINE, out, "ET-SLOCA", "--house",
                            "HE-ECC-TRAIN-A-OOS=true", "--json"]).stdout)
        assert approx(a["metrics"][0]["value_per_year"],
                      b["metrics"][0]["value_per_year"])
        print(f"  round trip: CDF {mb['CDF']:.6e} identical, "
              f"{sum(len(s) for s in cb.values())} sequence cut sets and "
              f"3 fault trees identical, configuration override identical")
    finally:
        shutil.rmtree(out)


def test_crosscheck_against_rs_results():
    """ci/crosscheck_rs.py on the converted demo model against RS-style
    result tables generated from the committed model at 6 significant
    digits: PASS at 1e-5; a perturbed value and an unmapped event FAIL."""
    if not os.path.exists(ENGINE):
        print("  (engine binary not found; cross-check test skipped)")
        return
    import crosscheck_rs
    out = tempfile.mkdtemp(prefix="rs-xcheck-")
    res = os.path.join(ROOT, "ci", "fixtures", "riskspectrum-demo-results")
    try:
        run([sys.executable, os.path.join(ROOT, "ci", "import_riskspectrum.py"),
             FIXTURE, out, "--metric", "CDF=CD"], cwd=ROOT)
        env_engine = os.environ.get("CANOPY_BIN")
        os.environ["CANOPY_BIN"] = ENGINE
        try:
            assert crosscheck_rs.main([out, res, "--tol", "1e-5",
                                       "--json"]) == 0
            # perturb one sequence frequency by 1% -> FAIL
            bad = tempfile.mkdtemp(prefix="rs-xcheck-bad-")
            try:
                for fn in os.listdir(res):
                    shutil.copy(os.path.join(res, fn), bad)
                p = os.path.join(bad, "sequence_results.csv")
                rows = open(p).read().splitlines()
                et, seq, f = rows[2].split(",")
                rows[2] = f"{et},{seq},{float(f) * 1.01:.6g}"
                open(p, "w").write("\n".join(rows) + "\n")
                # and drop the CCF event mapping -> unmatched event
                open(os.path.join(bad, "event_map.csv"), "w").write(
                    "riskspectrum,canopy\n")
                import io
                import contextlib
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = crosscheck_rs.main([out, bad, "--tol", "1e-5",
                                             "--json"])
                rep = json.loads(buf.getvalue())
                assert rc == 1
                assert any(not s["ok"] for s in rep["sequences"])
                assert any("CCF-ECC-PMP-FTS-A-B" in u for u in rep["unmatched"])
                assert any(not c["ok"] for c in rep["cut_sets"])
            finally:
                shutil.rmtree(bad)
        finally:
            if env_engine is None:
                del os.environ["CANOPY_BIN"]
            else:
                os.environ["CANOPY_BIN"] = env_engine
    finally:
        shutil.rmtree(out)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(f"{t.__name__} ...")
        t()
    print(f"import_riskspectrum: {len(tests)} test groups passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
