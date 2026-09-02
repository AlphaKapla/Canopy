#!/usr/bin/env python3
"""Convert a RiskSpectrum PSA model export into a Canopy YAML model.

Usage:
  import_riskspectrum.py <export-dir-or-json> <out-model-dir>
      [--model-id ID] [--model-name NAME]
      [--metric METRIC=STATE[,STATE...]]...   e.g. --metric CDF=CD
      [--time-unit hour|year]                 (default hour)
      [--frequency-unit per_year|per_hour]    (default per_year)
      [--mgl-to-alpha]                        convert MGL groups (see below)
      [--allow-unsupported]                   downgrade logic-affecting
                                              errors to warnings and drop
      [--strict]                              any warning -> exit 1
      [--single-file]                         one basic-events file instead
                                              of one per system

The converter never reads RiskSpectrum's database or files directly. It
reads a neutral *table export* — one flat table per RiskSpectrum record
type — that either a SQL extractor (ci/extract_riskspectrum_sql.py) or a
RiskSpectrum PSA Macro / Excel export can produce. The contract is:

  a directory of CSV files (UTF-8, header row), or one JSON file whose
  top-level object maps table name -> list of row objects. Column names
  are case-insensitive. Unknown columns are ignored; unknown tables are
  ignored. Missing optional tables are treated as empty.

  parameters         id, description, type, value, distribution, p1, p2,
                     reference, comment
                       type: probability | rate | frequency | time |
                             dimensionless
                       distribution: lognormal (p1 = error factor),
                             beta (p1 = alpha, p2 = beta),
                             gamma (p1 = shape, p2 = scale),
                             uniform (p1 = lower, p2 = upper), or blank.
                             Any other distribution is dropped with a
                             warning (point value kept).
  basic_events       id, description, model, q, rate, mission_time,
                     repair_time, test_interval, first_test, test_duration,
                     q_mean, system, component, failure_mode, reference,
                     comment
                       model: probability | mission | repairable | tested |
                              frequency
                       Numeric columns hold a literal number OR the id of a
                       parameter row (RiskSpectrum links reliability data to
                       parameter records; both forms are accepted).
  house_events       id, description, value (TRUE|FALSE), reference, comment
  fault_trees        id, description, top_gate
  gates              id, description, type (AND|OR|KN|NOT|XOR|NAND|NOR), k,
                     fault_tree
  gate_inputs        gate, input, position, [input_type: GATE|BE|HE]
  ccf_groups         id, description, model (beta|alpha|mgl), testing
                     (staggered|non-staggered|blank), total, reference,
                     comment
  ccf_members        group, member, position
  ccf_factors        group, name, value
                       beta: name "beta"; alpha: "alpha_1".."alpha_n";
                       mgl: "rho_2".."rho_n" (or beta, gamma, delta,
                       epsilon, zeta, eta, theta = rho_2..rho_8)
  event_trees        id, description, initiator, bc_set
  function_events    id, description
  et_columns         event_tree, position, function_event, logic
  sequences          event_tree, sequence, consequence, transfer, description
  sequence_branches  event_tree, sequence, function_event, outcome (S|F|-),
                     bc_set
  bc_sets            id, description
  bc_set_entries     bc_set, target, value
  exchange_events    id, original, replacement, condition
  consequences       id, description
  project            id, name, exported_at, source   (single row, optional)

What the converter refuses (exit 1) unless --allow-unsupported, because
the converted model would quantify differently from RiskSpectrum:
exchange events; boundary-condition entries that force a basic event
(rather than a house event); frequency-type events used inside fault
trees; an event tree whose initiator is a gate; CCF groups larger than 8
or with an unsupported model (MGL without --mgl-to-alpha, UPM); duplicate
sequence paths. With --allow-unsupported these are dropped and logged.

What the converter approximates and *always* logs (warning): a Tested
reliability model with repair time / test duration / first-test terms
(Canopy's rate-periodic-test is the idealized model; if the export carries
RiskSpectrum's computed q_mean, that point value is used instead and the
model is recorded in the justification); distributions Canopy does not
have; a CCF group without an explicit testing convention (Canopy's default,
staggered, is assumed).

MGL conversion (--mgl-to-alpha) uses the non-staggered NUREG/CR-5485
relations: Q_k = (1/C(m-1,k-1)) (prod_{i<=k} rho_i)(1 - rho_{k+1}) Q_t
with rho_1 = 1, rho_{m+1} = 0, then alpha_k = C(m,k) Q_k / sum_j C(m,j) Q_j;
the group is emitted with `testing: non-staggered`. Verify one converted
group against RiskSpectrum's expanded CCF events before trusting it:
conventions differ between codes (see V&V finding F-1).

Every entity carries `external_ids: {riskspectrum: <original id>}` so the
conversion is re-runnable and ci/crosscheck_rs.py can match results back.
Provenance is filled from the reference/comment columns; where
RiskSpectrum has none, a marked placeholder is written and counted in the
conversion log (<out-dir>/conversion-log.md) so provenance debt is a
number, not an unknown.
"""
from __future__ import annotations

import csv
import datetime as _dt
import json
import math
import os
import re
import sys
from collections import OrderedDict, defaultdict

import yaml

PLACEHOLDER = ("MIGRATED from RiskSpectrum: no justification recorded — "
               "review required")

TABLES = [
    "project", "parameters", "basic_events", "house_events", "fault_trees",
    "gates", "gate_inputs", "ccf_groups", "ccf_members", "ccf_factors",
    "event_trees", "function_events", "et_columns", "sequences",
    "sequence_branches", "bc_sets", "bc_set_entries", "exchange_events",
    "consequences",
]


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------

class Log:
    """Errors stop the conversion; warnings are written to the conversion
    log and to stderr. Unsupported constructs are errors unless
    --allow-unsupported, in which case they become warnings and are
    dropped (never silently)."""

    def __init__(self, allow_unsupported: bool):
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []
        self.allow_unsupported = allow_unsupported
        self.placeholders = 0

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    def unsupported(self, msg: str) -> bool:
        """Return True if the caller must drop the construct and continue."""
        if self.allow_unsupported:
            self.warnings.append("UNSUPPORTED (dropped): " + msg)
            return True
        self.errors.append("UNSUPPORTED: " + msg +
                           " (re-run with --allow-unsupported to drop it)")
        return False


class ConversionError(Exception):
    pass


# ---------------------------------------------------------------------------
# table loading
# ---------------------------------------------------------------------------

def _norm_row(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if k is None:
            continue
        key = str(k).strip().lower()
        if isinstance(v, str):
            v = v.strip()
        elif v is None:
            v = ""
        else:
            v = str(v).strip() if not isinstance(v, (int, float)) else v
        out[key] = v
    return out


def load_tables(path: str) -> dict[str, list[dict]]:
    tables: dict[str, list[dict]] = {t: [] for t in TABLES}
    if os.path.isfile(path) and path.lower().endswith(".json"):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ConversionError(f"{path}: top level must be an object")
        for name, rows in data.items():
            key = name.strip().lower()
            if key in tables and isinstance(rows, list):
                tables[key] = [_norm_row(r) for r in rows if isinstance(r, dict)]
        return tables
    if not os.path.isdir(path):
        raise ConversionError(f"{path}: not a directory of CSV tables or a "
                              f".json export")
    for fname in sorted(os.listdir(path)):
        if not fname.lower().endswith(".csv"):
            continue
        key = os.path.splitext(fname)[0].strip().lower()
        if key not in tables:
            continue
        with open(os.path.join(path, fname), encoding="utf-8-sig",
                  newline="") as f:
            reader = csv.DictReader(f)
            tables[key] = [_norm_row(r) for r in reader]
    return tables


def col(row: dict, *names: str, default: str = "") -> str:
    for n in names:
        v = row.get(n)
        if v not in (None, ""):
            return v if isinstance(v, str) else str(v)
    return default


def as_float(text) -> float | None:
    if text in (None, ""):
        return None
    if isinstance(text, (int, float)):
        return float(text)
    try:
        return float(str(text).replace(",", "."))
    except ValueError:
        return None


def as_bool(text) -> bool | None:
    t = str(text).strip().lower()
    if t in ("true", "t", "1", "yes", "y"):
        return True
    if t in ("false", "f", "0", "no", "n"):
        return False
    return None


# ---------------------------------------------------------------------------
# ID grammar
# ---------------------------------------------------------------------------

class Names:
    """Deterministic RiskSpectrum-ID -> Canopy-ID mapping, collision-safe,
    with the original kept for `external_ids`. Same scheme as import_mef.py
    so the two importers agree on how a foreign name becomes an ID."""

    def __init__(self):
        self.maps: dict[str, dict[str, str]] = {}
        self.used: set[str] = set()
        self.origin: dict[str, str] = {}   # canopy id -> original

    @staticmethod
    def sanitize(orig: str) -> str:
        base = re.sub(r"[^A-Z0-9-]", "-", orig.strip().upper())
        base = re.sub(r"-+", "-", base).strip("-")
        if not base or not re.match(r"[A-Z0-9]", base):
            base = "X" + base
        return base

    def get(self, prefix: str, orig: str) -> str:
        m = self.maps.setdefault(prefix, {})
        if orig in m:
            return m[orig]
        base = self.sanitize(orig)
        cand, i = f"{prefix}-{base}", 1
        while cand in self.used:
            i += 1
            cand = f"{prefix}-{base}-{i}"
        self.used.add(cand)
        m[orig] = cand
        self.origin[cand] = orig
        return cand

    def has(self, prefix: str, orig: str) -> bool:
        return orig in self.maps.get(prefix, {})


def ext(orig: str) -> dict:
    return {"riskspectrum": orig}


def label_for(desc: str, orig: str) -> str:
    lab = desc.strip() if desc else ""
    if len(lab) < 3:
        lab = f"{orig} (imported from RiskSpectrum)"
    return lab


# ---------------------------------------------------------------------------
# reliability data
# ---------------------------------------------------------------------------

PARAM_TYPES = {
    "probability": "probability", "prob": "probability", "q": "probability",
    "rate": "rate", "failure rate": "rate", "failure_rate": "rate",
    "lambda": "rate",
    "frequency": "frequency", "freq": "frequency", "f": "frequency",
    "time": "time", "mission time": "time", "mission_time": "time",
    "repair time": "time", "repair_time": "time", "test interval": "time",
    "test_interval": "time", "mttr": "time", "tm": "time", "ti": "time",
    "tr": "time",
    "dimensionless": "dimensionless", "factor": "dimensionless",
    "": "dimensionless",
}

BE_MODELS = {
    "probability": "probability", "prob": "probability", "q": "probability",
    "constant": "probability", "1": "probability",
    "mission": "mission", "mission-time": "mission", "mission_time": "mission",
    "non-repairable": "mission", "nonrepairable": "mission",
    "repairable": "repairable", "repair": "repairable", "monitored":
    "repairable",
    "tested": "tested", "periodic-test": "tested", "periodic_test": "tested",
    "standby": "tested",
    "frequency": "frequency", "initiator": "frequency", "freq": "frequency",
}

MGL_NAMES = {"beta": 2, "gamma": 3, "delta": 4, "epsilon": 5, "zeta": 6,
             "eta": 7, "theta": 8}


class Converter:
    def __init__(self, tables: dict, opts: dict, log: Log):
        self.t = tables
        self.o = opts
        self.log = log
        self.names = Names()
        self.time_unit = opts["time_unit"]
        self.freq_unit = opts["frequency_unit"]
        self.project = tables["project"][0] if tables["project"] else {}
        stamp = col(self.project, "exported_at") or _dt.date.today().isoformat()
        pid = col(self.project, "id", "name") or "RiskSpectrum project"
        self.src_prefix = f"RiskSpectrum export of {pid}, {stamp}"

        # outputs
        self.parameters: dict[str, dict] = OrderedDict()
        self.basic_events: dict[str, dict] = OrderedDict()
        self.be_system: dict[str, str] = {}
        self.house: dict[str, dict] = OrderedDict()
        self.fault_trees: dict[str, dict] = OrderedDict()
        self.ccf: dict[str, dict] = OrderedDict()
        self.event_trees: dict[str, dict] = OrderedDict()
        self.configurations: dict[str, dict] = OrderedDict()

        # indexes
        self.param_rows = {col(r, "id"): r for r in tables["parameters"]
                           if col(r, "id")}
        self.be_rows = {col(r, "id"): r for r in tables["basic_events"]
                        if col(r, "id")}
        self.he_rows = {col(r, "id"): r for r in tables["house_events"]
                        if col(r, "id")}
        self.gate_rows = {col(r, "id"): r for r in tables["gates"]
                          if col(r, "id")}
        self.be_model: dict[str, str] = {}        # rs id -> model kind
        self.be_point: dict[str, float] = {}      # rs id -> point value
        self.frequency_bes: set[str] = set()

    # -- provenance -------------------------------------------------------
    def prov(self, row: dict, kind: str, orig: str) -> dict:
        src = col(row, "reference", "source")
        if not src:
            src = f"{self.src_prefix}: {kind} {orig}"
        just = col(row, "comment", "justification")
        if not just:
            just = PLACEHOLDER
            self.log.placeholders += 1
        return {"source": src, "justification": just}

    # -- parameters -------------------------------------------------------
    def unit_for(self, ptype: str) -> str:
        return {"probability": "per_demand", "rate": "per_hour",
                "frequency": self.freq_unit, "time": self.time_unit,
                "dimensionless": "dimensionless"}[ptype]

    def uncertainty(self, row: dict, ctx: str) -> dict | None:
        dist = col(row, "distribution", "dist").lower()
        if dist in ("", "none", "point", "constant", "-"):
            return None
        p1, p2 = as_float(row.get("p1")), as_float(row.get("p2"))
        if dist in ("lognormal", "log-normal", "ln"):
            if p1 is None or p1 <= 1:
                self.log.warn(f"{ctx}: lognormal needs error factor > 1 "
                              f"(got {row.get('p1')!r}); distribution dropped")
                return None
            return {"distribution": "lognormal", "error_factor": p1}
        if dist == "beta":
            if not (p1 and p2 and p1 > 0 and p2 > 0):
                self.log.warn(f"{ctx}: beta needs alpha, beta > 0; dropped")
                return None
            return {"distribution": "beta", "alpha": p1, "beta": p2}
        if dist == "gamma":
            if not (p1 and p2 and p1 > 0 and p2 > 0):
                self.log.warn(f"{ctx}: gamma needs shape, scale > 0; dropped")
                return None
            return {"distribution": "gamma", "shape": p1, "scale": p2}
        if dist == "uniform":
            if p1 is None or p2 is None or p1 < 0 or p2 <= 0:
                self.log.warn(f"{ctx}: uniform needs lower >= 0, upper > 0; "
                              f"dropped")
                return None
            return {"distribution": "uniform", "lower": p1, "upper": p2}
        self.log.warn(f"{ctx}: distribution {dist!r} has no Canopy "
                      f"equivalent; point value kept, distribution dropped")
        return None

    def convert_parameters(self) -> None:
        for rs_id, row in sorted(self.param_rows.items()):
            ptype = PARAM_TYPES.get(col(row, "type").lower())
            if ptype is None:
                self.log.error(f"parameter {rs_id}: unknown type "
                               f"{col(row, 'type')!r}")
                continue
            value = as_float(row.get("value"))
            if value is None or value < 0:
                self.log.error(f"parameter {rs_id}: value {row.get('value')!r} "
                               f"is not a non-negative number")
                continue
            pid = self.names.get("PAR", rs_id)
            entry = OrderedDict()
            entry["label"] = label_for(col(row, "description"), rs_id)
            entry["value"] = value
            entry["unit"] = self.unit_for(ptype)
            unc = self.uncertainty(row, f"parameter {rs_id}")
            if unc:
                entry["uncertainty"] = unc
            entry["provenance"] = self.prov(row, "parameter", rs_id)
            entry["external_ids"] = ext(rs_id)
            self.parameters[pid] = entry

    # -- quantities -------------------------------------------------------
    def quantity(self, text, expected_type: str, ctx: str):
        """A numeric column is a literal or a parameter id. Returns
        (yaml_value, point_float) or (None, None) after logging an error."""
        if text in (None, ""):
            return None, None
        if isinstance(text, (int, float)) or as_float(text) is not None:
            v = as_float(text)
            if v < 0:
                self.log.error(f"{ctx}: negative value {v}")
                return None, None
            return ({"value": v, "unit": self.unit_for(expected_type)}, v)
        rs_pid = str(text)
        if rs_pid not in self.param_rows:
            self.log.error(f"{ctx}: {rs_pid!r} is neither a number nor a "
                           f"parameter id")
            return None, None
        prow = self.param_rows[rs_pid]
        ptype = PARAM_TYPES.get(col(prow, "type").lower())
        if ptype != expected_type:
            self.log.warn(f"{ctx}: parameter {rs_pid} has type {ptype!r}, "
                          f"expected {expected_type!r}")
        pid = self.names.get("PAR", rs_pid)
        return ({"param": pid}, as_float(prow.get("value")))

    # -- basic events -----------------------------------------------------
    @staticmethod
    def periodic_test_q(rate: float, interval: float) -> float:
        x = rate * interval
        return 0.0 if x == 0 else 1.0 - (1.0 - math.exp(-x)) / x

    def convert_basic_events(self) -> None:
        for rs_id, row in sorted(self.be_rows.items()):
            model = BE_MODELS.get(col(row, "model", "type").lower())
            ctx = f"basic event {rs_id}"
            if model is None:
                self.log.error(f"{ctx}: unknown reliability model "
                               f"{col(row, 'model', 'type')!r}")
                continue
            self.be_model[rs_id] = model
            fm: dict | None = None
            point: float | None = None
            just_extra = ""

            if model == "frequency":
                # Initiators live in event trees, never in basic-events/
                # (the engine refuses frequency-type basic events at load).
                q, point = self.quantity(col(row, "q", "frequency", "value"),
                                         "frequency", ctx)
                self.frequency_bes.add(rs_id)
                self.be_point[rs_id] = point if point is not None else 0.0
                continue

            if model == "probability":
                q, point = self.quantity(col(row, "q", "value", "probability"),
                                         "probability", ctx)
                if q is None:
                    self.log.error(f"{ctx}: probability model needs q")
                    continue
                fm = {"type": "probability", "value": q}

            elif model == "mission":
                r, rv = self.quantity(col(row, "rate", "lambda"), "rate", ctx)
                tm, tv = self.quantity(col(row, "mission_time", "tm"), "time",
                                       ctx)
                if r is None or tm is None:
                    self.log.error(f"{ctx}: mission model needs rate and "
                                   f"mission_time")
                    continue
                fm = {"type": "rate-mission", "rate": r, "mission_time": tm}
                point = 1.0 - math.exp(-rv * tv)

            elif model == "repairable":
                r, rv = self.quantity(col(row, "rate", "lambda"), "rate", ctx)
                tr, trv = self.quantity(col(row, "repair_time", "mttr", "tr"),
                                        "time", ctx)
                if r is None or tr is None:
                    self.log.error(f"{ctx}: repairable model needs rate and "
                                   f"repair_time")
                    continue
                fm = {"type": "rate-repair", "rate": r, "mttr": tr}
                point = (rv * trv) / (1.0 + rv * trv)

            elif model == "tested":
                r, rv = self.quantity(col(row, "rate", "lambda"), "rate", ctx)
                ti, tiv = self.quantity(col(row, "test_interval", "ti"),
                                        "time", ctx)
                if r is None or ti is None:
                    self.log.error(f"{ctx}: tested model needs rate and "
                                   f"test_interval")
                    continue
                extras = {k: as_float(row.get(k)) for k in
                          ("repair_time", "test_duration", "first_test")}
                extras = {k: v for k, v in extras.items() if v}
                q_mean = as_float(row.get("q_mean"))
                ideal = self.periodic_test_q(rv, tiv)
                if not extras:
                    fm = {"type": "rate-periodic-test", "rate": r,
                          "test_interval": ti}
                    point = ideal
                elif q_mean is not None:
                    fm = {"type": "probability",
                          "value": {"value": q_mean, "unit": "per_demand"}}
                    point = q_mean
                    just_extra = (
                        f" Point value is RiskSpectrum's computed q_mean for "
                        f"a Tested model (rate={rv}, test_interval={tiv}, "
                        + ", ".join(f"{k}={v}" for k, v in extras.items())
                        + f"); Canopy's idealized rate-periodic-test would "
                        f"give {ideal:.6e}.")
                    self.log.warn(f"{ctx}: Tested model with "
                                  f"{sorted(extras)} emitted as point "
                                  f"probability q_mean={q_mean:.6e} "
                                  f"(idealized formula: {ideal:.6e})")
                else:
                    fm = {"type": "rate-periodic-test", "rate": r,
                          "test_interval": ti}
                    point = ideal
                    self.log.warn(
                        f"{ctx}: Tested model has {sorted(extras)} which "
                        f"Canopy's idealized rate-periodic-test ignores and "
                        f"no q_mean column to fall back on — expect a "
                        f"difference against RiskSpectrum for this event")

            self.be_point[rs_id] = point if point is not None else 0.0
            bid = self.names.get("BE", rs_id)
            entry = OrderedDict()
            entry["label"] = label_for(col(row, "description"), rs_id)
            for k in ("system", "component", "failure_mode"):
                v = col(row, k)
                if v:
                    entry[k] = v
            entry["failure_model"] = fm
            unc = self.uncertainty(row, ctx)
            if unc:
                entry["uncertainty"] = unc
            p = self.prov(row, "basic event", rs_id)
            if just_extra:
                p["justification"] = (p["justification"] + just_extra).strip()
            entry["provenance"] = p
            entry["external_ids"] = ext(rs_id)
            self.basic_events[bid] = entry
            self.be_system[bid] = col(row, "system")

    # -- house events -----------------------------------------------------
    def convert_house_events(self) -> None:
        for rs_id, row in sorted(self.he_rows.items()):
            v = as_bool(col(row, "value", "default", default="false"))
            if v is None:
                self.log.error(f"house event {rs_id}: value "
                               f"{row.get('value')!r} is not TRUE/FALSE")
                continue
            hid = self.names.get("HE", rs_id)
            self.house[hid] = OrderedDict([
                ("label", label_for(col(row, "description"), rs_id)),
                ("default", v),
                ("provenance", self.prov(row, "house event", rs_id)),
                ("external_ids", ext(rs_id)),
            ])

    # -- gates and fault trees --------------------------------------------
    def resolve_input(self, ref: str, itype: str, ctx: str) -> str | None:
        itype = (itype or "").upper()
        cands = []
        if itype in ("", "GATE", "GT") and ref in self.gate_rows:
            cands.append(("GT", ref))
        if itype in ("", "BE", "BASIC", "BASIC_EVENT") and ref in self.be_rows:
            cands.append(("BE", ref))
        if itype in ("", "HE", "HOUSE", "HOUSE_EVENT") and ref in self.he_rows:
            cands.append(("HE", ref))
        if not cands:
            self.log.error(f"{ctx}: input {ref!r} is not a known gate, basic "
                           f"event or house event")
            return None
        if len(cands) > 1:
            self.log.error(f"{ctx}: input {ref!r} is ambiguous "
                           f"({', '.join(p for p, _ in cands)}); add an "
                           f"input_type column")
            return None
        prefix, orig = cands[0]
        if prefix == "BE":
            if orig in self.frequency_bes:
                self.log.unsupported(f"{ctx}: frequency-type event {orig} "
                                     f"used inside a fault tree (initiator "
                                     f"fault trees are not supported)")
                return None
            if orig not in self.be_model:
                return None      # already reported as an error
        return self.names.get(prefix, orig)

    def convert_gates(self) -> None:
        inputs: dict[str, list[tuple[float, str, str]]] = defaultdict(list)
        for row in self.t["gate_inputs"]:
            g = col(row, "gate")
            if not g:
                continue
            pos = as_float(row.get("position"))
            inputs[g].append((pos if pos is not None else 1e9,
                              col(row, "input"), col(row, "input_type")))

        formulas: dict[str, object] = {}
        gate_ft: dict[str, str] = {}
        for rs_id, row in sorted(self.gate_rows.items()):
            ctx = f"gate {rs_id}"
            gtype = col(row, "type").upper().replace("/", "")
            ops: list[str] = []
            seen: set[str] = set()
            for _, ref, itype in sorted(inputs.get(rs_id, []),
                                        key=lambda x: (x[0], x[1])):
                cid = self.resolve_input(ref, itype, ctx)
                if cid is None:
                    continue
                if cid in seen:
                    self.log.warn(f"{ctx}: duplicate input {ref} removed")
                    continue
                seen.add(cid)
                ops.append(cid)
            if not ops:
                self.log.error(f"{ctx}: gate has no (resolvable) inputs")
                continue
            if gtype in ("AND", "OR", "XOR"):
                f = ops[0] if len(ops) == 1 else {gtype.lower(): ops}
                if len(ops) == 1:
                    self.log.note(f"{ctx}: single-input {gtype} gate emitted "
                                  f"as pass-through")
            elif gtype in ("KN", "KOFN", "VOTE", "ATLEAST", "K"):
                k = as_float(row.get("k"))
                if k is None or int(k) < 1 or int(k) > len(ops):
                    self.log.error(f"{ctx}: K/N gate needs 1 <= k <= "
                                   f"{len(ops)} (got {row.get('k')!r})")
                    continue
                k = int(k)
                if k == 1:
                    f = {"or": ops} if len(ops) > 1 else ops[0]
                elif k == len(ops):
                    f = {"and": ops} if len(ops) > 1 else ops[0]
                else:
                    f = {"atleast": {"k": k, "of": ops}}
            elif gtype == "NOT":
                if len(ops) != 1:
                    self.log.error(f"{ctx}: NOT gate needs exactly one input")
                    continue
                f = {"not": ops[0]}
            elif gtype in ("NAND", "NOR"):
                inner = ops[0] if len(ops) == 1 else {gtype[1:].lower(): ops}
                f = {"not": inner}
            else:
                self.log.error(f"{ctx}: unknown gate type {gtype!r}")
                continue
            gid = self.names.get("GT", rs_id)
            formulas[gid] = f
            gate_ft[gid] = col(row, "fault_tree", "ft")

        # Fault trees: one per RiskSpectrum FT record; gates go to the FT
        # that owns them. Gates with no owning FT are attached to the FT of
        # a parent gate, else each becomes its own single-gate tree so the
        # ID space stays complete (the validator warns on orphans).
        parents: dict[str, str] = {}
        for gid, f in formulas.items():
            for ref in _refs(f):
                if ref.startswith("GT-") and ref not in parents:
                    parents[ref] = gid
        changed = True
        while changed:
            changed = False
            for gid in formulas:
                if not gate_ft.get(gid) and gid in parents and \
                        gate_ft.get(parents[gid]):
                    gate_ft[gid] = gate_ft[parents[gid]]
                    changed = True

        ft_rows = {col(r, "id"): r for r in self.t["fault_trees"]
                   if col(r, "id")}
        by_ft: dict[str, list[str]] = defaultdict(list)
        for gid in formulas:
            by_ft[gate_ft.get(gid, "")].append(gid)

        # A fault tree named by gates but absent from the fault_trees table
        # (e.g. an export without that table): synthesize it when exactly
        # one of its gates is not an input of another gate in the same tree.
        refs_in_ft: dict[str, set[str]] = defaultdict(set)
        for gid, f in formulas.items():
            for ref in _refs(f):
                if ref.startswith("GT-"):
                    refs_in_ft[gate_ft.get(gid, "")].add(ref)
        for rs_ft, gids in list(by_ft.items()):
            if not rs_ft or rs_ft in ft_rows:
                continue
            tops = [g for g in gids if g not in refs_in_ft[rs_ft]]
            if len(tops) == 1:
                ft_rows[rs_ft] = {"id": rs_ft, "description": "",
                                  "top_gate": self.names.origin[tops[0]]}
                self.log.note(f"fault tree {rs_ft}: not in the fault_trees "
                              f"table; synthesized with top gate "
                              f"{self.names.origin[tops[0]]}")
            else:
                self.log.warn(f"fault tree {rs_ft}: not in the fault_trees "
                              f"table and its top gate is ambiguous "
                              f"({len(tops)} candidates); gates emitted as "
                              f"single-gate trees")

        for rs_ft, row in sorted(ft_rows.items()):
            top = col(row, "top_gate", "top")
            if not self.names.has("GT", top):
                self.log.error(f"fault tree {rs_ft}: top gate {top!r} is not "
                               f"a converted gate")
                continue
            ftid = self.names.get("FT", rs_ft)
            gates = OrderedDict()
            for gid in sorted(by_ft.get(rs_ft, [])):
                gates[gid] = OrderedDict([
                    ("label", label_for(
                        col(self.gate_rows[self.names.origin[gid]],
                            "description"), self.names.origin[gid])),
                    ("formula", formulas[gid]),
                    ("external_ids", ext(self.names.origin[gid])),
                ])
            self.fault_trees[ftid] = OrderedDict([
                ("label", label_for(col(row, "description"), rs_ft)),
                ("top_gate", self.names.get("GT", top)),
                ("gates", gates),
                ("external_ids", ext(rs_ft)),
            ])
        stray = [g for ft, gs in by_ft.items() if ft not in ft_rows
                 for g in gs]
        for gid in sorted(stray):
            orig = self.names.origin[gid]
            ftid = self.names.get("FT", orig)
            self.log.warn(f"gate {orig}: no owning fault tree in the export; "
                          f"emitted as its own tree {ftid}")
            self.fault_trees[ftid] = OrderedDict([
                ("label", f"{orig} (gate without a fault tree in "
                          f"RiskSpectrum)"),
                ("top_gate", gid),
                ("gates", {gid: OrderedDict([
                    ("label", label_for(
                        col(self.gate_rows[orig], "description"), orig)),
                    ("formula", formulas[gid]),
                    ("external_ids", ext(orig))])}),
                ("external_ids", ext(orig)),
            ])
        self.formulas = formulas

    # -- CCF groups -------------------------------------------------------
    @staticmethod
    def binom(n: int, k: int) -> float:
        return float(math.comb(n, k))

    @classmethod
    def mgl_to_alpha(cls, m: int, rho: list[float]) -> list[float]:
        """rho = [rho_2, ..., rho_m]; returns [alpha_1, ..., alpha_m]
        (non-staggered NUREG/CR-5485 relations; see module docstring)."""
        if len(rho) != m - 1:
            raise ValueError(f"MGL group of size {m} needs {m - 1} factors "
                             f"rho_2..rho_{m}")
        r = [1.0] + list(rho) + [0.0]          # rho_1 .. rho_{m+1}
        q = []                                   # Q_k / Q_t
        for k in range(1, m + 1):
            prod = 1.0
            for i in range(1, k + 1):
                prod *= r[i - 1]
            q.append(prod * (1.0 - r[k]) / cls.binom(m - 1, k - 1))
        w = [cls.binom(m, k + 1) * q[k] for k in range(m)]
        s = sum(w)
        return [x / s for x in w]

    def convert_ccf(self) -> None:
        members: dict[str, list[tuple[float, str]]] = defaultdict(list)
        for row in self.t["ccf_members"]:
            g = col(row, "group")
            if g:
                pos = as_float(row.get("position"))
                members[g].append((pos if pos is not None else 1e9,
                                   col(row, "member")))
        factors: dict[str, dict[str, str]] = defaultdict(dict)
        for row in self.t["ccf_factors"]:
            g = col(row, "group")
            if g:
                factors[g][col(row, "name").lower()] = col(row, "value")

        for row in sorted(self.t["ccf_groups"], key=lambda r: col(r, "id")):
            rs_id = col(row, "id")
            if not rs_id:
                continue
            ctx = f"CCF group {rs_id}"
            model = col(row, "model").lower().replace("-factor", "").strip()
            mem = [m for _, m in sorted(members.get(rs_id, []),
                                        key=lambda x: (x[0], x[1]))]
            n = len(mem)
            if n < 2:
                self.log.error(f"{ctx}: needs >= 2 members")
                continue
            if n > 8:
                self.log.unsupported(f"{ctx}: {n} members exceeds Canopy's "
                                     f"cap of 8")
                continue
            bad = [m for m in mem if m not in self.be_model or
                   self.be_model.get(m) == "frequency"]
            if bad:
                self.log.error(f"{ctx}: members {bad} are not converted basic "
                               f"events")
                continue
            cids = [self.names.get("BE", m) for m in mem]

            # total probability: explicit column, else the members' common
            # reliability data.
            total_text = col(row, "total", "total_probability", "q")
            if total_text:
                total, _ = self.quantity(total_text, "probability", ctx)
            else:
                fms = [json.dumps(self.basic_events[c]["failure_model"],
                                  sort_keys=True) for c in cids]
                if len(set(fms)) != 1:
                    self.log.error(f"{ctx}: no total given and members have "
                                   f"different reliability data; add a "
                                   f"total column")
                    continue
                fm0 = self.basic_events[cids[0]]["failure_model"]
                if fm0["type"] == "probability":
                    total = fm0["value"]
                else:
                    pv = self.be_point[mem[0]]
                    total = {"value": pv, "unit": "per_demand"}
                    self.log.note(f"{ctx}: total probability taken as the "
                                  f"members' point unavailability {pv:.6e} "
                                  f"({fm0['type']} model)")
            if total is None:
                continue

            fac = factors.get(rs_id, {})
            out_factors: dict[str, float] = OrderedDict()
            testing = col(row, "testing").lower()
            if model == "beta":
                b = self.fvalue(fac.get("beta"), ctx)
                if b is None or not (0.0 < b < 1.0):
                    self.log.error(f"{ctx}: beta-factor needs 0 < beta < 1")
                    continue
                cmodel = "beta-factor"
                out_factors["beta"] = b
            elif model == "alpha":
                alphas = []
                for k in range(1, n + 1):
                    v = self.fvalue(fac.get(f"alpha_{k}", fac.get(f"alpha{k}")),
                                    ctx)
                    if v is None:
                        self.log.error(f"{ctx}: alpha-factor group of size "
                                       f"{n} needs alpha_1..alpha_{n}")
                        break
                    alphas.append(v)
                if len(alphas) != n:
                    continue
                if abs(sum(alphas) - 1.0) > 1e-2:
                    self.log.error(f"{ctx}: alpha factors sum to "
                                   f"{sum(alphas):.4f}, expected 1")
                    continue
                cmodel = "alpha-factor"
                for k, v in enumerate(alphas, start=1):
                    out_factors[f"alpha_{k}"] = v
            elif model == "mgl":
                if not self.o["mgl_to_alpha"]:
                    self.log.unsupported(f"{ctx}: MGL model (Canopy rejects "
                                         f"MGL; re-run with --mgl-to-alpha "
                                         f"to convert)")
                    continue
                rho = []
                for k in range(2, n + 1):
                    key = f"rho_{k}"
                    alt = [nm for nm, idx in MGL_NAMES.items() if idx == k]
                    v = self.fvalue(fac.get(key, fac.get(alt[0]) if alt else
                                            None), ctx)
                    if v is None:
                        self.log.error(f"{ctx}: MGL group of size {n} needs "
                                       f"rho_2..rho_{n} (or beta, gamma, ...)")
                        break
                    rho.append(v)
                if len(rho) != n - 1:
                    continue
                alphas = self.mgl_to_alpha(n, rho)
                cmodel = "alpha-factor"
                for k, v in enumerate(alphas, start=1):
                    out_factors[f"alpha_{k}"] = v
                testing = "non-staggered"
                self.log.warn(f"{ctx}: MGL factors {rho} converted to alpha "
                              f"factors {[round(a, 6) for a in alphas]} "
                              f"(non-staggered NUREG/CR-5485 relations); "
                              f"verify one expanded CCF event against "
                              f"RiskSpectrum")
            else:
                self.log.unsupported(f"{ctx}: CCF model {model!r} has no "
                                     f"Canopy equivalent")
                continue

            gid = self.names.get("CCF", rs_id)
            entry = OrderedDict()
            entry["label"] = label_for(col(row, "description"), rs_id)
            entry["model"] = cmodel
            entry["members"] = cids
            entry["total_probability"] = total
            entry["factors"] = out_factors
            if testing in ("staggered", "non-staggered"):
                entry["testing"] = testing
            elif testing:
                self.log.error(f"{ctx}: testing must be staggered or "
                               f"non-staggered (got {testing!r})")
                continue
            else:
                self.log.warn(f"{ctx}: no testing convention in the export; "
                              f"Canopy's default (staggered) applies — "
                              f"confirm against RiskSpectrum's CCF setting")
            p = self.prov(row, "CCF group", rs_id)
            if model == "mgl":
                p["justification"] = (p["justification"] +
                                      f" Converted from MGL factors "
                                      f"{rho} by import_riskspectrum.py "
                                      f"--mgl-to-alpha.")
            entry["provenance"] = p
            entry["external_ids"] = ext(rs_id)
            self.ccf[gid] = entry

    def fvalue(self, text, ctx: str) -> float | None:
        """A CCF factor: literal or dimensionless parameter id."""
        if text in (None, ""):
            return None
        v = as_float(text)
        if v is not None:
            return v
        if str(text) in self.param_rows:
            return as_float(self.param_rows[str(text)].get("value"))
        self.log.error(f"{ctx}: factor {text!r} is neither a number nor a "
                       f"parameter id")
        return None

    # -- boundary conditions ----------------------------------------------
    def bc_entries(self, bc_id: str, ctx: str) -> dict[str, bool]:
        """House-event overrides of one BC set. Basic-event forcing and
        exchange entries are unsupported (they change the logic)."""
        out: dict[str, bool] = {}
        if not bc_id:
            return out
        if bc_id not in self.bc_index:
            self.log.error(f"{ctx}: boundary condition set {bc_id!r} not in "
                           f"the export")
            return out
        for row in self.bc_index[bc_id]:
            target, value = col(row, "target"), col(row, "value")
            if target in self.he_rows:
                b = as_bool(value)
                if b is None:
                    self.log.error(f"{ctx}: BC set {bc_id}: {target}="
                                   f"{value!r} is not TRUE/FALSE")
                    continue
                out[self.names.get("HE", target)] = b
            elif target in self.be_rows:
                self.log.unsupported(f"{ctx}: BC set {bc_id} forces basic "
                                     f"event {target} to {value!r} (Canopy "
                                     f"has no per-sequence basic-event "
                                     f"override; model it as a house event)")
            else:
                self.log.error(f"{ctx}: BC set {bc_id}: target {target!r} is "
                               f"not a house event or basic event")
        return out

    def convert_bc_sets(self) -> None:
        self.bc_index: dict[str, list[dict]] = defaultdict(list)
        for row in self.t["bc_set_entries"]:
            if col(row, "bc_set"):
                self.bc_index[col(row, "bc_set")].append(row)
        self.bc_desc = {col(r, "id"): col(r, "description")
                        for r in self.t["bc_sets"] if col(r, "id")}
        self.configurations["BASE"] = OrderedDict([
            ("label", "All house events at their declared default"),
            ("house_events", {}),
        ])
        for bc_id in sorted(set(self.bc_index) | set(self.bc_desc)):
            he = self.bc_entries(bc_id, f"BC set {bc_id}")
            if not he:
                continue
            cid = Names.sanitize(bc_id)
            if cid in self.configurations:
                cid = cid + "-BC"
            self.configurations[cid] = OrderedDict([
                ("label", self.bc_desc.get(bc_id) or
                 f"RiskSpectrum boundary condition set {bc_id}"),
                ("house_events", OrderedDict(sorted(he.items()))),
            ])

    # -- event trees ------------------------------------------------------
    def convert_event_trees(self) -> None:
        fe_desc = {col(r, "id"): col(r, "description")
                   for r in self.t["function_events"] if col(r, "id")}
        cols: dict[str, list[tuple[float, str, str]]] = defaultdict(list)
        for row in self.t["et_columns"]:
            et = col(row, "event_tree", "et")
            if et:
                pos = as_float(row.get("position"))
                cols[et].append((pos if pos is not None else 1e9,
                                 col(row, "function_event", "fe"),
                                 col(row, "logic", "gate")))
        seq_rows: dict[str, list[dict]] = defaultdict(list)
        for row in self.t["sequences"]:
            et = col(row, "event_tree", "et")
            if et:
                seq_rows[et].append(row)
        branches: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for row in self.t["sequence_branches"]:
            key = (col(row, "event_tree", "et"), col(row, "sequence", "seq"))
            branches[key].append(row)
        cons_desc = {col(r, "id"): col(r, "description")
                     for r in self.t["consequences"] if col(r, "id")}
        et_ids_all = {col(r, "id") for r in self.t["event_trees"]}

        for row in sorted(self.t["event_trees"], key=lambda r: col(r, "id")):
            rs_et = col(row, "id")
            if not rs_et:
                continue
            ctx = f"event tree {rs_et}"
            init = col(row, "initiator", "initiating_event")
            if init in self.gate_rows and init not in self.frequency_bes:
                self.log.unsupported(f"{ctx}: initiator {init} is a gate "
                                     f"(initiator fault trees are not "
                                     f"supported; give the initiating event "
                                     f"a frequency)")
                continue
            if init not in self.frequency_bes:
                self.log.error(f"{ctx}: initiator {init!r} is not a "
                               f"frequency-type basic event")
                continue
            irow = self.be_rows[init]
            fq = OrderedDict([("value", self.be_point[init]),
                              ("unit", self.freq_unit)])
            if self.freq_unit != "per_year":
                self.log.error(f"{ctx}: Canopy requires initiating-event "
                               f"frequencies in per_year; convert the export "
                               f"or use --frequency-unit per_year")
                continue
            unc = self.uncertainty(irow, f"initiating event {init}")
            if unc is None:
                # RiskSpectrum keeps the distribution on the parameter.
                ptext = col(irow, "q", "frequency", "value")
                if ptext in self.param_rows:
                    unc = self.uncertainty(self.param_rows[ptext],
                                           f"initiating event {init}")
            if unc:
                fq["uncertainty"] = unc
            etid = self.names.get("ET", rs_et)
            ie = OrderedDict([
                ("id", self.names.get("IE", init)),
                ("label", label_for(col(irow, "description"), init)),
                ("frequency", fq),
                ("provenance", self.prov(irow, "initiating event", init)),
                ("external_ids", ext(init)),
            ])

            # columns
            fes = OrderedDict()
            fe_map: dict[str, str] = {}
            ok = True
            for _, fe, logic in sorted(cols.get(rs_et, []),
                                       key=lambda x: (x[0], x[1])):
                if not fe:
                    continue
                # Functional-event IDs are scoped to their event tree in
                # Canopy, so a RiskSpectrum function event reused across
                # trees keeps one ID everywhere.
                feid = self.names.get("FE", fe)
                fe_map[fe] = feid
                if logic in self.gate_rows and self.names.has("GT", logic):
                    top = self.names.get("GT", logic)
                elif logic in self.be_rows and logic in self.be_model and \
                        logic not in self.frequency_bes:
                    top = self.pass_through_gate(logic, rs_et, fe)
                else:
                    self.log.error(f"{ctx}: column {fe} links to {logic!r}, "
                                   f"which is not a converted gate or basic "
                                   f"event")
                    ok = False
                    continue
                fes[feid] = OrderedDict([
                    ("label", label_for(fe_desc.get(fe, ""), fe)),
                    ("top_gate", top),
                    ("external_ids", ext(fe)),
                ])
            if not ok:
                continue
            if not fes:
                self.log.error(f"{ctx}: no columns (et_columns rows)")
                continue

            et_he = self.bc_entries(col(row, "bc_set"), ctx)
            seqs = OrderedDict()
            seen_paths: dict[tuple, str] = {}
            for srow in sorted(seq_rows.get(rs_et, []),
                               key=lambda r: _seq_sort_key(col(r, "sequence"))):
                rs_seq = col(srow, "sequence")
                sctx = f"{ctx} sequence {rs_seq}"
                path = OrderedDict((feid, "bypassed") for feid in fes)
                he = dict(et_he)
                for brow in sorted(branches.get((rs_et, rs_seq), []),
                                   key=lambda r: list(fe_map).index(
                                       col(r, "function_event", "fe"))
                                   if col(r, "function_event", "fe") in fe_map
                                   else 1e9):
                    fe = col(brow, "function_event", "fe")
                    if fe not in fe_map:
                        self.log.error(f"{sctx}: branch on unknown column "
                                       f"{fe!r}")
                        continue
                    outcome = _outcome(col(brow, "outcome"))
                    if outcome is None:
                        self.log.error(f"{sctx}: outcome "
                                       f"{col(brow, 'outcome')!r} is not "
                                       f"S/F/-")
                        continue
                    path[fe_map[fe]] = outcome
                    he.update(self.bc_entries(col(brow, "bc_set"), sctx))
                key = tuple(path.items())
                if key in seen_paths:
                    self.log.unsupported(f"{sctx}: duplicate sequence path "
                                         f"(same as {seen_paths[key]})")
                    continue
                seen_paths[key] = rs_seq
                seqid = self.names.get("SEQ", f"{rs_et}-{rs_seq}")
                entry = OrderedDict([("path", path)])
                transfer = col(srow, "transfer")
                cons = col(srow, "consequence", "end_state")
                if transfer:
                    if transfer not in et_ids_all:
                        self.log.warn(f"{sctx}: transfer target {transfer!r} "
                                      f"is not in the export")
                    tid = self.names.get("ET", transfer)
                    entry["end_state"] = f"XFER-{tid[3:]}"
                    entry["transfer"] = tid
                else:
                    entry["end_state"] = Names.sanitize(cons) if cons else "OK"
                    if cons and cons not in cons_desc and cons_desc:
                        self.log.warn(f"{sctx}: consequence {cons!r} is not "
                                      f"in the consequences table")
                if he:
                    entry["house_events"] = OrderedDict(sorted(he.items()))
                entry["external_ids"] = ext(rs_seq)
                seqs[seqid] = entry
            if not seqs:
                self.log.error(f"{ctx}: no sequences")
                continue
            self.event_trees[etid] = OrderedDict([
                ("id", etid),
                ("label", label_for(col(row, "description"), rs_et)),
                ("initiating_event", ie),
                ("functional_events", fes),
                ("sequences", seqs),
                ("external_ids", ext(rs_et)),
            ])

    def pass_through_gate(self, be: str, rs_et: str, fe: str) -> str:
        """A column linked directly to a basic event gets a pass-through
        gate (Canopy functional events point at gates)."""
        gid = self.names.get("GT", f"FE-{be}")
        ftid = self.names.get("FT", f"FE-{be}")
        if ftid not in self.fault_trees:
            self.fault_trees[ftid] = OrderedDict([
                ("label", f"Pass-through for event-tree column {fe} "
                          f"(basic event {be})"),
                ("top_gate", gid),
                ("gates", {gid: OrderedDict([
                    ("label", f"{fe}: {be} (pass-through)"),
                    ("formula", self.names.get("BE", be)),
                    ("external_ids", ext(be))])}),
                ("external_ids", ext(f"{rs_et}/{fe}")),
            ])
            self.log.note(f"event tree {rs_et}: column {fe} links to basic "
                          f"event {be}; pass-through gate {gid} created")
        return gid

    # -- unsupported tables -----------------------------------------------
    def check_unsupported(self) -> None:
        for row in self.t["exchange_events"]:
            self.log.unsupported(
                f"exchange event {col(row, 'id')} ({col(row, 'original')} -> "
                f"{col(row, 'replacement')}, condition "
                f"{col(row, 'condition') or 'n/a'}): Canopy has no exchange "
                f"construct yet; model the alternative as a house-event "
                f"selected gate")

    # -- driver -----------------------------------------------------------
    def run(self) -> None:
        self.convert_parameters()
        self.convert_basic_events()
        self.convert_house_events()
        self.convert_gates()
        self.convert_ccf()
        self.convert_bc_sets()
        self.convert_event_trees()
        self.check_unsupported()


def _refs(f):
    if isinstance(f, str):
        yield f
        return
    (op, args), = f.items()
    if op == "not":
        yield from _refs(args)
    elif op == "atleast":
        for a in args["of"]:
            yield from _refs(a)
    else:
        for a in args:
            yield from _refs(a)


def _outcome(text: str) -> str | None:
    t = (text or "").strip().lower()
    if t in ("s", "success", "ok", "up", "0", "true"):
        return "success"
    if t in ("f", "failure", "fail", "failed", "down", "1", "false"):
        return "failure"
    if t in ("", "-", "b", "bypass", "bypassed", "n/a", "na", "none"):
        return "bypassed"
    return None


def _seq_sort_key(s: str):
    m = re.match(r"^(\D*)(\d+)(.*)$", s or "")
    return (m.group(1), int(m.group(2)), m.group(3)) if m else (s or "", 0, "")


# ---------------------------------------------------------------------------
# YAML emission
# ---------------------------------------------------------------------------

class _Dumper(yaml.SafeDumper):
    """No anchors/aliases ever (design rule 3), mappings in insertion
    order, block style throughout."""

    def ignore_aliases(self, data):
        return True


def _represent_odict(dumper, data):
    return dumper.represent_mapping("tag:yaml.org,2002:map", data.items())


_Dumper.add_representer(OrderedDict, _represent_odict)
_Dumper.add_representer(dict, _represent_odict)


def dump_yaml(path: str, obj, header: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.dump(obj, f, Dumper=_Dumper, sort_keys=False,
                  default_flow_style=False, allow_unicode=True, width=88)


def write_model(cv: Converter, out_dir: str, opts: dict, log: Log) -> None:
    gen = (f"# Generated by ci/import_riskspectrum.py from "
           f"{os.path.basename(opts['source'])} — re-run the converter "
           f"rather than editing by hand while the RiskSpectrum model is "
           f"still the source of truth.\n")
    os.makedirs(out_dir, exist_ok=True)
    for sub in ("basic-events", "fault-trees", "event-trees"):
        os.makedirs(os.path.join(out_dir, sub), exist_ok=True)

    metrics = []
    for mid, states in opts["metrics"]:
        metrics.append(OrderedDict([("id", mid), ("label", mid),
                                    ("end_states", states)]))
    includes = OrderedDict([
        ("parameters", ["parameters.yaml"]),
        ("house_events", ["house-events.yaml"]),
    ])
    if cv.ccf:
        includes["ccf_groups"] = ["ccf-groups.yaml"]
    includes["basic_events"] = ["basic-events/*.yaml"]
    includes["fault_trees"] = ["fault-trees/*.yaml"]
    if cv.event_trees:
        includes["event_trees"] = ["event-trees/*.yaml"]
    manifest = OrderedDict([
        ("schema_version", "0.1.0"),
        ("model", OrderedDict([
            ("id", opts["model_id"]),
            ("name", opts["model_name"]),
            ("description", f"Converted from {cv.src_prefix} by "
                            f"ci/import_riskspectrum.py."),
            ("risk_metrics", metrics),
        ])),
        ("includes", includes),
        ("configurations", cv.configurations),
    ])
    dump_yaml(os.path.join(out_dir, "model.yaml"), manifest, gen)
    dump_yaml(os.path.join(out_dir, "parameters.yaml"),
              {"parameters": cv.parameters}, gen)
    dump_yaml(os.path.join(out_dir, "house-events.yaml"),
              {"house_events": cv.house}, gen)
    if cv.ccf:
        dump_yaml(os.path.join(out_dir, "ccf-groups.yaml"),
                  {"ccf_groups": cv.ccf}, gen)

    # basic events: one file per RiskSpectrum system (diff locality), or one
    groups: dict[str, OrderedDict] = defaultdict(OrderedDict)
    for bid, be in cv.basic_events.items():
        sysname = "" if opts["single_file"] else cv.be_system.get(bid, "")
        fname = (Names.sanitize(sysname).lower() if sysname else "imported")
        groups[fname][bid] = be
    for fname, bes in sorted(groups.items()):
        dump_yaml(os.path.join(out_dir, "basic-events", f"{fname}.yaml"),
                  {"basic_events": bes}, gen)

    for ftid, ft in cv.fault_trees.items():
        dump_yaml(os.path.join(out_dir, "fault-trees",
                               f"{ftid.lower()}.yaml"),
                  {"fault_trees": {ftid: ft}}, gen)
    for etid, et in cv.event_trees.items():
        dump_yaml(os.path.join(out_dir, "event-trees",
                               f"{etid.lower()}.yaml"),
                  {"event_tree": et}, gen)

    # conversion log = the migration report
    n_be, n_gt = len(cv.basic_events), sum(len(ft["gates"])
                                          for ft in cv.fault_trees.values())
    lines = [
        "# RiskSpectrum → Canopy conversion log", "",
        f"Source: {cv.src_prefix}  ", f"Converter: ci/import_riskspectrum.py  ",
        f"Run: {_dt.datetime.now().isoformat(timespec='seconds')}", "",
        "## Result", "",
        f"| entity | count |", f"|---|---|",
        f"| parameters | {len(cv.parameters)} |",
        f"| basic events | {n_be} |",
        f"| house events | {len(cv.house)} |",
        f"| gates | {n_gt} |",
        f"| fault trees | {len(cv.fault_trees)} |",
        f"| CCF groups | {len(cv.ccf)} |",
        f"| event trees | {len(cv.event_trees)} |",
        f"| sequences | {sum(len(e['sequences']) for e in cv.event_trees.values())} |",
        f"| configurations (from BC sets) | {len(cv.configurations) - 1} |",
        "",
        f"Provenance placeholders written: **{log.placeholders}** "
        f"(entities with no reference/comment in RiskSpectrum; grep "
        f"`MIGRATED from RiskSpectrum` to find them).", "",
        f"## Warnings ({len(log.warnings)})", "",
    ]
    lines += [f"- {w}" for w in log.warnings] or ["- none"]
    lines += ["", f"## Notes ({len(log.notes)})", ""]
    lines += [f"- {n}" for n in log.notes] or ["- none"]
    lines += ["", "## Next step", "",
              "Validate, quantify, and cross-check against RiskSpectrum's "
              "own results:", "",
              "```", f"python ci/validate.py {out_dir} "
              f"schema/psa-model.schema.json",
              f"python ci/quantify.py {out_dir} converted.json",
              f"python ci/crosscheck_rs.py {out_dir} <rs-results-dir> "
              f"--results converted.json", "```", ""]
    with open(os.path.join(out_dir, "conversion-log.md"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> dict:
    if len(argv) < 2 or argv[0].startswith("-"):
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        sys.exit(2)
    opts = {
        "source": argv[0], "out": argv[1], "model_id": None,
        "model_name": None, "metrics": [], "time_unit": "hour",
        "frequency_unit": "per_year", "mgl_to_alpha": False,
        "allow_unsupported": False, "strict": False, "single_file": False,
    }
    rest = argv[2:]
    i = 0
    while i < len(rest):
        a = rest[i]

        def val():
            nonlocal i
            i += 1
            if i >= len(rest):
                print(f"{a} needs a value", file=sys.stderr)
                sys.exit(2)
            return rest[i]

        if a == "--model-id":
            opts["model_id"] = val()
        elif a == "--model-name":
            opts["model_name"] = val()
        elif a == "--metric":
            spec = val()
            if "=" not in spec:
                print("--metric expects ID=STATE[,STATE...]", file=sys.stderr)
                sys.exit(2)
            mid, states = spec.split("=", 1)
            opts["metrics"].append(
                (Names.sanitize(mid),
                 [Names.sanitize(s) for s in states.split(",") if s.strip()]))
        elif a == "--time-unit":
            opts["time_unit"] = val()
            if opts["time_unit"] not in ("hour", "year"):
                print("--time-unit must be hour or year", file=sys.stderr)
                sys.exit(2)
        elif a == "--frequency-unit":
            opts["frequency_unit"] = val()
            if opts["frequency_unit"] not in ("per_year", "per_hour"):
                print("--frequency-unit must be per_year or per_hour",
                      file=sys.stderr)
                sys.exit(2)
        elif a == "--mgl-to-alpha":
            opts["mgl_to_alpha"] = True
        elif a == "--allow-unsupported":
            opts["allow_unsupported"] = True
        elif a == "--strict":
            opts["strict"] = True
        elif a == "--single-file":
            opts["single_file"] = True
        else:
            print(f"unknown option {a}", file=sys.stderr)
            sys.exit(2)
        i += 1
    return opts


def main(argv: list[str] | None = None) -> int:
    opts = parse_args(sys.argv[1:] if argv is None else argv)
    log = Log(opts["allow_unsupported"])
    try:
        tables = load_tables(opts["source"])
    except ConversionError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    if not tables["basic_events"] and not tables["gates"]:
        print(f"ERROR: {opts['source']}: no basic_events or gates table found "
              f"(expected CSV files named after the tables, or a JSON "
              f"export)", file=sys.stderr)
        return 1
    project = tables["project"][0] if tables["project"] else {}
    if not opts["model_id"]:
        opts["model_id"] = Names.sanitize(col(project, "id", "name")
                                          or "RISKSPECTRUM-IMPORT")
    if not opts["model_name"]:
        opts["model_name"] = (col(project, "name", "id")
                              or "Model converted from RiskSpectrum")
    if not opts["metrics"]:
        log.warn("no --metric given: sequences will not aggregate into any "
                 "risk metric (e.g. --metric CDF=CD)")

    cv = Converter(tables, opts, log)
    cv.run()

    for w in log.warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    for n in log.notes:
        print(f"note: {n}", file=sys.stderr)
    if log.errors:
        for e in log.errors:
            print(f"ERROR:   {e}", file=sys.stderr)
        print(f"conversion aborted: {len(log.errors)} error(s); nothing "
              f"written", file=sys.stderr)
        return 1

    write_model(cv, opts["out"], opts, log)
    n_seq = sum(len(e["sequences"]) for e in cv.event_trees.values())
    print(f"converted {len(cv.basic_events)} basic events, "
          f"{sum(len(f['gates']) for f in cv.fault_trees.values())} gates in "
          f"{len(cv.fault_trees)} fault trees, {len(cv.ccf)} CCF groups, "
          f"{len(cv.event_trees)} event trees / {n_seq} sequences -> "
          f"{opts['out']} ({len(log.warnings)} warning(s), "
          f"{log.placeholders} provenance placeholder(s); see "
          f"{os.path.join(opts['out'], 'conversion-log.md')})")
    if opts["strict"] and log.warnings:
        print("--strict: warnings present, exit 1", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
