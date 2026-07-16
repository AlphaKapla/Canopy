# Model format reference

The model is a set of YAML files under `model/`, validated against
`schema/psa-model.schema.json`. This page documents every entity type and
field. Two rules shape everything:

**Everything is a mapping keyed by a stable ID — never a positional list.**
Reordering entries produces no meaningful diff; adding one entity touches
only its own lines. IDs are immutable and namespaced by prefix:

| Prefix | Entity | Prefix | Entity |
|---|---|---|---|
| `BE-` | basic event | `FE-` | functional event |
| `GT-` | gate | `IE-` | initiating event |
| `FT-` | fault tree | `HE-` | house event |
| `ET-` | event tree | `PAR-` | parameter |
| `SEQ-` | sequence | `CCF-` | common-cause group |

IDs match `^(PREFIX)-[A-Z0-9][A-Z0-9-]*$`. The schema rejects anything
else; the reference linter rejects any dangling reference, so renames are
deliberate, whole-repository operations.

**File layout is a team convention, not a format rule.** Every file holds a
mapping of one-or-many entities of its kind (`fault_trees:`,
`basic_events:` …). The loader merges all files listed in the manifest into
one ID space; file boundaries carry no meaning, and CI enforces ID
uniqueness across files. Small models can live in one file per entity type;
large models should split per system so `git log -- <file>` gives
per-system history and CODEOWNERS can route review.

Two format prohibitions, both diff-motivated: no YAML anchors/aliases
(reuse goes through explicit `{param: PAR-…}` references), and no formula
strings (logic is structured data, see Gates below).

## model.yaml — the manifest

The single entry point; tools discover everything from here.

```yaml
schema_version: "0.1.0"
model:
  id: "DEMO-PWR-L1-INTERNAL"
  name: "Demo PWR Level 1 PSA, internal events, at-power"
  scope: {level: 1, hazard: internal-events, plant_state: at-power}
  risk_metrics:
    - id: CDF
      label: "Core damage frequency"
      end_states: [CD]        # sequence end states summed into this metric
includes:                     # file index; globs allowed
  parameters: [parameters.yaml]
  basic_events: ["basic-events/*.yaml"]
  fault_trees: ["fault-trees/*.yaml"]
  event_trees: ["event-trees/*.yaml"]
  house_events: [house-events.yaml]
  ccf_groups: [ccf-groups.yaml]
configurations:               # named sets of house-event overrides
  TRAIN-A-OOS:
    label: "ECCS train A out of service"
    house_events: {HE-ECC-TRAIN-A-OOS: true}
```

`risk_metrics` defines how sequence end states aggregate into reported
metrics. `configurations` is documentation-plus-convention: apply one at
quantification time via `--house` flags.

## Quantities, units, references

Every physical quantity is a structured value with a mandatory unit:

```yaml
rate: {value: 3.0e-5, unit: per_hour}
```

Units are a closed enum: `per_hour`, `per_year`, `per_demand`, `hour`,
`year`, `dimensionless`. Anywhere a quantity is accepted, a parameter
reference may stand in:

```yaml
rate: {param: PAR-ECC-PMP-FR}
```

## parameters.yaml

Named constants — anything used in more than one place or worth varying in
a sensitivity study.

```yaml
parameters:
  PAR-MISSION-TIME-24H:
    label: "Standard 24-hour mission time"
    value: 24.0
    unit: hour
    uncertainty: {distribution: lognormal, error_factor: 3.0}   # optional
    provenance:
      source: "Plant PSA success criteria notebook, sec. 3.2"
      justification: "24 h stabilization assumed for front-line systems"
```

`provenance` (with both `source` and `justification`) is **required** on
parameters and basic events. Combined with git this closes the audit loop:
the diff shows what changed, `git blame` shows who and when, provenance
shows why and from where.

Uncertainty distributions (schema-validated; see
[limitations.md](limitations.md) on propagation):

| distribution | fields |
|---|---|
| `lognormal` | `error_factor` (> 1) |
| `beta` | `alpha`, `beta` |
| `gamma` | `shape`, `scale` |
| `uniform` | `lower`, `upper` |

## basic-events/

```yaml
basic_events:
  BE-ECC-PMP-A-FTR:
    label: "ECCS pump A fails to run (24 h mission)"
    system: ECC              # optional grouping metadata
    component: PMP-A
    failure_mode: FTR
    failure_model:
      type: rate-mission
      rate: {param: PAR-ECC-PMP-FR}
      mission_time: {param: PAR-MISSION-TIME-24H}
    provenance: {source: "…", justification: "…"}
```

`failure_model.type` is a closed, discriminated union — each type has its
own required fields, so a mission time cannot be attached to a per-demand
event by accident:

| type | fields | probability computed as |
|---|---|---|
| `probability` | `value` | value (per demand) |
| `rate-mission` | `rate`, `mission_time` | 1 − exp(−rate·t) |
| `rate-repair` | `rate`, `mttr` | rate·MTTR ⁄ (1 + rate·MTTR) |
| `rate-periodic-test` | `rate`, `test_interval` | 1 − (1 − exp(−rate·T)) ⁄ (rate·T), time-averaged standby unavailability between idealized (instantaneous, perfect) periodic tests |
| `frequency` | `value` | initiators only, never in fault trees |

Test & maintenance unavailability is modeled as its own basic event
(failure_mode `TM`) so it appears explicitly in cut sets.

## fault-trees/

```yaml
fault_trees:
  FT-ECCS-INJECTION:
    label: "ECCS fails to inject during small LOCA"
    top_gate: GT-ECC-INJ-TOP
    gates:
      GT-ECC-INJ-TOP:
        label: "ECCS injection function fails"
        formula: {and: [GT-ECC-TRAIN-A, GT-ECC-TRAIN-B]}
      GT-ECC-TRAIN-A:
        label: "ECCS train A fails to inject"
        formula:
          or: [BE-ECC-PMP-A-FTS, BE-ECC-PMP-A-FTR, HE-ECC-TRAIN-A-OOS,
               GT-ECC-SUPPORT-A]
```

Formulas are structured mappings, one operator per node — never strings:

| formula | meaning |
|---|---|
| `{and: [A, B, …]}` | all fail (≥ 2 operands) |
| `{or: [A, B, …]}` | any fails (≥ 2 operands) |
| `{atleast: {k: 2, of: [A, B, C]}}` | k-of-n vote |
| `{not: A}` | negation (makes the tree non-coherent) |
| `{xor: [A, B]}` | exclusive or (non-coherent) |
| `SOME-ID` | pass-through reference |

Operands are gate, basic-event, or house-event IDs. Gate IDs live in one
global namespace, so a **transfer** to a gate defined in another file is
just a reference — the linter resolves it and rejects cycles. Single-operand
`and`/`or` is rejected by schema (a pass-through is written as a bare ID).

## house-events.yaml

Boolean flags folded into the logic at compile time — configuration
control and success-criteria switching.

```yaml
house_events:
  HE-ECC-TRAIN-A-OOS:
    label: "ECCS train A out of service"
    default: false
    provenance: {source: "…", justification: "…"}
```

Defaults apply unless overridden by a configuration, a `--house` flag, or a
sequence's `house_events` block.

## ccf-groups.yaml

```yaml
ccf_groups:
  CCF-ECC-PMP-FTS:
    label: "ECCS pumps A/B common-cause fail to start"
    model: alpha-factor            # alpha-factor | mgl | beta-factor
    members: [BE-ECC-PMP-A-FTS, BE-ECC-PMP-B-FTS]
    total_probability: {param: PAR-ECC-PMP-FTS}
    factors: {alpha_1: 0.9787, alpha_2: 0.0213}
    provenance: {source: "…", justification: "…"}
```

Groups are declared, never hand-expanded: the engine expands them at load
time (see quantification.md). Optional `testing: staggered|non-staggered`
(default staggered). Supported models: `alpha-factor`, `beta-factor`; MGL
is rejected with an explicit error.

## event-trees/

One tree per file by convention. The deliberate design choice: **sequences
are a flat table keyed by sequence ID, not a nested branch structure** —
nested trees diff terribly (one inserted branch re-indents everything),
while a flat table means adding a sequence touches only its own lines. The
graphical staircase is a *rendering* derived from the table.

```yaml
event_tree:
  id: ET-SLOCA
  label: "Small-break LOCA"
  initiating_event:
    id: IE-SLOCA
    label: "Small-break LOCA (0.5–2 inch equivalent)"
    frequency:
      value: 5.0e-4
      unit: per_year               # must be per_year (CI-checked)
      uncertainty: {distribution: lognormal, error_factor: 6.0}
    provenance: {source: "…", justification: "…"}
  functional_events:               # mapping order = column order
    FE-RT:  {label: "Reactor trip",            top_gate: GT-RT-TOP}
    FE-ECC: {label: "ECCS injection",          top_gate: GT-ECC-INJ-TOP}
    FE-RHR: {label: "Long-term heat removal",  top_gate: GT-RHR-TOP}
  sequences:
    SEQ-SLOCA-02:
      path: {FE-RT: success, FE-ECC: success, FE-RHR: failure}
      end_state: CD
    SEQ-SLOCA-04:
      path: {FE-RT: failure, FE-ECC: bypassed, FE-RHR: bypassed}
      end_state: XFER-ATWS
      transfer: ET-ATWS            # hand off to another event tree
      # house_events: {HE-…: true} # optional per-sequence overrides
```

Each sequence's `path` must resolve **every** functional event to
`success`, `failure`, or `bypassed` (linter-enforced), duplicate paths are
rejected, and end states map to risk metrics through the manifest. A
non-OK end state mapped to no metric draws a warning.

## Provenance discipline

Any change to a `value` should change the `provenance` block in the same
commit. Reviewers should treat a value change with untouched provenance the
way a code reviewer treats a logic change with untouched tests.
