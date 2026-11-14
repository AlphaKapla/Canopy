#!/usr/bin/env python3
"""SQL projection of a model (V&V FR-60): an SQLite working copy of the
YAML model, for queries and bulk edits, that writes back as canonical YAML.

Git and the YAML files stay the only source of truth. The database is a
derived, editable copy of one state of the model: `load` builds it,
`dump` writes it back through the canonical writer (ci/canonical.py,
FR-59), and what changed shows up as an ordinary git diff. It is never
committed (`guard` fails if one is tracked).

  canopy sql load [MODEL] [--db PATH]   build the database
  canopy sql dump [MODEL] [--db PATH]   write the database back as YAML
  canopy sql path                       the database `dump` would use
  python ci/sql_projection.py guard     fail if a database is tracked by git

Without --db, `load` writes .canopy/cache/<commit>/model.sqlite (commit:
HEAD, with `-dirty` when the model has uncommitted changes) and records it
in .canopy/current, which `dump` and `path` read. `.canopy/` is ignored by
git.

Tables. Each model file with one root key holding entities becomes rows:

  basic_events, parameters, house_events, ccf_groups, fault_trees —
      one row per entity of the file's root mapping, keyed by `_id`;
  event_tree — one row per event-tree file, `_id` its `id`;
  gates, functional_events, sequences — one row per entry of a mapping
      of entities inside another (`_parent` is the owner's `_id`).

  Every row has `_file` (the model file it lives in) and `_ord` (its
  position there: the order of entities is data). The other columns are
  the entity's fields, nested fields joined by `__`:
  `failure_model__rate__value`, `provenance__source`. A field that holds
  a list, a formula or a mapping whose key order is data (an event-tree
  path, house-event overrides, CCF factors) is one column of JSON text. An absent field is
  NULL. Columns come from the schema (so an empty table still has them
  all) and from the data (fields the schema leaves open).

  files      one row per model file: path, position, header comment,
             root key, and whether it is rows or a document
  documents  files that are not entity tables (model.yaml), as JSON
  comments   the comments of each file, by the path of the key they
             annotate
  columns_, tables_, meta  how columns map to fields; where the copy
             came from. Do not edit.

Exactness. `load` ends by writing every file back in memory and comparing
it with the canonical form of the file on disk: if a file cannot be
reproduced byte for byte, `load` fails and says where. So
`dump(load(model))` is the canonical model, always.

`dump` rewrites only the files whose text changed. It refuses to change a
file generated from a template (edit the template instead) or a file
edited on disk since the load (unless --force), reports comments whose
key no longer exists, and never deletes a file. All refusals come before
the first write.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import canonical  # noqa: E402

ID = re.compile(r"^(BE|GT|FT|ET|FE|IE|HE|PAR|CCF|SEQ)-[A-Z0-9][A-Z0-9-]*$")
GENERATED = "# GENERATED"
SEP = "__"
RESERVED_COLUMNS = ("_id", "_parent", "_file", "_ord")
DB_PATTERN = re.compile(r"(^|/)\.canopy/|\.(sqlite3?|db)(-journal|-wal|-shm)?$")


class ProjectionError(Exception):
    pass


# ---------------------------------------------------------------- schema
def _schema():
    with open(os.path.join(ROOT, "schema", "psa-model.schema.json")) as f:
        return json.load(f)


def _resolve(node, schema):
    while isinstance(node, dict) and "$ref" in node:
        node = schema["$defs"][node["$ref"].rsplit("/", 1)[1]]
    return node


def _object_props(node, schema):
    """Ordered properties of a schema node that is an object with declared
    properties (directly, or as the union of its oneOf branches when all of
    them are); None otherwise."""
    node = _resolve(node, schema)
    if not isinstance(node, dict):
        return None
    props = {}
    branches = [_resolve(b, schema) for b in node.get("oneOf", [])]
    if isinstance(node.get("properties"), dict):
        props.update(node["properties"])
    for b in branches:
        sub = _object_props(b, schema)
        if sub is None:
            return None if not props else props
        for k, v in sub.items():
            props.setdefault(k, v)
    return props or None


def _is_entity_map(node, schema):
    """The entity schema when `node` is a mapping keyed by IDs whose values
    are objects with properties; None otherwise."""
    node = _resolve(node, schema)
    if (isinstance(node, dict) and "properties" not in node
            and isinstance(node.get("additionalProperties"), dict)):
        return node["additionalProperties"] if _object_props(
            node["additionalProperties"], schema) else None
    return None


def schema_layout(schema=None):
    """{table: (columns, json columns)} from the schema: for each mapping of
    entities found under a file definition, the flattened scalar paths of
    its entity in schema order, and the paths stored as JSON."""
    schema = schema or _schema()
    tables = {}

    def flatten(entity, table):
        cols, jsons = tables.setdefault(table, ([], set()))

        def walk(node, path, seen):
            node_r = _resolve(node, schema)
            key = id(node_r)
            child = _is_entity_map(node, schema)
            if child is not None:
                flatten(child, path[-1])
                return
            props = _object_props(node, schema)
            if props is not None and key not in seen:
                for k, v in props.items():
                    walk(v, path + (k,), seen | {key})
                return
            scalar = isinstance(node_r, dict) and (
                node_r.get("type") in ("string", "number", "integer", "boolean")
                or "enum" in node_r or "const" in node_r) and "oneOf" not in node_r
            if path not in cols:
                cols.append(path)
            if not scalar:
                jsons.add(path)

        for k, v in (_object_props(entity, schema) or {}).items():
            walk(v, (k,), frozenset())

    for name, d in schema["$defs"].items():
        if not name.endswith("File"):
            continue
        for root_key, node in d.get("properties", {}).items():
            child = _is_entity_map(node, schema)
            if child is not None:
                flatten(child, root_key)
            elif _object_props(node, schema):
                flatten(node, root_key)
    return tables


# --------------------------------------------------------------- flatten
def _is_entities(v) -> bool:
    return (isinstance(v, dict) and bool(v) and all(isinstance(k, str) and ID.match(k)
                                                    for k in v)
            and all(isinstance(x, dict) for x in v.values()))


def _keyed_by_ids(v) -> bool:
    return isinstance(v, dict) and (not v or all(isinstance(k, str) and ID.match(k)
                                                 for k in v))


class _Tables:
    """Rows gathered from the model before the SQL tables are created."""

    def __init__(self):
        self.layout = schema_layout()
        self.rows = {}        # table -> list of (fixed fields, {path: (kind, value)})
        self.meta = {}        # table -> (parent table, path of the mapping in the parent)

    def add(self, table, parent, where, fixed, obj):
        if table in self.meta and self.meta[table] != (parent, where):
            raise ProjectionError(f"two different mappings are named {table!r}; the "
                                  "projection needs one table per name")
        self.meta[table] = (parent, where)
        jsons = self.layout.get(table, ([], set()))[1]
        cells = {}

        def walk(v, path):
            if _is_entities(v) and path not in jsons:
                for n, (cid, cobj) in enumerate(v.items()):
                    self.add(path[-1], table, list(path),
                             {"_id": cid, "_parent": fixed["_id"], "_file": fixed["_file"],
                              "_ord": n}, cobj)
            elif (path in jsons or isinstance(v, list)
                  or (isinstance(v, dict) and canonical.object_order(v.keys()) is None)):
                # a list, a formula, or a mapping whose key order is data
                cells[path] = ("json", json.dumps(v, ensure_ascii=False))
            elif isinstance(v, bool):
                cells[path] = ("bool", int(v))
            elif v is None:
                raise ProjectionError(f"{fixed['_file']}: {fixed['_id']}: explicit null at "
                                      f"{'.'.join(path)} cannot be told from an absent field")
            elif isinstance(v, (str, int, float)):
                cells[path] = ("scalar", v)
            else:
                for k, x in canonical.ordered(v):
                    walk(x, path + (k,))

        if obj and canonical.object_order(obj.keys()) is None:
            raise ProjectionError(
                f"{fixed['_file']}: {fixed['_id']}: its fields ({', '.join(obj)}) are not "
                "those of one known object, so their order cannot be rebuilt from columns")
        for k, x in canonical.ordered(obj):
            walk(x, (k,))
        self.rows.setdefault(table, []).append((fixed, cells))


def _column_name(path, taken) -> str:
    name = SEP.join(re.sub(r"[^A-Za-z0-9_]", "_", p) for p in path)
    if name in RESERVED_COLUMNS or name in taken or not name:
        n = 2
        while f"{name}_{n}" in taken:
            n += 1
        name = f"{name}_{n}"
    return name


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# ------------------------------------------------------------------ load
def load(model_dir: str, db_path: str, commit: str = "") -> dict:
    """Build the database at `db_path` from the model; returns counts."""
    if not os.path.exists(os.path.join(model_dir, "model.yaml")):
        raise ProjectionError(f"{model_dir}: no model.yaml")
    files = []
    t = _Tables()
    documents, comments = [], []
    for n, path in enumerate(canonical.model_files(model_dir)):
        rel = os.path.relpath(path, model_dir).replace(os.sep, "/")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        try:
            header, data, cs = canonical.loads(text)
            canon = canonical.dump(data, header, cs)
            if canon != text:       # not canonical on disk: the checked rewrite
                canon = canonical.canonical_text(text)
        except canonical.CanonError as e:
            raise ProjectionError(f"{rel}: {e}")
        root = list(data)[0] if isinstance(data, dict) and len(data) == 1 else None
        value = data[root] if root is not None else None
        if root is not None and _is_entities(value):
            kind = "rows"
            for k, (eid, obj) in enumerate(value.items()):
                t.add(root, None, None, {"_id": eid, "_parent": None, "_file": rel,
                                         "_ord": k}, obj)
        elif (root is not None and isinstance(value, dict) and value
              and (root in t.layout or not _keyed_by_ids(value))
              and isinstance(value.get("id"), str)):
            kind = "row"
            t.add(root, None, None, {"_id": value["id"], "_parent": None, "_file": rel,
                                     "_ord": 0}, value)
        elif root is not None and value == {} and root in t.layout:
            kind = "rows"                      # an empty collection
            t.meta.setdefault(root, (None, None))
        else:
            kind, root = "document", None
            documents.append((rel, json.dumps(data, ensure_ascii=False)))
        files.append((rel, n, "\n".join(header), root, kind, canon))
        for p, texts in cs.before.items():
            for k, c in enumerate(texts):
                comments.append((rel, json.dumps(list(p)), "before", k, c))
        for p, c in cs.after.items():
            comments.append((rel, json.dumps(list(p)), "after", 0, c))
        for k, c in enumerate(cs.tail):
            comments.append((rel, "[]", "tail", k, c))

    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    for suffix in ("", "-journal", "-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            os.remove(db_path + suffix)
    db = sqlite3.connect(db_path)
    try:
        db.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE files (path TEXT PRIMARY KEY, ord INTEGER NOT NULL, header TEXT NOT NULL,
                                root_key TEXT, kind TEXT NOT NULL
                                CHECK (kind IN ('rows', 'row', 'document')),
                                loaded TEXT);
            CREATE TABLE documents (file TEXT PRIMARY KEY REFERENCES files(path), json TEXT NOT NULL);
            CREATE TABLE comments (file TEXT NOT NULL REFERENCES files(path), path TEXT NOT NULL,
                                   kind TEXT NOT NULL CHECK (kind IN ('before', 'after', 'tail')),
                                   n INTEGER NOT NULL, text TEXT NOT NULL,
                                   PRIMARY KEY (file, path, kind, n));
            CREATE TABLE tables_ (name TEXT PRIMARY KEY, parent TEXT, path TEXT);
            CREATE TABLE columns_ (table_ TEXT NOT NULL, column_ TEXT NOT NULL, path TEXT NOT NULL,
                                   kind TEXT NOT NULL, PRIMARY KEY (table_, column_));
        """)
        db.executemany("INSERT INTO meta VALUES (?, ?)",
                       [("format", "1"), ("commit", commit),
                        ("model", os.path.abspath(model_dir))])
        db.executemany("INSERT INTO files VALUES (?, ?, ?, ?, ?, ?)",
                       [f[:5] + (_digest(f[5]),) for f in files])
        db.executemany("INSERT INTO documents VALUES (?, ?)", documents)
        db.executemany("INSERT INTO comments VALUES (?, ?, ?, ?, ?)", comments)
        for table in list(t.layout) + [x for x in t.meta if x not in t.layout]:
            if table not in t.meta:
                continue                        # a schema table this model has no file for
            parent, where = t.meta[table]
            rows = t.rows.get(table, [])
            paths = list(t.layout.get(table, ([], set()))[0])
            kinds = {p: "json" for p in t.layout.get(table, ([], set()))[1]}
            for _, cells in rows:       # fields the schema does not declare, as met
                paths += [p for p in cells if p not in paths]
            for _, cells in rows:
                for p, (kind, _) in cells.items():
                    if kinds.setdefault(p, kind) != kind:
                        raise ProjectionError(
                            f"{table}: field {'.'.join(p)} holds {kinds[p]} values in some "
                            f"entities and {kind} values in others")
            # a path is a column only if nothing lies below it in the data
            names, taken = {}, set()
            for p in paths:
                names[p] = _column_name(p, taken)
                taken.add(names[p])
            cols = ", ".join(_q(names[p]) for p in paths)
            db.execute(f"CREATE TABLE {_q(table)} (_id TEXT NOT NULL, _parent TEXT, "
                       f"_file TEXT NOT NULL REFERENCES files(path), _ord INTEGER NOT NULL"
                       + (", " + cols if cols else "")
                       + ", PRIMARY KEY (_id, _parent))")
            db.execute("INSERT INTO tables_ VALUES (?, ?, ?)",
                       (table, parent, json.dumps(where) if where is not None else None))
            db.executemany("INSERT INTO columns_ VALUES (?, ?, ?, ?)",
                           [(table, names[p], json.dumps(list(p)), kinds.get(p, "scalar"))
                            for p in paths])
            marks = ", ".join("?" for _ in range(4 + len(paths)))
            db.executemany(
                f"INSERT INTO {_q(table)} VALUES ({marks})",
                [(fx["_id"], fx["_parent"], fx["_file"], fx["_ord"],
                  *[cells[p][1] if p in cells else None for p in paths])
                 for fx, cells in rows])
        db.commit()
        # exactness: every file comes back as its canonical form
        texts, _ = render(db)
        for rel, _, _, _, _, canon in files:
            if texts.get(rel) != canon:
                raise ProjectionError(
                    f"{rel} cannot be projected exactly: "
                    + canonical.first_difference(canon, texts.get(rel, "")))
    except Exception:
        db.close()
        os.remove(db_path)
        raise
    counts = {name: db.execute(f"SELECT COUNT(*) FROM {_q(name)}").fetchone()[0]
              for (name,) in db.execute("SELECT name FROM tables_ ORDER BY name")}
    db.close()
    return counts


# ------------------------------------------------------------------ dump
def _rows(db, table):
    cols = db.execute("SELECT column_, path, kind FROM columns_ WHERE table_ = ? "
                      "ORDER BY rowid", (table,)).fetchall()
    known = {c for c, _, _ in cols}
    actual = [r[1] for r in db.execute(f"PRAGMA table_info({_q(table)})")]
    extra = [c for c in actual if c not in known and c not in RESERVED_COLUMNS]
    if extra:
        raise ProjectionError(f"table {table}: column(s) {', '.join(extra)} were added in "
                              "SQL; the projection does not know which field they are")
    select = ", ".join(["_id", "_parent", "_file", "_ord"] + [_q(c) for c, _, _ in cols])
    out = []
    for r in db.execute(f"SELECT {select} FROM {_q(table)} ORDER BY _file, _parent, _ord, _id"):
        obj = {}
        for (cname, path, kind), v in zip(cols, r[4:]):
            if v is None:
                continue
            if kind == "json":
                try:
                    v = json.loads(v)
                except (TypeError, ValueError):
                    raise ProjectionError(f"{table}.{cname} of {r[0]}: not JSON: {v!r}")
            elif kind == "bool":
                if v not in (0, 1, "true", "false"):
                    raise ProjectionError(f"{table}.{cname} of {r[0]}: {v!r} is not a "
                                          "boolean (use 1 or 0)")
                v = v in (1, "true")
            elif isinstance(v, bytes):
                raise ProjectionError(f"{table}.{cname} of {r[0]}: a blob is not model data")
            cur = obj
            parts = json.loads(path)
            for p in parts[:-1]:
                cur = cur.setdefault(p, {})
                if not isinstance(cur, dict):
                    raise ProjectionError(f"{table} {r[0]}: {'.'.join(parts)} lies under a "
                                          "field that holds a value")
            if isinstance(cur.get(parts[-1]), dict):
                raise ProjectionError(f"{table} {r[0]}: {'.'.join(parts)} holds a value and "
                                      "has fields below it")
            cur[parts[-1]] = v
        out.append((r[0], r[1], r[2], obj))
    return out


def _has_path(doc, path) -> bool:
    for p in path:
        if isinstance(doc, dict) and p in doc:
            doc = doc[p]
        elif isinstance(doc, list) and isinstance(p, int) and p < len(doc):
            doc = doc[p]
        else:
            return False
    return True


def render(db):
    """({file: canonical text}, [messages]) of the model in the database."""
    tables = db.execute("SELECT name, parent, path FROM tables_").fetchall()
    by_parent = {}
    for name, parent, path in tables:
        if parent is not None:
            by_parent.setdefault(parent, []).append((name, json.loads(path)))
    data = {name: _rows(db, name) for name, _, _ in tables}
    known_files = {p for (p,) in db.execute("SELECT path FROM files")}
    for name, rows in data.items():
        for rid, _, rfile, _ in rows:
            if rfile not in known_files:
                raise ProjectionError(f"{name} {rid}: _file {rfile!r} is not in the files "
                                      "table (add the file there first)")

    def attach(table):
        for child, where in by_parent.get(table, []):
            attach(child)
            owners = {}
            for rid, rparent, rfile, obj in data[child]:
                owners.setdefault((rparent, rfile), {})[rid] = obj
            placed = set()
            for rid, _, rfile, obj in data[table]:
                kids = owners.get((rid, rfile))
                if kids is None:
                    continue
                placed.add((rid, rfile))
                cur = obj
                for p in where[:-1]:
                    cur = cur.setdefault(p, {})
                cur[where[-1]] = kids
            orphans = sorted(set(owners) - placed)
            if orphans:
                raise ProjectionError(f"{child}: _parent {orphans[0][0]!r} in {orphans[0][1]} "
                                      f"is not a row of {table} in that file")

    for name, parent, _ in tables:
        if parent is None:
            attach(name)
    texts, notes = {}, []
    for path, header, root, kind in db.execute(
            "SELECT path, header, root_key, kind FROM files ORDER BY ord, path"):
        if kind == "document":
            row = db.execute("SELECT json FROM documents WHERE file = ?", (path,)).fetchone()
            if row is None:
                raise ProjectionError(f"{path}: no row in documents")
            doc = json.loads(row[0])
        else:
            if root not in data:
                rows = []
            else:
                rows = [(rid, obj) for rid, _, rfile, obj in data[root] if rfile == path]
            if kind == "row":
                if len(rows) != 1:
                    raise ProjectionError(f"{path}: expected one {root} row, found {len(rows)}")
                doc = {root: rows[0][1]}
            else:
                doc = {root: dict(rows)}
                if len(doc[root]) != len(rows):
                    raise ProjectionError(f"{path}: two {root} rows with the same _id")
        cs = canonical.Comments()
        for cpath, ckind, text in db.execute(
                "SELECT path, kind, text FROM comments WHERE file = ? ORDER BY path, kind, n",
                (path,)):
            p = tuple(json.loads(cpath))
            if ckind == "before":
                cs.before.setdefault(p, []).append(text)
            elif ckind == "after":
                cs.after[p] = text
            else:
                cs.tail.append(text)
        try:
            text = canonical.dump(doc, header.split("\n") if header else [], cs)
        except canonical.CanonError as e:
            raise ProjectionError(f"{path}: {e}")
        lost = sum(len(v) for p, v in cs.before.items() if not _has_path(doc, p)) \
            + sum(1 for p in cs.after if not _has_path(doc, p))
        if lost:
            notes.append(f"{path}: {lost} comment(s) dropped: the key they annotated is gone")
        texts[path] = text
    return texts, notes


def dump(db_path: str, model_dir: str, force: bool = False) -> list:
    """Write the database back into `model_dir`; returns the files changed.
    A file edited on disk since `load` is not overwritten unless `force`."""
    if not os.path.exists(db_path):
        raise ProjectionError(f"{db_path}: no such database (run `canopy sql load` first)")
    db = sqlite3.connect(db_path)
    try:
        texts, notes = render(db)
        loaded = dict(db.execute("SELECT path, loaded FROM files"))
    finally:
        db.close()
    changed = []
    for rel, text in texts.items():
        path = os.path.join(model_dir, rel)
        old = None
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                old = f.read()
        if old == text:
            continue
        if old is not None and old.startswith(GENERATED):
            try:
                same = canonical.canonical_text(old) == text
            except canonical.CanonError:
                same = False
            if not same:
                raise ProjectionError(
                    f"{rel} is generated from a template: change the template and run "
                    "`canopy expand`, not the database")
        if old is not None and not force and loaded.get(rel) is not None:
            try:
                now = _digest(canonical.canonical_text(old))
            except canonical.CanonError:
                now = None
            if now != loaded[rel]:
                raise ProjectionError(
                    f"{rel} changed on disk since the database was loaded; writing it "
                    "would discard that change (load again, or pass --force)")
        changed.append(rel)
    for rel in changed:
        path = os.path.join(model_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(texts[rel])
    for n in notes:
        print(f"sql: note: {n}", file=sys.stderr)
    return changed


# ------------------------------------------------------------------- cli
def _git(*args, cwd=ROOT):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def default_db(model_dir: str):
    """(path for a new database, commit label)."""
    top = _git("rev-parse", "--show-toplevel", cwd=model_dir) or ROOT
    head = _git("rev-parse", "HEAD", cwd=model_dir)
    label = head or "no-commit"
    if head and _git("status", "--porcelain", "--", os.path.abspath(model_dir), cwd=model_dir):
        label += "-dirty"
    return os.path.join(top, ".canopy", "cache", label, "model.sqlite"), label


def _current(model_dir: str) -> str:
    top = _git("rev-parse", "--show-toplevel", cwd=model_dir) or ROOT
    return os.path.join(top, ".canopy", "current")


def guard(root: str = ROOT) -> int:
    tracked = (_git("ls-files", cwd=root) or "").split("\n")
    bad = [f for f in tracked if f and DB_PATTERN.search(f)]
    if bad:
        print("sql: a database is tracked by git (derived artifacts are never committed): "
              + ", ".join(bad), file=sys.stderr)
        return 1
    print("sql: no database tracked by git")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("load", "dump"):
        p = sub.add_parser(name)
        p.add_argument("model", nargs="?", default=os.path.join(ROOT, "model"))
        p.add_argument("--db")
        if name == "dump":
            p.add_argument("--force", action="store_true",
                           help="overwrite files edited on disk since the load")
    p = sub.add_parser("path")
    p.add_argument("model", nargs="?", default=os.path.join(ROOT, "model"))
    sub.add_parser("guard")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "guard":
            return guard()
        if a.cmd == "path":
            cur = _current(a.model)
            if not os.path.exists(cur):
                print("sql: no database loaded (run `canopy sql load`)", file=sys.stderr)
                return 1
            print(open(cur).read().strip())
            return 0
        if a.cmd == "load":
            db, label = (a.db, "") if a.db else default_db(a.model)
            counts = load(a.model, db, label)
            if not a.db:
                with open(_current(a.model), "w") as f:
                    f.write(db + "\n")
            print(f"sql: loaded {a.model} into {db}")
            print("sql: " + ", ".join(f"{n} {k}" for k, n in counts.items()))
            return 0
        db = a.db
        if not db:
            cur = _current(a.model)
            if not os.path.exists(cur):
                print("sql: no database loaded (run `canopy sql load`, or pass --db)",
                      file=sys.stderr)
                return 1
            db = open(cur).read().strip()
        changed = dump(db, a.model, a.force)
        print(f"sql: wrote {len(changed)} file(s) from {db}"
              + (": " + ", ".join(changed) if changed else " (the model already matches)"))
        return 0
    except (ProjectionError, sqlite3.Error) as e:
        print(f"sql: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
