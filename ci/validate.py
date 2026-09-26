#!/usr/bin/env python3
"""Validate a git-native PSA model: strict YAML, JSON Schema, reference lint.

Usage: validate.py <model-dir> <schema.json>
Exit 0 = clean (warnings allowed), 1 = errors.
"""
import glob
import json
import os
import sys

import yaml
from jsonschema import Draft202012Validator

ERRORS: list[str] = []
WARNINGS: list[str] = []


def err(msg: str) -> None:
    ERRORS.append(msg)


def warn(msg: str) -> None:
    WARNINGS.append(msg)


class StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys (git-merge damage
    detector: a bad conflict resolution often leaves two copies of a key)."""


def _no_dup(loader, node, deep=False):
    mapping = {}
    for k_node, v_node in node.value:
        key = loader.construct_object(k_node, deep=deep)
        if key in mapping:
            raise yaml.YAMLError(
                f"duplicate key {key!r} at line {k_node.start_mark.line + 1}"
            )
        mapping[key] = loader.construct_object(v_node, deep=deep)
    return mapping


StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dup
)


def load(path: str):
    try:
        with open(path) as f:
            return yaml.load(f, Loader=StrictLoader)
    except yaml.YAMLError as e:
        err(f"{path}: YAML parse failure: {e}")
        return None


def schema_check(schema: dict, data, path: str, defname: str) -> None:
    sub = {"$ref": f"#/$defs/{defname}", "$defs": schema["$defs"]}
    for e in Draft202012Validator(sub).iter_errors(data):
        loc = "/".join(map(str, e.path)) or "<root>"
        err(f"{path}: schema: {loc}: {e.message}")


# Allowed relative mismatch between a point value and the mean of a fully
# specified distribution; must equal MEAN_REL_TOL in engine/src/uncertainty.rs.
MEAN_REL_TOL = 1e-2


def dist_mean(unc: dict, point: float):
    """Mean of an uncertainty block given its quantity's point value, or
    None if the block is malformed (the schema check reports that)."""
    d = unc.get("distribution")
    try:
        if d == "lognormal":
            return point            # lognormal: the point value IS the mean
        if d == "beta":
            return unc["alpha"] / (unc["alpha"] + unc["beta"])
        if d == "gamma":
            return unc["shape"] * unc["scale"]
        if d == "uniform":
            return 0.5 * (unc["lower"] + unc["upper"])
    except (KeyError, TypeError, ZeroDivisionError):
        return None
    return None


def check_uncertainty(schema: dict, unc, point, ctx: str,
                      schema_covered: bool = True) -> None:
    """Semantic checks on one `uncertainty:` block (the engine refuses the
    same conditions when sampling; see docs/quantification.md). Blocks in
    files without a schema definition (ccf-groups.yaml) are schema-checked
    here; elsewhere the file schema has already reported shape errors."""
    if not schema_covered:
        before = len(ERRORS)
        schema_check(schema, unc, ctx, "uncertainty")
        if len(ERRORS) > before:
            return
    if not isinstance(unc, dict) or dist_mean(unc, 1.0) is None \
            or not isinstance(point, (int, float)):
        return
    d = unc["distribution"]
    if d == "lognormal" and not point > 0:
        err(f"{ctx}: lognormal needs a positive point value (the mean), "
            f"got {point}")
        return
    if d == "uniform" and not unc["lower"] < unc["upper"]:
        err(f"{ctx}: uniform needs lower < upper")
        return
    m = dist_mean(unc, point)
    if m is not None and abs(m - point) > MEAN_REL_TOL * max(abs(point), abs(m)):
        err(f"{ctx}: point value {point:g} is not the mean {m:g} of its {d} "
            f"distribution (relative tolerance {MEAN_REL_TOL:g}); the point "
            f"value must be the distribution mean")


def formula_refs(formula):
    """Yield every ID referenced by a structured formula."""
    if isinstance(formula, str):
        yield formula
        return
    (op, args), = formula.items()
    if op == "not":
        yield from formula_refs(args)
    elif op == "atleast":
        for a in args["of"]:
            yield from formula_refs(a)
    else:
        for a in args:
            yield from formula_refs(a)


def main() -> int:
    model_dir, schema_path = sys.argv[1], sys.argv[2]
    schema = json.load(open(schema_path))

    def mfiles(pattern: str):
        return sorted(glob.glob(os.path.join(model_dir, pattern)))

    # ---- load + schema-validate every file --------------------------------
    basic_events: dict[str, tuple[dict, str]] = {}
    gates: dict[str, tuple[dict, str]] = {}
    fault_trees: dict[str, tuple[dict, str]] = {}
    event_trees: dict[str, tuple[dict, str]] = {}
    params: dict[str, dict] = {}
    house: dict[str, dict] = {}
    ccf_members: list[tuple[str, str, str]] = []  # (group, member, file)

    def merge(target: dict, items: dict, kind: str, path: str):
        for k, v in items.items():
            if k in target:
                err(f"{path}: duplicate {kind} ID {k} "
                    f"(also in {target[k][1]})")
            else:
                target[k] = (v, path)

    for p in mfiles("basic-events/*.yaml"):
        d = load(p)
        if d is None:
            continue
        schema_check(schema, d, p, "basicEventsFile")
        merge(basic_events, d.get("basic_events", {}), "basic event", p)

    for p in mfiles("fault-trees/*.yaml"):
        d = load(p)
        if d is None:
            continue
        schema_check(schema, d, p, "faultTreeFile")
        for ft_id, ft in d.get("fault_trees", {}).items():
            merge(fault_trees, {ft_id: ft}, "fault tree", p)
            merge(gates, ft.get("gates", {}), "gate", p)

    for p in mfiles("event-trees/*.yaml"):
        d = load(p)
        if d is None:
            continue
        schema_check(schema, d, p, "eventTreeFile")
        et = d.get("event_tree", {})
        if "id" in et:
            merge(event_trees, {et["id"]: et}, "event tree", p)

    pfile = os.path.join(model_dir, "parameters.yaml")
    d = load(pfile)
    if d:
        schema_check(schema, d, pfile, "parametersFile")
        params = d.get("parameters", {}) or {}
    hfile = os.path.join(model_dir, "house-events.yaml")
    d = load(hfile)
    if d:
        house = d.get("house_events", {})
    cfile = os.path.join(model_dir, "ccf-groups.yaml")
    if os.path.exists(cfile):
        d = load(cfile)
        for gid, g in (d or {}).get("ccf_groups", {}).items():
            members = g.get("members", [])
            for m in members:
                ccf_members.append((gid, m, cfile))
            if len(members) < 2:
                err(f"{cfile}:{gid}: CCF group needs >= 2 members")
            model_t = g.get("model")
            factors = g.get("factors", {})
            if model_t == "alpha-factor":
                alphas = [v for k, v in factors.items()
                          if k.startswith("alpha_")]
                if len(alphas) != len(members):
                    err(f"{cfile}:{gid}: alpha-factor group of size "
                        f"{len(members)} needs alpha_1..alpha_{len(members)}")
                elif abs(sum(alphas) - 1.0) > 1e-2:
                    err(f"{cfile}:{gid}: alpha factors sum to "
                        f"{sum(alphas):.4f}, expected 1.0")
            elif model_t == "beta-factor":
                b = factors.get("beta")
                if b is None or not (0.0 < b < 1.0):
                    err(f"{cfile}:{gid}: beta-factor needs 0 < beta < 1")
            tp = g.get("total_probability")
            if isinstance(tp, dict) and "uncertainty" in tp:
                check_uncertainty(schema, tp["uncertainty"], tp.get("value"),
                                  f"{cfile}:{gid}/total_probability",
                                  schema_covered=False)

    manifest = load(os.path.join(model_dir, "model.yaml")) or {}
    metrics = manifest.get("model", {}).get("risk_metrics", [])
    metric_states = {s for m in metrics for s in m.get("end_states", [])}

    # ---- reference lint ----------------------------------------------------
    def resolve(ref: str, ctx: str):
        if ref.startswith("BE-") and ref not in basic_events:
            err(f"{ctx}: dangling basic event reference {ref}")
        elif ref.startswith("GT-") and ref not in gates:
            err(f"{ctx}: dangling gate reference {ref}")
        elif ref.startswith("HE-") and ref not in house:
            err(f"{ctx}: dangling house event reference {ref}")
        elif ref.startswith("PAR-") and ref not in params:
            err(f"{ctx}: dangling parameter reference {ref}")

    def param_refs(obj, ctx: str):
        if isinstance(obj, dict):
            if set(obj) == {"param"}:
                resolve(obj["param"], ctx)
            else:
                for v in obj.values():
                    param_refs(v, ctx)

    for be_id, (be, path) in basic_events.items():
        param_refs(be.get("failure_model", {}), f"{path}:{be_id}")

    # ---- uncertainty semantics ---------------------------------------------
    for pid, pdef in params.items():
        if isinstance(pdef, dict) and "uncertainty" in pdef:
            check_uncertainty(schema, pdef["uncertainty"], pdef.get("value"),
                              f"{pfile}:{pid}")

    def input_uncertain(q) -> bool:
        if not isinstance(q, dict):
            return False
        if "param" in q:
            pd = params.get(q["param"])
            return isinstance(pd, dict) and "uncertainty" in pd
        return "uncertainty" in q

    def param_value(q):
        if isinstance(q, dict) and "param" in q:
            pd = params.get(q["param"])
            return pd.get("value") if isinstance(pd, dict) else None
        return q.get("value") if isinstance(q, dict) else None

    ccf_member_ids = {m for _, m, _ in ccf_members}
    for be_id, (be, path) in basic_events.items():
        ctx = f"{path}:{be_id}"
        fm = be.get("failure_model", {}) or {}
        inputs = {k: v for k, v in fm.items() if k != "type"}
        for field, q in inputs.items():
            if isinstance(q, dict) and "uncertainty" in q:
                check_uncertainty(schema, q["uncertainty"], q.get("value"),
                                  f"{ctx}/{field}")
        if "uncertainty" in be:
            if fm.get("type") != "probability":
                err(f"{ctx}: an event-level `uncertainty` is only defined for "
                    f"failure_model type `probability` (got "
                    f"{fm.get('type')!r}); put the distribution on the rate's "
                    f"parameter or inline quantity instead")
            elif any(input_uncertain(q) for q in inputs.values()):
                err(f"{ctx}: uncertainty given twice (on the event and on its "
                    f"input); keep one")
            elif be_id in ccf_member_ids:
                err(f"{ctx}: event-level `uncertainty` on a CCF group member is "
                    f"never used (the member's probability is derived from the "
                    f"group's total_probability); put the distribution there")
            else:
                check_uncertainty(schema, be["uncertainty"],
                                  param_value(fm.get("value")), ctx)

    for g_id, (g, path) in gates.items():
        for ref in formula_refs(g.get("formula", {})):
            resolve(ref, f"{path}:{g_id}")

    for ft_id, (ft, path) in fault_trees.items():
        if ft.get("top_gate") not in gates:
            err(f"{path}:{ft_id}: top_gate {ft.get('top_gate')} undefined")

    for gid, m, path in ccf_members:
        if m not in basic_events:
            err(f"{path}:{gid}: CCF member {m} is not a defined basic event")

    for et_id, (et, path) in event_trees.items():
        freq = et.get("initiating_event", {}).get("frequency", {})
        if isinstance(freq, dict) and "uncertainty" in freq:
            check_uncertainty(schema, freq["uncertainty"], freq.get("value"),
                              f"{path}:{et_id}/initiating_event")
        fes = et.get("functional_events", {})
        for fe_id, fe in fes.items():
            if fe.get("top_gate") not in gates:
                err(f"{path}:{et_id}/{fe_id}: top_gate "
                    f"{fe.get('top_gate')} undefined")
        seen_paths = {}
        for seq_id, seq in et.get("sequences", {}).items():
            ctx = f"{path}:{seq_id}"
            for fe in seq.get("path", {}):
                if fe not in fes:
                    err(f"{ctx}: path references undefined {fe}")
            missing = set(fes) - set(seq.get("path", {}))
            if missing:
                err(f"{ctx}: path does not resolve {sorted(missing)}")
            key = tuple(sorted(seq.get("path", {}).items()))
            if key in seen_paths:
                err(f"{ctx}: duplicate sequence path "
                    f"(same as {seen_paths[key]})")
            seen_paths[key] = seq_id
            for he in seq.get("house_events", {}):
                resolve(he, ctx)
            es = seq.get("end_state", "")
            if seq.get("transfer"):
                if seq["transfer"] not in event_trees:
                    warn(f"{ctx}: transfer target {seq['transfer']} "
                         f"not defined in this model")
            elif es != "OK" and es not in metric_states:
                warn(f"{ctx}: end state {es} is not mapped to any "
                     f"risk metric in model.yaml")

    # ---- gate cycle detection ---------------------------------------------
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {g: WHITE for g in gates}

    def dfs(g: str, stack: list[str]) -> None:
        color[g] = GRAY
        stack.append(g)
        for ref in formula_refs(gates[g][0].get("formula", {})):
            if ref.startswith("GT-") and ref in gates:
                if color[ref] == GRAY:
                    i = stack.index(ref)
                    err("gate cycle: " + " -> ".join(stack[i:] + [ref]))
                elif color[ref] == WHITE:
                    dfs(ref, stack)
        stack.pop()
        color[g] = BLACK

    sys.setrecursionlimit(100_000)
    for g in gates:
        if color[g] == WHITE:
            dfs(g, [])

    # ---- orphan detection (warning) ----------------------------------------
    reachable: set[str] = set()

    def reach(ref: str) -> None:
        if ref in reachable or not ref.startswith("GT-") or ref not in gates:
            return
        reachable.add(ref)
        for r in formula_refs(gates[ref][0].get("formula", {})):
            reach(r)

    for ft_id, (ft, _) in fault_trees.items():
        reach(ft.get("top_gate", ""))
    for et_id, (et, _) in event_trees.items():
        for fe in et.get("functional_events", {}).values():
            reach(fe.get("top_gate", ""))
    for g in gates:
        if g not in reachable:
            warn(f"orphaned gate {g} (not reachable from any top gate)")

    used_bes: set[str] = set()
    for g in reachable:
        for r in formula_refs(gates[g][0].get("formula", {})):
            if r.startswith("BE-"):
                used_bes.add(r)
    for be in basic_events:
        if be not in used_bes:
            warn(f"orphaned basic event {be} (never referenced)")

    # ---- report -------------------------------------------------------------
    for w in WARNINGS:
        print(f"WARNING: {w}")
    for e in ERRORS:
        print(f"ERROR:   {e}")
    n_ent = (len(basic_events) + len(gates) + len(fault_trees)
             + len(event_trees) + len(params) + len(house))
    print(f"validated {n_ent} entities: "
          f"{len(ERRORS)} error(s), {len(WARNINGS)} warning(s)")
    return 1 if ERRORS else 0


if __name__ == "__main__":
    sys.exit(main())
