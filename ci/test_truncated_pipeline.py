#!/usr/bin/env python3
"""Tests for truncated results through the reporting pipeline (FR-42).

`quantify.py --truncated` writes event-tree results that carry bounds, not
values; compare.py, consequence_report.py, appendix.py and the viewer read
them through ci/bounds.py. Checked here:

  * bounds.py: printed bounds are rounded outward (a printed lower bound
    never above the bound, an upper one never below, one unit apart at
    most) over 4,000 values across 600 orders of magnitude; a point value
    prints exactly as before; extra digits separate different bounds;
    totals over mixed exact/truncated trees.
  * quantify.py on the demo at four cut-offs: every sequence frequency and
    the metric of the exact run lie within the truncated bounds, the
    partition bounds bracket 1, the smallest cut-off reproduces the exact
    values; configurations are quantified truncated and contain the exact
    configuration values; refusals; and the partition check on bounds —
    fed by a fake engine — fails on bounds that miss 1 or a transfer
    whose expansions miss the row, and only notes a tree with house
    overrides.
  * compare.py: the change cell against hand-computed strings (increase,
    straddle, identical, new, mixed exact/truncated), and — the property
    that makes it rigorous — for 2,000 random interval pairs and true
    values inside them, the printed change and ratio intervals contain the
    true change and ratio; the truncated report's header, sections and
    caveats.
  * consequence_report.py: total, coverage and share bounds (JSON) against
    the sums and quotients of the results, the below-cut-off note, no
    point total on the truncated path.
  * appendix.py and the viewer's data: intervals where values were; the
    viewer's metrics summed over event trees (V&V D-21) and its diff of
    bounds.
  * `canopy quantify --truncated` = quantify.py byte for byte;
    `canopy delta --truncated` reports bounds.

Usage: python ci/test_truncated_pipeline.py [--engine PATH]
"""
import argparse
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODEL = os.path.join(ROOT, "model")
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "viz"))
import bounds  # noqa: E402
import compare  # noqa: E402
import build_viz  # noqa: E402

failures = []
n_checks = [0]


def check(cond, msg):
    n_checks[0] += 1
    if not cond:
        failures.append(msg)


def run(script, *args, env=None):
    return subprocess.run([sys.executable, os.path.join(HERE, script), *args],
                          capture_output=True, text=True, cwd=ROOT,
                          env={**os.environ, **(env or {})})


def within(x, lo, hi, rel=1e-12):
    s = rel * max(abs(lo), abs(hi), 1e-300)
    return lo - s <= x <= hi + s


def num(t: str) -> Decimal:
    return Decimal(t)


# ---------------------------------------------------------------- bounds.py
def test_bounds_module():
    rng = random.Random(20260929)
    worst = 0
    for i in range(4000):
        x = 10.0 ** rng.uniform(-300, 300) * rng.choice([1, 1, 1, -1])
        d = rng.choice([2, 3, 4, 6])
        lo, hi = bounds.fmt_bound(x, d, False), bounds.fmt_bound(x, d, True)
        xd = Decimal(repr(x))
        tol = abs(xd) * bounds.NOISE
        if not (num(lo) <= xd + tol and xd - tol <= num(hi)):
            failures.append(f"fmt_bound({x!r}, {d}) not outward: {lo} .. {hi}")
            break
        ulp = Decimal(1).scaleb(xd.adjusted() - d)
        worst = max(worst, (num(hi) - num(lo)) / ulp)
        if not re.fullmatch(r"-?\d\.\d{%d}e[+-]\d{2,3}" % d, lo):
            failures.append(f"fmt_bound style: {lo!r}")
            break
    check(worst <= 1, f"outward rounding widens by at most one unit (worst {worst})")
    check(bounds.fmt_bound(5e-4, 4, True) == "5.0000e-04" == bounds.fmt_bound(5e-4, 4, False),
          "a short decimal prints unchanged both ways")
    check(bounds.fmt_bound(9.99995e-8, 4, True) == "1.0000e-07", "carry into the exponent")
    for v in (2.2081729426257355e-08, 1.0, 3.3e-300, 0.0):
        check(bounds.fmt_interval(v, v, False) == f"{v:.4e}", f"point value {v} as before")
    check(bounds.fmt_interval(1e-6, 1e-6, True) == "[1.0000e-06, 1.0000e-06]",
          "a truncated result is bracketed even when its bounds coincide")
    check(bounds.fmt_interval(2.208173e-8, 2.208247e-8, True) == "[2.2081e-08, 2.2083e-08]",
          "outward bounds of a narrow interval")
    check(bounds.fmt_interval(1.00001e-6, 1.00002e-6, True) == "[1.0000e-06, 1.0001e-06]",
          "a narrow interval prints wider, never narrower")
    check(bounds.fmt_bound(2.5e-6 - 1.0e-6, 3, True) == "1.500e-06"
          and bounds.fmt_bound(1.5e-6 * (1 + 1e-9), 3, True) == "1.501e-06",
          "floating-point noise (1e-12 relative) is not rounded outward; a real excess is")
    check(bounds.fmt_ratio_bound(2.5, True) == "2.50" and bounds.fmt_ratio_bound(1 / 0.6, False) == "1.66"
          and bounds.fmt_ratio_bound(1234.5, True) == "1240", "ratio bounds, outward, 3 digits")
    mixed = {"ET-B": {"method": "truncated-mcs", "cutoff": 1e-9,
                      "metrics": [{"id": "CDF", "value_lower_bound": 1e-6, "value_upper_bound": 2e-6}]},
             "ET-A": {"metrics": [{"id": "CDF", "value_per_year": 5e-7},
                                  {"id": "LERF", "value_per_year": 1e-8}]}}
    check(bounds.metric_totals(mixed) == {"CDF": (5e-7 + 1e-6, 5e-7 + 2e-6), "LERF": (1e-8, 1e-8)},
          f"totals over mixed trees: {bounds.metric_totals(mixed)}")
    check(bounds.method_note(mixed) == "cut-off 1e-09" and bounds.method_note({"ET-A": mixed["ET-A"]}) is None,
          "method note")


# -------------------------------------------------------------- quantify.py
def test_quantify(engine, tmp):
    exact = os.path.join(tmp, "exact.json")
    ecfg = os.path.join(tmp, "exact-cfg.json")
    p = run("quantify.py", MODEL, exact, "--engine", engine, "--configurations", ecfg)
    check(p.returncode == 0, f"exact quantify: {p.stderr}")
    ex = json.load(open(exact))
    excfg = json.load(open(ecfg))
    for cutoff in (1e-15, 1e-9, 1e-7, 1e-5):
        out = os.path.join(tmp, f"t{cutoff:g}.json")
        cfg = os.path.join(tmp, f"t{cutoff:g}-cfg.json")
        p = run("quantify.py", MODEL, out, "--engine", engine, "--truncated", repr(cutoff),
                "--configurations", cfg)
        check(p.returncode == 0 and "truncated quantification (cut-off" in p.stdout,
              f"quantify --truncated {cutoff}: {p.stdout}{p.stderr}")
        if p.returncode != 0:
            continue
        tr = json.load(open(out))
        for et_id, r in tr.items():
            check(r["method"] == "truncated-mcs" and r["cutoff"] == cutoff, f"{et_id} method")
            exs = {s["id"]: s["frequency_per_year"] for s in ex[et_id]["sequences"]}
            for s in r["sequences"]:
                lo, hi = s["frequency_lower_bound"], s["frequency_upper_bound"]
                check(within(exs[s["id"]], lo, hi),
                      f"cut-off {cutoff}: {s['id']} exact {exs[s['id']]} outside [{lo}, {hi}]")
                if cutoff == 1e-15:
                    check(abs(lo - exs[s["id"]]) <= 1e-12 * exs[s["id"]]
                          and abs(hi - exs[s["id"]]) <= 1e-12 * exs[s["id"]],
                          f"cut-off 1e-15 reproduces {s['id']}")
            part = r["partition"]
            check(part["sum_probability_lower_bound"] <= 1 + 1e-12 <= part["sum_probability_upper_bound"] + 2e-12,
                  f"cut-off {cutoff}: partition bounds {part}")
        for mid, (lo, hi) in bounds.metric_totals(tr).items():
            e = bounds.metric_totals(ex)[mid][0]
            check(within(e, lo, hi), f"cut-off {cutoff}: {mid} {e} outside [{lo}, {hi}]")
            check(f"{mid}: {bounds.fmt_interval(lo, hi, True)} /yr" in p.stdout,
                  f"printed model-wide bounds: {p.stdout}")
        trc = json.load(open(cfg))
        for cid, res in trc.items():
            for mid, (lo, hi) in bounds.metric_totals(res).items():
                e = bounds.metric_totals(excfg[cid])[mid][0]
                check(within(e, lo, hi) and all(bounds.is_truncated(x) for x in res.values()),
                      f"configuration {cid} cut-off {cutoff}: {mid} {e} outside [{lo}, {hi}]")
    for args, msg in [(["--truncated", "1e-9", "--samples", "10"], "--truncated applies without"),
                      (["--order-limit", "2"], "--order-limit applies only with --truncated"),
                      (["--truncated", "1e-9", "--prime-implicants"], "--truncated applies without")]:
        p = run("quantify.py", MODEL, os.path.join(tmp, "x.json"), "--engine", engine, *args)
        check(p.returncode != 0 and msg in p.stderr, f"refused {args}: {p.stderr}")
    # order limit passed through
    p = run("quantify.py", MODEL, os.path.join(tmp, "o1.json"), "--engine", engine,
            "--truncated", "0.0", "--order-limit", "1")
    o1 = json.load(open(os.path.join(tmp, "o1.json"))) if p.returncode == 0 else {}
    check(p.returncode == 0 and all(r.get("order_limit") == 1 and all(len(c["events"]) <= 1
                                                                      for s in r["sequences"]
                                                                      for c in s["cut_sets"])
                                    for r in o1.values()),
          f"--order-limit reaches the engine: {p.stderr}")
    return ex


def fake_engine(tmp, name, payload):
    """An executable standing in for the engine: prints `payload` as JSON."""
    path = os.path.join(tmp, name)
    with open(path, "w") as f:
        f.write(f"#!{sys.executable}\nimport json, sys\n"
                f"print(json.dumps({payload!r}))\n")
    os.chmod(path, 0o755)
    return path


def trunc_result(rows, part, overrides=False):
    return {"type": "event_tree", "id": "ET-SLOCA", "method": "truncated-mcs", "cutoff": 1e-9,
            "initiating_event": {"id": "IE-SLOCA", "frequency_per_year": 1.0},
            "sequences": rows, "metrics": [],
            "partition": {"sum_probability_lower_bound": part[0],
                          "sum_probability_upper_bound": part[1],
                          "per_sequence_house_overrides": overrides},
            "basic_event_probabilities": {}}


def row(sid, lo, hi, followed=None):
    return {"id": sid, "end_state": "CD", "transfer": None, "transfer_path": None,
            "followed": followed, "probability_lower_bound": lo, "probability_upper_bound": hi,
            "frequency_lower_bound": lo, "frequency_upper_bound": hi, "cut_sets": []}


def test_partition_check(tmp):
    cases = [
        ("bounds bracket 1", trunc_result([row("S1", 0.4, 0.5), row("S2", 0.5, 0.6)], (0.9, 1.1)),
         0, None),
        ("bounds miss 1 from below", trunc_result([row("S1", 0.4, 0.45)], (0.4, 0.99)),
         1, "does not contain 1"),
        ("bounds miss 1 from above", trunc_result([row("S1", 0.4, 0.45)], (1.01, 1.2)),
         1, "does not contain 1"),
        ("house overrides: noted only", trunc_result([row("S1", 0.4, 0.45)], (0.4, 0.5), True),
         0, "per-sequence house-event overrides"),
        ("expansions overlap the row", trunc_result(
            [row("S1", 0.2, 0.3, {"probability_lower_bound": 0.2, "probability_upper_bound": 0.3,
                                  "sum_probability_lower_bound": 0.29, "sum_probability_upper_bound": 0.4,
                                  "per_sequence_house_overrides": False})], (0.9, 1.1)), 0, None),
        ("expansions miss the row", trunc_result(
            [row("S1", 0.2, 0.3, {"probability_lower_bound": 0.2, "probability_upper_bound": 0.3,
                                  "sum_probability_lower_bound": 0.31, "sum_probability_upper_bound": 0.4,
                                  "per_sequence_house_overrides": False})], (0.9, 1.1)),
         1, "transfer expansions' bounds"),
    ]
    for i, (name, payload, rc, text) in enumerate(cases):
        eng = fake_engine(tmp, f"fake{i}", payload)
        p = run("quantify.py", MODEL, os.path.join(tmp, f"f{i}.json"), "--engine", eng,
                "--truncated", "1e-9")
        check(p.returncode == rc and (text is None or text in p.stdout + p.stderr),
              f"partition check, {name}: exit {p.returncode}\n{p.stdout}{p.stderr}")


# --------------------------------------------------------------- compare.py
def test_compare_cells():
    c = compare.interval_delta_cell
    check(c((1.0e-6, 1.2e-6), (2.0e-6, 2.5e-6)) == "🔺 +8.000e-07 to +1.500e-06 (×1.66–2.50)",
          f"increase: {c((1.0e-6, 1.2e-6), (2.0e-6, 2.5e-6))}")
    check(c((2.0e-6, 2.5e-6), (1.0e-6, 1.2e-6)) == "🔽 -1.500e-06 to -8.000e-07 (×0.400–0.600)",
          f"decrease: {c((2.0e-6, 2.5e-6), (1.0e-6, 1.2e-6))}")
    check(c((1.0e-6, 2.0e-6), (1.5e-6, 2.5e-6)) == "-5.000e-07 to +1.500e-06 (×0.750–2.50)",
          f"straddle, no arrow: {c((1.0e-6, 2.0e-6), (1.5e-6, 2.5e-6))}")
    check(c((1e-6, 2e-6), (1e-6, 2e-6)) == "—", "identical bounds")
    check(c((1e-6, 2e-6), (1e-6 * (1 + 1e-12), 2e-6)) == "—"
          and c((1e-6, 2e-6), (1e-6 * (1 + 1e-6), 2e-6)) != "—",
          "a bound moved by less than 1e-9 relative is no change, by 1e-6 it is")
    check(c((0.0, 0.0), (1e-7, 2e-7)) == "**new**", "new")
    check(c((3e-6, 3e-6), (1e-6, 4e-6)).startswith("-2.000e-06 to +1.000e-06"),
          f"exact base, truncated head: {c((3e-6, 3e-6), (1e-6, 4e-6))}")
    check(c((0.0, 1e-6), (1e-7, 2e-7)) == "-9.000e-07 to +2.000e-07", "no ratio when base can be 0")
    rng = random.Random(7)
    bad = 0
    for _ in range(2000):
        e = rng.uniform(-12, -3)
        bl = 10 ** e
        bh = bl * (1 + rng.choice([0, rng.uniform(0, 1e-9), rng.uniform(0, 1e-3), rng.uniform(0, 2)]))
        hl = bl * 10 ** rng.uniform(-1, 1)
        hh = hl * (1 + rng.choice([0, rng.uniform(0, 1e-6), rng.uniform(0, 1)]))
        bv, hv = rng.uniform(bl, bh), rng.uniform(hl, hh)
        cell = c((bl, bh), (hl, hh))
        if cell == "—":
            bad += not compare.same_bounds((bl, bh), (hl, hh))
            continue
        m = re.fullmatch(r"(?:🔺 |🔽 )?([-+][\d.e+-]+) to ([-+][\d.e+-]+) \(×([\d.]+)–([\d.]+)\)", cell)
        if not m:
            bad += 1
            continue
        lo, hi, rlo, rhi = (Decimal(x) for x in m.groups())
        dv = Decimal(hv) - Decimal(bv)
        rv = Decimal(hv) / Decimal(bv)
        # the printed interval contains the true change, to the rounding of
        # the float subtraction and the display's NOISE tolerance
        slack = (Decimal("1e-15") * max(abs(Decimal(hh)), abs(Decimal(bh)))
                 + bounds.NOISE * max(abs(lo), abs(hi)))
        rs = Decimal("1e-12") * rhi
        if not (lo - slack <= dv <= hi + slack and rlo - rs <= rv <= rhi + rs):
            bad += 1
        if (cell.startswith("🔺") and not hl > bh) or (cell.startswith("🔽") and not hh < bl):
            bad += 1
    check(bad == 0, f"change interval contains the true change and ratio ({bad} of 2000 not)")


def test_compare_report(tmp, ex):
    t9 = json.load(open(os.path.join(tmp, "t1e-09.json")))
    t7 = json.load(open(os.path.join(tmp, "t1e-07.json")))
    def cmp(b, h, *extra):
        pb, ph = os.path.join(tmp, "cb.json"), os.path.join(tmp, "ch.json")
        json.dump(b, open(pb, "w"))
        json.dump(h, open(ph, "w"))
        return run("compare.py", pb, ph, *extra).stdout
    same = cmp(t9, t9)
    check("**Truncated quantification** (both sides cut-off 1e-09)" in same
          and "| **CDF** | [" in same and "| — |" in same
          and "quantitatively neutral" in same
          and "_Importance not reported: truncated quantification computes no importance._" in same
          and "_Truncated quantification: each value lies within its bounds" in same,
          f"identical truncated results:\n{same}")
    cdf = bounds.metric_totals(t9)["CDF"]
    check(f"| **CDF** | {bounds.fmt_interval(*cdf, True)} | {bounds.fmt_interval(*cdf, True)} | — |" in same,
          "metric row: outward-rounded bounds on both sides")
    mixed = cmp(ex, t7)
    check("(base exact; head cut-off 1e-07)" in mixed
          and f"| **CDF** | {bounds.metric_totals(ex)['CDF'][0]:.4e} | "
              f"{bounds.fmt_interval(*bounds.metric_totals(t7)['CDF'], True)} |" in mixed
          and "### Changed sequences" in mixed,
          f"mixed exact/truncated:\n{mixed}")
    # at 1e-7 every CD cut set of the demo is retained, and an OK row lists
    # none on either path (V&V D-20): no cut-set change at all
    check("### Cut set changes" not in mixed,
          f"mixed exact/truncated at 1e-7: same cut sets on both paths:\n{mixed}")
    # a change: SEQ-02 retains nothing at 1e-5, everything at 1e-9
    t5 = json.load(open(os.path.join(tmp, "t1e-05.json")))
    ch = cmp(t9, t5)
    check("### Cut set changes" in ch and "_Cut sets retained at the cut-off: a set listed as new or "
          "removed may only have crossed it._" in ch and "**Removed cut sets:**" in ch,
          f"cut-set caveat on truncated results:\n{ch}")


# ------------------------------------------------------- consequence report
def test_consequence_report(tmp):
    for cutoff in ("1e-07", "1e-05"):
        res = os.path.join(tmp, f"t{float(cutoff):g}.json")
        r = json.load(open(res))
        p = run("consequence_report.py", res, "--end-state", "CD", "--json", "--top", "0")
        check(p.returncode == 0, f"report --json on truncated: {p.stderr}")
        if p.returncode != 0:
            continue
        j = json.loads(p.stdout)
        rows = [s for t in r.values() for s in t["sequences"] if s["end_state"] == "CD" and not s["transfer"]]
        lo = sum(s["frequency_lower_bound"] for s in rows)
        hi = sum(s["frequency_upper_bound"] for s in rows)
        check("total_frequency_per_year" not in j and "coverage" not in j
              and j["total_frequency_lower_bound"] == lo and j["total_frequency_upper_bound"] == hi
              and j["method"] == "truncated-mcs",
              f"cut-off {cutoff}: total bounds {j.get('total_frequency_lower_bound')}, "
              f"{j.get('total_frequency_upper_bound')} vs {lo}, {hi}")
        ok = all(c["fraction_lower_bound"] == c["frequency_per_year"] / hi
                 and c["fraction_upper_bound"] == c["frequency_per_year"] / lo
                 and "fraction" not in c for c in j["cut_sets"] + j["basic_event_importance"])
        check(ok, f"cut-off {cutoff}: shares are f/upper .. f/lower")
        pooled = sum(c["frequency_per_year"] for c in j["cut_sets"])
        check(math.isclose(j["coverage_lower_bound"], pooled / hi, rel_tol=1e-12)
              and math.isclose(j["coverage_upper_bound"], pooled / lo, rel_tol=1e-12),
              "coverage bounds")
        below = {x["sequence"] for x in j["below_cutoff_sequences"]}
        want = {s["id"] for s in rows if not s["cut_sets"]}
        check(below == want, f"cut-off {cutoff}: below cut-off {below} vs {want}")
        if cutoff == "1e-05":
            check("SEQ-SLOCA-02" in below, "at 1e-5, SEQ-SLOCA-02 retains nothing")
        t = run("consequence_report.py", res, "--metric", "CDF", "--model", MODEL)
        check(t.returncode == 0 and f"total frequency  : {bounds.fmt_interval(lo, hi, True)} /yr" in t.stdout
              and "truncated quantification computes no importance" in t.stdout
              and "WARNING" not in t.stdout
              and (cutoff != "1e-05" or "no cut set retained at the cut-off" in t.stdout),
              f"report text at {cutoff}:\n{t.stdout}{t.stderr}")


# -------------------------------------------------------- appendix, viewer
def test_appendix_viewer(tmp, ex):
    res = os.path.join(tmp, "t1e-07.json")
    r = json.load(open(res))
    out = os.path.join(tmp, "app.md")
    p = run("appendix.py", MODEL, res, out)
    txt = open(out).read() if p.returncode == 0 else ""
    cdf = bounds.metric_totals(r)["CDF"]
    check(p.returncode == 0 and f"| CDF | Core damage frequency | CD | {bounds.fmt_interval(*cdf, True)} | — |" in txt
          and "| bounds (/yr) |" in txt and "Truncated quantification (cut-off 1e-07)" in txt,
          f"appendix A.1 on truncated results: {p.stderr}")
    for s in r["ET-SLOCA"]["sequences"]:
        check(bounds.fmt_interval(*bounds.seq_interval(s), True) in txt, f"appendix row {s['id']}")

    d = build_viz.build_data(MODEL, r)
    seqs = d["event_trees"]["ET-SLOCA"]["sequences"]
    check(all(q["freq"] is None and q["freq_bounds"] == list(bounds.seq_interval(
        next(s for s in r["ET-SLOCA"]["sequences"] if s["id"] == sid)))
              and q["freq_text"].startswith("[") for sid, q in seqs.items()),
          "viewer: truncated sequences carry bounds and a text, no value")
    check(d["metrics"] == [{"id": "CDF", "label": "Core damage frequency",
                            "value_lower_bound": cdf[0], "value_upper_bound": cdf[1],
                            "text": bounds.fmt_interval(*cdf, True, digits=3)}]
          and d["truncation"] == "cut-off 1e-07", f"viewer metrics: {d['metrics']}")
    de = build_viz.build_data(MODEL, ex)
    check("truncation" not in de and all("freq_bounds" not in q for q in
                                         de["event_trees"]["ET-SLOCA"]["sequences"].values()),
          "viewer: exact results carry no bounds")
    # V&V D-21: metrics are model-wide, summed over event trees
    two = {"ET-SLOCA": ex["ET-SLOCA"],
           "ET-OTHER": {"id": "ET-OTHER", "sequences": [],
                        "metrics": [{"id": "CDF", "label": "Core damage frequency",
                                     "value_per_year": 1e-6}]}}
    d2 = build_viz.build_data(MODEL, two)
    total = ex["ET-SLOCA"]["metrics"][0]["value_per_year"]
    check([(m["id"], m["value_per_year"]) for m in d2["metrics"]] == [("CDF", 1e-6 + total)]
          or [(m["id"], m["value_per_year"]) for m in d2["metrics"]] == [("CDF", total + 1e-6)],
          f"viewer: metric summed over event trees (D-21): {d2['metrics']}")
    # diff of bounds
    t9 = json.load(open(os.path.join(tmp, "t1e-09.json")))
    same = build_viz.diff_data(build_viz.build_data(MODEL, t9, True), build_viz.build_data(MODEL, t9, True))
    check(not same["event_trees"] and not any(m["changed"] for m in same["metrics"]),
          "viewer diff: identical truncated results, no change")
    dd = build_viz.diff_data(build_viz.build_data(MODEL, t9, True), build_viz.build_data(MODEL, r, True))
    ch = dd["event_trees"].get("ET-SLOCA", {}).get("sequences", {})
    moved = {s["id"] for s in r["ET-SLOCA"]["sequences"]
             if bounds.seq_interval(s) != bounds.seq_interval(
                 next(x for x in t9["ET-SLOCA"]["sequences"] if x["id"] == s["id"]))}
    check(set(ch) == moved and all(q["fields"] == ["freq_text"] for q in ch.values())
          and dd["metrics"][0]["changed"] and dd["metrics"][0]["base_text"].startswith("["),
          f"viewer diff: bounds changed on {sorted(ch)} vs {sorted(moved)}")
    mx = build_viz.diff_data(build_viz.build_data(MODEL, ex, True), build_viz.build_data(MODEL, r, True))
    b0 = build_viz.build_data(MODEL, ex, True)["event_trees"]["ET-SLOCA"]["sequences"]
    check(all(q["freq"] is not None and q["freq_text"] == f"{q['freq']:.2e}" for q in b0.values())
          and mx["metrics"][0]["base_text"] == f"{total:.3e}",
          "viewer diff, exact base against truncated head: base shown as values")


# --------------------------------------------------------------------- CLI
def test_cli(engine, tmp):
    env = {"CANOPY_BIN": engine}
    a = subprocess.run([sys.executable, os.path.join(HERE, "canopy.py"), "quantify", MODEL,
                        "-o", os.path.join(tmp, "c1.json"), "--truncated", "1e-9"],
                       capture_output=True, text=True, cwd=ROOT, env={**os.environ, **env})
    b = run("quantify.py", MODEL, os.path.join(tmp, "c2.json"), "--engine", engine, "--truncated", "1e-9")
    check(a.returncode == b.returncode == 0 and a.stdout.replace("c1.json", "") == b.stdout.replace("c2.json", "")
          and open(os.path.join(tmp, "c1.json")).read() == open(os.path.join(tmp, "c2.json")).read(),
          f"canopy quantify --truncated = quantify.py byte for byte: {a.stderr}")
    r = subprocess.run([sys.executable, os.path.join(HERE, "canopy.py"), "quantify", MODEL,
                        "-o", os.path.join(tmp, "c3.json"), "--order-limit", "2"],
                       capture_output=True, text=True, cwd=ROOT, env={**os.environ, **env})
    check(r.returncode == 2 and "--order-limit needs --truncated" in r.stderr,
          f"canopy quantify --order-limit without --truncated refused: {r.stderr}")
    d = subprocess.run([sys.executable, os.path.join(HERE, "canopy.py"), "delta", MODEL,
                        "--truncated", "1e-9"], capture_output=True, text=True, cwd=ROOT,
                       env={**os.environ, **env})
    check(d.returncode == 0 and "**Truncated quantification** (both sides cut-off 1e-09)" in d.stdout,
          f"canopy delta --truncated: {d.stdout[:300]}{d.stderr[-300:]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", os.path.join(ROOT, "engine", "target", "release", "canopy")))
    a = ap.parse_args()
    engine = os.path.abspath(a.engine)
    tmp = tempfile.mkdtemp(prefix="canopy-trunc-pipe-")
    try:
        test_bounds_module()
        ex = test_quantify(engine, tmp)
        test_partition_check(tmp)
        test_compare_cells()
        test_compare_report(tmp, ex)
        test_consequence_report(tmp)
        test_appendix_viewer(tmp, ex)
        test_cli(engine, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print(f"truncated pipeline: all {n_checks[0]} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
