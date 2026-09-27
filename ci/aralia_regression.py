#!/usr/bin/env python3
"""Aralia regression on every push: quantify the industrial fault trees of
the Aralia suite with Canopy alone and compare P(top) with SCRAM's
reference values (ci/fixtures/aralia-scram-reference.json).

Gating on correctness, reporting on performance: every tree in the
reference must be quantified (under the memory cap and timeout) and agree
within the reference tolerance, or the run fails; wall time, BDD arena
size and peak resident memory are reported (markdown, and optionally the
GitHub job summary) so trends are visible per commit without gating on
machine speed.

With --truncated CUTOFF [--order-limit K] (FR-34) every tree is quantified
by truncated minimal cut sets instead, and the verdict is whether SCRAM's
exact P(top) lies within Canopy's bounds [lower, upper]; the table shows
the bounds and their relative width. Trees missing from the reference are
also run and their bounds reported (no verdict). Non-coherent trees must
be refused (truncation is defined for coherent logic only): a refusal is
the expected outcome for them and a failure for any other tree.

Usage: aralia_regression.py <aralia-xml-dir> [--reference PATH]
         [--engine PATH] [--timeout 120] [--mem-gib 4] [--summary PATH]
         [--order dfs|rdfs] [--reorder] [--truncated CUTOFF [--order-limit K]]

With --reorder the engine sifts the variable order dynamically (FR-35);
results must agree exactly as without it.
"""
import argparse
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def run_measured(cmd, timeout, mem_bytes):
    """(stdout, seconds, peak RSS in MiB or None, failure or None), with
    this child's own resource usage (os.wait4), an address-space cap where
    the platform enforces one, and a timeout."""
    def limits():
        try:
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        except (ValueError, OSError):
            pass
    out, err = tempfile.TemporaryFile(), tempfile.TemporaryFile()
    t0 = time.monotonic()
    p = subprocess.Popen(cmd, stdout=out, stderr=err, preexec_fn=limits)
    killed = []
    timer = threading.Timer(timeout, lambda: (killed.append(1), p.kill()))
    timer.start()
    _, status, ru = os.wait4(p.pid, 0)
    timer.cancel()
    p.returncode = status
    dt = time.monotonic() - t0
    rss = ru.ru_maxrss / (1024 * 1024 if sys.platform == "darwin" else 1024)
    if killed:
        return None, dt, rss, "timeout"
    if status != 0:
        err.seek(0)
        if b"needs coherent logic" in err.read():
            return None, dt, rss, "refused (non-coherent)"
        return None, dt, rss, "crash/oom"
    out.seek(0)
    return out.read().decode(), dt, rss, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("xml_dir")
    ap.add_argument("--reference", default=os.path.join(
        HERE, "fixtures", "aralia-scram-reference.json"))
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", os.path.join(ROOT, "engine/target/release/canopy")))
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--mem-gib", type=int, default=4)
    ap.add_argument("--order", choices=["dfs", "rdfs"], default="dfs",
                    help="engine variable order (results must not depend on it)")
    ap.add_argument("--summary", help="also append the table to this file "
                                      "(e.g. $GITHUB_STEP_SUMMARY)")
    ap.add_argument("--reorder", action="store_true",
                    help="dynamic variable reordering (results must not depend on it)")
    ap.add_argument("--truncated", type=float, metavar="CUTOFF",
                    help="truncated quantification: check SCRAM's P(top) lies "
                         "within the bounds")
    ap.add_argument("--order-limit", type=int, metavar="K",
                    help="with --truncated: drop cut sets of more than K events")
    a = ap.parse_args()
    if a.order_limit is not None and a.truncated is None:
        ap.error("--order-limit needs --truncated")
    if a.truncated is not None:
        return truncated(a)
    ref = json.load(open(a.reference))
    tol = ref["relative_tolerance"]
    rows, bad = [], []
    for name, p_ref in sorted(ref["trees"].items()):
        xml = os.path.join(a.xml_dir, f"{name}.xml")
        d = tempfile.mkdtemp(prefix="aralia-")
        try:
            imp = subprocess.run([sys.executable, os.path.join(HERE, "import_mef.py"),
                                  xml, d], capture_output=True, text=True)
            if imp.returncode != 0:
                bad.append(f"{name}: import failed: {imp.stderr.strip()}")
                rows.append((name, "—", "—", "—", "import failed", "—", "—", "—"))
                continue
            out, dt, rss, fail = run_measured(
                [a.engine, d, "FT-MAIN", "--json", "--prob-only", "--order", a.order,
                 *(["--reorder"] if a.reorder else [])],
                a.timeout,
                a.mem_gib << 30)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        if fail:
            bad.append(f"{name}: {fail} after {dt:.1f} s")
            rows.append((name, "—", f"{p_ref:.6e}", "—", fail, f"{dt:.1f}", "—",
                         f"{rss:.0f}" if rss else "—"))
            continue
        j = json.loads(out)
        p = j["probability"]
        rel = abs(p - p_ref) / max(abs(p), abs(p_ref), 1e-300)
        ok = rel <= tol
        if not ok:
            bad.append(f"{name}: P(top) {p:.9e} vs SCRAM {p_ref:.6e} (rel {rel:.1e})")
        rows.append((name, f"{p:.6e}", f"{p_ref:.6e}", f"{rel:.1e}",
                     "AGREE" if ok else "DISAGREE", f"{dt:.1f}", f"{j['bdd_nodes']}",
                     f"{rss:.0f}" if rss else "—"))
    lines = [f"### Aralia regression, variable order {a.order}"
             f"{' + dynamic reordering' if a.reorder else ''} ({len(ref['trees'])} trees "
             f"vs SCRAM reference, tolerance {tol:g}, timeout {a.timeout} s, "
             f"{a.mem_gib} GiB cap)", "",
             "| tree | Canopy P(top) | SCRAM | rel. diff | verdict | time (s) | BDD nodes | peak RSS (MiB) |",
             "|---|---|---|---|---|---|---|---|"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    agree = sum(1 for r in rows if r[4] == "AGREE")
    lines += ["", f"**{agree} of {len(rows)} agree**"
              + ("" if not bad else f"; {len(bad)} problem(s): " + "; ".join(bad)),
              f"_Not in the reference (SCRAM could not quantify them): "
              f"{', '.join(ref.get('not_quantified_by_scram', [])) or 'none'}._"]
    text = "\n".join(lines)
    print(text)
    if a.summary:
        with open(a.summary, "a") as f:
            f.write(text + "\n")
    return 1 if bad else 0


def truncated(a) -> int:
    ref = json.load(open(a.reference))
    names = sorted(set(ref["trees"]) | set(ref.get("not_quantified_by_scram", [])))
    rows, bad = [], []
    extra = ["--order-limit", str(a.order_limit)] if a.order_limit is not None else []
    for name in names:
        p_ref = ref["trees"].get(name)
        xml = os.path.join(a.xml_dir, f"{name}.xml")
        d = tempfile.mkdtemp(prefix="aralia-")
        try:
            imp = subprocess.run([sys.executable, os.path.join(HERE, "import_mef.py"),
                                  xml, d], capture_output=True, text=True)
            if imp.returncode != 0:
                bad.append(f"{name}: import failed: {imp.stderr.strip()}")
                rows.append((name, "—", "—", "—", "—", "import failed", "—", "—"))
                continue
            noncoh = any(k in open(os.path.join(dp, f)).read()
                         for dp, _, fs in os.walk(os.path.join(d, "fault-trees"))
                         for f in fs for k in ("not:", "xor:"))
            out, dt, rss, fail = run_measured(
                [a.engine, d, "FT-MAIN", "--json", "--mcs-limit", "0", "--order", a.order,
                 "--truncated", repr(a.truncated), *extra],
                a.timeout, a.mem_gib << 30)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        ref_s = f"{p_ref:.6e}" if p_ref is not None else "—"
        if noncoh or fail == "refused (non-coherent)":
            if not (noncoh and fail == "refused (non-coherent)"):
                bad.append(f"{name}: {'non-coherent' if noncoh else 'coherent'} tree, "
                           f"outcome {fail or 'quantified'}")
            rows.append((name, "—", "—", ref_s, "—", fail or "not refused", f"{dt:.1f}",
                         f"{rss:.0f}" if rss else "—"))
            continue
        if fail:
            if p_ref is not None:
                bad.append(f"{name}: {fail} after {dt:.1f} s")
            rows.append((name, "—", "—", ref_s, "—", fail, f"{dt:.1f}",
                         f"{rss:.0f}" if rss else "—"))
            continue
        j = json.loads(out)
        lo, up = j["probability_lower_bound"], j["probability_upper_bound"]
        width = (up - lo) / up if up > 0 else 0.0
        if p_ref is None:
            verdict = "bounds only"
        else:
            tol = ref["relative_tolerance"] * max(abs(p_ref), 1e-300)
            verdict = "WITHIN" if lo - tol <= p_ref <= up + tol else "OUTSIDE"
            if verdict == "OUTSIDE":
                bad.append(f"{name}: SCRAM {p_ref:.6e} outside [{lo:.6e}, {up:.6e}]")
        rows.append((name, f"{lo:.6e}", f"{up:.6e}", ref_s, f"{width:.1e}", verdict,
                     f"{dt:.1f}", f"{rss:.0f}" if rss else "—"))
    lim = f", order <= {a.order_limit}" if a.order_limit is not None else ""
    lines = [f"### Aralia truncated quantification, cut-off {a.truncated:g}{lim}, "
             f"variable order {a.order} (timeout {a.timeout} s, {a.mem_gib} GiB cap)", "",
             "| tree | lower bound | upper bound | SCRAM exact | rel. width | verdict "
             "| time (s) | peak RSS (MiB) |", "|---|---|---|---|---|---|---|---|"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    within = sum(1 for r in rows if r[5] == "WITHIN")
    refused = sum(1 for r in rows if r[5] == "refused (non-coherent)")
    lines += ["", f"**{within} of {len(ref['trees']) - refused} coherent reference trees "
              f"within the bounds**; {refused} non-coherent tree(s) refused as designed"
              + ("" if not bad else f"; {len(bad)} problem(s): " + "; ".join(bad))]
    text = "\n".join(lines)
    print(text)
    if a.summary:
        with open(a.summary, "a") as f:
            f.write(text + "\n")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
