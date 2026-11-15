#!/usr/bin/env python3
"""Tests for ci/sql_results.py (FR-61): results beside the model in SQL.

  * a hand-made results file of two event trees (no engine): every table
    row by row; the metric total and the model-wide importance worked
    out by hand; a prime implicant with a negated event; a followed
    transfer; uncertainty rows; a truncated file has bounds and NULL
    values, and no model-wide importance;
  * the demo model, quantified by the engine: every number of the file is
    in the tables bit for bit; sequence frequencies summed in SQL by end
    state agree with the engine's own end-state frequencies; the pooled
    cut-set table and the importance table of `consequence_report.py`
    are reproduced by two SQL queries; cut-set events join to the
    model's basic events (the expanded common-cause events excepted);
  * loading again replaces the tables; `sql load` starts without them;
    `dump` ignores them (nothing written) and says so once the model in
    the database has been edited;
  * refusals, the tables left as they were: results naming an event tree
    or a sequence the model does not have, or another end state; a
    fault-tree entry; a file that is not results; a value that is not a
    number; a cut set naming an event twice; a sequence listed twice (the
    failure comes inside the transaction); no database; a database not
    made by `sql load`;
  * the command, through `canopy sql results`.

Usage: python ci/test_sql_results.py     (needs the engine for the demo part)
"""
import copy
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import sql_projection as sp  # noqa: E402
import sql_results as sr  # noqa: E402

MODEL = os.path.join(ROOT, "model")


def imp(event, p, f1, f0, f):
    return {"event": event, "probability": p, "frequency_if_true_per_year": f1,
            "frequency_if_false_per_year": f0, "birnbaum_per_year": f1 - f0,
            "fussell_vesely": (f - f0) / f, "raw": f1 / f, "rrw": f / f0}


def hand():
    """Two event trees; CDF = 1e-6 + 3e-6. BE-X is in ET-A only."""
    a = {
        "type": "event_tree", "id": "ET-A",
        "initiating_event": {"id": "IE-A", "frequency_per_year": 0.01},
        "basic_event_probabilities": {"BE-X": 0.001, "BE-Y": 0.002},
        "partition": {"sum_probability": 1.0, "per_sequence_house_overrides": False},
        "sequences": [
            {"id": "SEQ-A-1", "end_state": "OK", "frequency_per_year": 0.009999,
             "transfer": None, "cut_sets": []},
            {"id": "SEQ-A-2", "end_state": "CD", "frequency_per_year": 1e-6, "transfer": None,
             "cut_sets": [{"events": ["BE-X"], "frequency_per_year": 8e-7},
                          {"events": ["BE-Y", "BE-Z"], "frequency_per_year": 2e-7}],
             "uncertainty": {"mean": 1.1e-6, "p05": 1e-7, "p50": 9e-7, "p95": 3e-6,
                             "std": 5e-7, "std_error_of_mean": 1e-8}},
            {"id": "SEQ-A-3>SEQ-B-2", "end_state": "CD", "frequency_per_year": 5e-8,
             "transfer": "ET-B", "prime_implicants": [
                 {"events": ["BE-Y"], "negated": ["BE-X"], "frequency_per_year": 5e-8}]},
        ],
        "end_states": [{"id": "CD", "frequency_per_year": 1e-6,
                        "importance": [imp("BE-X", 0.001, 1e-4, 5e-7, 1e-6)]}],
        "metrics": [{"id": "CDF", "label": "Core damage", "value_per_year": 1e-6,
                     "importance": [imp("BE-X", 0.001, 1e-4, 5e-7, 1e-6),
                                    imp("BE-Y", 0.002, 2e-5, 8e-7, 1e-6)],
                     "uncertainty": {"mean": 1.2e-6, "p05": 2e-7, "p50": 1e-6, "p95": 4e-6,
                                     "std": 6e-7, "draws": [1e-6, 2e-6]}}],
    }
    a["metrics"][0]["importance"][0]["uncertainty"] = {
        "fussell_vesely": {"mean": 0.4, "p05": 0.1, "p50": 0.45, "p95": 0.7, "std": 0.2},
        "draws_if_true": [1.0], "iterations_with_zero_frequency": 0,
        "not_a_measure": {"mean": 1.0, "p05": 1.0, "p50": 1.0, "p95": 1.0, "std": 0.0}}
    b = {
        "type": "event_tree", "id": "ET-B",
        "initiating_event": {"id": "IE-B", "frequency_per_year": 0.1},
        "basic_event_probabilities": {"BE-Y": 0.002},
        "partition": {"sum_probability": 1.0, "per_sequence_house_overrides": False},
        "sequences": [{"id": "SEQ-B-2", "end_state": "CD", "frequency_per_year": 3e-6,
                       "transfer": None,
                       "cut_sets": [{"events": ["BE-Y"], "frequency_per_year": 3e-6}]}],
        "end_states": [{"id": "CD", "frequency_per_year": 3e-6,
                        "importance": [imp("BE-Y", 0.002, 1e-4, 2.8e-6, 3e-6)]}],
        "metrics": [{"id": "CDF", "label": "Core damage", "value_per_year": 3e-6,
                     "importance": [imp("BE-Y", 0.002, 1e-4, 2.8e-6, 3e-6)]}],
    }
    return {"ET-A": a, "ET-B": b}


def main() -> int:
    failures = []
    tmp = tempfile.mkdtemp(prefix="canopy-sqlr-")

    def check(cond, msg):
        (print(f"  ok  {msg}") if cond else failures.append(msg))

    # --- the hand-made file, row by row
    rows = sr.rows_of(hand())
    check(rows["r_event_trees"] == [("ET-A", "IE-A", 0.01, "exact", None, 1.0, 1.0),
                                    ("ET-B", "IE-B", 0.1, "exact", None, 1.0, 1.0)],
          "one row per event tree, with its initiating event and partition sum")
    check(rows["r_sequences"] == [
        ("ET-A", "SEQ-A-1", "OK", None, 0.009999, 0.009999, 0.009999),
        ("ET-A", "SEQ-A-2", "CD", None, 1e-6, 1e-6, 1e-6),
        ("ET-A", "SEQ-A-3>SEQ-B-2", "CD", "ET-B", 5e-8, 5e-8, 5e-8),
        ("ET-B", "SEQ-B-2", "CD", None, 3e-6, 3e-6, 3e-6)],
        "one row per sequence: an exact value is also its own bounds")
    check(rows["r_cut_sets"] == [
        (1, "ET-A", "SEQ-A-2", "CD", "cut_set", 1, 1, 8e-7, '["BE-X"]'),
        (2, "ET-A", "SEQ-A-2", "CD", "cut_set", 2, 2, 2e-7, '["BE-Y", "BE-Z"]'),
        (3, "ET-A", "SEQ-A-3>SEQ-B-2", "CD", "prime_implicant", 1, 2, 5e-8,
         '{"events": ["BE-Y"], "negated": ["BE-X"]}'),
        (4, "ET-B", "SEQ-B-2", "CD", "cut_set", 1, 1, 3e-6, '["BE-Y"]')]
        and rows["r_cut_set_events"] == [(1, "BE-X", 0), (2, "BE-Y", 0), (2, "BE-Z", 0),
                                         (3, "BE-Y", 0), (3, "BE-X", 1), (4, "BE-Y", 0)],
        "cut sets ranked in the file's order; a prime implicant's negated event is negated = 1")
    check(rows["r_metrics"] == [("ET-A", "CDF", "Core damage", 1e-6, 1e-6, 1e-6),
                                ("ET-B", "CDF", "Core damage", 3e-6, 3e-6, 3e-6)]
          and rows["r_metric_totals"] == [("CDF", 1e-6 + 3e-6, 1e-6 + 3e-6, 1e-6 + 3e-6)],
          "metric per tree, and its total over the trees: 1e-6 + 3e-6")
    model = {r[1]: r for r in rows["r_importance_model"]}
    x, y = model["BE-X"], model["BE-Y"]
    # BE-X is absent from ET-B: that tree contributes its own frequency to both cofactors
    check(len(model) == 2 and x[3] == (4e-6 - (5e-7 + 3e-6)) / 4e-6
          and math.isclose(x[3], 0.125, rel_tol=1e-12) and x[7] == 1e-4 + 3e-6
          and x[8] == 5e-7 + 3e-6 and x[4] == (1e-4 + 3e-6) / 4e-6
          and y[7] == 2e-5 + 1e-4 and y[8] == 8e-7 + 2.8e-6
          and math.isclose(y[3], 0.1, rel_tol=1e-12),
          "model-wide importance by hand: FV(BE-X) = (4e-6 − 3.5e-6)/4e-6 = 0.125, "
          "FV(BE-Y) = 0.1")
    check(len(rows["r_importance"]) == 5 and ("ET-A", "end_state", "CD", "BE-X", 0.001,
                                              (1e-6 - 5e-7) / 1e-6, 1e-4 / 1e-6, 1e-6 / 5e-7,
                                              1e-4 - 5e-7, 1e-4, 5e-7) in rows["r_importance"],
          "importance per tree, per metric and per end state")
    check(rows["r_uncertainty"] == [
        ("ET-A", "sequence", "SEQ-A-2", None, "frequency_per_year", 1.1e-6, 1e-7, 9e-7, 3e-6, 5e-7),
        ("ET-A", "metric", "CDF", None, "value_per_year", 1.2e-6, 2e-7, 1e-6, 4e-6, 6e-7),
        ("ET-A", "metric", "CDF", "BE-X", "fussell_vesely", 0.4, 0.1, 0.45, 0.7, 0.2)],
        "uncertainty of a sequence, a metric and an importance measure; draws are not loaded")
    check(rows["r_end_states"] == [("ET-A", "CD", 1e-6), ("ET-B", "CD", 3e-6)]
          and rows["r_basic_events"] == [("ET-A", "BE-X", 0.001), ("ET-A", "BE-Y", 0.002),
                                         ("ET-B", "BE-Y", 0.002)],
          "end-state frequencies and event probabilities per tree")

    trunc = hand()
    for et in trunc.values():
        et["method"], et["cutoff"] = "truncated-mcs", 1e-9
        et["partition"] = {"sum_probability_lower_bound": 0.999, "sum_probability_upper_bound": 1.01}
        del et["end_states"]
        for s in et["sequences"]:
            v = s.pop("frequency_per_year")
            s["frequency_lower_bound"], s["frequency_upper_bound"] = v, v * 1.5
        for m in et["metrics"]:
            v = m.pop("value_per_year")
            del m["importance"]
            m["value_lower_bound"], m["value_upper_bound"] = v, v * 2
    rows = sr.rows_of(trunc)
    check(rows["r_sequences"][1] == ("ET-A", "SEQ-A-2", "CD", None, None, 1e-6, 1e-6 * 1.5)
          and rows["r_metrics"][0] == ("ET-A", "CDF", "Core damage", None, 1e-6, 2e-6)
          and rows["r_metric_totals"] == [("CDF", None, 1e-6 + 3e-6, 2e-6 + 6e-6)]
          and rows["r_event_trees"][0] == ("ET-A", "IE-A", 0.01, "truncated-mcs", 1e-9, 0.999, 1.01)
          and rows["r_importance_model"] == [] and rows["r_end_states"] == [],
          "a truncated file has bounds and NULL values (FR-42), and no importance")
    mixed = hand()
    mixed["ET-B"] = trunc["ET-B"]
    check(sr.rows_of(mixed)["r_metric_totals"] == [("CDF", None, 1e-6 + 3e-6, 1e-6 + 6e-6)]
          and sr.rows_of(mixed)["r_importance_model"] == [],
          "one truncated tree makes the total a pair of bounds, never a value")

    def bad(name, needle, change):
        r = hand()
        change(r)
        try:
            sr.rows_of(r)
            failures.append(f"refused: {name}: accepted")
        except sr.ProjectionError as e:
            check(needle in str(e), f"refused: {name} ({str(e)[:70]})")

    bad("a fault-tree entry", "only event-tree results",
        lambda r: r["ET-A"].update(type="fault_tree"))
    bad("a value that is not a number", "is not a number",
        lambda r: r["ET-A"]["sequences"][1].update(frequency_per_year="1e-6"))
    bad("a cut set naming an event twice", "names an event twice",
        lambda r: r["ET-A"]["sequences"][1]["cut_sets"][0].update(events=["BE-X", "BE-X"]))
    bad("a prime implicant with an event and its negation", "names an event twice",
        lambda r: r["ET-A"]["sequences"][2]["prime_implicants"][0].update(negated=["BE-Y"]))
    try:
        sr.rows_of({})
        failures.append("refused: an empty file: accepted")
    except sr.ProjectionError as e:
        check("no event tree" in str(e), "refused: a file with no event tree")

    # --- the demo model
    engine = os.environ.get("CANOPY_BIN") or os.path.join(ROOT, "engine", "target", "release",
                                                          "canopy")
    if not os.path.exists(engine):
        print(f"test_sql_results: engine not built ({engine})", file=sys.stderr)
        return 1
    res = os.path.join(tmp, "head.json")
    r = subprocess.run([sys.executable, os.path.join(HERE, "quantify.py"), MODEL, res,
                        "--samples", "200", "--seed", "20260708", "--importance-uncertainty", "2"],
                       capture_output=True, text=True)
    if r.returncode:
        print("quantify.py failed:\n" + r.stdout + r.stderr, file=sys.stderr)
        return 1
    with open(res) as f:
        results = json.load(f)
    d = os.path.join(tmp, "model")
    shutil.copytree(MODEL, d)
    db = os.path.join(tmp, "m.sqlite")
    sp.load(d, db)
    counts = sr.load_results(db, res)
    et = results["ET-SLOCA"]
    con = sqlite3.connect(db)
    q = lambda s, *a: con.execute(s, a).fetchall()  # noqa: E731
    check(counts["r_sequences"] == len(et["sequences"]) == 4
          and counts["r_cut_sets"] == sum(len(s["cut_sets"]) for s in et["sequences"])
          and counts["r_cut_set_events"] == sum(len(c["events"]) for s in et["sequences"]
                                                for c in s["cut_sets"])
          and counts["r_importance"] == sum(len(o["importance"]) for o in
                                            et["metrics"] + et["end_states"])
          and counts["r_basic_events"] == len(et["basic_event_probabilities"]),
          f"the demo results: {counts['r_sequences']} sequences, {counts['r_cut_sets']} cut "
          f"sets, {counts['r_cut_set_events']} cut-set events, {counts['r_importance']} "
          f"importance rows")
    check(dict(q("SELECT sequence, frequency_per_year FROM r_sequences"))
          == {s["id"]: s["frequency_per_year"] for s in et["sequences"]}
          and q("SELECT value_per_year, value_lower_bound FROM r_metric_totals WHERE metric "
                "= 'CDF'") == [(et["metrics"][0]["value_per_year"],) * 2]
          and dict(q("SELECT event, probability FROM r_basic_events"))
          == et["basic_event_probabilities"]
          and dict(q("SELECT event, raw FROM r_importance WHERE scope = 'metric'"))
          == {r["event"]: r["raw"] for r in et["metrics"][0]["importance"]},
          "numbers are the file's, bit for bit")
    by_state = dict(q("SELECT end_state, SUM(frequency_per_year) FROM r_sequences "
                      "WHERE transfer IS NULL GROUP BY end_state"))
    engine_states = dict(q("SELECT end_state, frequency_per_year FROM r_end_states"))
    check(engine_states and all(math.isclose(by_state[k], v, rel_tol=1e-12)
                                for k, v in engine_states.items()),
          "sequence frequencies summed in SQL by end state = the engine's end-state "
          "frequencies")
    rep = json.loads(subprocess.run(
        [sys.executable, os.path.join(HERE, "consequence_report.py"), res, "--metric", "CDF",
         "--model", MODEL, "--json"], capture_output=True, text=True, check=True).stdout)
    pooled = q("SELECT c.events, SUM(c.frequency_per_year) FROM r_cut_sets c "
               "WHERE c.end_state = 'CD' GROUP BY c.events")
    want = {json.dumps(c["events"]): c["frequency_per_year"] for c in rep["cut_sets"]}
    check(len(pooled) == len(want) == 11
          and all(math.isclose(want[e], f, rel_tol=1e-12) for e, f in pooled),
          "the report's pooled cut-set table (11 cut sets) is one GROUP BY")
    exact = rep["bdd_exact_importance"]["importance"]
    got = q("SELECT event, fussell_vesely, raw, rrw, birnbaum_per_year FROM r_importance_model "
            "WHERE metric = 'CDF'")
    check(sorted(got) == sorted((r["event"], r["fussell_vesely"], r["raw"], r["rrw"],
                                 r["birnbaum_per_year"]) for r in exact) and len(got) == 12,
          "the report's exact importance table is r_importance_model, bit for bit")
    unknown = [r[0] for r in q("SELECT DISTINCT e.event FROM r_cut_set_events e LEFT JOIN "
                               "basic_events b ON b._id = e.event WHERE b._id IS NULL")]
    check(unknown and all(u.startswith("BE-CCF-") for u in unknown)
          and q("SELECT COUNT(*) FROM r_cut_set_events e LEFT JOIN r_basic_events b "
                "ON b.event = e.event WHERE b.event IS NULL") == [(0,)],
          f"cut-set events join to the model's basic events; the {len(unknown)} that do not "
          "are expanded common-cause events, all in r_basic_events")
    top = q("SELECT b.system, c.frequency_per_year FROM r_cut_sets c JOIN r_cut_set_events e "
            "USING (cut_set) JOIN basic_events b ON b._id = e.event WHERE c.size = 2 "
            "ORDER BY c.frequency_per_year DESC, b._id LIMIT 2")
    check(top == [("ECC", 3.52332e-09)] * 2
          and math.isclose(0.0005 * 0.006 * 0.9787 * 0.0012, 3.52332e-09, rel_tol=1e-12),
          "results join to the model: the most frequent two-event cut set is two ECC events "
          "(train A in maintenance, pump B fails to start: 0.0005 × 0.006 × 0.9787 × 0.0012)")
    check(q("SELECT COUNT(*) FROM r_uncertainty WHERE scope = 'sequence'") == [(4,)]
          and q("SELECT COUNT(*) FROM r_uncertainty WHERE event IS NOT NULL") == [(12,)]
          and dict(q("SELECT key, value FROM r_meta"))["samples"] == 200,
          "uncertainty: 4 sequences, 6 measures of 2 events; r_meta records the sampling")
    con.close()

    # --- lifecycle
    plain = os.path.join(tmp, "plain.json")
    subprocess.run([sys.executable, os.path.join(HERE, "quantify.py"), MODEL, plain],
                   capture_output=True, check=True)
    again = sr.load_results(db, plain)
    con = sqlite3.connect(db)
    check(again["r_uncertainty"] == 0
          and con.execute("SELECT value FROM r_meta WHERE key = 'samples'").fetchone() == (None,),
          "loading results again replaces the tables")
    con.close()
    before = {p: open(os.path.join(d, p)).read() for p in ("parameters.yaml", "model.yaml")}
    check(sp.dump(db, d) == [], "dump ignores the results tables: nothing written")
    con = sqlite3.connect(db)
    con.execute("UPDATE parameters SET value = 12.0 WHERE _id = 'PAR-MISSION-TIME-24H'")
    con.commit()
    con.close()
    r = subprocess.run([sys.executable, os.path.join(HERE, "sql_projection.py"), "dump", d,
                        "--db", db], capture_output=True, text=True)
    check(r.returncode == 0 and "results tables (r_*) are those of the model as it was loaded"
          in r.stderr and before["parameters.yaml"] != open(
              os.path.join(d, "parameters.yaml")).read(),
          "after an edit, dump says the results are those of the model as loaded")
    sp.load(d, db)
    con = sqlite3.connect(db)
    check(con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'r\\_%' ESCAPE '\\'")
          .fetchone() == (0,), "`sql load` starts again without results")
    con.close()

    # --- refusals against the database
    sr.load_results(db, plain)

    def snapshot():
        con = sqlite3.connect(db)
        try:
            return {t: con.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()
                    for t in sr.TABLES}
        finally:
            con.close()

    kept = snapshot()

    def refused(name, needle, change=None, text=None, database=None):
        p = os.path.join(tmp, "bad.json")
        if text is None:
            r = copy.deepcopy(results)
            change(r)
            text = json.dumps(r)
        with open(p, "w") as f:
            f.write(text)
        try:
            sr.load_results(database or db, p)
            failures.append(f"refused: {name}: accepted")
        except sr.ProjectionError as e:
            check(needle in str(e) and (database or snapshot() == kept),
                  f"refused, tables as they were: {name} ({str(e)[:70]})")

    refused("results of another model (event tree)", "event tree ET-OTHER is not in the model",
            lambda r: r.update({"ET-OTHER": dict(r.pop("ET-SLOCA"), id="ET-OTHER")}))
    refused("a sequence the model does not have", "sequence SEQ-SLOCA-09 is not in the model",
            lambda r: r["ET-SLOCA"]["sequences"][0].update(id="SEQ-SLOCA-09"))
    refused("another end state than the model's", "end state 'CD', the model has 'OK'",
            lambda r: r["ET-SLOCA"]["sequences"][0].update(end_state="CD"))
    refused("a sequence listed twice (fails inside the transaction)", "UNIQUE",
            lambda r: r["ET-SLOCA"]["sequences"].append(r["ET-SLOCA"]["sequences"][0]))
    refused("a file that is not JSON", "bad.json", text="not json")
    refused("JSON that is not a results file", "not a results file",
            text=json.dumps({"ET-SLOCA": {"type": "event_tree", "sequences": [{"x": 1}]}}))
    refused("no database", "no such database", lambda r: None,
            database=os.path.join(tmp, "none.sqlite"))
    other = os.path.join(tmp, "other.sqlite")
    sqlite3.connect(other).close()
    refused("a database not made by `sql load`", "not a database made by", lambda r: None,
            database=other)

    # --- the command
    r = subprocess.run([sys.executable, os.path.join(HERE, "canopy.py"), "sql", "results", plain,
                        "--db", db], capture_output=True, text=True)
    check(r.returncode == 0 and "13 cut_sets" in r.stdout and "12 importance_model" in r.stdout,
          "`canopy sql results FILE --db DB` loads and reports the rows")
    r = subprocess.run([sys.executable, os.path.join(HERE, "canopy.py"), "sql", "results"],
                       capture_output=True, text=True)
    check(r.returncode == 2 and "give the results file" in r.stderr,
          "`canopy sql results` without a file: usage, exit 2")
    with open(os.path.join(tmp, "bad.json"), "w") as f:
        f.write("{}")
    r = subprocess.run([sys.executable, os.path.join(HERE, "sql_results.py"),
                        os.path.join(tmp, "bad.json"), "--db", db], capture_output=True, text=True)
    check(r.returncode == 1 and r.stderr.startswith("sql results: "),
          "a refusal is exit 1 on stderr")

    shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print(f"\n{len(failures)} FAILED:")
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("\nsql results: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
