#!/usr/bin/env python3
"""Cross-verify the engine's uncertainty special functions against SciPy.

On demand (needs `pip install scipy`; not a CI dependency, like SCRAM for
crosscheck_scram.py). Runs engine/examples/special_functions_grid.rs and
compares every value with an independent implementation (SciPy's
ndtri / gammaincinv / gammainccinv / betaincinv / gammaln, which wrap
Cephes and Boost). Exit 1 if any value exceeds its tolerance.

Tolerances are relative. For u > 0.5 the beta quantile is x = 1 - y with y
solved on the upper tail; 1 - x is compared with SciPy's y after
discounting 4 ulps of 1.0 (x cannot carry more absolute precision than that
near 1).
Reference values below 1e-300 count as zero.

Usage: crosscheck_special_functions.py [--grid FILE]
"""
import argparse
import subprocess
import sys

TOL = {"N": 1e-14, "L": 1e-13, "G": 1e-11, "B": 1e-10}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid", help="pre-computed grid (default: run the example)")
    a = ap.parse_args()
    try:
        from scipy.special import (betaincinv, gammaincinv, gammainccinv,
                                   gammaln, ndtri)
    except ImportError:
        print("scipy is required: pip install scipy", file=sys.stderr)
        return 2
    if a.grid:
        lines = open(a.grid).read().splitlines()
    else:
        p = subprocess.run(
            ["cargo", "run", "--release", "--quiet", "--manifest-path",
             "engine/Cargo.toml", "--example", "special_functions_grid"],
            capture_output=True, text=True)
        if p.returncode != 0:
            print(p.stderr, file=sys.stderr)
            return 1
        lines = p.stdout.splitlines()

    worst: dict[str, tuple[float, str]] = {}
    count: dict[str, int] = {}
    for line in lines:
        kind, *v = line.split()
        v = [float(x) for x in v]
        if kind == "N":
            u, got = v
            ref = float(ndtri(u))
            err = abs(got) if ref == 0 else abs(got - ref) / abs(ref)
        elif kind == "L":
            x, got = v
            ref = float(gammaln(x))
            err = abs(got - ref) / max(abs(ref), 1.0)
        elif kind == "G":
            k, u, got = v
            ref = float(gammainccinv(k, 1 - u) if u > 0.5 else gammaincinv(k, u))
            err = ((0.0 if got < 1e-300 else 1.0) if ref < 1e-300
                   else abs(got - ref) / ref)
        elif kind == "B":
            al, be, u, got = v
            if u > 0.5:
                ref, got = float(betaincinv(be, al, 1 - u)), 1.0 - got
                # x = 1 - y carries at most ~1e-16 absolute precision near 1:
                # discount 4 ulps of 1.0 before taking the relative error.
                err = max(0.0, abs(got - ref) - 4 * 2.220446049250313e-16) / ref
            else:
                ref = float(betaincinv(al, be, u))
                err = ((0.0 if got < 1e-300 else 1.0) if ref < 1e-300
                       else abs(got - ref) / ref)
        else:
            continue
        count[kind] = count.get(kind, 0) + 1
        if err >= worst.get(kind, (-1.0, ""))[0]:
            worst[kind] = (err, line)

    names = {"N": "normal quantile", "L": "ln gamma",
             "G": "gamma quantile", "B": "beta quantile"}
    failed = False
    for k in "NLGB":
        e, line = worst[k]
        ok = e <= TOL[k]
        failed |= not ok
        print(f"{names[k]:16s} {count[k]:5d} points  worst rel err "
              f"{e:.2e} (tol {TOL[k]:.0e})  {'PASS' if ok else 'FAIL'}  "
              f"[{line}]")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
