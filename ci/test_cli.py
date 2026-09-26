#!/usr/bin/env python3
"""Tests for the `canopy` dispatcher (ci/canopy.py).

The dispatcher must add nothing: each subcommand's output is compared
byte for byte with the underlying tool run directly, exit codes must
propagate (a failing validation fails `canopy validate`), and `canopy
delta` is exercised end to end in a throwaway git repository holding a
copy of the demo model with one committed and one uncommitted change —
including that its base worktree is always removed.

`canopy verify` runs the full verification suite and is exercised by
running it (it is how the commits carrying this file were verified), not
from here.

Usage: python ci/test_cli.py
"""
import filecmp
import os
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODEL = os.path.join(ROOT, "model")
SCHEMA = os.path.join(ROOT, "schema", "psa-model.schema.json")
ENGINE = os.environ.get("CANOPY_BIN",
                        os.path.join(ROOT, "engine", "target", "release", "canopy"))


def run(*argv, cwd=ROOT):
    return subprocess.run([sys.executable, *argv], cwd=cwd,
                          capture_output=True, text=True)


def canopy(*args, cwd=ROOT):
    return run(os.path.join(HERE, "canopy.py"), *args, cwd=cwd)


def main() -> int:
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)
        else:
            print(f"  ok  {msg}")

    tmp = tempfile.mkdtemp(prefix="canopy-cli-")
    try:
        # validate: same output, exit code propagates
        a, b = canopy("validate", MODEL), run(os.path.join(HERE, "validate.py"), MODEL, SCHEMA)
        check(a.returncode == b.returncode == 0 and a.stdout == b.stdout,
              "validate = ci/validate.py on the demo model")
        broken = os.path.join(tmp, "broken")
        shutil.copytree(MODEL, broken)
        p = os.path.join(broken, "fault-trees", "ft-rps.yaml")
        ft = yaml.safe_load(open(p))
        ft["fault_trees"]["FT-RPS"]["gates"]["GT-RT-TOP"]["formula"]["or"].append("BE-NOPE")
        yaml.safe_dump(ft, open(p, "w"))
        a = canopy("validate", broken)
        check(a.returncode == 1 and "BE-NOPE" in a.stdout,
              "validate exit code 1 propagates on a broken model")

        # quantify: identical JSON to ci/quantify.py, with and without samples
        for extra in ([], ["--samples", "300", "--seed", "11"]):
            o1, o2 = os.path.join(tmp, "q1.json"), os.path.join(tmp, "q2.json")
            a = canopy("quantify", MODEL, "-o", o1, *extra)
            b = run(os.path.join(HERE, "quantify.py"), MODEL, o2, "--engine", ENGINE, *extra)
            check(a.returncode == b.returncode == 0 and filecmp.cmp(o1, o2, shallow=False),
                  f"quantify {' '.join(extra) or '(point)'} = ci/quantify.py byte for byte")
        c1, c2 = os.path.join(tmp, "c1.json"), os.path.join(tmp, "c2.json")
        a = canopy("quantify", MODEL, "-o", os.path.join(tmp, "q3.json"),
                   "--configurations", c1)
        b = run(os.path.join(HERE, "quantify.py"), MODEL, os.path.join(tmp, "q4.json"),
                "--engine", ENGINE, "--configurations", c2)
        check(a.returncode == b.returncode == 0 and filecmp.cmp(c1, c2, shallow=False),
              "quantify --configurations = ci/quantify.py byte for byte")
        a = canopy("quantify", MODEL, "--target", "ET-SLOCA", "--json", "--mcs-limit", "3")
        b = subprocess.run([ENGINE, MODEL, "ET-SLOCA", "--json", "--mcs-limit", "3"],
                           capture_output=True, text=True)
        check(a.returncode == 0 and a.stdout == b.stdout,
              "quantify --target passes engine arguments through")
        a = canopy("quantify", MODEL, "--target", "ET-NOPE")
        check(a.returncode != 0, "quantify --target: engine failure propagates")
        a = canopy("quantify", MODEL, "--json")
        check(a.returncode == 2 and "need --target" in a.stderr,
              "quantify: engine arguments without --target refused")
        a = canopy("quantify", MODEL, "--seed", "3", "-o", os.path.join(tmp, "x.json"))
        check(a.returncode == 2, "quantify: --seed without --samples refused")

        # report and compare: identical output
        res = os.path.join(tmp, "q1.json")
        for args in (["--metric", "CDF", "--model", MODEL],
                     ["--end-state", "CD", "--json"],
                     ["--metric", "CDF", "--model", MODEL, "--top", "3"]):
            a = canopy("report", res, *args)
            b = run(os.path.join(HERE, "consequence_report.py"), res, *args)
            check(a.returncode == b.returncode == 0 and a.stdout == b.stdout,
                  f"report {' '.join(args[:2])} = ci/consequence_report.py")
        a = canopy("compare", res, res)
        b = run(os.path.join(HERE, "compare.py"), res, res)
        check(a.returncode == b.returncode == 0 and a.stdout == b.stdout
              and "quantitatively neutral" in a.stdout, "compare = ci/compare.py")

        # viz: identical file
        v1, v2 = os.path.join(tmp, "v1.html"), os.path.join(tmp, "v2.html")
        a = canopy("viz", MODEL, "-o", v1, "--results", res)
        b = run(os.path.join(ROOT, "viz", "build_viz.py"), MODEL, v2, "--results", res)
        check(a.returncode == b.returncode == 0 and filecmp.cmp(v1, v2, shallow=False),
              "viz = viz/build_viz.py byte for byte")

        # appendix: identical file
        a1, a2 = os.path.join(tmp, "a1.md"), os.path.join(tmp, "a2.md")
        a = canopy("appendix", MODEL, "--results", res, "-o", a1, "--revision", "abc1234")
        b = run(os.path.join(HERE, "appendix.py"), MODEL, res, a2, "--revision", "abc1234")
        check(a.returncode == b.returncode == 0 and filecmp.cmp(a1, a2, shallow=False),
              "appendix = ci/appendix.py byte for byte")

        # unknown arguments are refused
        check(canopy("validate", MODEL, "--bogus").returncode == 2,
              "unknown arguments refused")

        # delta, end to end, in a throwaway repository
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        shutil.copytree(MODEL, os.path.join(repo, "model"))
        g = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True,
                                      capture_output=True, text=True)
        g("init", "-q")
        g("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
        g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
        a = canopy("delta", os.path.join(repo, "model"), cwd=repo)
        check(a.returncode == 0 and "quantitatively neutral" in a.stdout,
              "delta: unchanged working tree vs HEAD is neutral")
        # uncommitted change: RHR pump A fail-to-start x10 (provenance kept
        # as is: a throwaway copy, never committed to this repository)
        p = os.path.join(repo, "model", "basic-events", "rhr-pumps.yaml")
        be = yaml.safe_load(open(p))
        be["basic_events"]["BE-RHR-PMP-A-FTS"]["failure_model"]["value"]["value"] = 1.2e-2
        yaml.safe_dump(be, open(p, "w"), sort_keys=False)
        out = os.path.join(tmp, "delta.md")
        a = canopy("delta", os.path.join(repo, "model"), "-o", out, cwd=repo)
        md = open(out).read() if os.path.exists(out) else ""
        check(a.returncode == 0 and "🔺" in md and "BE-RHR-PMP-A-FTS" in md,
              "delta: an uncommitted change shows as a CDF increase with re-ranking")
        g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "head")
        a = canopy("delta", os.path.join(repo, "model"), "--base", "HEAD~1", cwd=repo)
        check(a.returncode == 0 and "🔺" in a.stdout,
              "delta --base HEAD~1: the committed change")
        # the temporary directory is reached through a symlink on macOS
        # (/var -> /private/var): the base must still be the base
        link = os.path.join(tmp, "link")
        os.symlink(repo, link)
        a = canopy("delta", os.path.join(link, "model"), "--base", "HEAD~1", cwd=repo)
        check(a.returncode == 0 and "🔺" in a.stdout,
              "delta through a symlinked path still compares against the base")
        a = canopy("delta", MODEL, cwd=repo)
        check(a.returncode == 0, "delta on a model in another repository uses that repository")
        a = canopy("delta", os.path.join(repo, "model"), "--base", "no-such-ref", cwd=repo)
        check(a.returncode == 2, "delta: unknown ref refused")
        wt = g("worktree", "list").stdout.strip().splitlines()
        check(len(wt) == 1, f"delta: base worktrees always removed ({len(wt)} listed)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("canopy CLI: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
