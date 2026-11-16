#!/usr/bin/env python3
"""Checks on the reference model (FR-66), reference/README.md.

The reference model is a fixture, so what is checked here are properties
that must hold whatever its numbers are -- no expected value is stored in
this file, because a stored number would be a derived artifact and would
have to be updated by hand on every data change.

  * it validates with no error and no warning; its files are in canonical
    form; its generated basic-event files match their templates;
  * it quantifies exactly: every event tree's sequence probabilities sum
    to one, no sequence frequency exceeds its initiating-event frequency,
    and every metric total is the sum of its end states' frequencies over
    the trees, computed here independently of ci/bounds.py;
  * the bypass metric is a subset of the core-damage metric;
  * the loss-of-offsite-power variants mirror the trees they duplicate:
    every -LOOP gate's formula is its original's formula with each gate
    that has a variant replaced by that variant -- except the two bus
    gates, where the variant is the original with the grid term dropped,
    which is the whole point of the duplication. The loss-of-offsite-power
    tree uses a -LOOP top wherever one exists, and no other tree uses one;
  * the model is self-contained: every parameter is referenced, every
    basic event is used by a gate, every common-cause member exists, no
    provenance is a placeholder;
  * it round-trips through MEF (with the common-cause groups expanded, the
    only comparable form across the convention boundary) to 1e-12 on every
    sequence probability, and through the SQL working copy byte for byte;
  * each named configuration quantifies and none of them lowers the core
    damage frequency below the base case (a train taken out of service or
    a failure rate doubled cannot reduce risk);
  * a Monte Carlo run accepts the model and brackets the point value
    between the 5th and 95th percentiles;
  * the viewer builds.

Usage: python ci/test_reference_model.py        (needs the engine built)
"""
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import canonical as cn  # noqa: E402

REF = os.path.join(ROOT, "reference")
TEMPLATES = os.path.join(ROOT, "reference-templates")
SCHEMA = os.path.join(ROOT, "schema", "psa-model.schema.json")
ENGINE = os.environ.get("CANOPY_BIN") or os.path.join(
    ROOT, "engine", "target", "release", "canopy")


def run(*args, **kw):
    return subprocess.run([sys.executable, *[str(a) for a in args]],
                          capture_output=True, text=True, **kw)


def load(rel):
    with open(os.path.join(REF, rel), encoding="utf-8") as f:
        return cn.loads(f.read())[1]


def model_docs():
    out = {}
    for path in cn.model_files(REF):
        rel = os.path.relpath(path, REF).replace(os.sep, "/")
        out[rel] = load(rel)
    return out


def gates(docs):
    out = {}
    for rel, doc in docs.items():
        for ft in (doc.get("fault_trees") or {}).values():
            out.update(ft["gates"])
    return out


def operands(formula):
    """Every identifier a formula names, in order."""
    if isinstance(formula, str):
        return [formula]
    out = []
    for k, v in formula.items():
        if k == "atleast":
            out += [x for o in v["of"] for x in operands(o)]
        else:
            for o in v:
                out += operands(o)
    return out


def substitute(formula, has_variant):
    """The formula with every gate that has a -LOOP variant replaced by it."""
    if isinstance(formula, str):
        return formula + "-LOOP" if has_variant(formula) else formula
    out = {}
    for k, v in formula.items():
        if k == "atleast":
            out[k] = {"k": v["k"], "of": [substitute(o, has_variant) for o in v["of"]]}
        else:
            out[k] = [substitute(o, has_variant) for o in v]
    return out


def main() -> int:
    failures = []
    tmp = tempfile.mkdtemp(prefix="canopy-ref-")

    def check(cond, msg):
        (print(f"  ok  {msg}") if cond else failures.append(msg))

    if not os.path.exists(ENGINE):
        print(f"test_reference_model: engine not built ({ENGINE})", file=sys.stderr)
        return 1

    # --- the model as written
    r = run(os.path.join(HERE, "validate.py"), REF, SCHEMA)
    check(r.returncode == 0 and "0 error(s), 0 warning(s)" in r.stdout,
          f"validates with no error and no warning ({r.stdout.strip().splitlines()[-1]
                                                     if r.stdout else r.stderr[:60]})")
    r = run(os.path.join(HERE, "canonical.py"), REF, "--check")
    if r.returncode == 2:                      # no --check on canonical.py directly
        r = run(os.path.join(HERE, "canopy.py"), "canon", REF)
    check(r.returncode == 0, "every model file is in canonical form")
    r = run(os.path.join(HERE, "expand_templates.py"), "--templates", TEMPLATES,
            "--model", REF, "--check")
    check(r.returncode == 0, "every generated basic-event file matches its template")

    docs = model_docs()
    gs = gates(docs)
    manifest = docs["model.yaml"]
    metrics = {m["id"]: m for m in manifest["model"]["risk_metrics"]}
    check(len(gs) == 73 and len(metrics) == 2,
          f"{len(gs)} gates, {len(metrics)} risk metrics")

    # --- quantification
    results = os.path.join(tmp, "ref.json")
    r = run(os.path.join(HERE, "quantify.py"), REF, results, "--engine", ENGINE)
    if r.returncode != 0:
        print("quantify.py failed:\n" + r.stdout + r.stderr, file=sys.stderr)
        return 1
    with open(results) as f:
        res = json.load(f)
    check(len(res) == 7 and all(r["method"] == "exact" if "method" in r else True
                                for r in res.values()),
          f"{len(res)} event trees quantified exactly (the eighth is transfer-only)")
    bad = [f"{t}: {r['partition']['sum_probability']!r}" for t, r in res.items()
           if abs(r["partition"]["sum_probability"] - 1.0) > 1e-9]
    check(not bad, "every tree's sequence probabilities sum to one"
          + (": " + ", ".join(bad) if bad else ""))
    over = [f"{t}/{s['id']}" for t, r in res.items() for s in r["sequences"]
            if not 0.0 <= s["frequency_per_year"]
            <= r["initiating_event"]["frequency_per_year"] * (1 + 1e-12)]
    check(not over, "no sequence frequency is negative or above its initiating-event "
                    "frequency" + (": " + ", ".join(over[:3]) if over else ""))

    # metric totals, summed here from the sequences rather than read from a tool
    def total(metric):
        want = set(metrics[metric]["end_states"])
        return math.fsum(s["frequency_per_year"] for r in res.values()
                         for s in r["sequences"]
                         if s.get("end_state") in want and not s.get("transfer"))
    tools = {}
    for t, r in res.items():
        for m in r["metrics"]:
            tools[m["id"]] = tools.get(m["id"], 0.0) + m["value_per_year"]
    for m in metrics:
        a, b = total(m), tools[m]
        check(abs(a - b) <= 1e-12 * max(abs(a), abs(b)),
              f"{m} total {b:.4e} /yr is the sum of its end states over the trees "
              f"({a:.4e} /yr)")
    check(0 < tools["CDF-BYPASS"] < tools["CDF"],
          f"the bypass metric ({tools['CDF-BYPASS']:.2e} /yr) is a non-empty part of "
          f"core damage ({tools['CDF']:.2e} /yr)")

    # --- the loss-of-offsite-power variants mirror their originals
    variants = {g[:-len("-LOOP")]: g for g in gs if g.endswith("-LOOP")}
    has = lambda x: x in variants  # noqa: E731
    check(len(variants) == 27, f"{len(variants)} gates have a -LOOP variant")
    drift = []
    for base, var in sorted(variants.items()):
        want = substitute(gs[base]["formula"], has)
        if base in ("GT-ACP-A", "GT-ACP-B"):
            # the point of the exercise: the grid term goes, the diesel stays
            edg = "GT-EDG-" + base[-1]
            want = {"or": [f"BE-ACP-BUS-{base[-1]}-FAIL", edg]}
        if gs[var]["formula"] != want:
            drift.append(base)
    check(not drift, "every -LOOP gate is its original with the variants substituted"
          + (f"; drifted: {', '.join(drift)}" if drift else ""))
    check(gs["GT-ACP-A"]["formula"]["or"][1] == {
              "and": ["BE-ACP-OSP-UNAVAIL", "GT-EDG-A"]},
          "the ordinary bus gate needs the grid AND the diesel to be lost")
    loop = load("event-trees/et-loop.yaml")["event_tree"]
    wrong = [f for f, d in loop["functional_events"].items() if has(d["top_gate"])]
    check(not wrong, "the loss-of-offsite-power tree uses a -LOOP top wherever one "
                     "exists" + (f"; not for {', '.join(wrong)}" if wrong else ""))
    others = [f"{rel}:{f}" for rel, doc in docs.items() if "event_tree" in doc
              and doc["event_tree"]["id"] != "ET-LOOP"
              for f, d in doc["event_tree"]["functional_events"].items()
              if d["top_gate"].endswith("-LOOP")]
    check(not others, "no other event tree uses a -LOOP top"
          + (": " + ", ".join(others) if others else ""))

    # --- self-contained
    used_ids, params_used = set(), set()
    def walk(x):
        if isinstance(x, dict):
            if set(x) == {"param"}:
                params_used.add(x["param"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    for rel, doc in docs.items():
        walk(doc)
        for ft in (doc.get("fault_trees") or {}).values():
            for g in ft["gates"].values():
                used_ids.update(operands(g["formula"]))
    bes = {b for rel, doc in docs.items() for b in (doc.get("basic_events") or {})}
    pars = set(docs["parameters.yaml"]["parameters"])
    check(not pars - params_used, "every parameter is referenced"
          + (": unused " + ", ".join(sorted(pars - params_used)) if pars - params_used
             else ""))
    unused = sorted(bes - used_ids)
    check(not unused, "every basic event is named by a gate"
          + (": " + ", ".join(unused) if unused else ""))
    members = [m for g in docs["ccf-groups.yaml"]["ccf_groups"].values()
               for m in g["members"]]
    check(set(members) <= bes and len(members) == len(set(members)),
          f"{len(members)} common-cause members, each an existing basic event of "
          "exactly one group")
    placeholder = [rel for rel, doc in docs.items()
                   if any(w in json.dumps(doc).lower()
                          for w in ("todo", "tbd", "fixme", "xxx"))]
    check(not placeholder, "no provenance or label is a placeholder"
          + (": " + ", ".join(placeholder) if placeholder else ""))

    # --- MEF round trip (common-cause groups expanded: the comparable form)
    xml = os.path.join(tmp, "ref.xml")
    back = os.path.join(tmp, "back")
    r = run(os.path.join(HERE, "export_mef.py"), REF, xml, "--expand-ccf")
    ok = r.returncode == 0 and run(os.path.join(HERE, "import_mef.py"), xml,
                                   back).returncode == 0
    if ok:
        bj = os.path.join(tmp, "back.json")
        ok = run(os.path.join(HERE, "quantify.py"), back, bj,
                 "--engine", ENGINE).returncode == 0
    if not ok:
        failures.append("MEF round trip: export, import or quantification failed")
    else:
        with open(bj) as f:
            got = json.load(f)
        worst, n, where = 0.0, 0, None
        for t, a in res.items():
            fa = a["initiating_event"]["frequency_per_year"]
            fb = got[t]["initiating_event"]["frequency_per_year"]
            sa = {s["id"]: s["frequency_per_year"] / fa for s in a["sequences"]}
            sb = {s["id"]: s["frequency_per_year"] / fb for s in got[t]["sequences"]}
            if set(sa) != set(sb):
                failures.append(f"MEF round trip: {t}: sequences differ")
                continue
            for k in sa:
                d = abs(sa[k] - sb[k]) / max(abs(sa[k]), 1e-300)
                n += 1
                if d > worst:
                    worst, where = d, f"{t}/{k}"
        check(worst < 1e-12, f"MEF round trip: {n} sequence probabilities agree to "
                             f"{worst:.1e} (worst at {where}); the frequency of an "
                             "initiating event is not part of MEF, so probabilities "
                             "are what can be compared")

    # --- SQL working copy
    copy = os.path.join(tmp, "reference")
    shutil.copytree(REF, copy)
    db = os.path.join(tmp, "ref.sqlite")
    import sql_projection
    try:
        sql_projection.load(copy, db)
        changed = sql_projection.dump(db, copy)
        check(changed == [], "the SQL working copy of the model dumps back unchanged")
    except sql_projection.ProjectionError as e:
        failures.append(f"SQL working copy: {e}")

    # --- configurations
    cfg = os.path.join(tmp, "cfg.json")
    r = run(os.path.join(HERE, "quantify.py"), REF, os.path.join(tmp, "c.json"),
            "--engine", ENGINE, "--configurations", cfg)
    if r.returncode != 0:
        failures.append("configurations: quantify.py failed\n" + r.stdout + r.stderr)
    else:
        with open(cfg) as f:
            runs = json.load(f)
        base = None
        worse = []
        for name, c in runs.items():
            v = math.fsum(m["value_per_year"] for t in c.values()
                          for m in t["metrics"] if m["id"] == "CDF")
            if name == "BASE":
                base = v
            elif base is not None and v < base * (1 - 1e-12):
                worse.append(f"{name} {v:.3e} < base {base:.3e}")
        check(base is not None and len(runs) == 5 and not worse,
              f"all 5 configurations quantify and none is below the base case "
              f"({base:.3e} /yr)" + ("; " + ", ".join(worse) if worse else ""))

    # --- uncertainty
    unc = os.path.join(tmp, "unc.json")
    r = run(os.path.join(HERE, "quantify.py"), REF, unc, "--engine", ENGINE,
            "--samples", "2000", "--seed", "20260708")
    if r.returncode != 0:
        failures.append("uncertainty: quantify.py failed\n" + r.stdout + r.stderr)
    else:
        with open(unc) as f:
            u = json.load(f)
        outside = []
        for t, tr in u.items():
            for m in tr["metrics"]:
                d = m["uncertainty"]
                if not d["p05"] <= m["value_per_year"] <= d["p95"]:
                    outside.append(f"{t}/{m['id']}")
        check(not outside, "a 2000-sample Monte Carlo run brackets every metric's "
                           "point value between its 5th and 95th percentiles"
              + (": " + ", ".join(outside) if outside else ""))

    # --- viewer
    page = os.path.join(tmp, "ref.html")
    r = run(os.path.join(ROOT, "viz", "build_viz.py"), REF, page, "--results", results)
    check(r.returncode == 0 and os.path.getsize(page) > 50000,
          f"the viewer builds ({os.path.getsize(page) // 1024 if os.path.exists(page) else 0} KiB)")

    shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print(f"\n{len(failures)} FAILED:")
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("\nreference model: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
