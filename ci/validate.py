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
    except FileNotFoundError:
        return None             # reported by file_index_problems
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


# Dimensional rules: one table, identical to engine/src/model.rs
# `unit_problem` and cross-checked on every combination by
# ci/test_units.py. The tools do no unit arithmetic, so a rate and a time
# must share a time base; mixed bases are refused, never converted.
RATE_BASE = {"per_hour": "hour", "per_year": "year"}
FM_FIELDS = {
    "probability": ["value"], "frequency": ["value"],
    "rate-mission": ["rate", "mission_time"],
    "rate-repair": ["rate", "mttr"],
    "rate-periodic-test": ["rate", "test_interval"],
}


def unit_problem(kind: str, fields: list):
    """Why the units of one quantity group are invalid, or None. `kind`:
    a failure-model type, `ccf-total` or `initiating-event`; `fields`:
    (field, unit or None) in the failure model's operand order."""
    for f, u in fields:
        if u is None:
            return f"{f} has no unit"
    u = [x for _, x in fields]

    def one_of(allowed):
        if u[0] not in allowed:
            return (f"{fields[0][0]} must be {' or '.join(allowed)} "
                    f"(got {u[0]})")
        return None
    if kind in ("probability", "ccf-total"):
        return one_of(["per_demand", "dimensionless"])
    if kind in ("frequency", "initiating-event"):
        return one_of(["per_year"])
    if kind in ("rate-mission", "rate-repair", "rate-periodic-test"):
        rate, time = u[0], u[1]
        if rate not in RATE_BASE:
            return f"{fields[0][0]} must be per_hour or per_year (got {rate})"
        if time not in ("hour", "year"):
            return f"{fields[1][0]} must be hour or year (got {time})"
        if RATE_BASE[rate] != time:
            return (f"{fields[0][0]} ({rate}) and {fields[1][0]} ({time}) are "
                    f"on different time bases; use per_hour with hour or "
                    f"per_year with year (units are never converted)")
        return None
    return f"unknown quantity group {kind}"


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


def partition_problems(fe_order: list, sequences: dict,
                       max_examples: int = 3) -> list:
    """Structural partition check of an event tree's sequence table.

    Each sequence path is a cube over the functional-event outcomes
    (success/failure fixed, bypassed = either). The table partitions the
    outcome space {success, failure}^n exactly when the cubes are pairwise
    disjoint and cover it; then Σ P(sequence) = 1 for ANY fault-tree logic
    (in the absence of per-sequence house-event overrides, which change
    the logic per sequence). Returns messages for overlapping pairs and for
    uncovered outcomes (up to `max_examples` of each, plus totals).
    Sequences whose paths are malformed must be filtered out by the caller.
    """
    cubes = [(sid, {fe: seq["path"][fe] for fe in fe_order
                    if seq["path"][fe] != "bypassed"})
             for sid, seq in sequences.items()]
    n = len(fe_order)

    def outcome(fixed: dict) -> str:
        return ", ".join(f"{fe}={fixed.get(fe, 'success')}" for fe in fe_order)

    out = []
    overlaps = []
    for i in range(len(cubes)):
        for j in range(i + 1, len(cubes)):
            (si, ci), (sj, cj) = cubes[i], cubes[j]
            if ci == cj:
                continue            # identical paths: reported as duplicates
            if all(ci[fe] == cj[fe] for fe in ci.keys() & cj.keys()):
                overlaps.append((si, sj, {**ci, **cj}))
    for si, sj, both in overlaps[:max_examples]:
        out.append(f"sequences {si} and {sj} overlap: both cover the outcome "
                   f"({outcome(both)}); the table must partition the outcome "
                   f"space")
    if len(overlaps) > max_examples:
        out.append(f"... {len(overlaps) - max_examples} more overlapping "
                   f"sequence pair(s)")

    # Uncovered outcomes: depth-first split over the functional events,
    # pruned as soon as one compatible cube leaves every remaining event
    # free (the whole sub-space is covered).
    uncovered: list = []
    n_uncovered = 0

    def walk(k: int, fixed: dict, live: list) -> None:
        nonlocal n_uncovered
        if not live:
            n_uncovered += 2 ** (n - k)
            if len(uncovered) < max_examples:
                uncovered.append(dict(fixed))
            return
        rest = fe_order[k:]
        if any(not (c.keys() & set(rest)) for c in live):
            return
        fe = fe_order[k]
        for val in ("success", "failure"):
            fixed[fe] = val
            walk(k + 1, fixed,
                 [c for c in live if c.get(fe, val) == val])
            del fixed[fe]

    walk(0, {}, [c for _, c in cubes])
    for u in uncovered:
        free = [fe for fe in fe_order if fe not in u]
        tail = (f" (for any outcome of {', '.join(free)})" if free else "")
        out.append("no sequence covers the outcome ("
                   + ", ".join(f"{fe}={u[fe]}" for fe in fe_order if fe in u)
                   + ")" + tail)
    if n_uncovered:
        out.append(f"{n_uncovered} of {2 ** n} functional-event outcome "
                   f"combination(s) are covered by no sequence; their "
                   f"frequency would be silently missing from every metric")
    return out


# What the loaders actually read (engine/src/model.rs, this file,
# ci/quantify.py): fixed top-level files plus every top-level *.yaml file of
# the entity directories. Anything else on disk is ignored by every tool.
ROOT_FILES_REQUIRED = ["model.yaml", "parameters.yaml", "house-events.yaml"]
# keys of model.yaml (it has no JSON Schema; V&V D-22)
MANIFEST_KEYS = {"schema_version", "model", "includes", "configurations"}
MANIFEST_MODEL_KEYS = {"id", "name", "description", "scope", "risk_metrics"}
ROOT_FILES_OPTIONAL = ["ccf-groups.yaml"]
ENTITY_DIRS = {"basic-events": True, "fault-trees": True, "event-trees": False}


def file_index_problems(model_dir: str, manifest) -> tuple[list, list]:
    """(errors, warnings) for the model's file layout: required files and
    directories exist; no file the loaders would silently skip sits where a
    model file is expected (e.g. `basic-events/pumps.yml`, a sub-directory);
    and the manifest's `includes` index names exactly the files that are
    loaded — every loaded file matched by some pattern, every literal path
    present, nothing indexed that no tool reads."""
    errors, warnings = [], []
    loaded = set()
    for f in ROOT_FILES_REQUIRED:
        if not os.path.isfile(os.path.join(model_dir, f)):
            errors.append(f"required file {f} is missing")
        elif f != "model.yaml":
            loaded.add(f)
    for f in ROOT_FILES_OPTIONAL:
        if os.path.isfile(os.path.join(model_dir, f)):
            loaded.add(f)
    for f in sorted(os.listdir(model_dir)):
        if f.lower().endswith((".yml", ".yaml")) and f not in (
                ROOT_FILES_REQUIRED + ROOT_FILES_OPTIONAL):
            errors.append(f"{f}: not a model file name; no tool reads it "
                          f"(entity files go in {', '.join(ENTITY_DIRS)}/)")
    for d, required in ENTITY_DIRS.items():
        full = os.path.join(model_dir, d)
        if not os.path.isdir(full):
            if required:
                errors.append(f"required directory {d}/ is missing")
            continue
        for f in sorted(os.listdir(full)):
            rel = f"{d}/{f}"
            if f.startswith("."):
                # The engine's directory scan reads hidden *.yaml files;
                # Python's glob (this validator, quantify.py) skips them.
                if f.endswith(".yaml"):
                    errors.append(f"{rel}: hidden model file (the engine would "
                                  f"load it, the validator would not check it)")
                continue
            if os.path.isdir(os.path.join(full, f)):
                errors.append(f"{rel}/: sub-directories are not read; its "
                              f"files would be silently ignored")
            elif f.endswith(".yaml"):
                loaded.add(rel)
            else:
                errors.append(f"{rel}: only *.yaml files are loaded; this file "
                              f"would be silently ignored")
    if not isinstance(manifest, dict) or "includes" not in manifest:
        warnings.append("model.yaml has no `includes` index")
        return errors, warnings
    inc = manifest.get("includes") or {}
    indexed = set()
    for kind, patterns in inc.items():
        for pat in patterns or []:
            hits = {os.path.relpath(p, model_dir).replace(os.sep, "/")
                    for p in glob.glob(os.path.join(model_dir, pat))}
            if not glob.has_magic(pat) and not hits:
                errors.append(f"includes/{kind}: indexed file {pat} does "
                              f"not exist")
            indexed |= hits
    for f in sorted(loaded - indexed):
        errors.append(f"{f}: loaded but not indexed in model.yaml includes")
    for f in sorted(indexed - loaded):
        errors.append(f"{f}: indexed in model.yaml includes but never loaded")
    return errors, warnings


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: validate.py <model-dir> <schema.json>", file=sys.stderr)
        return 2
    model_dir, schema_path = sys.argv[1], sys.argv[2]
    # a missing directory or schema is an error to report, not a traceback
    # (V&V D-30)
    for path, what, ok in ((model_dir, "model directory", os.path.isdir),
                           (schema_path, "schema", os.path.isfile)):
        if not ok(path):
            print(f"ERROR:   {what} {path} not found")
            print("validated 0 entities: 1 error(s), 0 warning(s)")
            return 1
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
                # fractions: a negative factor with a compensating one
                # above 1 still sums to 1 (V&V D-29)
                for k, v in sorted(factors.items()):
                    if k.startswith("alpha_") and not (
                            isinstance(v, (int, float)) and not isinstance(v, bool)
                            and 0.0 <= v <= 1.0):
                        err(f"{cfile}:{gid}: factor {k} = {v!r} outside [0,1]")
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
                if "testing" in g:
                    warn(f"{cfile}:{gid}: `testing` has no effect on a "
                         f"beta-factor group (Q_1 = (1-beta)Q_t, Q_n = beta*Q_t "
                         f"under any testing scheme)")
            fu = g.get("factor_uncertainty")
            if fu is not None:
                ctx = f"{cfile}:{gid}/factor_uncertainty"
                if model_t not in ("alpha-factor", "beta-factor"):
                    err(f"{ctx}: only alpha-factor and beta-factor groups have factors")
                elif not isinstance(fu, dict) or fu.get("distribution") != "dirichlet":
                    err(f"{ctx}: must be {{distribution: dirichlet, concentration: N}}")
                else:
                    for k in sorted(set(fu) - {"distribution", "concentration"}):
                        err(f"{ctx}: unknown field {k!r}")
                    n_c = fu.get("concentration")
                    if (not isinstance(n_c, (int, float)) or isinstance(n_c, bool)
                            or not (0 < n_c < float("inf"))):
                        err(f"{ctx}: concentration must be a finite number > 0 "
                            f"(the Dirichlet parameters are concentration * alpha_k)")
            tp = g.get("total_probability")
            if isinstance(tp, dict):
                tv = ((params.get(tp["param"]) or {}).get("value") if "param" in tp
                      else tp.get("value"))
                if isinstance(tv, (int, float)) and not isinstance(tv, bool) \
                        and not 0.0 <= tv <= 1.0:
                    err(f"{cfile}:{gid}: total probability {tv!r} outside [0,1]")
            if isinstance(tp, dict) and ("param" not in tp or tp["param"] in params):
                tunit = (params[tp["param"]].get("unit") if "param" in tp
                         else tp.get("unit"))
                msg = unit_problem("ccf-total", [("total_probability", tunit)])
                if msg:
                    err(f"{cfile}:{gid}: {msg}")
            if isinstance(tp, dict) and "uncertainty" in tp:
                check_uncertainty(schema, tp["uncertainty"], tp.get("value"),
                                  f"{cfile}:{gid}/total_probability",
                                  schema_covered=False)

    manifest = load(os.path.join(model_dir, "model.yaml")) or {}
    mpath = os.path.join(model_dir, "model.yaml")
    # model.yaml has no JSON Schema: its keys are checked here, so that a
    # misplaced entry (say `configurations` under `model:`) is an error,
    # not silently ignored (V&V D-22)
    if isinstance(manifest, dict):
        for k in sorted(set(manifest) - MANIFEST_KEYS):
            err(f"{mpath}: unknown top-level key {k!r} (expected one of "
                f"{', '.join(sorted(MANIFEST_KEYS))})")
        mm = manifest.get("model")
        if isinstance(mm, dict):
            for k in sorted(set(mm) - MANIFEST_MODEL_KEYS):
                err(f"{mpath}: unknown key {k!r} under model (expected one of "
                    f"{', '.join(sorted(MANIFEST_MODEL_KEYS))})")
            for rm in mm.get("risk_metrics") or []:
                if isinstance(rm, dict):
                    for k in sorted(set(rm) - {"id", "label", "end_states"}):
                        err(f"{mpath}: risk metric {rm.get('id', '?')}: unknown key {k!r}")
    # named configurations: override sets quantified next to the base case
    cfgs = manifest.get("configurations") if isinstance(manifest, dict) else None
    if cfgs is not None and not isinstance(cfgs, dict):
        err(f"{mpath}: configurations must be a mapping of configuration IDs")
        cfgs = {}
    for cid, c in (cfgs or {}).items():
        ctx = f"{mpath}: configuration {cid}"
        if not isinstance(c, dict):
            err(f"{ctx}: must be a mapping (label, house_events, parameters)")
            continue
        for k in sorted(set(c) - {"label", "house_events", "parameters"}):
            err(f"{ctx}: unknown field {k!r}")
        for h, v in (c.get("house_events") or {}).items():
            if h not in house:
                err(f"{ctx}: dangling house event reference {h}")
            if not isinstance(v, bool):
                err(f"{ctx}: house event {h} must be true or false")
        for q, v in (c.get("parameters") or {}).items():
            if q not in params:
                err(f"{ctx}: dangling parameter reference {q}")
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not v >= 0:
                err(f"{ctx}: parameter {q} must be a number >= 0 (in the "
                    f"parameter's own unit)")

    fi_errors, fi_warnings = file_index_problems(model_dir, manifest)
    for e in fi_errors:
        err(f"{model_dir}: files: {e}")
    for w in fi_warnings:
        warn(f"{model_dir}: files: {w}")
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

    # ---- dimensional rules -------------------------------------------------
    def unit_of(q):
        if not isinstance(q, dict):
            return None
        if "param" in q:
            pd = params.get(q["param"])
            return pd.get("unit") if isinstance(pd, dict) else None
        return q.get("unit")

    for be_id, (be, path) in basic_events.items():
        fm = be.get("failure_model") or {}
        kind = fm.get("type")
        if kind not in FM_FIELDS or any(f not in fm for f in FM_FIELDS[kind]):
            continue                    # the schema has reported the shape
        if any(isinstance(fm[f], dict) and "param" in fm[f]
               and fm[f]["param"] not in params for f in FM_FIELDS[kind]):
            continue                    # dangling parameter, reported above
        msg = unit_problem(kind, [(f, unit_of(fm[f])) for f in FM_FIELDS[kind]])
        if msg:
            err(f"{path}:{be_id}: {kind} failure model: {msg}")

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
        if isinstance(freq, dict) and "unit" in freq:
            msg = unit_problem("initiating-event", [("frequency", freq["unit"])])
            if msg:
                err(f"{path}:{et_id}: initiating event: {msg}")
        if isinstance(freq, dict) and "uncertainty" in freq:
            check_uncertainty(schema, freq["uncertainty"], freq.get("value"),
                              f"{path}:{et_id}/initiating_event")
        fes = et.get("functional_events", {})
        for fe_id, fe in fes.items():
            if fe.get("top_gate") not in gates:
                err(f"{path}:{et_id}/{fe_id}: top_gate "
                    f"{fe.get('top_gate')} undefined")
        seen_paths = {}
        well_formed = {}
        for seq_id, seq in et.get("sequences", {}).items():
            ctx = f"{path}:{seq_id}"
            for fe in seq.get("path", {}):
                if fe not in fes:
                    err(f"{ctx}: path references undefined {fe}")
            missing = set(fes) - set(seq.get("path", {}))
            if missing:
                err(f"{ctx}: path does not resolve {sorted(missing)}")
            if set(seq.get("path", {})) == set(fes):
                well_formed[seq_id] = seq
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
                if es in metric_states:
                    warn(f"{ctx}: end state {es} of a transfer sequence is "
                         f"mapped to a risk metric, but transfer sequences are "
                         f"never counted in metrics (a followed transfer is "
                         f"counted through its expansions)")
            elif es != "OK" and es not in metric_states:
                warn(f"{ctx}: end state {es} is not mapped to any "
                     f"risk metric in model.yaml")
        # Partition (exact cover) only on a table whose every path is
        # well formed; malformed paths are already errors above.
        seqs = et.get("sequences", {})
        if seqs and len(well_formed) == len(seqs) and all(
                v in ("success", "failure", "bypassed")
                for s in seqs.values() for v in s["path"].values()):
            for msg in partition_problems(list(fes), seqs):
                err(f"{path}:{et_id}: partition: {msg}")

    # ---- transfers between event trees ------------------------------------
    # Followed transfers form a graph over the model's event trees; a cycle
    # would expand forever (the engine refuses it too). A tree without an
    # initiating event is transfer-only: quantified solely through trees
    # that transfer into it, so one nobody reaches is never quantified.
    xfer = {et_id: sorted({s["transfer"] for s in et.get("sequences", {}).values()
                           if isinstance(s, dict) and s.get("transfer") in event_trees})
            for et_id, (et, _) in event_trees.items()}
    state = {t: 0 for t in xfer}

    def xdfs(t: str, stack: list) -> None:
        state[t] = 1
        stack.append(t)
        for u in xfer[t]:
            if state[u] == 1:
                err("transfer cycle: " + " -> ".join(stack[stack.index(u):] + [u]))
            elif state[u] == 0:
                xdfs(u, stack)
        stack.pop()
        state[t] = 2

    for t in sorted(xfer):
        if state[t] == 0:
            xdfs(t, [])
    targets = {u for us in xfer.values() for u in us}
    for et_id, (et, path) in event_trees.items():
        if "initiating_event" not in et and et_id not in targets:
            warn(f"{path}:{et_id}: transfer-only event tree (no initiating "
                 f"event) that no sequence transfers into; it is never "
                 f"quantified")

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
