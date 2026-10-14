#!/usr/bin/env python3
"""Synthetic full-scope benchmark (roadmap v0.3 exit, V&V FR-57): generate
fault trees with ci/generate_fault_tree.py, import them (ci/import_mef.py),
and quantify each one exactly (--prob-only) and by truncated minimal cut
sets (--truncated CUTOFF or --truncated-relative R), under a timeout and a
memory cap.

Gating on what must hold, reporting on the rest:
  * every truncated run completes (no timeout, no crash) — the hybrid path
    must deliver bounds where the exact BDD cannot;
  * wherever the exact run also completes, the exact P(top) lies within the
    truncated bounds [lower, upper] (relative slack 1e-12);
  * a generated model that fails to import is a failure.
The exact run is allowed to fail (that is what truncation is for); its
outcome, the width of the bounds, how each bound was obtained (FR-56),
times and peak memory are reported as a markdown table, optionally
appended to the GitHub job summary.

The default structure is `psa` (systems, trains, components, support
systems and sequences; see generate_fault_tree.py). `--structure random`
uses the random DAG with the parameters of published engine benchmarks;
on those trees neither method is expected to scale (docs/limitations.md),
so use --report-only with it.

Usage: benchmark_synthetic.py [--sizes 1000,5000,40000] [--seeds 1,2,3]
         [--structure psa|random] [--truncated 1e-8 | --truncated-relative R]
         [--timeout 900] [--exact-timeout 300] [--mem-gib 6] [--engine PATH]
         [--summary PATH] [--report-only] [--keep DIR]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from aralia_regression import run_measured  # noqa: E402

SLACK = 1e-12


def generate(d, structure, n, seed):
    xml = os.path.join(d, f"{structure}-{n}-s{seed}.xml")
    g = subprocess.run([sys.executable, os.path.join(HERE, "generate_fault_tree.py"), xml,
                        "--structure", structure, "--basic-events", str(n),
                        "--seed", str(seed)], capture_output=True, text=True)
    if g.returncode != 0:
        return None, f"generator failed: {g.stderr.strip()}"
    model = os.path.join(d, f"{structure}-{n}-s{seed}")
    imp = subprocess.run([sys.executable, os.path.join(HERE, "import_mef.py"), xml, model],
                         capture_output=True, text=True)
    if imp.returncode != 0:
        return None, f"import failed: {imp.stderr.strip()}"
    return model, None


def sizes_of(model):
    """(basic events, gates) of an imported model."""
    import yaml
    n_be = n_g = 0
    for sub, key in (("basic-events", "basic_events"), ("fault-trees", "fault_trees")):
        for f in sorted(os.listdir(os.path.join(model, sub))):
            data = yaml.safe_load(open(os.path.join(model, sub, f)))[key]
            if key == "basic_events":
                n_be += len(data)
            else:
                n_g += sum(len(ft.get("gates", {})) for ft in data.values())
    return n_be, n_g


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sizes", default="1000,5000,40000")
    ap.add_argument("--seeds", default="1,2,3")
    ap.add_argument("--structure", choices=["psa", "random"], default="psa")
    how = ap.add_mutually_exclusive_group()
    how.add_argument("--truncated", type=float, metavar="CUTOFF")
    how.add_argument("--truncated-relative", type=float, metavar="R")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--exact-timeout", type=int, default=300)
    ap.add_argument("--mem-gib", type=int, default=6)
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", os.path.join(ROOT, "engine/target/release/canopy")))
    ap.add_argument("--summary", help="also append the table to this file "
                    "(e.g. $GITHUB_STEP_SUMMARY)")
    ap.add_argument("--report-only", action="store_true",
                    help="report every outcome, gate on nothing")
    ap.add_argument("--keep", help="keep the generated models in this directory")
    a = ap.parse_args()
    if a.truncated is None and a.truncated_relative is None:
        a.truncated = 1e-8
    trunc_args = (["--truncated", repr(a.truncated)] if a.truncated is not None
                  else ["--truncated-relative", repr(a.truncated_relative)])
    mem = a.mem_gib << 30
    d = a.keep or tempfile.mkdtemp(prefix="synthetic-")
    os.makedirs(d, exist_ok=True)
    rows, bad = [], []
    try:
        for n in [int(x) for x in a.sizes.split(",")]:
            for seed in [int(x) for x in a.seeds.split(",")]:
                name = f"{a.structure} {n} s{seed}"
                model, err = generate(d, a.structure, n, seed)
                if err:
                    bad.append(f"{name}: {err}")
                    rows.append((name, "—", "—", err, "—", "—", "—", "—", "—"))
                    continue
                n_be, n_g = sizes_of(model)
                out, dt_e, rss_e, fail_e = run_measured(
                    [a.engine, model, "FT-MAIN", "--json", "--mcs-limit", "0", "--prob-only"],
                    a.exact_timeout, mem)
                p_exact = json.loads(out)["probability"] if out else None
                exact_s = (f"{p_exact:.6e} ({dt_e:.1f} s, {rss_e:.0f} MiB)" if p_exact is not None
                           else f"{fail_e} ({dt_e:.1f} s)")
                out, dt, rss, fail = run_measured(
                    [a.engine, model, "FT-MAIN", "--json", "--mcs-limit", "0", *trunc_args],
                    a.timeout, mem)
                if fail:
                    bad.append(f"{name}: truncated run {fail} after {dt:.1f} s")
                    rows.append((name, str(n_be), str(n_g), exact_s, fail, "—", "—", f"{dt:.1f}",
                                 f"{rss:.0f}" if rss else "—"))
                    if not a.keep:
                        shutil.rmtree(model, ignore_errors=True)
                    continue
                j = json.loads(out)
                lo, up = j["probability_lower_bound"], j["probability_upper_bound"]
                width = (up - lo) / up if up > 0 else 0.0
                how_s = (f"{j['lower_bound_method']}"
                         f"{'' if j['truncation_error_filtered'] else ', error unfiltered'}"
                         f", upper {j['upper_bound_method']}")
                if p_exact is None:
                    verdict = "bounds only"
                else:
                    s = SLACK * max(p_exact, 1e-300)
                    verdict = "WITHIN" if lo - s <= p_exact <= up + s else "OUTSIDE"
                    if verdict == "OUTSIDE":
                        bad.append(f"{name}: exact {p_exact:.6e} outside [{lo:.6e}, {up:.6e}]")
                rows.append((name, str(n_be), str(n_g), exact_s,
                             f"[{lo:.4e}, {up:.4e}], {j['retained_cut_sets']:.0f} retained",
                             f"{width:.1e}", f"{verdict}; {how_s}", f"{dt:.1f}",
                             f"{rss:.0f}" if rss else "—"))
                if not a.keep:
                    shutil.rmtree(model, ignore_errors=True)
    finally:
        if not a.keep:
            shutil.rmtree(d, ignore_errors=True)
    how_hdr = (f"cut-off {a.truncated:g}" if a.truncated is not None
               else f"relative cut-off {a.truncated_relative:g}")
    lines = [f"### Synthetic {a.structure} fault trees, truncated {how_hdr} "
             f"(timeout {a.timeout} s, exact {a.exact_timeout} s, {a.mem_gib} GiB cap)", "",
             "| model | basic events | gates | exact P(top) | truncated bounds | rel. width "
             "| verdict; how | time (s) | peak RSS (MiB) |",
             "|---|---|---|---|---|---|---|---|---|"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    done = sum(1 for r in rows if r[4].startswith("["))
    within = sum(1 for r in rows if r[6].startswith("WITHIN"))
    exact = sum(1 for r in rows if r[6].startswith(("WITHIN", "OUTSIDE")))
    lines += ["", f"**{done} of {len(rows)} truncated runs completed; exact P(top) within the "
              f"bounds in {within} of the {exact} models the exact BDD also quantified**"
              + ("" if not bad else f"; {len(bad)} problem(s): " + "; ".join(bad))]
    text = "\n".join(lines)
    print(text)
    if a.summary:
        with open(a.summary, "a") as f:
            f.write(text + "\n")
    return 0 if a.report_only or not bad else 1


if __name__ == "__main__":
    sys.exit(main())
