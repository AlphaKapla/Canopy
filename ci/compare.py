#!/usr/bin/env python3
"""Compare two quantification results; emit a markdown risk-delta report.

Usage: compare.py <base.json> <head.json> [--configurations BASE_CFG HEAD_CFG]
                  > delta.md
Exit 0 always (reporting, not gating; add thresholds here if you want gates).

If both results carry Monte Carlo draws (quantify.py --samples N --seed S)
the report adds each metric's state-of-knowledge distribution for base and
head, and the distribution of the paired change head_i - base_i. Pairing is
meaningful because the engine's random numbers are keyed by (seed,
quantity ID, iteration): with the same N and seed, iteration i uses the
same sample of every quantity the change did not touch (common random
numbers), so the paired band shows the uncertainty of the change itself
rather than two independent noise clouds.

If both results carry the engine's BDD-exact consequence importance (not
quantified with --prob-only), the report adds, per metric, the basic
events whose model-wide Fussell-Vesely rank or value changed, among the
top TOP_IMPORTANCE of either side (ci/importance.py).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from uncertainty import metric_draws, sampling_settings, summarize  # noqa: E402
import importance  # noqa: E402

MARKER = "<!-- psa-delta -->"
REL_TOL = 1e-9          # ignore numerical noise below this relative change
TOP_CUT_SETS = 10
TOP_IMPORTANCE = 10


def fmt(x: float) -> str:
    return f"{x:.4e}"


def delta_cell(base: float, head: float) -> str:
    if base == head == 0.0:
        return "—"
    if base == 0.0:
        return "**new**"
    rel = (head - base) / base
    if abs(rel) < REL_TOL:
        return "—"
    arrow = "🔺" if rel > 0 else "🔽"
    return f"{arrow} {rel:+.2%} (×{head / base:.3g})"


def band(s: dict) -> str:
    return f"{fmt(s['mean'])} [{fmt(s['p05'])}, {fmt(s['p95'])}]"


def uncertainty_section(base: dict, head: dict) -> list[str]:
    """Markdown rows for the metric distributions (empty if unsampled)."""
    try:
        sb, sh = sampling_settings(base), sampling_settings(head)
    except ValueError as e:
        return [f"_Uncertainty not reported: {e}._", ""]
    if sb is None and sh is None:
        return []
    if sb is None or sh is None:
        side = "base" if sb is None else "head"
        return [f"_Uncertainty not reported: {side} was quantified without "
                f"--samples._", ""]
    db, dh = metric_draws(base), metric_draws(head)
    # Pairing needs the same draws for every unchanged quantity: same N,
    # seed and sampling method (an LHS draw depends on N as well).
    paired = sb == sh
    n, seed, method = sh
    out = [f"### State-of-knowledge uncertainty ({n} samples, seed {seed}, "
           f"{'Latin hypercube' if method == 'lhs' else 'simple random'} sampling)",
           "Mean [5th, 95th percentile], /yr.", ""]
    if paired:
        out += ["| metric | base | head | paired change head − base |",
                "|---|---|---|---|"]
    else:
        out += [f"_Base used {sb[0]} samples / seed {sb[1]} / {sb[2]}: base "
                f"and head are not paired, so no change band is shown._", "",
                "| metric | base | head |", "|---|---|---|"]
    for mid in sorted(set(db) | set(dh)):
        if mid not in db or mid not in dh:
            continue
        row = f"| **{mid}** | {band(summarize(db[mid]))} | {band(summarize(dh[mid]))} |"
        if paired:
            d = [h - b for b, h in zip(db[mid], dh[mid])]
            row += f" {band(summarize(d))} |"
        out.append(row)
    out.append("")
    return out


def importance_section(base: dict, head: dict, metric_ids) -> list[str]:
    """Markdown rows for FV re-ranking per metric (empty when unchanged or
    when either side has no exact importance)."""
    out = []
    for mid in sorted(metric_ids):
        ib, ih = importance.for_metric(base, mid), importance.for_metric(head, mid)
        if ib is None or ih is None:
            continue
        rb = {r["event"]: (k + 1, r) for k, r in enumerate(ib["importance"])}
        rh = {r["event"]: (k + 1, r) for k, r in enumerate(ih["importance"])}
        top = ({e for e, (k, _) in rb.items() if k <= TOP_IMPORTANCE}
               | {e for e, (k, _) in rh.items() if k <= TOP_IMPORTANCE})
        fv = lambda side, e: (side[e][1]["fussell_vesely"] if e in side
                              else None)
        rows = []
        for e in sorted(top, key=lambda e: (rh.get(e, (10**9,))[0], e)):
            b, h = fv(rb, e), fv(rh, e)
            kb = rb[e][0] if e in rb else None
            kh = rh[e][0] if e in rh else None
            same_val = (b is not None and h is not None
                        and abs(h - b) <= REL_TOL * max(abs(b), abs(h), 1e-300))
            if kb == kh and same_val:
                continue
            cell = lambda x: f"{x:.2%}" if x is not None else "—"
            rank = lambda k: f"#{k}" if k is not None else "—"
            rows.append(f"| {e} | {rank(kb)} → {rank(kh)} | {cell(b)} | "
                        f"{cell(h)} |")
        if rows:
            out += [f"### Importance re-ranking — {mid} (BDD-exact Fussell–Vesely)",
                    "| basic event | rank base → head | FV base | FV head |",
                    "|---|---|---|---|"]
            out += rows
            out.append("")
    return out


def importance_uncertainty_section(base: dict, head: dict, metric_ids) -> list[str]:
    """Markdown rows for model-wide importance under uncertainty (FR-37),
    when the results carry importance draws (quantify.py
    --importance-uncertainty K): each event whose Fussell–Vesely
    distribution moved, mean [5th, 95th percentile] base -> head. Paired
    sampling makes an unchanged model's draws identical, so only real
    changes are listed. A note replaces the table when draws are
    incomplete."""
    out = []
    for mid in sorted(metric_ids):
        try:
            uh = importance.uncertainty_for_metric(head, mid)
            ub = importance.uncertainty_for_metric(base, mid)
        except importance.IncompleteDraws as e:
            out += [f"_Importance under uncertainty — {mid}: not shown ({e})._", ""]
            continue
        if not uh:
            continue
        rank = importance.for_metric(head, mid)
        order = [r["event"] for r in (rank or {"importance": []})["importance"]
                 if r["event"] in uh["rows"]]

        def band(u, e):
            fv = ((u or {}).get("rows", {}).get(e) or {}).get("fussell_vesely")
            return fv
        rows = []
        for e in order:
            b, h = band(ub, e), band(uh, e)
            same = (b is not None and h is not None
                    and all(abs(h[k] - b[k]) <= REL_TOL * max(abs(b[k]), abs(h[k]), 1e-300)
                            for k in ("mean", "p05", "p95")))
            if same:
                continue
            cell = lambda x: (f"{x['mean']:.2%} [{x['p05']:.2%}, {x['p95']:.2%}]"
                              if x else "—")
            rows.append(f"| {e} | {cell(b)} | {cell(h)} |")
        if rows:
            n = len(uh["frequency_draws"])
            out += [f"### Importance under uncertainty — {mid} (model-wide, {n} samples)",
                    "Fussell–Vesely mean [5th, 95th percentile], for the events whose "
                    "distribution moved.",
                    "",
                    "| basic event | FV base | FV head |",
                    "|---|---|---|"]
            out += rows
            out.append("")
    return out


def config_totals(cfg: dict) -> dict:
    """{configuration: {metric: model-wide value}} from a quantify.py
    --configurations file."""
    out = {}
    for cid, results in cfg.items():
        tot = out.setdefault(cid, {})
        for r in results.values():
            for m in r.get("metrics", []):
                tot[m["id"]] = tot.get(m["id"], 0.0) + m["value_per_year"]
    return out


def configuration_section(bc: dict, hc: dict, head_base: dict) -> tuple[list[str], bool]:
    """(markdown rows, whether any configuration changed): each named
    configuration's metrics, base -> head, and head's configuration value
    relative to head's base case."""
    tb, th = config_totals(bc), config_totals(hc)
    rows = []
    changed = False
    for cid in sorted(set(tb) | set(th)):
        for mid in sorted(set(tb.get(cid, {})) | set(th.get(cid, {}))):
            b, h = tb.get(cid, {}).get(mid), th.get(cid, {}).get(mid)
            if b is None or h is None or (b != h and (b == 0.0 or abs(h - b) / b >= REL_TOL)):
                changed = True
            rel = (f"×{h / head_base[mid]:.3g}" if h is not None and head_base.get(mid)
                   else "—")
            rows.append(f"| {cid} | {mid} | {fmt(b) if b is not None else 'new'} | "
                        f"{fmt(h) if h is not None else 'removed'} | "
                        f"{delta_cell(b or 0.0, h or 0.0) if b is not None and h is not None else '—'} "
                        f"| {rel} |")
    if not rows:
        return [], False
    return (["### Named configurations (point values)",
             "| configuration | metric | base (/yr) | head (/yr) | change | head vs head base case |",
             "|---|---|---|---|---|---|", *rows, ""], changed)


def cut_key(cs: dict) -> tuple:
    return tuple(sorted(cs["events"]))


def main() -> int:
    base = json.load(open(sys.argv[1]))
    head = json.load(open(sys.argv[2]))
    cfgs = None
    if "--configurations" in sys.argv:
        i = sys.argv.index("--configurations")
        cfgs = (json.load(open(sys.argv[i + 1])), json.load(open(sys.argv[i + 2])))
    out = [MARKER, "## PSA risk-metric delta", ""]

    # ---- aggregate metrics across all event trees --------------------------
    def metric_totals(results: dict) -> dict[str, float]:
        totals: dict[str, float] = {}
        for et in results.values():
            for m in et.get("metrics", []):
                totals[m["id"]] = totals.get(m["id"], 0.0) + m["value_per_year"]
        return totals

    mb, mh = metric_totals(base), metric_totals(head)
    out += ["| metric | base (/yr) | head (/yr) | change |",
            "|---|---|---|---|"]
    for mid in sorted(set(mb) | set(mh)):
        b, h = mb.get(mid, 0.0), mh.get(mid, 0.0)
        out.append(f"| **{mid}** | {fmt(b)} | {fmt(h)} | {delta_cell(b, h)} |")
    out.append("")
    out += uncertainty_section(base, head)
    imp_rows = importance_section(base, head, set(mb) | set(mh))
    out += imp_rows
    out += importance_uncertainty_section(base, head, set(mb) | set(mh))
    cfg_rows, cfg_changed = configuration_section(*cfgs, mh) if cfgs else ([], False)
    out += cfg_rows

    # ---- per-sequence deltas ------------------------------------------------
    changed_rows = []
    for et_id in sorted(set(base) | set(head)):
        bseq = {s["id"]: s for s in base.get(et_id, {}).get("sequences", [])}
        hseq = {s["id"]: s for s in head.get(et_id, {}).get("sequences", [])}
        for sid in sorted(set(bseq) | set(hseq)):
            b = bseq.get(sid, {}).get("frequency_per_year", 0.0)
            h = hseq.get(sid, {}).get("frequency_per_year", 0.0)
            if b == 0.0 and h == 0.0:
                continue
            rel = abs(h - b) / b if b else float("inf")
            if rel >= REL_TOL:
                es = (hseq.get(sid) or bseq.get(sid)).get("end_state", "?")
                changed_rows.append(
                    f"| {et_id} / {sid} | {es} | {fmt(b)} | {fmt(h)} "
                    f"| {delta_cell(b, h)} |")
    if changed_rows:
        out += ["### Changed sequences",
                "| sequence | end state | base (/yr) | head (/yr) | change |",
                "|---|---|---|---|---|"]
        out += changed_rows
        out.append("")

    # ---- cut set diff --------------------------------------------------------
    def all_cuts(results: dict) -> dict[tuple, float]:
        cuts: dict[tuple, float] = {}
        for et_id, et in results.items():
            for s in et.get("sequences", []):
                for cs in s.get("cut_sets", []):
                    k = (et_id, s["id"], cut_key(cs))
                    cuts[k] = cs["frequency_per_year"]
        return cuts

    cb, ch = all_cuts(base), all_cuts(head)
    added = sorted(
        (ch[k], k) for k in ch.keys() - cb.keys())[::-1][:TOP_CUT_SETS]
    removed = sorted(
        (cb[k], k) for k in cb.keys() - ch.keys())[::-1][:TOP_CUT_SETS]
    moved = sorted(
        ((abs(ch[k] - cb[k]), k) for k in ch.keys() & cb.keys()
         if cb[k] and abs(ch[k] - cb[k]) / cb[k] >= REL_TOL),
        reverse=True)[:TOP_CUT_SETS]

    if added or removed or moved:
        out.append("### Cut set changes")
    if added:
        out.append("**New cut sets:**")
        for f, (et, sid, ev) in added:
            out.append(f"- `{{{', '.join(ev)}}}` in {et}/{sid} — {fmt(f)} /yr")
        out.append("")
    if removed:
        out.append("**Removed cut sets:**")
        for f, (et, sid, ev) in removed:
            out.append(f"- `{{{', '.join(ev)}}}` in {et}/{sid} — was "
                       f"{fmt(f)} /yr")
        out.append("")
    if moved:
        out.append("**Re-ranked cut sets:**")
        for _, k in moved:
            et, sid, ev = k
            out.append(f"- `{{{', '.join(ev)}}}` in {et}/{sid}: "
                       f"{fmt(cb[k])} → {fmt(ch[k])} /yr "
                       f"({delta_cell(cb[k], ch[k])})")
        out.append("")

    if not changed_rows and not (added or removed or moved) and not imp_rows \
            and not cfg_changed:
        out.append("_No risk-significant changes: model edit is "
                   "quantitatively neutral._")

    out.append("")
    out.append("_Exact BDD quantification; sequence frequencies include "
               "success-branch terms. Cut sets listed per delete-term "
               "convention. Point values use each quantity's mean; the "
               "uncertainty table (when present) is a Monte Carlo over the "
               "same BDDs._")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
