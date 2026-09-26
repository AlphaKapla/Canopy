#!/usr/bin/env python3
"""Build a self-contained interactive HTML viewer from the PSA model.

Usage: build_viz.py <model-dir> <out.html> [--results results.json]

The viewer is a derived artifact (like cut sets): regenerate it, don't
commit it. --results takes the JSON produced by ci/quantify.py and adds
sequence frequencies and risk metrics to the display.
"""
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


def main() -> int:
    model_dir, out_path = sys.argv[1], sys.argv[2]
    results = {}
    if "--results" in sys.argv:
        results = json.load(open(sys.argv[sys.argv.index("--results") + 1]))

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
          f"{len(data['event_trees'])} event trees)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
