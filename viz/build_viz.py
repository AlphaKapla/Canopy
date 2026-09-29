#!/usr/bin/env python3
"""Build a self-contained interactive HTML viewer from the PSA model.

Usage: build_viz.py <model-dir> <out.html> [--results results.json]
                    [--base <base-model-dir> [--base-results base.json]]

The viewer is a derived artifact (like cut sets): regenerate it, don't
commit it. --results takes the JSON produced by ci/quantify.py and adds
sequence frequencies and risk metrics to the display.

With --base, the viewer shows the head model with what changed relative
to the base model (a pull request's base, say): every basic event, gate,
fault tree, house event, event tree and sequence added, removed or changed,
with the fields that changed and their base values; with --base-results,
also each sequence frequency and risk metric base -> head. A frequency or
metric counts as changed at a relative difference of 1e-9 or more (the
threshold of ci/compare.py), so an edit that merely re-rounds unrelated
results does not flag them; probabilities and frequencies are compared
only when both sides have results (a note says so otherwise). Without
--base the embedded model data is exactly as before and the page shows
no diff.

Named configurations (model.yaml) are diff entities too (FR-48): a
configuration added, removed, or with its label, house-event or
parameter overrides changed.

Parameters and CCF groups are entities of their own (FR-45): each
parameter lists the basic events, CCF groups and initiating events that
use it, each basic event its parameters and CCF groups, and the diff
reports a parameter's value, unit, uncertainty, label or provenance and a
CCF group's model, members, total, factors, testing, factor uncertainty,
label or provenance changed — compared exactly: they are inputs.

Risk metrics are shown model-wide: summed over every event tree's results
(V&V D-21: the header used to show the first tree's value). Truncated
results (quantify.py --truncated, FR-42) show each sequence frequency and
metric as bounds [lower, upper], rounded outward (ci/bounds.py); in diff
mode a bounded value counts as changed when either bound moved by 1e-9
relative or more.
"""
import argparse
import glob
import json
import math
import os
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ci"))
import bounds  # noqa: E402


def resolve(q, params):
    if isinstance(q, dict) and "param" in q:
        return params[q["param"]]["value"]
    return q["value"]


def be_probability(fm, params):
    t = fm["type"]
    if t == "probability":
        return resolve(fm["value"], params)
    if t == "rate-mission":
        return 1.0 - math.exp(
            -resolve(fm["rate"], params) * resolve(fm["mission_time"], params)
        )
    if t == "rate-repair":
        rm = resolve(fm["rate"], params) * resolve(fm["mttr"], params)
        return rm / (1.0 + rm)
    if t == "rate-periodic-test":
        rt = resolve(fm["rate"], params) * resolve(fm["test_interval"], params)
        return 0.0 if rt == 0.0 else 1.0 - (1.0 - math.exp(-rt)) / rt
    return None


REL_TOL = 1e-9     # the relative change ci/compare.py treats as a change

PARAM_FIELDS = ("value", "unit", "uncertainty", "label", "provenance")
CONFIG_FIELDS = ("label", "house_events", "parameters")
CCF_FIELDS = ("model", "members", "total_probability", "factors", "testing",
              "factor_uncertainty", "label", "provenance")


def param_refs(x) -> set:
    """Every parameter a failure model, quantity or group references."""
    if isinstance(x, dict):
        out = {x["param"]} if isinstance(x.get("param"), str) else set()
        for v in x.values():
            out |= param_refs(v)
        return out
    if isinstance(x, list):
        return set().union(*(param_refs(v) for v in x)) if x else set()
    return set()


def build_data(model_dir: str, results: dict, texts: bool = False) -> dict:
    """The viewer's model data (embedded as JSON in the page). Sequence
    frequencies carry bounds and display texts when the results are
    truncated, or when `texts` asks for them (a diff against a truncated
    side, so both sides compare like for like)."""
    bounded = texts or bounds.any_truncated(results)
    params = yaml.safe_load(
        open(os.path.join(model_dir, "parameters.yaml")))["parameters"]

    manifest = yaml.safe_load(open(os.path.join(model_dir, "model.yaml")))
    data = {
        "model_id": manifest["model"]["id"],
        "model_name": manifest["model"].get("name", ""),
        "basic_events": {},
        "house_events": {},
        "gates": {},
        "fault_trees": {},
        "event_trees": {},
        "metrics": [],
        "has_results": bool(results),
        "parameters": {},
        "ccf_groups": {},
        "configurations": {
            cid: {k: (c or {}).get(k) for k in CONFIG_FIELDS if (c or {}).get(k) is not None}
            for cid, c in sorted((manifest.get("configurations") or {}).items())},
    }
    used_by: dict[str, set] = {}

    # Probabilities: the engine's own values when results are given (after
    # CCF expansion: a CCF member shows its independent part Q1); otherwise
    # the failure model's closed form, computed here (pre-CCF for members).
    engine_p = {}
    for r in results.values():
        engine_p.update(r.get("basic_event_probabilities", {}))
    ccf_members = set()
    group_of: dict[str, list] = {}
    cpath = os.path.join(model_dir, "ccf-groups.yaml")
    if os.path.exists(cpath):
        for gid, g in sorted(((yaml.safe_load(open(cpath)) or {}).get("ccf_groups") or {}).items()):
            ccf_members.update(g.get("members", []))
            for mbr in g.get("members", []):
                group_of.setdefault(mbr, []).append(gid)
            data["ccf_groups"][gid] = {k: g[k] for k in ("label", *CCF_FIELDS[:-1], "provenance")
                                       if k in g}
            for q in param_refs(g.get("total_probability")):
                used_by.setdefault(q, set()).add(gid)
    data["probability_source"] = "engine" if engine_p else "viewer"
    for p in sorted(glob.glob(os.path.join(model_dir, "basic-events/*.yaml"))):
        for bid, be in yaml.safe_load(open(p))["basic_events"].items():
            prob = (engine_p.get(bid) if engine_p
                    else be_probability(be["failure_model"], params))
            data["basic_events"][bid] = {
                "label": be["label"],
                "p": prob,
                "pre_ccf": (not engine_p) and bid in ccf_members,
                "model_type": be["failure_model"]["type"],
                "system": be.get("system", ""),
                "provenance": be.get("provenance", {}),
                "params": sorted(param_refs(be["failure_model"])
                                 | param_refs(be.get("uncertainty"))),
                "ccf_groups": group_of.get(bid, []),
            }
            for q in data["basic_events"][bid]["params"]:
                used_by.setdefault(q, set()).add(bid)

    for hid, he in yaml.safe_load(
            open(os.path.join(model_dir, "house-events.yaml")))[
            "house_events"].items():
        data["house_events"][hid] = {
            "label": he["label"], "default": he["default"]}

    for p in sorted(glob.glob(os.path.join(model_dir, "fault-trees/*.yaml"))):
        for ft_id, ft in yaml.safe_load(open(p))["fault_trees"].items():
            data["fault_trees"][ft_id] = {
                "label": ft["label"], "top_gate": ft["top_gate"]}
            for gid, g in ft["gates"].items():
                data["gates"][gid] = {
                    "label": g["label"], "formula": g["formula"],
                    "tree": ft_id}

    for p in sorted(glob.glob(os.path.join(model_dir, "event-trees/*.yaml"))):
        et = yaml.safe_load(open(p))["event_tree"]
        seq_freq = {}
        seq_trunc = False
        for et_res in results.values():
            if et_res.get("id") == et["id"]:
                seq_trunc = bounds.is_truncated(et_res)
                for s in et_res.get("sequences", []):
                    seq_freq[s["id"]] = bounds.seq_interval(s)

        def freq_fields(iv):
            if iv is None:
                return {"freq": None}
            if not bounded:
                return {"freq": iv[0]}
            return {"freq": None if seq_trunc else iv[0], "freq_bounds": list(iv),
                    "freq_text": bounds.fmt_interval(*iv, seq_trunc, digits=2)}
        ie = et.get("initiating_event")
        data["event_trees"][et["id"]] = {
            "label": et["label"],
            # A transfer-only tree has no initiator of its own; the viewer
            # renders its frequency as "—".
            "ie": {
                "id": ie["id"], "label": ie["label"],
                "freq": ie["frequency"]["value"],
            } if ie else {
                "id": "(transfer only)",
                "label": "entered through transfers from other event trees",
                "freq": None,
            },
            # mapping order in the YAML = column order of the tree
            "fe_order": list(et["functional_events"].keys()),
            "functional_events": et["functional_events"],
            "sequences": {
                sid: {**seq, **freq_fields(seq_freq.get(sid))}
                for sid, seq in et["sequences"].items()
            },
        }

    for p in sorted(glob.glob(os.path.join(model_dir, "event-trees/*.yaml"))):
        ie = (yaml.safe_load(open(p)).get("event_tree") or {}).get("initiating_event") or {}
        for q in param_refs(ie.get("frequency")):
            used_by.setdefault(q, set()).add(ie["id"])
    for pid, par in sorted(params.items()):
        data["parameters"][pid] = {**{k: par[k] for k in PARAM_FIELDS if k in par},
                                   "used_by": sorted(used_by.get(pid, ()))}

    # model-wide metrics, summed over event trees (V&V D-21)
    labels = {}
    for et_id in sorted(results):
        for m in results[et_id].get("metrics", []):
            labels.setdefault(m["id"], m.get("label", ""))
    trunc = bounds.any_truncated(results)
    for mid, (lo, hi) in bounds.metric_totals(results).items():
        if trunc:
            data["metrics"].append({"id": mid, "label": labels[mid],
                                    "value_lower_bound": lo, "value_upper_bound": hi,
                                    "text": bounds.fmt_interval(lo, hi, True, digits=3)})
        else:
            data["metrics"].append({"id": mid, "label": labels[mid], "value_per_year": lo})
    if trunc:
        data["truncation"] = bounds.method_note(results)
    return data


def _changed_num(b, h) -> bool:
    if b is None or h is None:
        return b != h
    return abs(h - b) > REL_TOL * max(abs(b), abs(h), 1e-300)


def diff_data(base: dict, head: dict) -> dict:
    """What changed from base to head, per entity kind: {id: {"status":
    "added" | "removed" | "changed", "fields": [...], "base": base entry}}
    (unchanged entities are absent), plus sequence frequency and metric
    changes. Deterministic: ids sorted, fields in a fixed order."""
    # Probabilities and frequencies are compared only like for like: with
    # results on one side only, one side's probabilities are the engine's
    # (after CCF expansion) and the other's the closed forms, and one side
    # has no frequencies at all.
    same_p = base["probability_source"] == head["probability_source"]
    both_res = base["has_results"] and head["has_results"]
    notes = []
    if not same_p:
        notes.append("basic-event probabilities not compared (quantification results "
                     "given for one side only)")
    if not both_res:
        notes.append("frequencies and metrics not compared (quantification results "
                     "missing on one side)")

    def compare(kind, fields, numeric=()):
        out = {}
        b, h = base[kind], head[kind]
        for i in sorted(set(b) | set(h)):
            if i not in h:
                out[i] = {"status": "removed", "fields": [], "base": b[i]}
            elif i not in b:
                out[i] = {"status": "added", "fields": []}
            else:
                ch = [f for f in fields
                      if (_changed_num(b[i].get(f), h[i].get(f)) if f in numeric
                          else b[i].get(f) != h[i].get(f))]
                if ch:
                    out[i] = {"status": "changed", "fields": ch, "base": b[i]}
        return out

    d = {
        "basic_events": compare("basic_events",
                                (["p"] if same_p else []) +
                                ["model_type", "label", "system", "provenance"],
                                numeric=("p",)),
        "gates": compare("gates", ["formula", "label", "tree"]),
        "parameters": compare("parameters", list(PARAM_FIELDS)),
        "ccf_groups": compare("ccf_groups", list(CCF_FIELDS)),
        "configurations": compare("configurations", list(CONFIG_FIELDS)),
        "fault_trees": compare("fault_trees", ["top_gate", "label"]),
        "house_events": compare("house_events", ["default", "label"]),
        "event_trees": {},
        "metrics": [],
    }
    for et in sorted(set(base["event_trees"]) | set(head["event_trees"])):
        b, h = base["event_trees"].get(et), head["event_trees"].get(et)
        if h is None:
            d["event_trees"][et] = {"status": "removed", "fields": [], "base": b,
                                    "sequences": {}}
            continue
        if b is None:
            d["event_trees"][et] = {"status": "added", "fields": [], "sequences": {}}
            continue
        fields = [f for f in ("label", "fe_order", "functional_events")
                  if b.get(f) != h.get(f)]
        if _changed_num(b["ie"].get("freq"), h["ie"].get("freq")) or \
                {k: v for k, v in b["ie"].items() if k != "freq"} != \
                {k: v for k, v in h["ie"].items() if k != "freq"}:
            fields.append("ie")
        seqs = {}
        bs, hs = b["sequences"], h["sequences"]
        for sid in sorted(set(bs) | set(hs)):
            if sid not in hs:
                seqs[sid] = {"status": "removed", "fields": [], "base": bs[sid]}
            elif sid not in bs:
                seqs[sid] = {"status": "added", "fields": []}
            else:
                sf = [f for f in ("path", "end_state", "transfer", "house_events")
                      if bs[sid].get(f) != hs[sid].get(f)]
                if both_res and ("freq_bounds" in bs[sid] or "freq_bounds" in hs[sid]):
                    fb, fh = bs[sid].get("freq_bounds"), hs[sid].get("freq_bounds")
                    if fb is None or fh is None or any(_changed_num(x, y) for x, y in zip(fb, fh)):
                        sf.append("freq_text")
                elif both_res and _changed_num(bs[sid].get("freq"), hs[sid].get("freq")):
                    sf.append("freq")
                if sf:
                    seqs[sid] = {"status": "changed", "fields": sf, "base": bs[sid]}
        if fields or seqs:
            d["event_trees"][et] = {"status": "changed", "fields": fields,
                                    "base": {k: v for k, v in b.items() if k != "sequences"},
                                    "sequences": seqs}
    bm = {m["id"]: bounds.metric_interval(m) for m in base["metrics"]}
    hm = {m["id"]: bounds.metric_interval(m) for m in head["metrics"]}
    bb, hb = "truncation" in base, "truncation" in head
    for mid in sorted(set(bm) | set(hm)):
        b, h = bm.get(mid), hm.get(mid)
        if bb or hb:
            text = lambda v, side: (bounds.fmt_interval(*v, side, digits=3)
                                    if v is not None else None)
            d["metrics"].append({
                "id": mid, "base_text": text(b, bb), "head_text": text(h, hb),
                "changed": both_res and (b is None or h is None
                                         or any(_changed_num(x, y) for x, y in zip(b, h)))})
            continue
        b = b[0] if b is not None else None
        h = h[0] if h is not None else None
        d["metrics"].append({"id": mid, "base": b, "head": h,
                             "changed": both_res and _changed_num(b, h)})
    d["summary"] = {
        status: sum(1 for kind in ("basic_events", "gates", "fault_trees", "house_events",
                                   "event_trees", "parameters", "ccf_groups",
                                   "configurations")
                    for e in d[kind].values() if e["status"] == status)
                + sum(1 for e in d["event_trees"].values()
                      for q in e["sequences"].values() if q["status"] == status)
        for status in ("added", "removed", "changed")}
    d["notes"] = notes
    d["base_model_id"] = base["model_id"]
    d["base_has_results"] = base["has_results"]
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the self-contained model viewer.")
    ap.add_argument("model_dir")
    ap.add_argument("out_path")
    ap.add_argument("--results", help="ci/quantify.py results for the model")
    ap.add_argument("--base", help="base model directory: show what changed")
    ap.add_argument("--base-results", help="ci/quantify.py results for the base model")
    a = ap.parse_args()
    if a.base_results and not a.base:
        ap.error("--base-results needs --base")
    model_dir, out_path = a.model_dir, a.out_path
    results = json.load(open(a.results)) if a.results else {}
    base_results = json.load(open(a.base_results)) if a.base_results else {}
    texts = bool(a.base) and (bounds.any_truncated(results) or bounds.any_truncated(base_results))
    data = build_data(model_dir, results, texts)
    if a.base:
        data["diff"] = diff_data(build_data(a.base, base_results, texts), data)

    template = open(
        os.path.join(os.path.dirname(__file__), "template.html")).read()
    html = template.replace(
        "/*__MODEL_JSON__*/null", json.dumps(data, sort_keys=True))
    with open(out_path, "w") as f:
        f.write(html)
    size = os.path.getsize(out_path) // 1024
    print(f"built {out_path} ({size} KiB, "
          f"{len(data['gates'])} gates, "
          f"{len(data['basic_events'])} basic events, "
          f"{len(data['event_trees'])} event trees)"
          + (f"; diff vs {a.base}: {data['diff']['summary']}" if a.base else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
