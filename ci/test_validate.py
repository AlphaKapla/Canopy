#!/usr/bin/env python3
"""Regression suite for ci/validate.py (V&V §4.1): every error and warning
class the validator claims, each provoked by one targeted mutation of the
demo model, plus a randomized check of the partition lint against
brute-force enumeration.

Each mutation case copies model/ to a temporary directory, applies one
edit, runs the validator as a subprocess (it keeps module-level state), and
requires: the expected exit code, every expected message fragment among
the ERROR (or WARNING) lines, and — where given — the exact error count, so
a mutation cannot pass by tripping some unrelated check.

Usage: python ci/test_validate.py
"""
import itertools
import os
import random
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODEL = os.path.join(ROOT, "model")
SCHEMA = os.path.join(ROOT, "schema", "psa-model.schema.json")
sys.path.insert(0, HERE)
from validate import partition_problems  # noqa: E402


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def yload(d, rel):
    with open(os.path.join(d, rel)) as f:
        return yaml.safe_load(f)


def ydump(d, rel, obj):
    with open(os.path.join(d, rel), "w") as f:
        yaml.safe_dump(obj, f, sort_keys=False, default_flow_style=False)


def edit(d, rel, fn):
    obj = yload(d, rel)
    fn(obj)
    ydump(d, rel, obj)


def append_text(d, rel, text):
    with open(os.path.join(d, rel), "a") as f:
        f.write(text)


def write(d, rel, text):
    os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
    with open(os.path.join(d, rel), "w") as f:
        f.write(text)


def be(d, rel="basic-events/rps.yaml"):
    return yload(d, rel)


ET = "event-trees/et-sloca.yaml"
RPS = "fault-trees/ft-rps.yaml"
PROV = {"source": "test", "justification": "validator regression case"}


def seqs(obj):
    return obj["event_tree"]["sequences"]


def run_validator(d):
    p = subprocess.run([sys.executable, os.path.join(HERE, "validate.py"),
                        d, SCHEMA], capture_output=True, text=True)
    errors = [l[len("ERROR:"):].strip() for l in p.stdout.splitlines()
              if l.startswith("ERROR:")]
    warnings = [l[len("WARNING:"):].strip() for l in p.stdout.splitlines()
                if l.startswith("WARNING:")]
    return p.returncode, errors, warnings, p.stdout + p.stderr


# --------------------------------------------------------------------------
# mutation cases: (name, mutate(d), expected errors, exact error count or
# None, expected warnings)
# --------------------------------------------------------------------------
def m_dup_key(d):
    append_text(d, "basic-events/rps.yaml",
                "\n  BE-RPS-LOGIC-FAIL:\n    label: again\n")


def m_malformed(d):
    append_text(d, "basic-events/rps.yaml", "\n  broken: [unclosed\n")


def m_unknown_field(d):
    edit(d, "basic-events/rps.yaml",
         lambda o: o["basic_events"]["BE-RPS-LOGIC-FAIL"].update(colour="red"))


def m_formula(extra):
    def f(d):
        def g(o):
            o["fault_trees"]["FT-RPS"]["gates"]["GT-RT-TOP"]["formula"] = {
                "or": ["BE-RPS-BREAKER-CCF", "BE-RPS-LOGIC-FAIL", extra]}
        edit(d, RPS, g)
    return f


def m_dangling_param(d):
    edit(d, "basic-events/rps.yaml", lambda o: o["basic_events"][
        "BE-RPS-LOGIC-FAIL"]["failure_model"].update(value={"param": "PAR-NOPE"}))


def m_cycle(d):
    def g(o):
        gates = o["fault_trees"]["FT-RPS"]["gates"]
        gates["GT-RT-TOP"]["formula"] = {"or": ["GT-C1", "BE-RPS-LOGIC-FAIL"]}
        gates["GT-C1"] = {"label": "c1", "formula": {"or": ["GT-C2", "BE-RPS-BREAKER-CCF"]}}
        gates["GT-C2"] = {"label": "c2", "formula": {"and": ["GT-C1", "BE-RPS-LOGIC-FAIL"]}}
    edit(d, RPS, g)


def m_dup_be_across_files(d):
    src = be(d)["basic_events"]["BE-RPS-LOGIC-FAIL"]
    edit(d, "basic-events/ecc-pumps.yaml",
         lambda o: o["basic_events"].update({"BE-RPS-LOGIC-FAIL": src}))


def m_dup_gate_across_files(d):
    g = yload(d, RPS)["fault_trees"]["FT-RPS"]["gates"]["GT-RT-TOP"]
    edit(d, "fault-trees/ft-rhr.yaml", lambda o: next(iter(
        o["fault_trees"].values()))["gates"].update({"GT-RT-TOP": g}))


def m_ft_top_undefined(d):
    edit(d, RPS, lambda o: o["fault_trees"]["FT-RPS"].update(top_gate="GT-NOPE"))


def m_fe_top_undefined(d):
    edit(d, ET, lambda o: o["event_tree"]["functional_events"]["FE-RHR"].update(
        top_gate="GT-NOPE"))


def m_path_undefined_fe(d):
    edit(d, ET, lambda o: seqs(o)["SEQ-SLOCA-01"]["path"].update({"FE-XX": "success"}))


def m_path_unresolved(d):
    edit(d, ET, lambda o: seqs(o)["SEQ-SLOCA-01"]["path"].pop("FE-RHR"))


def m_dup_path(d):
    def g(o):
        seqs(o)["SEQ-SLOCA-05"] = dict(seqs(o)["SEQ-SLOCA-02"])
    edit(d, ET, g)


def m_overlap(d):
    def g(o):
        seqs(o)["SEQ-SLOCA-05"] = {
            "path": {"FE-RT": "success", "FE-ECC": "failure", "FE-RHR": "failure"},
            "end_state": "CD"}
    edit(d, ET, g)


def m_gap(d):
    edit(d, ET, lambda o: seqs(o).pop("SEQ-SLOCA-02"))


def m_gap_wide(d):
    edit(d, ET, lambda o: seqs(o).pop("SEQ-SLOCA-04"))


def m_seq_house_dangling(d):
    edit(d, ET, lambda o: seqs(o)["SEQ-SLOCA-03"].update(
        house_events={"HE-NOPE": True}))


def m_ccf(fn):
    def f(d):
        edit(d, "ccf-groups.yaml", lambda o: fn(o["ccf_groups"]["CCF-ECC-PMP-FTS"]))
    return f


def m_fr22(d):
    """The four FR-22 conditions of V&V §4.1, together."""
    edit(d, "parameters.yaml", lambda o: o["parameters"]["PAR-ECC-PMP-FR"].update(
        colour="red"))
    edit(d, "parameters.yaml", lambda o: o["parameters"]["PAR-ECC-PMP-FTS"][
        "uncertainty"].update(error_factor=0.9))

    edit(d, "basic-events/ecc-pumps.yaml", lambda o: o["basic_events"][
        "BE-ECC-PMP-A-FTR"].update(uncertainty={"distribution": "lognormal",
                                                "error_factor": 3.0}))
    # the TM event lives in its own (hand-written) file since FR-41
    edit(d, "basic-events/ecc-pumps-tm.yaml", lambda o: o["basic_events"][
        "BE-ECC-PMP-A-TM"].update(uncertainty={"distribution": "beta",
                                               "alpha": 1.0, "beta": 9.0}))   # mean 0.1


def m_unc_twice(d):
    def g(o):
        o["parameters"]["PAR-X"] = {
            "label": "x", "value": 1e-3, "unit": "per_demand",
            "uncertainty": {"distribution": "lognormal", "error_factor": 3.0},
            "provenance": PROV}
    edit(d, "parameters.yaml", g)

    def h(o):
        e = o["basic_events"]["BE-RPS-LOGIC-FAIL"]
        e["failure_model"]["value"] = {"param": "PAR-X"}
        e["uncertainty"] = {"distribution": "lognormal", "error_factor": 3.0}
    edit(d, "basic-events/rps.yaml", h)


def m_unc_on_member(d):
    def g(o):
        e = o["basic_events"]["BE-ECC-PMP-B-FTS"]
        # An inline point value without its own distribution, so the rule
        # under test (not "uncertainty given twice") is the one that fires.
        e["failure_model"]["value"] = {"value": 1.2e-3, "unit": "per_demand"}
        e["uncertainty"] = {"distribution": "lognormal", "error_factor": 3.0}
    edit(d, "basic-events/ecc-pumps.yaml", g)


def m_unc_bad(kind):
    def f(d):
        def g(o):
            e = o["basic_events"]["BE-RPS-LOGIC-FAIL"]
            if kind == "gamma":
                e["uncertainty"] = {"distribution": "gamma", "shape": 2.0,
                                    "scale": 1.0}            # mean 2 != point
            elif kind == "uniform":
                e["uncertainty"] = {"distribution": "uniform", "lower": 1e-3,
                                    "upper": 1e-4}
            elif kind == "lognormal0":
                e["failure_model"]["value"]["value"] = 0.0
                e["uncertainty"] = {"distribution": "lognormal", "error_factor": 3.0}
        edit(d, "basic-events/rps.yaml", g)
    return f


def m_units_param(d):
    """The shared 24-hour mission time re-expressed in years: all four
    fail-to-run events using it (ECCS and RHR, rates per_hour) now mix
    time bases — one error each, none elsewhere."""
    edit(d, "parameters.yaml", lambda o: o["parameters"][
        "PAR-MISSION-TIME-24H"].update(value=24.0 / 8760.0, unit="year"))


def m_units_prob(d):
    edit(d, "basic-events/rps.yaml", lambda o: o["basic_events"][
        "BE-RPS-LOGIC-FAIL"]["failure_model"]["value"].update(unit="per_hour"))


def m_units_ie(d):
    edit(d, ET, lambda o: o["event_tree"]["initiating_event"][
        "frequency"].update(unit="per_hour"))


def m_units_ccf(d):
    edit(d, "parameters.yaml", lambda o: o["parameters"][
        "PAR-ECC-PMP-FTS"].update(unit="dimensionless"))   # still valid
    edit(d, "ccf-groups.yaml", lambda o: o["ccf_groups"]["CCF-ECC-PMP-FTS"]
         .update(total_probability={"value": 1.2e-3, "unit": "per_year"}))


def m_config(fn):
    return lambda d: edit(d, "model.yaml", lambda o: fn(o["configurations"]))


def m_file(rel, text="basic_events: {}\n"):
    return lambda d: write(d, rel, text)


def m_includes(fn):
    return lambda d: edit(d, "model.yaml", lambda o: fn(o["includes"]))


def m_remove(rel):
    def f(d):
        p = os.path.join(d, rel)
        shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    return f


def m_orphans(d):
    def g(o):
        o["fault_trees"]["FT-RPS"]["gates"]["GT-ORPHAN"] = {
            "label": "orphan", "formula": {"or": ["BE-RPS-LOGIC-FAIL",
                                                  "BE-RPS-BREAKER-CCF"]}}
    edit(d, RPS, g)
    edit(d, "basic-events/rps.yaml", lambda o: o["basic_events"].update({
        "BE-UNUSED": {"label": "unused", "failure_model": {
            "type": "probability", "value": {"value": 1e-3, "unit": "per_demand"}},
            "provenance": PROV}}))


def m_unmapped_end_state(d):
    edit(d, ET, lambda o: seqs(o)["SEQ-SLOCA-02"].update(end_state="LOCA-X"))


XFER = "transfer target ET-ATWS not defined"

CASES = [
    ("clean demo model", lambda d: None, [], 0, [XFER]),
    # A file that fails to parse contributes no entities, so the references
    # to its two events dangle as well: exactly 3 errors, by design.
    ("duplicate YAML key", m_dup_key, ["duplicate key 'BE-RPS-LOGIC-FAIL'",
                                       "dangling basic event reference "
                                       "BE-RPS-BREAKER-CCF"], 3, []),
    ("malformed YAML", m_malformed, ["YAML parse failure"], 3, []),
    ("unknown field (schema)", m_unknown_field,
     ["schema: basic_events/BE-RPS-LOGIC-FAIL", "colour"], 1, []),
    ("dangling basic event", m_formula("BE-NOPE"),
     ["dangling basic event reference BE-NOPE"], 1, []),
    ("dangling gate", m_formula("GT-NOPE"), ["dangling gate reference GT-NOPE"], 1, []),
    ("dangling house event", m_formula("HE-NOPE"),
     ["dangling house event reference HE-NOPE"], 1, []),
    ("dangling parameter", m_dangling_param,
     ["dangling parameter reference PAR-NOPE"], 1, []),
    ("gate cycle", m_cycle, ["gate cycle: GT-C1 -> GT-C2 -> GT-C1"], 1, []),
    ("duplicate basic event across files", m_dup_be_across_files,
     ["duplicate basic event ID BE-RPS-LOGIC-FAIL"], 1, []),
    ("duplicate gate across files", m_dup_gate_across_files,
     ["duplicate gate ID GT-RT-TOP"], 1, []),
    ("fault-tree top gate undefined", m_ft_top_undefined,
     ["FT-RPS: top_gate GT-NOPE undefined"], 1, []),
    ("functional-event top gate undefined", m_fe_top_undefined,
     ["FE-RHR: top_gate GT-NOPE undefined"], 1, []),
    ("path references undefined FE", m_path_undefined_fe,
     ["SEQ-SLOCA-01: path references undefined FE-XX"], 1, []),
    ("path leaves an FE unresolved", m_path_unresolved,
     ["SEQ-SLOCA-01: path does not resolve ['FE-RHR']"], 1, []),
    ("duplicate sequence path", m_dup_path,
     ["SEQ-SLOCA-05: duplicate sequence path (same as SEQ-SLOCA-02)"], 1, []),
    ("partition: overlapping sequences", m_overlap,
     ["partition: sequences SEQ-SLOCA-03 and SEQ-SLOCA-05 overlap",
      "FE-RT=success, FE-ECC=failure, FE-RHR=failure"], 1, []),
    ("partition: uncovered outcome", m_gap,
     ["no sequence covers the outcome (FE-RT=success, FE-ECC=success, "
      "FE-RHR=failure)", "1 of 8 functional-event outcome combination(s)"], 2, []),
    ("partition: uncovered sub-tree", m_gap_wide,
     ["no sequence covers the outcome (FE-RT=failure) (for any outcome of "
      "FE-ECC, FE-RHR)", "4 of 8"], 2, []),
    ("per-sequence house event dangling", m_seq_house_dangling,
     ["SEQ-SLOCA-03: dangling house event reference HE-NOPE"], 1, [XFER]),
    ("CCF alpha factors do not sum to 1",
     m_ccf(lambda g: g["factors"].update(alpha_2=0.5)),
     ["alpha factors sum to 1.4787"], 1, []),
    ("CCF alpha factors outside [0,1], summing to 1 (D-29)",
     m_ccf(lambda g: g["factors"].update(alpha_1=1.05, alpha_2=-0.05)),
     ["factor alpha_1 = 1.05 outside [0,1]", "factor alpha_2 = -0.05 outside [0,1]"],
     2, []),
    ("CCF total probability above 1 (D-29)",
     m_ccf(lambda g: g.update(total_probability={"value": 1.5, "unit": "per_demand"})),
     ["total probability 1.5 outside [0,1]"], 1, []),
    ("CCF alpha factor count", m_ccf(lambda g: g["factors"].pop("alpha_2")),
     ["alpha-factor group of size 2 needs alpha_1..alpha_2"], 1, []),
    ("CCF beta out of range",
     m_ccf(lambda g: g.update(model="beta-factor", factors={"beta": 1.5})),
     ["beta-factor needs 0 < beta < 1"], 1, []),
    ("CCF beta group with a testing scheme (no effect, warned)",
     m_ccf(lambda g: g.update(model="beta-factor", factors={"beta": 0.1},
                              testing="non-staggered")),
     [], 0, ["`testing` has no effect on a beta-factor group"]),
    ("CCF factor uncertainty: not a Dirichlet",
     m_ccf(lambda g: g.update(factor_uncertainty={"distribution": "lognormal",
                                                  "error_factor": 3})),
     ["must be {distribution: dirichlet, concentration: N}"], 1, []),
    ("CCF factor uncertainty: concentration not positive",
     m_ccf(lambda g: g.update(factor_uncertainty={"distribution": "dirichlet",
                                                  "concentration": 0})),
     ["concentration must be a finite number > 0"], 1, []),
    ("CCF factor uncertainty: unknown field",
     m_ccf(lambda g: g.update(factor_uncertainty={"distribution": "dirichlet",
                                                  "concentration": 10, "shape": 2})),
     ["unknown field 'shape'"], 1, []),
    ("CCF factor uncertainty: valid (clean)",
     m_ccf(lambda g: g.update(factor_uncertainty={"distribution": "dirichlet",
                                                  "concentration": 25})),
     [], 0, [XFER]),
    ("CCF member undefined",
     m_ccf(lambda g: g.update(members=["BE-ECC-PMP-A-FTS", "BE-NOPE"])),
     ["CCF member BE-NOPE is not a defined basic event"], 1, []),
    ("CCF group of one", m_ccf(lambda g: g.update(
        members=["BE-ECC-PMP-A-FTS"], factors={"alpha_1": 1.0})),
     ["CCF group needs >= 2 members"], 1, []),
    ("FR-22: the four §4.1 conditions", m_fr22,
     ["PAR-ECC-PMP-FR", "colour", "error_factor",
      "BE-ECC-PMP-A-FTR: an event-level `uncertainty` is only defined for "
      "failure_model type `probability`",
      "BE-ECC-PMP-A-TM: point value", "is not the mean 0.1 of its beta"], 4, []),
    ("FR-22: uncertainty given twice", m_unc_twice,
     ["BE-RPS-LOGIC-FAIL: uncertainty given twice"], 1, []),
    ("FR-22: distribution on a CCF member", m_unc_on_member,
     ["BE-ECC-PMP-B-FTS: event-level `uncertainty` on a CCF group member"], 1, []),
    ("FR-22: gamma mean mismatch", m_unc_bad("gamma"),
     ["is not the mean 2 of its gamma distribution"], 1, []),
    ("FR-22: uniform lower >= upper", m_unc_bad("uniform"),
     ["uniform needs lower < upper"], 1, []),
    ("FR-22: lognormal with zero point value", m_unc_bad("lognormal0"),
     ["lognormal needs a positive point value"], 1, []),
    ("units: shared parameter on a different time base", m_units_param,
     ["BE-ECC-PMP-A-FTR: rate-mission failure model: rate (per_hour) and "
      "mission_time (year) are on different time bases",
      "BE-ECC-PMP-B-FTR: rate-mission failure model",
      "BE-RHR-PMP-A-FTR: rate-mission failure model",
      "BE-RHR-PMP-B-FTR: rate-mission failure model"], 4, []),
    ("units: probability given per_hour", m_units_prob,
     ["BE-RPS-LOGIC-FAIL: probability failure model: value must be "
      "per_demand or dimensionless (got per_hour)"], 1, []),
    ("units: initiating event not per_year", m_units_ie,
     ["initiating event: frequency must be per_year (got per_hour)"], 1, []),
    ("units: CCF total per_year", m_units_ccf,
     ["CCF-ECC-PMP-FTS: total_probability must be per_demand or "
      "dimensionless (got per_year)"], 1, []),
    ("configuration: dangling house event and parameter",
     m_config(lambda c: c.update({"X": {"label": "x", "house_events": {"HE-NOPE": True},
                                        "parameters": {"PAR-NOPE": 1.0}}})),
     ["configuration X: dangling house event reference HE-NOPE",
      "configuration X: dangling parameter reference PAR-NOPE"], 2, []),
    ("configuration: bad values and unknown field",
     m_config(lambda c: c.update({"X": {"label": "x", "colour": 1,
                                        "house_events": {"HE-ECC-TRAIN-A-OOS": "yes"},
                                        "parameters": {"PAR-ECC-PMP-FTS": -1}}})),
     ["configuration X: unknown field 'colour'",
      "house event HE-ECC-TRAIN-A-OOS must be true or false",
      "parameter PAR-ECC-PMP-FTS must be a number >= 0"], 3, []),
    ("manifest: configurations misplaced under model (D-22)",
     lambda d: edit(d, "model.yaml", lambda o: o["model"].update(
         configurations=o.pop("configurations"))),
     ["unknown key 'configurations' under model"], 1, []),
    ("manifest: unknown top-level key and risk-metric key",
     lambda d: edit(d, "model.yaml", lambda o: (o.update(colour="red"),
                                                o["model"]["risk_metrics"][0].update(unit="per_year"))),
     ["unknown top-level key 'colour'", "risk metric CDF: unknown key 'unit'"], 2, []),
    ("files: .yml entity file", m_file("basic-events/extra.yml"),
     ["basic-events/extra.yml: only *.yaml files are loaded"], 1, []),
    ("files: sub-directory", m_file("fault-trees/sub/x.yaml", "fault_trees: {}\n"),
     ["fault-trees/sub/: sub-directories are not read"], 1, []),
    ("files: hidden model file", m_file("basic-events/.hidden.yaml"),
     ["basic-events/.hidden.yaml: hidden model file"], 1, []),
    ("files: stray root YAML", m_file("stray.yaml", "x: 1\n"),
     ["stray.yaml: not a model file name"], 1, []),
    ("files: loaded file not indexed",
     m_includes(lambda i: i.pop("ccf_groups")),
     ["ccf-groups.yaml: loaded but not indexed in model.yaml includes"], 1, []),
    ("files: indexed file missing",
     m_includes(lambda i: i["parameters"].append("more-parameters.yaml")),
     ["includes/parameters: indexed file more-parameters.yaml does not exist"],
     1, []),
    ("files: indexed but never loaded",
     lambda d: (write(d, "notes/n.yaml", "x: 1\n"),
                m_includes(lambda i: i.update(notes=["notes/*.yaml"]))(d)),
     ["notes/n.yaml: indexed in model.yaml includes but never loaded"], 1, []),
    ("files: required file missing (no crash)", m_remove("parameters.yaml"),
     ["required file parameters.yaml is missing"], None, []),
    ("files: required directory missing", m_remove("fault-trees"),
     ["required directory fault-trees/ is missing"], None, []),
    ("warnings: orphaned gate and basic event", m_orphans, [], 0,
     ["orphaned gate GT-ORPHAN", "orphaned basic event BE-UNUSED"]),
    ("warnings: unmapped end state", m_unmapped_end_state, [], 0,
     ["SEQ-SLOCA-02: end state LOCA-X is not mapped to any risk metric"]),
]


def run_cases() -> list:
    failures = []
    for name, mutate, exp_err, n_err, exp_warn in CASES:
        d = tempfile.mkdtemp(prefix="psa-val-")
        try:
            shutil.copytree(MODEL, d, dirs_exist_ok=True)
            mutate(d)
            code, errors, warnings, out = run_validator(d)
            problems = []
            if "Traceback" in out:
                problems.append("validator crashed")
            want_code = 1 if (exp_err or (n_err or 0) > 0) else 0
            if code != want_code:
                problems.append(f"exit {code}, expected {want_code}")
            for frag in exp_err:
                if not any(frag in e for e in errors):
                    problems.append(f"missing error fragment {frag!r}")
            if n_err is not None and len(errors) != n_err:
                problems.append(f"{len(errors)} errors, expected exactly {n_err}")
            for frag in exp_warn:
                if not any(frag in w for w in warnings):
                    problems.append(f"missing warning fragment {frag!r}")
            if problems:
                failures.append(f"{name}: " + "; ".join(problems)
                                + "\n      " + out.strip().replace("\n", "\n      "))
            else:
                print(f"  ok  {name}")
        finally:
            shutil.rmtree(d, ignore_errors=True)
    return failures


# --------------------------------------------------------------------------
# partition lint vs brute force on random tables
# --------------------------------------------------------------------------
def random_partition(rng, fes):
    """A random sequence table that partitions the outcome space exactly:
    recursive splitting, each event either questioned (two branches) or
    bypassed, with early termination bypassing the rest."""
    out = {}

    def rec(k, path):
        if k == len(fes) or (k > 0 and rng.random() < 0.15):
            full = dict(path)
            for fe in fes[k:]:
                full[fe] = "bypassed"
            out[f"SEQ-{len(out):03d}"] = {"path": full}
            return
        fe = fes[k]
        if rng.random() < 0.25:
            rec(k + 1, {**path, fe: "bypassed"})
        else:
            rec(k + 1, {**path, fe: "success"})
            rec(k + 1, {**path, fe: "failure"})
    rec(0, {})
    return out


def brute(fes, sequences):
    """(outcomes covered by no sequence, outcomes covered by >= 2)."""
    none = multi = 0
    for bits in itertools.product(["success", "failure"], repeat=len(fes)):
        o = dict(zip(fes, bits))
        c = sum(all(v == "bypassed" or o[fe] == v for fe, v in s["path"].items())
                for s in sequences.values())
        none += c == 0
        multi += c >= 2
    return none, multi


def lint_counts(fes, sequences):
    msgs = partition_problems(fes, sequences, max_examples=10**6)
    n_unc = 0
    for m in msgs:
        if "outcome combination(s) are covered by no sequence" in m:
            n_unc = int(m.split(" of ")[0])
    has_overlap = any(" overlap: " in m for m in msgs)
    return msgs, n_unc, has_overlap


def run_random(n_tables=300, seed=20260708) -> tuple:
    rng = random.Random(seed)
    failures, stats = [], {"exact": 0, "gaps": 0, "overlaps": 0}
    for t in range(n_tables):
        fes = [f"FE-{i}" for i in range(rng.randint(1, 7))]
        table = random_partition(rng, fes)
        variants = [("exact", table)]
        if len(table) > 1:
            victim = rng.choice(sorted(table))
            variants.append(("gap", {k: v for k, v in table.items() if k != victim}))
        # an overlapping extra sequence: a random cube
        extra = {fe: rng.choice(["success", "failure", "bypassed"]) for fe in fes}
        variants.append(("extra", {**table, "SEQ-EXTRA": {"path": extra}}))
        for kind, tb in variants:
            if kind == "extra" and any(s["path"] == extra for k, s in table.items()):
                continue            # identical path: a duplicate, not an overlap
            none, multi = brute(fes, tb)
            msgs, n_unc, has_overlap = lint_counts(fes, tb)
            if n_unc != none or has_overlap != (multi > 0):
                failures.append(f"table {t} ({kind}, {len(fes)} FEs): brute "
                                f"force uncovered {none}, multiply covered "
                                f"{multi}; lint {msgs}")
            if not msgs and (none or multi):
                failures.append(f"table {t} ({kind}): lint silent on a "
                                f"non-partition")
            stats["exact"] += not (none or multi)
            stats["gaps"] += none > 0
            stats["overlaps"] += multi > 0
    return failures, stats


def main() -> int:
    print("validator mutation cases:")
    failures = run_cases()
    rf, stats = run_random()
    failures += rf
    print(f"partition lint vs brute force: {stats['exact']} exact partitions "
          f"accepted, {stats['gaps']} tables with gaps and {stats['overlaps']} "
          f"with overlaps flagged with the brute-force count")
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print(f"validate.py: {len(CASES)} mutation cases and the randomized "
          f"partition check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
