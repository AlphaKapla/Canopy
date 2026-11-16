#!/usr/bin/env python3
"""Tests for the relative cut-off on event trees (FR-65):
`quantify.py --truncated-relative R [--relative-to METRIC]`.

A two-tree model is made from the demo model: ET-SLOCA as it is
(5.0e-4 /yr) and a copy, ET-MLOCA, at 5.0e-6 /yr — the same logic at a
hundredth of the frequency — both feeding CDF. The exact run gives the
reference: CDF and every minimal cut set with its frequency.

  * the cut-offs: tree t's is R x L / f_t, L the reported lower bound on
    the metric, so R x L is one frequency threshold for both trees and
    the rarer tree's cut-off on probability is 100 times higher;
    L <= exact CDF; the exact CDF lies within the bounds;
  * completeness, against the exact run: every cut set of frequency
    >= R x CDF is listed in its sequence — and, sharper, the listed cut
    sets are exactly those of probability >= the tree's cut-off;
  * a ratio small enough to keep everything gives bounds that close on
    the exact value; a tree whose cut-off is the estimation pass's is
    reused, not run twice (the engine is counted);
  * the reference when the first pass retains nothing: the pass is
    repeated at R/100 until the metric's lower bound is positive;
  * a metric that is exactly zero: no reference, the estimation cut-off
    stands;
  * --relative-to picks the metric; it is required when there are several;
  * refusals: with --truncated, --samples, --prime-implicants,
    --one-process, --configurations; a ratio outside (0, 1); an unknown
    metric; --relative-to alone;
  * the results load in the reports: bounds.py reads them as truncated.

Usage: python ci/test_relative_cutoff.py     (needs the engine)
"""
import json
import math
import os
import shutil
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import bounds  # noqa: E402

MODEL = os.path.join(ROOT, "model")
ENGINE = os.environ.get("CANOPY_BIN") or os.path.join(ROOT, "engine", "target", "release",
                                                      "canopy")


def main() -> int:
    if not os.path.exists(ENGINE):
        print(f"test_relative_cutoff: engine not built ({ENGINE})", file=sys.stderr)
        return 1
    failures = []
    tmp = tempfile.mkdtemp(prefix="canopy-relcut-")

    def check(cond, msg):
        (print(f"  ok  {msg}") if cond else failures.append(msg))

    model = os.path.join(tmp, "model")
    shutil.copytree(MODEL, model)
    with open(os.path.join(model, "event-trees", "et-sloca.yaml"), encoding="utf-8") as f:
        t = f.read()
    assert "value: 5.0e-4" in t
    with open(os.path.join(model, "event-trees", "et-mloca.yaml"), "w", encoding="utf-8") as f:
        f.write(t.replace("ET-SLOCA", "ET-MLOCA").replace("SEQ-SLOCA", "SEQ-MLOCA")
                 .replace("IE-SLOCA", "IE-MLOCA").replace("value: 5.0e-4", "value: 5.0e-6"))

    # a wrapper that counts the engine's runs
    count = os.path.join(tmp, "runs")
    wrapper = os.path.join(tmp, "engine.sh")
    with open(wrapper, "w") as f:
        f.write(f'#!/bin/sh\necho "$2 $@" >> "{count}"\nexec "{ENGINE}" "$@"\n')
    os.chmod(wrapper, os.stat(wrapper).st_mode | stat.S_IEXEC)

    def quantify(*flags, model_dir=model, name="out"):
        out = os.path.join(tmp, name + ".json")
        if os.path.exists(count):
            os.remove(count)
        r = subprocess.run([sys.executable, os.path.join(HERE, "quantify.py"), model_dir, out,
                            "--engine", wrapper, *flags], capture_output=True, text=True)
        runs = open(count).read().splitlines() if os.path.exists(count) else []
        res = json.load(open(out)) if r.returncode == 0 else None
        return r, res, runs

    r, exact, _ = quantify(name="exact")
    assert r.returncode == 0, r.stderr
    cdf = bounds.metric_totals(exact)["CDF"][0]
    # every exact cut set: (tree, sequence, events) -> probability, frequency
    full = {}
    for et_id in exact:
        j = json.loads(subprocess.run([ENGINE, model, et_id, "--json", "--mcs-limit", "100000"],
                                      capture_output=True, text=True, check=True).stdout)
        f_t = j["initiating_event"]["frequency_per_year"]
        for s in j["sequences"]:
            for c in s.get("cut_sets") or []:
                full[(et_id, s["id"], tuple(sorted(c["events"])))] = (
                    c["frequency_per_year"] / f_t, c["frequency_per_year"])
    check(len(exact) == 2 and len(full) > 20
          and math.isclose(cdf, 2.2081729426257123e-08 * 1.01, rel_tol=1e-12),
          f"the two-tree model: CDF = 1.01 x the demo's ({cdf:.6e} /yr), {len(full)} cut sets")

    def listed(res):
        return {(et_id, s["id"], tuple(sorted(c["events"])))
                for et_id, r_ in res.items() for s in r_["sequences"]
                for c in s.get("cut_sets") or []}

    # --- a ratio that drops cut sets
    ratio = 1e-3
    r, res, runs = quantify("--truncated-relative", repr(ratio), name="rel")
    assert r.returncode == 0, r.stdout + r.stderr
    a, b = res["ET-SLOCA"], res["ET-MLOCA"]
    L = a["cutoff_reference_per_year"]
    check(a["relative_cutoff"] == b["relative_cutoff"] == ratio
          and a["cutoff_metric"] == b["cutoff_metric"] == "CDF"
          and b["cutoff_reference_per_year"] == L and 0 < L <= cdf,
          f"the reference is a lower bound on the metric ({L:.4e} <= {cdf:.4e} /yr)")
    check(a["cutoff"] == ratio * L / 5.0e-4 and b["cutoff"] == min(ratio * L / 5.0e-6,
                                                                  a["estimation_cutoff"])
          and math.isclose(a["cutoff"] * 5.0e-4, ratio * L, rel_tol=1e-12)
          and math.isclose(b["cutoff"], 100 * a["cutoff"], rel_tol=1e-12),
          f"each tree's cut-off is R x L / f_t: {a['cutoff']:.3e} for 5.0e-4 /yr, "
          f"{b['cutoff']:.3e} for 5.0e-6 /yr (100 times higher)")
    lo, hi = bounds.metric_totals(res)["CDF"]
    check(lo <= cdf <= hi and bounds.any_truncated(res) and hi > lo,
          f"the exact CDF lies within the bounds: {lo:.6e} <= {cdf:.6e} <= {hi:.6e}")
    got = listed(res)
    must = {k for k, (p, f) in full.items() if f >= ratio * cdf}
    cut = {"ET-SLOCA": a["cutoff"], "ET-MLOCA": b["cutoff"]}
    exactly = {k for k, (p, f) in full.items() if p >= cut[k[0]]}
    dropped = len(full) - len(got)
    check(must <= got and dropped > 0,
          f"every cut set of frequency >= R x CDF is retained ({len(must)} of them; "
          f"{dropped} of {len(full)} dropped)")
    margin = [k for k, (p, f) in full.items()
              if abs(p - cut[k[0]]) <= 1e-9 * cut[k[0]]]
    check(got - set(margin) == exactly - set(margin),
          "the retained cut sets are exactly those of probability >= the tree's cut-off")
    check("relative cut-off: 0.001 x CDF" in r.stdout and "ET-MLOCA: cut-off" in r.stdout,
          "the run prints the ratio, the reference and each tree's cut-off")

    # --- a ratio that keeps everything
    r, res, runs = quantify("--truncated-relative", "1e-9", name="fine")
    lo, hi = bounds.metric_totals(res)["CDF"]
    check(r.returncode == 0 and listed(res) == set(full)
          and math.isclose(lo, cdf, rel_tol=1e-9) and math.isclose(hi, cdf, rel_tol=1e-6),
          f"R = 1e-9 retains all {len(full)} cut sets and the bounds close on the exact value")
    check(len(runs) == 4 and all(r_["cutoff"] < r_["estimation_cutoff"] for r_ in res.values()),
          "two passes of two trees: 4 engine runs")

    # --- the estimation pass repeats until something is retained
    r, res, runs = quantify("--truncated-relative", "0.5", name="coarse")
    est = sorted({float(x.split("--truncated ")[1].split(" ")[0]) for x in runs})
    a = res["ET-SLOCA"]
    check(r.returncode == 0 and a["estimation_cutoff"] < 0.5
          and a["estimation_cutoff"] in (0.5 / 100, 0.5 / 100 ** 2, 0.5 / 100 ** 3)
          and 0.5 in est and a["cutoff_reference_per_year"] > 0
          and all(r_["cutoff"] <= r_["estimation_cutoff"] for r_ in res.values()),
          f"R = 0.5: the pass at 0.5 retains nothing, the reference comes from the pass at "
          f"{a['estimation_cutoff']:g}; no tree's cut-off is coarser than that pass")
    lo, hi = bounds.metric_totals(res)["CDF"]
    check(lo <= cdf <= hi, "and the exact CDF is still within the bounds")
    same = [t_ for t_, r_ in res.items() if r_["cutoff"] == r_["estimation_cutoff"]]
    n_final = len([x for x in runs if float(x.split("--truncated ")[1].split(" ")[0])
                   == a["estimation_cutoff"]])
    check(len(same) == 2 and n_final == 2,
          "a tree whose cut-off is the estimation pass's own is reused, not run again "
          f"({len(runs)} engine runs in all)")

    # --- several metrics, --relative-to, a metric that is zero
    multi = os.path.join(tmp, "multi")
    shutil.copytree(model, multi)
    p = os.path.join(multi, "model.yaml")
    with open(p, encoding="utf-8") as f:
        t = f.read()
    anchor = "    - id: CDF\n"
    assert anchor in t
    with open(p, "w", encoding="utf-8") as f:
        f.write(t.replace(anchor, "    - id: NEVER\n      label: \"No such end state\"\n"
                                  "      end_states: [NOPE]\n" + anchor, 1))
    r, res, _ = quantify("--truncated-relative", "1e-3", model_dir=multi, name="multi")
    check(r.returncode == 1 and "needs --relative-to METRIC" in r.stderr
          and "CDF" in r.stderr and "NEVER" in r.stderr,
          "several metrics: --relative-to is required, and the metrics are named")
    r, res, _ = quantify("--truncated-relative", "1e-3", "--relative-to", "CDF",
                         model_dir=multi, name="multi")
    check(r.returncode == 0 and res["ET-SLOCA"]["cutoff_metric"] == "CDF"
          and res["ET-SLOCA"]["cutoff_reference_per_year"] > 0,
          "--relative-to CDF picks the metric")
    r, res, runs = quantify("--truncated-relative", "1e-3", "--relative-to", "NEVER",
                            model_dir=multi, name="zero")
    check(r.returncode == 0 and res["ET-SLOCA"]["cutoff_reference_per_year"] == 0.0
          and res["ET-SLOCA"]["cutoff"] == res["ET-SLOCA"]["estimation_cutoff"] == 1e-3
          and len(runs) == 2,
          "a metric that is exactly zero: no reference, the first pass stands (2 engine runs)")
    r, res, _ = quantify("--truncated-relative", "1e-3", "--relative-to", "LERF", name="bad")
    check(r.returncode == 1 and "--relative-to LERF: the model's risk metrics are CDF"
          in r.stderr, "an unknown metric is refused, the known ones named")

    # --- refusals of the command line
    for flags, needle in (
            (["--truncated-relative", "1e-3", "--truncated", "1e-9"], "not both"),
            (["--truncated-relative", "1e-3", "--samples", "10"], "without --samples"),
            (["--truncated-relative", "1e-3", "--prime-implicants"], "without --samples"),
            (["--truncated-relative", "1e-3", "--one-process"], "not with --one-process"),
            (["--truncated-relative", "1e-3", "--configurations", os.path.join(tmp, "c.json")],
             "does not quantify configurations"),
            (["--truncated-relative", "1.5"], "a ratio in (0, 1)"),
            (["--truncated-relative", "0"], "a ratio in (0, 1)"),
            (["--relative-to", "CDF"], "applies only with --truncated-relative")):
        r, res, runs = quantify(*flags, name="refused")
        check(r.returncode == 2 and needle in r.stderr and not runs,
              f"refused before any engine run: {' '.join(flags[:3])} ({needle})")

    # --- order limit passes through
    r, res, runs = quantify("--truncated-relative", "1e-3", "--order-limit", "2", name="order")
    check(r.returncode == 0 and all("--order-limit 2" in x for x in runs)
          and all(len(k[2]) <= 2 for k in listed(res)),
          "--order-limit applies to every pass")

    shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print(f"\n{len(failures)} FAILED:")
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("\nrelative cut-off: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
