#!/usr/bin/env python3
"""Extract a RiskSpectrum PSA project database into the converter's table
export (JSON), driven entirely by a mapping file.

Usage:
  extract_riskspectrum_sql.py <mapping.yaml> <out.json>
      [--dsn "Driver={ODBC Driver 18 for SQL Server};Server=...;Database=...;Trusted_Connection=yes"]
      [--dry-run]     print the SQL that would run, connect to nothing

This script contains NO knowledge of RiskSpectrum's database schema on
purpose: table and column names live in the mapping file, which the
person who administers the database fills in once (see
ci/riskspectrum-sql-mapping.example.yaml for the skeleton — every
`<...>` placeholder must be replaced, and the script refuses to run while
any remain). The output is exactly the table contract documented in
ci/import_riskspectrum.py, so the converter never depends on the schema
either; a RiskSpectrum version change touches the mapping file only.

For sites that cannot query the database, the same tables can be produced
with the RiskSpectrum PSA Macro's export and saved as CSV files named
after the tables — the converter accepts both forms.

The database is opened read-only (the connection is never written to,
and only SELECT statements are issued). Requires `pyodbc` at run time
(`pip install pyodbc`) unless --dry-run.
"""
from __future__ import annotations

import json
import re
import sys

import yaml

PLACEHOLDER = re.compile(r"<[^<>]+>")


def build_queries(mapping: dict) -> dict[str, tuple[str, list[str]]]:
    """table name -> (SQL, output column names)."""
    out = {}
    for table, spec in (mapping.get("tables") or {}).items():
        if not spec or spec.get("skip"):
            continue
        src = spec.get("from")
        cols = spec.get("columns") or {}
        if not src or not cols:
            raise SystemExit(f"mapping: table {table} needs 'from' and "
                             f"'columns'")
        select = ", ".join(f"{expr} AS [{name}]" for name, expr in cols.items())
        sql = f"SELECT {select} FROM {src}"
        if spec.get("where"):
            sql += f" WHERE {spec['where']}"
        if spec.get("order_by"):
            sql += f" ORDER BY {spec['order_by']}"
        out[table] = (sql, list(cols))
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    mapping_path, out_path = argv[0], argv[1]
    dsn, dry = None, False
    i = 2
    while i < len(argv):
        if argv[i] == "--dsn":
            i += 1
            dsn = argv[i]
        elif argv[i] == "--dry-run":
            dry = True
        else:
            print(f"unknown option {argv[i]}", file=sys.stderr)
            return 2
        i += 1

    mapping = yaml.safe_load(open(mapping_path, encoding="utf-8")) or {}
    text = open(mapping_path, encoding="utf-8").read()
    left = sorted(set(PLACEHOLDER.findall(
        "\n".join(l for l in text.splitlines() if not l.strip().startswith("#")))))
    if left and not dry:
        print("mapping still has placeholders — fill them in from the "
              "RiskSpectrum schema first:\n  " + "\n  ".join(left),
              file=sys.stderr)
        return 2
    queries = build_queries(mapping)
    if dry:
        for table, (sql, _) in queries.items():
            print(f"-- {table}\n{sql};\n")
        return 0

    dsn = dsn or mapping.get("dsn")
    if not dsn:
        print("no --dsn given and mapping has no dsn:", file=sys.stderr)
        return 2
    try:
        import pyodbc  # type: ignore
    except ImportError:
        print("pyodbc is required: pip install pyodbc", file=sys.stderr)
        return 2

    conn = pyodbc.connect(dsn, readonly=True, autocommit=True)
    export: dict[str, list[dict]] = {}
    try:
        cur = conn.cursor()
        for table, (sql, names) in queries.items():
            cur.execute(sql)
            rows = []
            for rec in cur.fetchall():
                row = {}
                for name, v in zip(names, rec):
                    if v is None:
                        v = ""
                    elif not isinstance(v, (int, float, str)):
                        v = str(v)
                    row[name] = v
                rows.append(row)
            export[table] = rows
            print(f"{table}: {len(rows)} rows", file=sys.stderr)
    finally:
        conn.close()
    if mapping.get("project"):
        export["project"] = [mapping["project"]]
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(export, f, indent=1, ensure_ascii=False)
    print(f"wrote {out_path} ({sum(len(r) for r in export.values())} rows in "
          f"{len(export)} tables)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
