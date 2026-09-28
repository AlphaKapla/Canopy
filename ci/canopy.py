#!/usr/bin/env python3
"""canopy — one entry point for the git-native PSA toolchain.

A thin dispatcher: every subcommand runs the existing tool (the single
source of truth for its behaviour) and returns its exit code, so
`canopy validate` IS `ci/validate.py`, and so on.

  canopy validate [MODEL]                      schema + lint (ci/validate.py)
  canopy quantify [MODEL] [-o OUT] [--samples N [--seed S]]
                                               every event tree -> JSON (ci/quantify.py)
  canopy quantify [MODEL] --target ID [ENGINE ARGS...]
                                               one fault/event tree via the engine
  canopy report [RESULTS] (--metric ID [--model MODEL] | --end-state S ...)
                                               consequence report (ci/consequence_report.py)
  canopy compare BASE.json HEAD.json           risk-delta markdown (ci/compare.py)
  canopy delta [MODEL] [--base REF] [--samples N [--seed S]]
                                               quantify MODEL and MODEL at git REF
                                               (default HEAD) with the same engine,
                                               then compare; the base worktree is
                                               always removed
  canopy appendix [MODEL] [--results RESULTS] [-o OUT] [--revision REV]
                                               report appendices, markdown (ci/appendix.py)
  canopy viz [MODEL] [-o OUT] [--results RESULTS]
                                               HTML viewer (viz/build_viz.py)
  canopy verify [--quick]                      the non-negotiable checks of CLAUDE.md:
                                               cargo test, validator, tooling tests,
                                               property harness (--quick: 12 cases,
                                               no Monte Carlo stage)

MODEL defaults to `model`, RESULTS to `results.json`. The engine binary is
engine/target/release/canopy unless CANOPY_BIN is set (build it with
`cargo build --release --manifest-path engine/Cargo.toml`).
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CI = os.path.join(ROOT, "ci")
SCHEMA = os.path.join(ROOT, "schema", "psa-model.schema.json")


def engine() -> str:
    return os.environ.get("CANOPY_BIN",
                          os.path.join(ROOT, "engine", "target", "release", "canopy"))


def py(script: str, *args) -> int:
    return subprocess.call([sys.executable, os.path.join(ROOT, script), *args])


def cmd_validate(a) -> int:
    return py("ci/validate.py", a.model, SCHEMA)


def cmd_quantify(a, extra) -> int:
    if a.target:
        return subprocess.call([engine(), a.model, a.target, *extra])
    if extra:
        print(f"canopy quantify: engine arguments {extra} need --target",
              file=sys.stderr)
        return 2
    args = [a.model, a.out, "--engine", engine()]
    if a.samples is not None:
        args += ["--samples", str(a.samples)]
        if a.seed is not None:
            args += ["--seed", str(a.seed)]
        if a.sampling:
            args += ["--sampling", a.sampling]
    if a.configurations:
        args += ["--configurations", a.configurations]
    if a.samples is None and (a.seed is not None or a.sampling):
        print("canopy quantify: --seed/--sampling need --samples", file=sys.stderr)
        return 2
    return py("ci/quantify.py", *args)


def cmd_report(a) -> int:
    args = [a.results]
    if a.metric:
        args += ["--metric", a.metric, "--model", a.model]
    for s in a.end_state:
        args += ["--end-state", s]
    if a.json:
        args.append("--json")
    if a.top is not None:
        args += ["--top", str(a.top)]
    return py("ci/consequence_report.py", *args)


def cmd_compare(a) -> int:
    return py("ci/compare.py", a.base, a.head)


def git(*args, cwd=ROOT, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check,
                          capture_output=True, text=True)


def cmd_delta(a) -> int:
    """Quantify the working-tree model and the same model path at a git
    ref with the SAME engine binary (the model is what is being diffed),
    then compare. Mirrors the CI pipeline."""
    # Resolve symlinks on both sides: git reports the resolved top level,
    # and an unresolved model path (e.g. macOS /var -> /private/var) would
    # make the relative path climb out of the worktree and silently point
    # the "base" back at the working-tree model.
    model = os.path.realpath(a.model)
    if not os.path.isdir(model):
        print(f"canopy delta: {a.model} is not a directory", file=sys.stderr)
        return 2
    try:
        top = os.path.realpath(git("rev-parse", "--show-toplevel",
                                   cwd=model).stdout.strip())
        rel = os.path.relpath(model, top)
        git("rev-parse", "--verify", f"{a.base}^{{commit}}", cwd=top)
    except subprocess.CalledProcessError as e:
        print(f"canopy delta: {e.stderr.strip() or e}", file=sys.stderr)
        return 2
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        print(f"canopy delta: {model} is not inside the repository {top}",
              file=sys.stderr)
        return 2
    tmp = tempfile.mkdtemp(prefix="canopy-delta-")
    wt = os.path.join(tmp, "base")
    extra = []
    if a.samples is not None:
        extra = ["--samples", str(a.samples)]
        if a.seed is not None:
            extra += ["--seed", str(a.seed)]
        if a.sampling:
            extra += ["--sampling", a.sampling]
    try:
        r = git("worktree", "add", "--detach", wt, a.base, cwd=top, check=False)
        if r.returncode != 0:
            print(f"canopy delta: git worktree add failed:\n{r.stderr}",
                  file=sys.stderr)
            return 2
        head_json = os.path.join(tmp, "head.json")
        base_json = os.path.join(tmp, "base.json")
        for label, mdir, out in (("head", model, head_json),
                                 (f"base ({a.base})", os.path.join(wt, rel), base_json)):
            if os.path.realpath(mdir) == model and label != "head":
                print("canopy delta: internal: base resolves to the working "
                      "tree", file=sys.stderr)
                return 2
            if not os.path.isdir(mdir):
                print(f"canopy delta: {rel} does not exist at {a.base}",
                      file=sys.stderr)
                return 2
            print(f"== quantify {label}", file=sys.stderr)
            rc = subprocess.call([sys.executable, os.path.join(CI, "quantify.py"),
                                  mdir, out, "--engine", engine(), *extra],
                                 stdout=sys.stderr)
            if rc != 0:
                return rc
        if a.viewer:
            # the viewer with the change painted on it (FR-40)
            rc = subprocess.call([sys.executable, os.path.join(ROOT, "viz", "build_viz.py"),
                                  model, a.viewer, "--results", head_json,
                                  "--base", os.path.join(wt, rel), "--base-results", base_json],
                                 stdout=sys.stderr)
            if rc != 0:
                return rc
        if a.out:
            rc = subprocess.call([sys.executable, os.path.join(CI, "compare.py"),
                                  base_json, head_json], stdout=open(a.out, "w"))
            print(f"delta written to {a.out}", file=sys.stderr)
            return rc
        return subprocess.call([sys.executable, os.path.join(CI, "compare.py"),
                                base_json, head_json])
    finally:
        git("worktree", "remove", "--force", wt, cwd=top, check=False)
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_viz(a) -> int:
    args = [a.model, a.out]
    if a.results:
        args += ["--results", a.results]
    if a.base:
        args += ["--base", a.base]
    if a.base_results:
        args += ["--base-results", a.base_results]
    return py("viz/build_viz.py", *args)


def cmd_appendix(a) -> int:
    args = [a.model, a.results, a.out]
    if a.revision:
        args += ["--revision", a.revision]
    return py("ci/appendix.py", *args)


def cmd_verify(a) -> int:
    """The non-negotiable checks (CLAUDE.md), actually run, in order; stops
    at the first failure and says which."""
    cases = "12" if a.quick else "60"
    steps = [
        ("engine unit tests", ["cargo", "test", "--release", "--manifest-path",
                               os.path.join(ROOT, "engine", "Cargo.toml")]),
        ("engine build", ["cargo", "build", "--release", "--manifest-path",
                          os.path.join(ROOT, "engine", "Cargo.toml")]),
        ("validate the committed model", [sys.executable, os.path.join(CI, "validate.py"),
                                          os.path.join(ROOT, "model"), SCHEMA]),
    ]
    for t in ("test_validate", "test_units", "test_transfers", "test_importance",
              "test_consequence_report", "test_import_riskspectrum", "test_cli",
              "test_sampling", "test_import_mef", "test_configurations",
              "test_appendix", "test_truncation", "test_reorder",
              "test_ccf_uncertainty", "test_importance_uncertainty", "test_viz_diff"):
        steps.append((t, [sys.executable, os.path.join(CI, f"{t}.py")]))
    prop = [sys.executable, os.path.join(CI, "property_test.py"),
            "--cases", cases, "--seed", "20260708"]
    if a.quick:
        prop += ["--mc-samples", "0"]
    steps.append((f"property harness ({cases} cases, seed 20260708"
                  f"{', no Monte Carlo stage' if a.quick else ''})", prop))
    env = dict(os.environ, CANOPY_BIN=engine())
    for i, (name, argv) in enumerate(steps, 1):
        print(f"== [{i}/{len(steps)}] {name}", flush=True)
        rc = subprocess.call(argv, cwd=ROOT, env=env)
        if rc != 0:
            print(f"canopy verify: FAILED at step {i} ({name}), exit {rc}",
                  flush=True)
            return rc
    note = (" (quick: not sufficient before committing engine/ or ci/ "
            "changes; run `canopy verify`)" if a.quick else "")
    print(f"canopy verify: all {len(steps)} steps passed{note}", flush=True)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="canopy", description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(__doc__.split("\n")[5:]))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("validate", help="schema + reference lint")
    p.add_argument("model", nargs="?", default="model")

    p = sub.add_parser("quantify", help="quantify every event tree, or one target")
    p.add_argument("model", nargs="?", default="model")
    p.add_argument("-o", "--out", default="results.json")
    p.add_argument("--target", help="one FT-/ET- id, run directly by the engine")
    p.add_argument("--samples", type=int)
    p.add_argument("--seed", type=int)
    p.add_argument("--sampling", choices=["srs", "lhs"])
    p.add_argument("--configurations", metavar="CFG.json",
                   help="also quantify every named configuration")

    p = sub.add_parser("report", help="consequence report for a metric or end states")
    p.add_argument("results", nargs="?", default="results.json")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--metric")
    g.add_argument("--end-state", action="append", default=[])
    p.add_argument("--model", default="model")
    p.add_argument("--json", action="store_true")
    p.add_argument("--top", type=int)

    p = sub.add_parser("compare", help="risk-delta markdown for two result files")
    p.add_argument("base")
    p.add_argument("head")

    p = sub.add_parser("delta", help="working tree vs a git ref, end to end")
    p.add_argument("model", nargs="?", default="model")
    p.add_argument("--base", default="HEAD")
    p.add_argument("-o", "--out")
    p.add_argument("--viewer", metavar="HTML",
                   help="also write the model viewer with the changes shown")
    p.add_argument("--samples", type=int)
    p.add_argument("--seed", type=int)
    p.add_argument("--sampling", choices=["srs", "lhs"])

    p = sub.add_parser("viz", help="build the HTML viewer")
    p.add_argument("model", nargs="?", default="model")
    p.add_argument("-o", "--out", default="psa-viewer.html")
    p.add_argument("--results")
    p.add_argument("--base", help="base model directory: show what changed")
    p.add_argument("--base-results")

    p = sub.add_parser("appendix", help="report appendices from model + results")
    p.add_argument("model", nargs="?", default="model")
    p.add_argument("--results", default="results.json")
    p.add_argument("-o", "--out", default="appendix.md")
    p.add_argument("--revision")

    p = sub.add_parser("verify", help="run the non-negotiable checks")
    p.add_argument("--quick", action="store_true")

    a, extra = ap.parse_known_args(argv)
    if extra and a.cmd != "quantify":
        ap.error(f"unrecognized arguments: {' '.join(extra)}")
    if a.cmd == "quantify":
        return cmd_quantify(a, extra)
    return {"validate": cmd_validate, "report": cmd_report,
            "compare": cmd_compare, "delta": cmd_delta, "viz": cmd_viz,
            "appendix": cmd_appendix,
            "verify": cmd_verify}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
