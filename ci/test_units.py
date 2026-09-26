#!/usr/bin/env python3
"""Exhaustive dimensional-rule test (docs/model-format.md, "Units"):
engine, validator and a hand-written table must agree on every unit
combination of every quantity group.

For each group — probability, rate-mission, rate-repair,
rate-periodic-test (each with inline units, and rate-mission again with
the rate taken from a parameter), CCF total probability (inline and from a
parameter), initiating-event frequency — and each unit (pair) out of the
six units, a minimal model is written and:
  * the validator reports exactly one error naming the offending field iff
    the hand table says the combination is invalid, none otherwise;
  * the engine refuses to load the model iff the combination is invalid;
  * for a valid combination the engine's P(top) equals the closed form of
    the failure model (probability 3e-3; rate 2e-3 with time 3 in the same
    base: 1 − e^(−λT), λτ/(1 + λτ), 1 − (1 − e^(−λT))/(λT)).

The hand table (VALID below) is written independently of both
implementations: probabilities and CCF totals per_demand | dimensionless,
frequencies per_year, rate × time (per_hour, hour) or (per_year, year).

Usage: python ci/test_units.py [--engine PATH]
"""
import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UNITS = ["per_hour", "per_year", "per_demand", "hour", "year", "dimensionless"]
PROV = {"source": "test", "justification": "unit rule case"}

VALID = {
    "probability": {("per_demand",), ("dimensionless",)},
    "ccf-total": {("per_demand",), ("dimensionless",)},
    "initiating-event": {("per_year",)},
    "rate-mission": {("per_hour", "hour"), ("per_year", "year")},
    "rate-repair": {("per_hour", "hour"), ("per_year", "year")},
    "rate-periodic-test": {("per_hour", "hour"), ("per_year", "year")},
}
TIME_FIELD = {"rate-mission": "mission_time", "rate-repair": "mttr",
              "rate-periodic-test": "test_interval"}
LAM, T = 2e-3, 3.0
CLOSED = {
    "probability": 3e-3,
    "rate-mission": 1 - math.exp(-LAM * T),
    "rate-repair": LAM * T / (1 + LAM * T),
    "rate-periodic-test": 1 - (1 - math.exp(-LAM * T)) / (LAM * T),
}


def model(case):
    """Files of a minimal model for one case (group, units, via_param)."""
    group, units, via_param = case
    params = {}
    bes = {"BE-Y": {"label": "unit case y", "failure_model": {
        "type": "probability", "value": {"value": 1e-3, "unit": "per_demand"}},
        "provenance": PROV}}
    if group == "probability":
        fm = {"type": "probability", "value": {"value": 3e-3, "unit": units[0]}}
    elif group in TIME_FIELD:
        rate = {"value": LAM, "unit": units[0]}
        if via_param:
            params["PAR-RATE"] = {"label": "rate", "value": LAM, "unit": units[0],
                                  "provenance": PROV}
            rate = {"param": "PAR-RATE"}
        fm = {"type": group, "rate": rate,
              TIME_FIELD[group]: {"value": T, "unit": units[1]}}
    else:
        fm = {"type": "probability", "value": {"value": 3e-3, "unit": "per_demand"}}
    bes["BE-X"] = {"label": "unit case x", "failure_model": fm, "provenance": PROV}
    files = {
        "model.yaml": {
            "schema_version": "0.1.0",
            "model": {"id": "UNIT-TEST", "name": "unit rule case",
                      "risk_metrics": [{"id": "CDF", "label": "unit case cd",
                                        "end_states": ["CD"]}]},
            "includes": {"parameters": ["parameters.yaml"],
                         "house_events": ["house-events.yaml"],
                         "basic_events": ["basic-events/*.yaml"],
                         "fault_trees": ["fault-trees/*.yaml"],
                         "event_trees": ["event-trees/*.yaml"]}},
        "house-events.yaml": {"house_events": {}},
        "basic-events/be.yaml": {"basic_events": bes},
        "fault-trees/ft.yaml": {"fault_trees": {"FT-T": {
            "label": "unit case t", "top_gate": "GT-T", "gates": {
                "GT-T": {"label": "unit case t", "formula": {"or": ["BE-X", "BE-Y"]}}}}}},
        "event-trees/et.yaml": {"event_tree": {
            "id": "ET-T", "label": "unit case t",
            "initiating_event": {
                "id": "IE-T", "label": "unit case t", "provenance": PROV,
                "frequency": {"value": 1e-2, "unit": units[0]
                              if group == "initiating-event" else "per_year"}},
            "functional_events": {"FE-1": {"label": "unit case f", "top_gate": "GT-T"}},
            "sequences": {"SEQ-1": {"path": {"FE-1": "success"}, "end_state": "OK"},
                          "SEQ-2": {"path": {"FE-1": "failure"}, "end_state": "CD"}}}},
    }
    if group == "ccf-total":
        total = {"value": 1e-3, "unit": units[0]}
        if via_param:
            params["PAR-QT"] = {"label": "unit case qt", "value": 1e-3, "unit": units[0],
                                "provenance": PROV}
            total = {"param": "PAR-QT"}
        files["ccf-groups.yaml"] = {"ccf_groups": {"CCF-G": {
            "label": "unit case g", "model": "alpha-factor", "members": ["BE-X", "BE-Y"],
            "total_probability": total,
            "factors": {"alpha_1": 0.95, "alpha_2": 0.05}, "provenance": PROV}}}
        files["model.yaml"]["includes"]["ccf_groups"] = ["ccf-groups.yaml"]
    files["parameters.yaml"] = {"parameters": params}
    return files


def cases():
    out = []
    for g in ("probability", "ccf-total", "initiating-event"):
        for u in UNITS:
            out.append((g, (u,), False))
        if g == "ccf-total":
            out += [(g, (u,), True) for u in UNITS]
    for g in TIME_FIELD:
        for r in UNITS:
            for t in UNITS:
                out.append((g, (r, t), False))
    out += [("rate-mission", (r, t), True) for r in UNITS for t in UNITS]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", os.path.join(ROOT, "engine/target/release/canopy")))
    a = ap.parse_args()
    failures, n_valid = [], 0
    all_cases = cases()
    for case in all_cases:
        group, units, via_param = case
        valid = units in VALID[group]
        n_valid += valid
        label = f"{group} {'/'.join(units)}{' (via parameter)' if via_param else ''}"
        d = tempfile.mkdtemp(prefix="psa-units-")
        try:
            for rel, obj in model(case).items():
                os.makedirs(os.path.dirname(os.path.join(d, rel)) or d, exist_ok=True)
                with open(os.path.join(d, rel), "w") as f:
                    yaml.safe_dump(obj, f, sort_keys=False)
            v = subprocess.run([sys.executable, os.path.join(HERE, "validate.py"), d,
                                os.path.join(ROOT, "schema", "psa-model.schema.json")],
                               capture_output=True, text=True)
            errors = [l for l in v.stdout.splitlines() if l.startswith("ERROR")]
            target = "ET-T" if group == "initiating-event" else "FT-T"
            e = subprocess.run([a.engine, d, target, "--json", "--prob-only"],
                               capture_output=True, text=True)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        if valid:
            if errors or v.returncode != 0:
                failures.append(f"{label}: validator rejected a valid case: {errors}")
            if e.returncode != 0:
                failures.append(f"{label}: engine refused a valid case: {e.stderr}")
            elif group in CLOSED:
                p_top = json.loads(e.stdout)["probability"]
                px = CLOSED[group]
                want = 1 - (1 - px) * (1 - 1e-3)          # BE-X or BE-Y
                if abs(p_top - want) > 1e-12 * want:
                    failures.append(f"{label}: P(top) {p_top}, closed form {want}")
        else:
            field = {"probability": "value", "ccf-total": "total_probability",
                     "initiating-event": "frequency"}.get(group)
            if len(errors) != 1 or v.returncode != 1:
                failures.append(f"{label}: validator gave {len(errors)} errors "
                                f"(expected exactly 1): {errors}")
            elif field and field not in errors[0]:
                failures.append(f"{label}: validator error does not name {field}: "
                                f"{errors[0]}")
            elif group in TIME_FIELD and not any(
                    f in errors[0] for f in ("rate", TIME_FIELD[group])):
                failures.append(f"{label}: validator error names no field: {errors[0]}")
            if e.returncode == 0:
                failures.append(f"{label}: engine accepted an invalid case")
            elif "Traceback" in e.stderr or "panicked" in e.stderr:
                failures.append(f"{label}: engine crashed: {e.stderr}")
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print(f"units: {len(all_cases)} combinations ({n_valid} valid) — engine, "
          f"validator and the hand table agree; valid rate models match their "
          f"closed forms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
