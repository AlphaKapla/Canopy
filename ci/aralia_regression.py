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

Usage: aralia_regression.py <aralia-xml-dir> [--reference PATH]
         [--engine PATH] [--timeout 120] [--mem-gib 4] [--summary PATH]
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
    out = tempfile.TemporaryFile()
    t0 = time.monotonic()
    p = subprocess.Popen(cmd, stdout=out, stderr=subprocess.DEVNULL,
                         preexec_fn=limits)
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
    ap.add_argument("--summary", help="also append the table to this file "
                                      "(e.g. $GITHUB_STEP_SUMMARY)")
    a = ap.parse_args()
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
                [a.engine, d, "FT-MAIN", "--json", "--prob-only"], a.timeout,
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
    lines = [f"### Aralia regression ({len(ref['trees'])} trees vs SCRAM reference, "
             f"tolerance {tol:g}, timeout {a.timeout} s, {a.mem_gib} GiB cap)", "",
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


if __name__ == "__main__":
    sys.exit(main())
