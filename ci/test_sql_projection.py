#!/usr/bin/env python3
"""Tests for ci/sql_projection.py (FR-60): the SQLite working copy.

  * exactness: the demo model loaded and dumped is the canonical model,
    byte for byte, into its own directory (nothing rewritten) and into an
    empty one (every file written);
  * the tables: one row per entity, child tables with their owner, the
    columns of the schema even when no entity uses them, lists and
    formulas as JSON that SQLite's own JSON functions can query;
  * edits in SQL come back as the YAML an author would have written:
    a value, a text, a boolean, a formula, a new entity, a removed one,
    a reordering, an entity moved to another file, a new gate in a tree;
    each changes only the lines it should, and the edited model still
    passes the validator where the edit keeps it valid;
  * comments stay with their key; one whose key was deleted is reported;
  * refusals, each with nothing written: a file generated from a
    template, a file edited on disk since the load (unless --force), a
    column added in SQL, text that is not JSON, a boolean that is not
    0/1, a blob, a row whose owner or file does not exist, two rows with
    one ID, a missing database; at load: an explicit null, an entity
    whose fields are not those of one object, a model without model.yaml;
  * the exactness check at load fails, and leaves no database, when a
    file would not come back as it is (the renderer is made wrong);
  * the schema layout covers every property of every entity;
  * the command: default database under .canopy/cache/<commit>, recorded
    in .canopy/current; `guard` fails when a database is tracked by git.

Usage: python ci/test_sql_projection.py
"""
import difflib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import canonical as cn  # noqa: E402
import sql_projection as sp  # noqa: E402

MODEL = os.path.join(ROOT, "model")
SCHEMA = os.path.join(ROOT, "schema", "psa-model.schema.json")


def read_tree(d):
    out = {}
    for path in cn.model_files(d):
        with open(path, encoding="utf-8") as f:
            out[os.path.relpath(path, d).replace(os.sep, "/")] = f.read()
    return out


def changed_lines(old, new):
    """(removed, added) lines between two texts."""
    rem, add = [], []
    for line in difflib.unified_diff(old.split("\n"), new.split("\n"), lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        (rem if line.startswith("-") else add).append(line[1:])
    return rem, add


def main() -> int:
    failures = []
    tmp = tempfile.mkdtemp(prefix="canopy-sql-")

    def check(cond, msg):
        (print(f"  ok  {msg}") if cond else failures.append(msg))

    def fresh(name, source=MODEL):
        """A copy of a model and a database loaded from it."""
        d = os.path.join(tmp, name)
        shutil.copytree(source, d)
        db = os.path.join(tmp, name + ".sqlite")
        counts = sp.load(d, db)
        return d, db, counts

    def sql(db, *statements):
        con = sqlite3.connect(db)
        for s in statements:
            con.execute(*s) if isinstance(s, tuple) else con.execute(s)
        con.commit()
        con.close()

    def edit(name, *statements, source=MODEL):
        """Files changed by SQL statements: {file: (removed, added)}."""
        d, db, _ = fresh(name, source)
        before = read_tree(d)
        sql(db, *statements)
        changed = sp.dump(db, d)
        after = read_tree(d)
        check(sorted(changed) == sorted(f for f in after if after[f] != before.get(f)),
              f"{name}: dump reports exactly the files it rewrote ({', '.join(changed)})")
        return d, {f: changed_lines(before.get(f, ""), after[f]) for f in changed}

    def refused(name, needle, *statements, source=MODEL, prepare=None, force=False):
        d, db, _ = fresh(name, source)
        if prepare:
            prepare(d)
        before = read_tree(d)
        sql(db, *statements)
        try:
            sp.dump(db, d, force)
            failures.append(f"refused: {name}: accepted")
        except sp.ProjectionError as e:
            check(needle in str(e) and read_tree(d) == before,
                  f"refused, nothing written: {name} ({str(e)[:80]})")

    def valid(d):
        r = subprocess.run([sys.executable, os.path.join(HERE, "validate.py"), d, SCHEMA],
                           capture_output=True, text=True)
        return r.returncode == 0

    # --- exactness on the demo model
    canon = {}
    for path in cn.model_files(MODEL):
        with open(path, encoding="utf-8") as f:
            canon[os.path.relpath(path, MODEL).replace(os.sep, "/")] = cn.canonical_text(f.read())
    d, db, counts = fresh("exact")
    check(sp.dump(db, d) == [] and read_tree(d) == canon,
          "load then dump rewrites nothing: the model is already what the database holds")
    empty = os.path.join(tmp, "empty")
    os.makedirs(empty)
    written = sp.dump(db, empty)
    check(sorted(written) == sorted(canon) and read_tree(empty) == canon,
          f"dump into an empty directory writes all {len(canon)} files, byte for byte")
    check(valid(empty), "the dumped model passes the validator")

    # --- the tables
    check(counts == {"basic_events": 11, "ccf_groups": 1, "event_tree": 1, "fault_trees": 3,
                     "functional_events": 3, "gates": 9, "house_events": 2, "parameters": 3,
                     "sequences": 4},
          f"one row per entity: {counts}")
    con = sqlite3.connect(db)
    check(con.execute("SELECT _parent, _file FROM gates WHERE _id = 'GT-ECC-INJ-TOP'").fetchone()
          == ("FT-ECCS-INJECTION", "fault-trees/ft-eccs-injection.yaml"),
          "a gate row names its fault tree and its file")
    check(con.execute("SELECT value, unit, uncertainty__error_factor, provenance__source "
                      "FROM parameters WHERE _id = 'PAR-ECC-PMP-FR'").fetchone()
          == (3.0e-5, "per_hour", 3.0, "NUREG/CR-6928, 2020 parameter update, MDP standby FTR"),
          "nested fields are columns joined by __")
    cols = [r[1] for r in con.execute("PRAGMA table_info(basic_events)")]
    check("failure_model__test_interval__value" in cols and "external_ids" in cols,
          "columns come from the schema, including fields no entity of the model uses")
    users = [r[0] for r in con.execute(
        "SELECT g._id FROM gates g, json_tree(g.formula) j "
        "WHERE j.value = 'BE-ECC-PMP-A-FTS' ORDER BY g._id")]
    check(users == ["GT-ECC-TRAIN-A"],
          "a formula is JSON: SQLite finds the gates that use a basic event")
    check(con.execute("SELECT COUNT(*) FROM sequences WHERE json_extract(path, '$.FE-RT') "
                      "= 'success' AND end_state = 'CD'").fetchone()[0] == 2,
          "a sequence path is JSON: sequences by branch and end state")
    check(con.execute("SELECT kind FROM files WHERE path = 'model.yaml'").fetchone()[0]
          == "document" and json.loads(con.execute(
              "SELECT json FROM documents WHERE file = 'model.yaml'").fetchone()[0])["model"],
          "model.yaml is kept as one JSON document")
    con.close()

    # --- edits
    d, ch = edit("value", "UPDATE parameters SET value = value * 2 WHERE _id = 'PAR-ECC-PMP-FR'")
    check(ch == {"parameters.yaml": (["    value: 3.0e-5"], ["    value: 6.0e-5"])},
          "a value changed in SQL changes one line of one file")
    d, ch = edit("text", ("UPDATE basic_events SET label = ?, provenance__justification = ? "
                          "WHERE _id = 'BE-RPS-LOGIC-FAIL'",
                          ('Logic: "2 of 4" fails', "Reviewed #12")))
    check(ch == {"basic-events/rps.yaml": (
        ['    label: "RPS actuation logic fails"',
         '      justification: "Illustrative logic-train failure value."'],
        ['    label: "Logic: \\"2 of 4\\" fails"', '      justification: "Reviewed #12"'])}
        and valid(d), "texts are written quoted and escaped; the model stays valid")
    d, ch = edit("bool", "UPDATE house_events SET \"default\" = 1 WHERE _id = 'HE-ECC-TRAIN-A-OOS'")
    check(ch == {"house-events.yaml": (["    default: false"], ["    default: true"])},
          "a boolean is 0/1 in SQL, false/true in YAML")
    d, ch = edit("formula", ("UPDATE gates SET formula = ? WHERE _id = 'GT-ECC-INJ-TOP'",
                             (json.dumps({"or": ["GT-ECC-TRAIN-A", "GT-ECC-TRAIN-B"]}),)))
    rem, add = ch.get("fault-trees/ft-eccs-injection.yaml", ([], []))
    check(len(ch) == 1 and rem == ["          and: [GT-ECC-TRAIN-A, GT-ECC-TRAIN-B]"]
          and add == ["          or: [GT-ECC-TRAIN-A, GT-ECC-TRAIN-B]"] and valid(d),
          "a formula edited as JSON changes its own line only")
    d, ch = edit("insert",
                 "UPDATE parameters SET _ord = _ord + 1 WHERE _ord >= 1",
                 "INSERT INTO parameters (_id, _file, _ord, label, value, unit, "
                 "provenance__source, provenance__justification) VALUES ('PAR-NEW', "
                 "'parameters.yaml', 1, 'New parameter', 0.5, 'per_demand', 'Test', 'Added in SQL')")
    rem, add = ch.get("parameters.yaml", ([], []))
    check(len(ch) == 1 and rem == [] and [x for x in add if x] == [
        "  PAR-NEW:", '    label: "New parameter"', "    value: 0.5", "    unit: per_demand",
        "    provenance:", '      source: "Test"', '      justification: "Added in SQL"']
        and len(add) == 8
        and list(cn.loads(read_tree(d)["parameters.yaml"])[1]["parameters"])[1] == "PAR-NEW"
        and valid(d), "an inserted row is a new entity, fields in schema order, at its _ord")
    d, ch = edit("delete", "DELETE FROM sequences WHERE _id = 'SEQ-SLOCA-04'")
    rem, add = ch.get("event-trees/et-sloca.yaml", ([], []))
    check(len(ch) == 1 and add == [] and any("SEQ-SLOCA-04" in x for x in rem),
          "a deleted row removes the entity and nothing else")
    d, ch = edit("reorder", "UPDATE house_events SET _ord = 1 - _ord")
    check(list(cn.loads(read_tree(d)["house-events.yaml"])[1]["house_events"])
          == ["HE-OFFSITE-POWER-AVAILABLE", "HE-ECC-TRAIN-A-OOS"] and valid(d),
          "the order of entities in a file is _ord")
    d, ch = edit("move", "UPDATE basic_events SET _file = 'basic-events/ecc-pumps-tm.yaml', "
                         "_ord = 99 WHERE _id = 'BE-RPS-LOGIC-FAIL'",
                 )
    check(sorted(ch) == ["basic-events/ecc-pumps-tm.yaml", "basic-events/rps.yaml"]
          and "BE-RPS-LOGIC-FAIL" in cn.loads(read_tree(d)["basic-events/ecc-pumps-tm.yaml"])[1][
              "basic_events"], "changing _file moves an entity to another file")
    d, ch = edit("gate",
                 ("INSERT INTO gates (_id, _parent, _file, _ord, label, formula) VALUES "
                  "('GT-RPS-NEW', 'FT-RPS', 'fault-trees/ft-rps.yaml', 50, 'New gate', ?)",
                  (json.dumps({"or": ["BE-RPS-LOGIC-FAIL", "BE-RPS-BREAKER-CCF"]}),)))
    gates = cn.loads(read_tree(d)["fault-trees/ft-rps.yaml"])[1]["fault_trees"]["FT-RPS"]["gates"]
    check(list(gates)[-1] == "GT-RPS-NEW" and gates["GT-RPS-NEW"]["formula"]
          == {"or": ["BE-RPS-LOGIC-FAIL", "BE-RPS-BREAKER-CCF"]},
          "a row inserted in a child table lands in its owner")
    d, ch = edit("null", "UPDATE parameters SET uncertainty__distribution = NULL, "
                         "uncertainty__error_factor = NULL WHERE _id = 'PAR-ECC-PMP-FR'")
    check(ch == {"parameters.yaml": (["    uncertainty:", "      distribution: lognormal",
                                      "      error_factor: 3.0"], [])},
          "NULL in every column of a nested field removes the field")

    d, ch = edit("new-file",
                 "INSERT INTO files (path, ord, header, root_key, kind) VALUES "
                 "('basic-events/new.yaml', 99, '# A new file', 'basic_events', 'rows')",
                 "UPDATE basic_events SET _file = 'basic-events/new.yaml' "
                 "WHERE _id = 'BE-RPS-LOGIC-FAIL'")
    new = read_tree(d).get("basic-events/new.yaml", "")
    check(sorted(ch) == ["basic-events/new.yaml", "basic-events/rps.yaml"]
          and new.startswith("# A new file\nbasic_events:\n\n  BE-RPS-LOGIC-FAIL:\n")
          and valid(d), "a row added to `files` is a new model file")
    d, ch = edit("integer", "UPDATE parameters SET value = 48 WHERE _id = "
                            "'PAR-MISSION-TIME-24H'")
    check(ch == {"parameters.yaml": (["    value: 24.0"], ["    value: 48"])},
          "a number keeps the type SQLite gives it: 48 is written as an integer")

    # --- comments
    def commented(d):
        p = os.path.join(d, "parameters.yaml")
        with open(p, encoding="utf-8") as f:
            t = f.read()
        t = t.replace("  PAR-ECC-PMP-FR:\n", "  # the rate under review\n  PAR-ECC-PMP-FR:\n")
        t = t.replace("    value: 24.0\n", "    value: 24.0  # one day\n")
        with open(p, "w", encoding="utf-8") as f:
            f.write(t)

    src = os.path.join(tmp, "commented")
    shutil.copytree(MODEL, src)
    commented(src)
    d, ch = edit("comment-kept", "UPDATE parameters SET value = 48.0 WHERE _id = "
                                 "'PAR-MISSION-TIME-24H'", source=src)
    check(ch == {"parameters.yaml": (["    value: 24.0  # one day"],
                                     ["    value: 48.0  # one day"])}
          and "  # the rate under review\n  PAR-ECC-PMP-FR:\n" in read_tree(d)["parameters.yaml"],
          "comments stay with their key through an edit")
    d, db, _ = fresh("comment-lost", src)
    sql(db, "DELETE FROM parameters WHERE _id = 'PAR-ECC-PMP-FR'")
    con = sqlite3.connect(db)
    _, notes = sp.render(con)
    con.close()
    check(notes == ["parameters.yaml: 1 comment(s) dropped: the key they annotated is gone"],
          "a comment whose entity was deleted is reported, not moved")

    # --- refusals at dump
    refused("generated file", "generated from a template",
            "UPDATE basic_events SET label = 'x' WHERE _file = 'basic-events/ecc-pumps.yaml'")

    def touch(d):
        p = os.path.join(d, "parameters.yaml")
        with open(p, encoding="utf-8") as f:
            t = f.read()
        with open(p, "w", encoding="utf-8") as f:
            f.write(t.replace("value: 24.0", "value: 12.0"))

    refused("file edited on disk since the load", "changed on disk",
            "UPDATE parameters SET unit = 'hour' WHERE _id = 'PAR-ECC-PMP-FR'", prepare=touch)
    d, db, _ = fresh("force")
    touch(d)
    sql(db, "UPDATE parameters SET label = 'Forced' WHERE _id = 'PAR-ECC-PMP-FR'")
    check(sp.dump(db, d, force=True) == ["parameters.yaml"]
          and "value: 24.0" in read_tree(d)["parameters.yaml"],
          "--force writes the database's version over a file edited since the load")
    d, db, _ = fresh("layout-only")
    p = os.path.join(d, "parameters.yaml")
    with open(p, encoding="utf-8") as f:
        t = f.read()
    with open(p, "w", encoding="utf-8") as f:
        f.write(t.replace("    unit: hour\n", "    unit:   hour\n"))
    sql(db, "UPDATE parameters SET label = 'Day' WHERE _id = 'PAR-MISSION-TIME-24H'")
    check(sp.dump(db, d) == ["parameters.yaml"],
          "a file whose layout alone changed on disk is not 'edited': same data, dump proceeds")
    refused("column added in SQL", "were added in SQL",
            "ALTER TABLE parameters ADD COLUMN note TEXT")
    refused("text that is not JSON", "not JSON",
            "UPDATE gates SET formula = 'or(A, B)' WHERE _id = 'GT-ECC-INJ-TOP'")
    refused("boolean that is not 0/1", "not a boolean",
            "UPDATE house_events SET \"default\" = 'yes' WHERE _id = 'HE-ECC-TRAIN-A-OOS'")
    refused("blob", "blob", "UPDATE parameters SET label = x'00ff' WHERE _id = 'PAR-ECC-PMP-FR'")
    refused("gate without its fault tree", "is not a row of fault_trees",
            "UPDATE gates SET _parent = 'FT-NONE' WHERE _id = 'GT-ECC-INJ-TOP'")
    refused("row in a file that does not exist", "not in the files table",
            "UPDATE parameters SET _file = 'other.yaml' WHERE _id = 'PAR-ECC-PMP-FR'")
    refused("two rows with one ID", "same _id",
            "INSERT INTO house_events (_id, _file, _ord, label, \"default\") VALUES "
            "('HE-ECC-TRAIN-A-OOS', 'house-events.yaml', 9, 'again', 0)")
    refused("document row deleted", "no row in documents",
            "DELETE FROM documents WHERE file = 'model.yaml'")
    refused("event tree file without its row", "is not a row of event_tree",
            "DELETE FROM event_tree")
    try:
        sp.dump(os.path.join(tmp, "none.sqlite"), empty)
        failures.append("refused: missing database: accepted")
    except sp.ProjectionError as e:
        check("no such database" in str(e), "refused: a database that does not exist")

    # --- refusals at load
    def load_refused(name, needle, change):
        d = os.path.join(tmp, name)
        shutil.copytree(MODEL, d)
        change(d)
        db = os.path.join(tmp, name + ".sqlite")
        try:
            sp.load(d, db)
            failures.append(f"refused at load: {name}: accepted")
        except sp.ProjectionError as e:
            check(needle in str(e) and not os.path.exists(db),
                  f"refused at load, no database left: {name} ({str(e)[:80]})")

    def sub(rel, old, new):
        def change(d):
            p = os.path.join(d, rel)
            with open(p, encoding="utf-8") as f:
                t = f.read()
            assert old in t, (rel, old)
            with open(p, "w", encoding="utf-8") as f:
                f.write(t.replace(old, new, 1))
        return change

    load_refused("explicit null", "explicit null",
                 sub("parameters.yaml", "    unit: hour\n", "    unit: hour\n    uncertainty: null\n"))
    load_refused("fields of no known object", "not those of one known object",
                 sub("house-events.yaml", "    default: false\n",
                     "    default: false\n    colour: red\n    shape: round\n"))
    load_refused("no model.yaml", "no model.yaml", lambda d: os.remove(
        os.path.join(d, "model.yaml")))

    # the exactness check itself: a projection that would not write a file
    # back as it is fails at load, and leaves no database
    real = sp.render

    def wrong(con):
        texts, notes = real(con)
        texts["parameters.yaml"] = texts["parameters.yaml"].replace("24.0", "24.5")
        return texts, notes

    sp.render = wrong
    try:
        load_refused("a projection that is not exact", "cannot be projected exactly",
                     lambda d: None)
    finally:
        sp.render = real

    # --- the schema layout
    with open(SCHEMA) as f:
        schema = json.load(f)
    layout = sp.schema_layout(schema)
    missing, entities = [], 0
    for name, d in schema["$defs"].items():
        if not name.endswith("File"):
            continue
        for root, node in d.get("properties", {}).items():
            node = sp._resolve(node, schema)
            entity = node.get("additionalProperties") if "properties" not in node else node
            props = sp._object_props(entity, schema) if isinstance(entity, dict) else None
            if not props:
                continue                         # a scalar of the file, not entities
            entities += 1
            heads = {p[0] for p in layout.get(root, ([], set()))[0]}
            missing += [f"{root}.{k}" for k in props if k not in heads and k not in layout]
    check(entities >= 4, f"the schema declares {entities} kinds of entity files")
    check(not missing, "every property of every schema entity is a column or a child table"
          + (": missing " + ", ".join(missing) if missing else ""))
    check({("formula",), ("path",), ("house_events",)}
          <= layout["gates"][1] | layout["sequences"][1],
          "formulas, paths and house-event overrides are JSON columns by schema")

    # --- the command
    repo = os.path.join(tmp, "repo")
    shutil.copytree(MODEL, os.path.join(repo, "model"))
    git = ["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "model"], check=True)
    head = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
    tool = [sys.executable, os.path.join(HERE, "sql_projection.py")]
    m = os.path.join(repo, "model")
    r = subprocess.run(tool + ["path", m], capture_output=True, text=True)
    check(r.returncode == 1 and "no database loaded" in r.stderr,
          "path before any load: says so, exit 1")
    r = subprocess.run(tool + ["load", m], capture_output=True, text=True)
    want = os.path.join(os.path.realpath(repo), ".canopy", "cache", head, "model.sqlite")
    got = subprocess.run(tool + ["path", m], capture_output=True, text=True).stdout.strip()
    check(r.returncode == 0 and os.path.realpath(got) == want and os.path.exists(want),
          "load without --db writes .canopy/cache/<commit>/model.sqlite and records it")
    sql(want, "UPDATE parameters SET value = 12.0 WHERE _id = 'PAR-MISSION-TIME-24H'")
    r = subprocess.run(tool + ["dump", m], capture_output=True, text=True)
    diff = subprocess.run(git + ["diff", "--stat"], capture_output=True, text=True).stdout
    check(r.returncode == 0 and "wrote 1 file(s)" in r.stdout and "parameters.yaml" in diff
          and "1 insertion(+), 1 deletion(-)" in diff,
          "dump without --db uses the recorded database; the edit is a one-line git diff")
    r = subprocess.run(tool + ["load", m], capture_output=True, text=True)
    got = subprocess.run(tool + ["path", m], capture_output=True, text=True).stdout.strip()
    check(r.returncode == 0 and got.endswith(os.path.join(head + "-dirty", "model.sqlite")),
          "a model with uncommitted changes loads into <commit>-dirty")
    r = subprocess.run(tool + ["dump", m, "--db", os.path.join(tmp, "none.sqlite")],
                       capture_output=True, text=True)
    check(r.returncode == 1 and "no such database" in r.stderr, "a refusal is exit 1 on stderr")
    check(sp.guard(repo) == 0, "guard: an untracked .canopy/ is fine")
    shutil.copy(want, os.path.join(repo, "model.sqlite"))
    subprocess.run(git + ["add", "-f", "model.sqlite"], check=True)
    check(sp.guard(repo) == 1, "guard: a tracked database fails")
    subprocess.run(git + ["rm", "-q", "--cached", "model.sqlite"], check=True)
    subprocess.run(git + ["add", "-f", ".canopy/current"], check=True)
    check(sp.guard(repo) == 1, "guard: a tracked .canopy/ fails")
    check(sp.guard(ROOT) == 0, "guard: this repository tracks no database")
    r = subprocess.run([sys.executable, os.path.join(HERE, "canopy.py"), "sql", "load", m,
                        "--db", os.path.join(tmp, "via-canopy.sqlite")],
                       capture_output=True, text=True)
    check(r.returncode == 0 and "11 basic_events" in r.stdout,
          "`canopy sql load` runs the tool")

    shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print(f"\n{len(failures)} FAILED:")
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("\nsql projection: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
