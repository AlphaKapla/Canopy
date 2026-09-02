#!/usr/bin/env python3
"""Cross-check a converted Canopy model against RiskSpectrum's own results.

Usage:
  crosscheck_rs.py <model-dir> <rs-results-dir-or-json>
      [--results head.json]     reuse ci/quantify.py output for event trees
      [--engine PATH]           default engine/target/release/canopy
      [--tol 1e-3]              relative tolerance on probabilities/frequencies
      [--top N]                 compare the N highest cut sets per scope (20)
      [--prob-only]             skip cut sets (large trees)
      [--json]                  machine-readable report on stdout

RiskSpectrum is the oracle for its own model: export its results at the
lowest cut-off it will accept (and with BDD quantification if licensed),
then this tool quantifies the converted model exactly and compares by
record ID. IDs are matched through the `external_ids: {riskspectrum: …}`
fields the converter writes, so nothing here depends on how the Canopy
IDs were spelled.

Result tables (CSV directory or one JSON object, same convention as the
converter's input; column names case-insensitive):

  fault_tree_results   id, q                 id = RiskSpectrum FT (or top
                                             gate) id; q = top probability
  sequence_results     event_tree, sequence, frequency
  cut_sets             scope, rank, value, events
                       scope = FT id or "ET/SEQ"; events = RS ids joined
                       by ';' (CCF combination events included as
                       RiskSpectrum names them)
  event_map            riskspectrum, canopy   optional extra ID mapping
                       (e.g. RiskSpectrum CCF event names -> Canopy
                       BE-<GROUP>-<idxs>) for anything external_ids
                       cannot resolve

Interpretation: RiskSpectrum's minimal-cut-set quantification with a
cut-off truncates, so Canopy's exact value is normally >= RiskSpectrum's;
a Canopy value *below* RiskSpectrum's beyond tolerance points at a
conversion defect (a lost input, a wrong reliability model) rather than
at truncation. Both directions are reported with their sign.

Exit 1 when any comparison exceeds the tolerance, a referenced record
cannot be matched, or an RS cut set within the top N is absent from the
converted model; that is the "cross-check green" gate for a migration.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from import_riskspectrum import as_float, col, load_tables  # noqa: E402

RESULT_TABLES = ["fault_tree_results", "sequence_results", "cut_sets",
                 "event_map"]


def load_results(path: str) -> dict[str, list[dict]]:
    # reuse the converter's loader; it only keeps known table names, so
    # patch its table list for this call.
    import import_riskspectrum as m
    saved = m.TABLES
    m.TABLES = RESULT_TABLES
    try:
        return load_tables(path)
    finally:
        m.TABLES = saved


def rs_id_map(model_dir: str) -> dict[str, dict[str, str]]:
    """RiskSpectrum id -> Canopy id, per kind, from external_ids."""
    maps: dict[str, dict[str, str]] = {"BE": {}, "FT": {}, "GT": {},
                                       "ET": {}, "SEQ": {}, "HE": {}}

    def ext(entity: dict) -> str | None:
        e = (entity or {}).get("external_ids") or {}
        return e.get("riskspectrum")

    for p in glob.glob(os.path.join(model_dir, "basic-events", "*.yaml")):
        for bid, be in (yaml.safe_load(open(p)) or {}).get(
                "basic_events", {}).items():
            if ext(be):
                maps["BE"][ext(be)] = bid
    for p in glob.glob(os.path.join(model_dir, "fault-trees", "*.yaml")):
        for fid, ft in (yaml.safe_load(open(p)) or {}).get(
                "fault_trees", {}).items():
            if ext(ft):
                maps["FT"][ext(ft)] = fid
            for gid, g in ft.get("gates", {}).items():
                if ext(g):
                    maps["GT"][ext(g)] = gid
                    # an RS top-gate id also names its fault tree
                    if gid == ft.get("top_gate"):
                        maps["FT"].setdefault(ext(g), fid)
    for p in glob.glob(os.path.join(model_dir, "event-trees", "*.yaml")):
        et = (yaml.safe_load(open(p)) or {}).get("event_tree", {})
        if ext(et):
            maps["ET"][ext(et)] = et["id"]
            for sid, s in et.get("sequences", {}).items():
                if ext(s):
                    maps["SEQ"][f"{ext(et)}/{ext(s)}"] = (et["id"], sid)
    hp = os.path.join(model_dir, "house-events.yaml")
    if os.path.exists(hp):
        for hid, h in (yaml.safe_load(open(hp)) or {}).get(
                "house_events", {}).items():
            if ext(h):
                maps["HE"][ext(h)] = hid
    return maps


def engine_json(engine: str, model_dir: str, target: str,
                prob_only: bool) -> dict:
    cmd = [engine, model_dir, target, "--json"]
    if prob_only:
        cmd.append("--prob-only")
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"engine failed on {target}:\n{p.stderr}")
    return json.loads(p.stdout)


def rel(a: float, b: float) -> float:
    return (a - b) / b if b else (0.0 if a == 0 else float("inf"))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    model_dir, rs_path = argv[0], argv[1]
    engine = os.environ.get("CANOPY_BIN", "engine/target/release/canopy")
    results_path, tol, top, prob_only, as_json = None, 1e-3, 20, False, False
    i = 2
    while i < len(argv):
        a = argv[i]
        if a == "--results":
            i += 1
            results_path = argv[i]
        elif a == "--engine":
            i += 1
            engine = argv[i]
        elif a == "--tol":
            i += 1
            tol = float(argv[i])
        elif a == "--top":
            i += 1
            top = int(argv[i])
        elif a == "--prob-only":
            prob_only = True
        elif a == "--json":
            as_json = True
        else:
            print(f"unknown option {a}", file=sys.stderr)
            return 2
        i += 1

    rs = load_results(rs_path)
    maps = rs_id_map(model_dir)
    for row in rs["event_map"]:
        maps["BE"][col(row, "riskspectrum")] = col(row, "canopy")

    report = {"fault_trees": [], "sequences": [], "cut_sets": [],
              "unmatched": [], "tolerance": tol}
    fails = 0

    # ---- fault trees -------------------------------------------------------
    ft_json: dict[str, dict] = {}
    for row in rs["fault_tree_results"]:
        rid, q = col(row, "id"), as_float(row.get("q"))
        if q is None:
            report["unmatched"].append(f"fault_tree_results {rid}: q missing")
            fails += 1
            continue
        fid = maps["FT"].get(rid)
        if fid is None:
            report["unmatched"].append(f"fault tree {rid}: no converted "
                                       f"fault tree carries this "
                                       f"riskspectrum id")
            fails += 1
            continue
        if fid not in ft_json:
            ft_json[fid] = engine_json(engine, model_dir, fid, prob_only)
        p = ft_json[fid]["probability"]
        d = rel(p, q)
        ok = abs(d) <= tol
        fails += 0 if ok else 1
        report["fault_trees"].append({"riskspectrum": rid, "canopy": fid,
                                      "rs": q, "canopy_value": p,
                                      "rel_delta": d, "ok": ok})

    # ---- sequences ---------------------------------------------------------
    et_json: dict[str, dict] = {}
    if results_path:
        et_json = json.load(open(results_path))
    for row in rs["sequence_results"]:
        et, seq = col(row, "event_tree", "et"), col(row, "sequence", "seq")
        f = as_float(row.get("frequency"))
        key = f"{et}/{seq}"
        if key not in maps["SEQ"] or f is None:
            report["unmatched"].append(f"sequence {key}: not in converted "
                                       f"model or frequency missing")
            fails += 1
            continue
        etid, sid = maps["SEQ"][key]
        if etid not in et_json:
            et_json[etid] = engine_json(engine, model_dir, etid, prob_only)
        sj = {s["id"]: s for s in et_json[etid]["sequences"]}.get(sid)
        if sj is None:
            report["unmatched"].append(f"sequence {key}: {sid} not quantified")
            fails += 1
            continue
        c = sj["frequency_per_year"]
        d = rel(c, f)
        ok = abs(d) <= tol
        fails += 0 if ok else 1
        report["sequences"].append({"riskspectrum": key,
                                    "canopy": f"{etid}/{sid}", "rs": f,
                                    "canopy_value": c, "rel_delta": d,
                                    "ok": ok})

    # ---- cut sets ----------------------------------------------------------
    if rs["cut_sets"] and not prob_only:
        by_scope: dict[str, list[tuple[float, frozenset]]] = {}
        unmapped: set[str] = set()
        for row in rs["cut_sets"]:
            scope = col(row, "scope")
            v = as_float(row.get("value")) or 0.0
            evs = [e.strip() for e in col(row, "events").split(";")
                   if e.strip()]
            mapped = []
            for e in evs:
                if e in maps["BE"]:
                    mapped.append(maps["BE"][e])
                elif e in maps["HE"]:
                    mapped.append(maps["HE"][e])
                else:
                    unmapped.add(e)
                    mapped.append("?" + e)
            by_scope.setdefault(scope, []).append((v, frozenset(mapped)))
        for e in sorted(unmapped):
            report["unmatched"].append(f"cut-set event {e}: no Canopy id "
                                       f"(add it to event_map)")
        fails += len(unmapped)
        for scope, rows in by_scope.items():
            rows.sort(key=lambda x: -x[0])
            want = [s for _, s in rows[:top]]
            if "/" in scope:
                key = maps["SEQ"].get(scope)
                if key is None:
                    report["unmatched"].append(f"cut_sets scope {scope}: "
                                               f"unknown sequence")
                    fails += 1
                    continue
                etid, sid = key
                if etid not in et_json:
                    et_json[etid] = engine_json(engine, model_dir, etid,
                                                False)
                sj = {s["id"]: s for s in et_json[etid]["sequences"]}[sid]
                have = [frozenset(c["events"]) for c in sj.get("cut_sets", [])]
            else:
                fid = maps["FT"].get(scope)
                if fid is None:
                    report["unmatched"].append(f"cut_sets scope {scope}: "
                                               f"unknown fault tree")
                    fails += 1
                    continue
                if fid not in ft_json:
                    ft_json[fid] = engine_json(engine, model_dir, fid, False)
                have = [frozenset(c["events"])
                        for c in ft_json[fid]["minimal_cut_sets"]]
            have_set = set(have)
            missing = [sorted(s) for s in want if s not in have_set]
            fails += len(missing)
            report["cut_sets"].append({"scope": scope, "compared": len(want),
                                       "canopy_total": len(have),
                                       "rs_missing_in_canopy": missing,
                                       "ok": not missing})

    report["fail_count"] = fails
    if as_json:
        json.dump(report, sys.stdout, indent=2)
        print()
        return 1 if fails else 0

    print(f"# RiskSpectrum cross-check — {model_dir}\n")
    print(f"tolerance {tol:g} relative; cut sets compared: top {top} per "
          f"scope\n")
    if report["fault_trees"]:
        print("| fault tree | RiskSpectrum | Canopy (exact) | Δ rel | |")
        print("|---|---:|---:|---:|---|")
        for r in report["fault_trees"]:
            print(f"| {r['riskspectrum']} → {r['canopy']} | {r['rs']:.4e} | "
                  f"{r['canopy_value']:.4e} | {r['rel_delta']:+.2e} | "
                  f"{'ok' if r['ok'] else 'DIFF'} |")
        print()
    if report["sequences"]:
        print("| sequence | RiskSpectrum /yr | Canopy /yr | Δ rel | |")
        print("|---|---:|---:|---:|---|")
        for r in report["sequences"]:
            print(f"| {r['riskspectrum']} → {r['canopy']} | {r['rs']:.4e} | "
                  f"{r['canopy_value']:.4e} | {r['rel_delta']:+.2e} | "
                  f"{'ok' if r['ok'] else 'DIFF'} |")
        print()
    for r in report["cut_sets"]:
        state = "ok" if r["ok"] else f"{len(r['rs_missing_in_canopy'])} " \
                                     f"RiskSpectrum cut set(s) absent"
        print(f"- cut sets {r['scope']}: top {r['compared']} vs Canopy's "
              f"{r['canopy_total']} — {state}")
        for m in r["rs_missing_in_canopy"]:
            print(f"    missing: {{{', '.join(m)}}}")
    for u in report["unmatched"]:
        print(f"- UNMATCHED: {u}")
    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} finding(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
