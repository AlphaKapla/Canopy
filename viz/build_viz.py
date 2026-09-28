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
"""
import argparse
import glob
import json
import math
import os
import sys

import yaml


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


def build_data(model_dir: str, results: dict) -> dict:
    """The viewer's model data (embedded as JSON in the page)."""
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
    }

    # Probabilities: the engine's own values when results are given (after
    # CCF expansion: a CCF member shows its independent part Q1); otherwise
    # the failure model's closed form, computed here (pre-CCF for members).
    engine_p = {}
    for r in results.values():
        engine_p.update(r.get("basic_event_probabilities", {}))
    ccf_members = set()
    cpath = os.path.join(model_dir, "ccf-groups.yaml")
    if os.path.exists(cpath):
        for g in (yaml.safe_load(open(cpath)) or {}).get("ccf_groups", {}).values():
            ccf_members.update(g.get("members", []))
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
            }

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
        for et_res in results.values():
            if et_res.get("id") == et["id"]:
                for s in et_res.get("sequences", []):
                    seq_freq[s["id"]] = s["frequency_per_year"]
                data["metrics"] += et_res.get("metrics", [])
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
                sid: {**seq, "freq": seq_freq.get(sid)}
                for sid, seq in et["sequences"].items()
            },
        }

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
                if both_res and _changed_num(bs[sid].get("freq"), hs[sid].get("freq")):
                    sf.append("freq")
                if sf:
                    seqs[sid] = {"status": "changed", "fields": sf, "base": bs[sid]}
        if fields or seqs:
            d["event_trees"][et] = {"status": "changed", "fields": fields,
                                    "base": {k: v for k, v in b.items() if k != "sequences"},
                                    "sequences": seqs}
    bm, hm = {}, {}
    for m in base["metrics"]:
        bm[m["id"]] = bm.get(m["id"], 0.0) + m["value_per_year"]
    for m in head["metrics"]:
        hm[m["id"]] = hm.get(m["id"], 0.0) + m["value_per_year"]
    for mid in sorted(set(bm) | set(hm)):
        d["metrics"].append({"id": mid, "base": bm.get(mid), "head": hm.get(mid),
                             "changed": both_res and _changed_num(bm.get(mid), hm.get(mid))})
    d["summary"] = {
        status: sum(1 for kind in ("basic_events", "gates", "fault_trees", "house_events",
                                   "event_trees")
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
    data = build_data(model_dir, results)
    if a.base:
        base_results = json.load(open(a.base_results)) if a.base_results else {}
        data["diff"] = diff_data(build_data(a.base, base_results), data)

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
