#!/usr/bin/env python3
"""Quantification results beside the model in the SQL working copy (FR-61).

    python ci/quantify.py model head.json
    python ci/sql_projection.py load model
    python ci/sql_results.py head.json [model] [--db FILE]

loads a results file written by `ci/quantify.py` into the database that
`sql_projection.py load` made of the same model, as tables named `r_*`,
so that cut sets, sequence frequencies and importance can be queried and
joined to the model's own tables:

    r_meta               what was loaded: file, digest, method, sampling
    r_event_trees        one row per event tree quantified
    r_basic_events       the probability of every event of a tree (the
                         expanded common-cause events included)
    r_sequences          frequency (or bounds) and end state per sequence
    r_end_states         frequency per end state of a tree
    r_metrics            value (or bounds) per risk metric of a tree
    r_metric_totals      a metric summed over the event trees
    r_cut_sets           one row per listed minimal cut set / prime implicant
    r_cut_set_events     one row per event of a cut set (negated: 0/1)
    r_importance         FV, RAW, RRW, Birnbaum per tree, per metric or
                         end state, per event
    r_importance_model   the same per metric over all event trees
                         (ci/importance.py: exact, frequencies are multilinear)
    r_uncertainty        mean, percentiles, standard deviation of a
                         sequence, a metric or an importance measure

Numbers are stored as the results file has them (the same doubles): no
rounding, no recomputation, except the two sums `r_metric_totals`
(ci/bounds.py) and `r_importance_model` (ci/importance.py), which are the
ones the reports print. A truncated result has bounds and no value
(FR-42): its value columns are NULL, and `*_lower_bound`/`*_upper_bound`
hold the bounds; an exact result has its value in all three.

Results are derived: `sql_projection.py dump` never reads these tables,
and loading results again replaces them. The file must be of the model
in the database: every event tree and sequence it names must be a row of
the model's tables with the same end state, or the load is refused and
the tables are left as they were. That is a consistency check, not proof
that the numbers were computed from this exact model state — the tables
describe the results file, whose digest is in `r_meta`.

Not loaded: Monte Carlo draws (`--keep-samples`, importance draws), the
per-configuration files of `--configurations`, fault-tree results.
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bounds  # noqa: E402
import importance  # noqa: E402
import sql_projection  # noqa: E402

ProjectionError = sql_projection.ProjectionError

STATS = ("mean", "p05", "p50", "p95", "std")
MEASURES = ("fussell_vesely", "raw", "rrw", "birnbaum_per_year",
            "frequency_if_true_per_year", "frequency_if_false_per_year")

TABLES = {
    "r_meta": "key TEXT PRIMARY KEY, value",
    "r_event_trees": "event_tree TEXT PRIMARY KEY, initiating_event TEXT, "
                     "initiating_event_frequency_per_year REAL, method TEXT, cutoff REAL, "
                     "partition_lower_bound REAL, partition_upper_bound REAL",
    "r_basic_events": "event_tree TEXT, event TEXT, probability REAL, "
                      "PRIMARY KEY (event_tree, event)",
    "r_sequences": "event_tree TEXT, sequence TEXT, end_state TEXT, transfer TEXT, "
                   "frequency_per_year REAL, frequency_lower_bound REAL, "
                   "frequency_upper_bound REAL, PRIMARY KEY (event_tree, sequence)",
    "r_end_states": "event_tree TEXT, end_state TEXT, frequency_per_year REAL, "
                    "PRIMARY KEY (event_tree, end_state)",
    "r_metrics": "event_tree TEXT, metric TEXT, label TEXT, value_per_year REAL, "
                 "value_lower_bound REAL, value_upper_bound REAL, "
                 "PRIMARY KEY (event_tree, metric)",
    "r_metric_totals": "metric TEXT PRIMARY KEY, value_per_year REAL, "
                       "value_lower_bound REAL, value_upper_bound REAL",
    "r_cut_sets": "cut_set INTEGER PRIMARY KEY, event_tree TEXT, sequence TEXT, "
                  "end_state TEXT, kind TEXT, rank INTEGER, size INTEGER, "
                  "frequency_per_year REAL, events TEXT",
    "r_cut_set_events": "cut_set INTEGER, event TEXT, negated INTEGER, "
                        "PRIMARY KEY (cut_set, event)",
    "r_importance": "event_tree TEXT, scope TEXT, id TEXT, event TEXT, probability REAL, "
                    + ", ".join(f"{m} REAL" for m in MEASURES)
                    + ", PRIMARY KEY (event_tree, scope, id, event)",
    "r_importance_model": "metric TEXT, event TEXT, probability REAL, "
                          + ", ".join(f"{m} REAL" for m in MEASURES)
                          + ", PRIMARY KEY (metric, event)",
    "r_uncertainty": "event_tree TEXT, scope TEXT, id TEXT, event TEXT, measure TEXT, "
                     + ", ".join(f"{s} REAL" for s in STATS),
}
INDEXES = ("CREATE INDEX r_cut_set_events_event ON r_cut_set_events (event)",
           "CREATE INDEX r_cut_sets_sequence ON r_cut_sets (event_tree, sequence)")


def _num(x, where):
    """A number of the results file, as it is; null stays NULL."""
    if x is None:
        return None
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise ProjectionError(f"{where}: {x!r} is not a number")
    return x


def _stats(u, where):
    return tuple(_num(u.get(s), f"{where}.{s}") for s in STATS)


def rows_of(results: dict) -> dict:
    """{table: [row tuples]} for a results file's content."""
    out = {t: [] for t in TABLES}
    if not isinstance(results, dict) or not results:
        raise ProjectionError("the results file holds no event tree")
    cut_id = 0
    for et_id in sorted(results):
        et = results[et_id]
        if not isinstance(et, dict) or et.get("type") != "event_tree":
            kind = et.get("type") if isinstance(et, dict) else type(et).__name__
            raise ProjectionError(f"{et_id}: only event-tree results are loaded "
                                  f"(this entry is {kind!r})")
        ie = et.get("initiating_event") or {}
        part = et.get("partition") or {}
        p_lo = part.get("sum_probability", part.get("sum_probability_lower_bound"))
        p_hi = part.get("sum_probability", part.get("sum_probability_upper_bound"))
        out["r_event_trees"].append((
            et_id, ie.get("id"), _num(ie.get("frequency_per_year"), f"{et_id}: initiating event"),
            et.get("method", "exact"), _num(et.get("cutoff"), f"{et_id}: cutoff"),
            _num(p_lo, f"{et_id}: partition"), _num(p_hi, f"{et_id}: partition")))
        for ev, p in (et.get("basic_event_probabilities") or {}).items():
            out["r_basic_events"].append((et_id, ev, _num(p, f"{et_id}: {ev}")))
        for seq in et.get("sequences", []):
            sid = seq["id"]
            where = f"{et_id}:{sid}"
            lo, hi = bounds.seq_interval(seq)
            out["r_sequences"].append((
                et_id, sid, seq.get("end_state"), seq.get("transfer"),
                _num(seq.get("frequency_per_year"), where), _num(lo, where), _num(hi, where)))
            if "uncertainty" in seq:
                out["r_uncertainty"].append((et_id, "sequence", sid, None, "frequency_per_year")
                                            + _stats(seq["uncertainty"], where))
            listed = [("cut_set", c, c["events"], []) for c in seq.get("cut_sets") or []] \
                + [("prime_implicant", c, c["events"], c.get("negated", []))
                   for c in seq.get("prime_implicants") or []]
            rank = {}
            for kind, c, events, negated in listed:
                cut_id += 1
                rank[kind] = rank.get(kind, 0) + 1
                if set(events) & set(negated) or len(set(events)) != len(events) \
                        or len(set(negated)) != len(negated):
                    raise ProjectionError(f"{where}: a {kind} names an event twice")
                out["r_cut_sets"].append((
                    cut_id, et_id, sid, seq.get("end_state"), kind, rank[kind],
                    len(events) + len(negated), _num(c.get("frequency_per_year"), where),
                    json.dumps({"events": events, "negated": negated} if kind
                               == "prime_implicant" else events, ensure_ascii=False)))
                out["r_cut_set_events"] += [(cut_id, e, 0) for e in events] \
                    + [(cut_id, e, 1) for e in negated]

        def importance_rows(scope, owner):
            where = f"{et_id}: {scope} {owner['id']}"
            for r in owner.get("importance") or []:
                out["r_importance"].append(
                    (et_id, scope, owner["id"], r["event"], _num(r.get("probability"), where))
                    + tuple(_num(r.get(m), f"{where}: {r['event']}.{m}") for m in MEASURES))
                for m, u in (r.get("uncertainty") or {}).items():
                    if m in MEASURES and isinstance(u, dict):
                        out["r_uncertainty"].append((et_id, scope, owner["id"], r["event"], m)
                                                    + _stats(u, where))

        for es in et.get("end_states") or []:
            out["r_end_states"].append((et_id, es["id"], _num(es.get("frequency_per_year"),
                                                              f"{et_id}: end state {es['id']}")))
            importance_rows("end_state", es)
        for m in et.get("metrics") or []:
            where = f"{et_id}: metric {m['id']}"
            lo, hi = bounds.metric_interval(m)
            out["r_metrics"].append((et_id, m["id"], m.get("label"),
                                     _num(m.get("value_per_year"), where),
                                     _num(lo, where), _num(hi, where)))
            if isinstance(m.get("uncertainty"), dict):
                out["r_uncertainty"].append((et_id, "metric", m["id"], None, "value_per_year")
                                            + _stats(m["uncertainty"], where))
            importance_rows("metric", m)
    truncated = bounds.any_truncated(results)
    for metric, (lo, hi) in sorted(bounds.metric_totals(results).items()):
        out["r_metric_totals"].append((metric, None if truncated else lo, lo, hi))
        if truncated:
            continue
        table = importance.for_metric(results, metric)
        for r in (table or {}).get("importance", []):
            out["r_importance_model"].append((metric, r["event"], r["probability"])
                                             + tuple(r.get(m) for m in MEASURES))
    return out


def _meta(results: dict, path: str, digest: str) -> list:
    methods = sorted({r.get("method", "exact") for r in results.values()})
    cutoffs = sorted({r["cutoff"] for r in results.values() if r.get("cutoff") is not None})
    unc = next((r["uncertainty"] for r in results.values()
                if isinstance(r.get("uncertainty"), dict)), {})
    return [("file", os.path.abspath(path)), ("sha256", digest),
            ("method", ", ".join(methods)),
            ("cutoff", ", ".join(repr(c) for c in cutoffs) or None),
            ("samples", unc.get("samples")), ("seed", unc.get("seed")),
            ("sampling", unc.get("method"))]


def _check_model(db, rows: dict):
    """Every event tree and sequence of the results is one of the model in
    the database, with the same end state."""
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "tables_" not in names:
        raise ProjectionError("not a database made by `sql load`")
    trees = {r[0] for r in db.execute("SELECT id FROM event_tree")} \
        if "event_tree" in names else set()
    seqs = {(p, i): e for p, i, e in db.execute("SELECT _parent, _id, end_state FROM sequences")} \
        if "sequences" in names else {}
    owner = {r[0]: r[1] for r in db.execute("SELECT _id, id FROM event_tree")} \
        if "event_tree" in names else {}
    seqs = {(owner.get(p, p), i): e for (p, i), e in seqs.items()}
    problems = []
    for row in rows["r_event_trees"]:
        if row[0] not in trees:
            problems.append(f"event tree {row[0]} is not in the model")
    for et, sid, end_state, *_ in rows["r_sequences"]:
        if et not in trees:
            continue
        hops = sid.split(">")                # a followed transfer: SEQ-S>SEQ-T
        if (et, hops[0]) not in seqs:
            problems.append(f"{et}: sequence {hops[0]} is not in the model")
        elif len(hops) == 1 and seqs[(et, sid)] != end_state:
            problems.append(f"{et}:{sid}: end state {end_state!r}, the model has "
                            f"{seqs[(et, sid)]!r}")
    if problems:
        raise ProjectionError("these results are not of the model in the database: "
                              + "; ".join(problems[:5])
                              + (f" (and {len(problems) - 5} more)" if len(problems) > 5 else ""))


def load_results(db_path: str, results_path: str) -> dict:
    """Load a results file into the `r_*` tables; returns {table: rows}.
    All or nothing: on a refusal the database is as it was."""
    if not os.path.exists(db_path):
        raise ProjectionError(f"no such database: {db_path} (run `sql load` first)")
    try:
        with open(results_path, "rb") as f:
            raw = f.read()
        results = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as e:
        raise ProjectionError(f"{results_path}: {e}")
    try:
        rows = rows_of(results)
    except (KeyError, TypeError, AttributeError) as e:
        raise ProjectionError(f"{results_path}: not a results file of ci/quantify.py "
                              f"({type(e).__name__}: {e})")
    rows["r_meta"] = _meta(results, results_path, hashlib.sha256(raw).hexdigest())
    db = sqlite3.connect(db_path, isolation_level=None)     # transactions by hand:
    try:                                                    # the DDL must roll back too
        _check_model(db, rows)
        db.execute("BEGIN")
        try:
            for t in TABLES:
                db.execute(f"DROP TABLE IF EXISTS {t}")
            for t, cols in TABLES.items():
                db.execute(f"CREATE TABLE {t} ({cols})")
                if rows[t]:
                    marks = ", ".join("?" * len(rows[t][0]))
                    db.executemany(f"INSERT INTO {t} VALUES ({marks})", rows[t])
            for stmt in INDEXES:
                db.execute(stmt)
            db.execute("COMMIT")
        except sqlite3.Error as e:
            db.execute("ROLLBACK")
            raise ProjectionError(f"{results_path}: {e}")
    except sqlite3.Error as e:
        raise ProjectionError(f"{results_path}: {e}")
    finally:
        db.close()
    return {t: len(rows[t]) for t in TABLES}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("results", help="a results file written by ci/quantify.py")
    ap.add_argument("model", nargs="?", default="model")
    ap.add_argument("--db", help="database file (default: the one `sql load` recorded)")
    a = ap.parse_args()
    try:
        db = a.db or sql_projection.current_db(a.model)
        counts = load_results(db, a.results)
    except ProjectionError as e:
        print(f"sql results: {e}", file=sys.stderr)
        return 1
    shown = ", ".join(f"{n} {t[2:]}" for t, n in counts.items() if n and t != "r_meta")
    print(f"sql results: {a.results} -> {db}: {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
