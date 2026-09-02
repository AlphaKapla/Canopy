# Importing a RiskSpectrum PSA model

`ci/import_riskspectrum.py` converts a RiskSpectrum PSA model into a Canopy
YAML model; `ci/crosscheck_rs.py` then proves the conversion against
RiskSpectrum's own results. Together they are the migration path for a
plant model that lives in RiskSpectrum today — and, run on a schedule, a
way to give such a model git history, pull-request diffs and ΔCDF review
before anyone authors in Canopy at all.

```
RiskSpectrum DB ──extract_riskspectrum_sql.py──┐
                                               ├──> table export ──import_riskspectrum.py──> model/ + conversion-log.md
RiskSpectrum PSA Macro / Excel export ──(CSV)──┘                                                  │
                                                                                                  ▼
RiskSpectrum results (Excel/CSV) ────────────────────────────── crosscheck_rs.py <── quantify (engine)
```

## 1. The table export (the only thing the converter reads)

The converter never touches RiskSpectrum's database or files. It reads a
neutral **table export**: one flat table per RiskSpectrum record type,
either as a directory of CSV files named after the tables or as a single
JSON file mapping table name → list of rows. This keeps every piece of
schema knowledge in one replaceable extractor, so a RiskSpectrum version
change never reaches the converter.

Two extractors produce it:

* **SQL** — `ci/extract_riskspectrum_sql.py <mapping.yaml> <out.json>` runs
  read-only `SELECT`s against the project database, driven entirely by a
  mapping file (`ci/riskspectrum-sql-mapping.example.yaml` is the
  skeleton; every `<placeholder>` is a table or column name only the
  database administrator can supply, and the script refuses to run while
  any remain). `CASE` expressions in the mapping translate RiskSpectrum's
  coded fields (reliability-model codes, gate types, TRUE/FALSE flags)
  into the words below.
* **Macro** — the RiskSpectrum PSA Macro (PowerShell API) exports record
  tables to Excel; saved as CSV files with the names and columns below,
  they are accepted unchanged.

Column names are case-insensitive; unknown columns and tables are ignored;
missing optional tables are empty. Numeric reliability columns hold either
a literal number or the id of a `parameters` row — RiskSpectrum links
reliability data to parameter records, and both forms are accepted.

| table | columns |
|---|---|
| `parameters` | `id`, `description`, `type` (probability \| rate \| frequency \| time \| dimensionless), `value`, `distribution` (lognormal \| beta \| gamma \| uniform \| blank), `p1`, `p2`, `reference`, `comment` |
| `basic_events` | `id`, `description`, `model` (probability \| mission \| repairable \| tested \| frequency), `q`, `rate`, `mission_time`, `repair_time`, `test_interval`, `first_test`, `test_duration`, `q_mean`, `system`, `component`, `failure_mode`, `distribution`, `p1`, `p2`, `reference`, `comment` |
| `house_events` | `id`, `description`, `value` (TRUE \| FALSE), `reference`, `comment` |
| `fault_trees` | `id`, `description`, `top_gate` |
| `gates` | `id`, `description`, `type` (AND \| OR \| KN \| NOT \| XOR \| NAND \| NOR), `k`, `fault_tree` |
| `gate_inputs` | `gate`, `input`, `position`, optional `input_type` (GATE \| BE \| HE) to disambiguate ids shared across record types |
| `ccf_groups` | `id`, `description`, `model` (beta \| alpha \| mgl), `testing` (staggered \| non-staggered \| blank), `total`, `reference`, `comment` |
| `ccf_members` | `group`, `member`, `position` |
| `ccf_factors` | `group`, `name` (`beta`; `alpha_1`…`alpha_n`; `rho_2`…`rho_n` or beta/gamma/delta/…), `value` |
| `event_trees` | `id`, `description`, `initiator`, `bc_set` |
| `function_events` | `id`, `description` |
| `et_columns` | `event_tree`, `position`, `function_event`, `logic` (gate or basic event id) |
| `sequences` | `event_tree`, `sequence`, `consequence`, `transfer`, `description` |
| `sequence_branches` | `event_tree`, `sequence`, `function_event`, `outcome` (S \| F \| -), `bc_set` |
| `bc_sets` | `id`, `description` |
| `bc_set_entries` | `bc_set`, `target`, `value` |
| `exchange_events` | `id`, `original`, `replacement`, `condition` |
| `consequences` | `id`, `description` |
| `project` | `id`, `name`, `exported_at`, `source` (one row; used for provenance) |

`ci/fixtures/riskspectrum-demo/` is a complete example: the demo model
written as such an export.

## 2. Running the conversion

```
python ci/import_riskspectrum.py rs-export/ converted --metric CDF=CD
python ci/validate.py converted schema/psa-model.schema.json
python ci/quantify.py converted converted.json
```

Options: `--metric ID=STATE[,STATE…]` (repeatable) maps RiskSpectrum
consequences to risk metrics; `--time-unit hour|year` and
`--frequency-unit per_year|per_hour` state the export's units (defaults:
hours, per year); `--mgl-to-alpha` converts MGL groups (§4);
`--allow-unsupported` drops logic-affecting constructs instead of
refusing; `--strict` turns any warning into exit 1; `--single-file`
writes one basic-events file instead of one per RiskSpectrum system.

The output is a normal Canopy model plus `conversion-log.md`: entity
counts, every warning, every dropped construct, and the number of
provenance placeholders. That file is the migration report.

## 3. How RiskSpectrum concepts map

| RiskSpectrum | Canopy | notes |
|---|---|---|
| record ids (free text) | prefixed ids (`BE-`, `GT-`, `PAR-`…) | deterministic sanitising (upper-case, non-`[A-Z0-9-]` → `-`), collision-safe; the original id is kept in `external_ids: {riskspectrum: …}` on every entity |
| Probability / Mission / Repairable model | `probability` / `rate-mission` / `rate-repair` | direct |
| Tested model (λ, test interval) | `rate-periodic-test` | exact when the model has no repair-time / test-duration / first-test terms |
| Tested model with those terms | point `probability` = RiskSpectrum's `q_mean` (if exported), else the idealized formula | always a warning; the model and its parameters are written into the justification |
| Frequency model | initiating-event frequency of the event tree | never a fault-tree basic event (the engine refuses those) |
| parameter type | `unit` | probability → `per_demand`, rate → `per_hour`, frequency → `per_year`, time → `hour` |
| lognormal (EF), beta, gamma, uniform | `uncertainty` | direct; normal, log-uniform, histogram, discrete are dropped with a warning (point value kept) |
| AND / OR / K-N / NOT / XOR / NAND / NOR gates | `and` / `or` / `atleast` / `not` / `xor` / `not(and)` / `not(or)` | single-input gates become pass-throughs; duplicate inputs removed with a warning |
| transfer gates, continuation pages | nothing | Canopy has one global gate id space; a gate referenced from another tree is just a reference |
| fault tree | one file `fault-trees/<id>.yaml` | gates go to the tree that owns them; a tree absent from `fault_trees` is synthesized when its top gate is unambiguous |
| house event | `HE-` with `default` | direct |
| boundary-condition set | named configuration in `model.yaml` + per-sequence `house_events` | house-event entries only |
| BC entry forcing a basic event | — | refused (see §5) |
| CCF beta / alpha | `beta-factor` / `alpha-factor` | `testing` copied when present, else Canopy's default (staggered) with a warning |
| CCF MGL | `alpha-factor`, `testing: non-staggered` | only with `--mgl-to-alpha` (§4) |
| CCF group > 8 members, UPM | — | refused |
| event tree columns | `functional_events` → `top_gate` | a column linked to a basic event gets a pass-through gate `GT-FE-<id>` |
| sequence (branch path) | one row of the flat sequence table | columns the sequence does not pass are `bypassed`; duplicate paths are refused |
| consequence | `end_state`, metrics via `--metric` | blank consequence → `OK` |
| sequence transfer | `transfer: ET-…`, `end_state: XFER-…` | reported, not followed (as for hand-written models) |
| exchange event | — | refused (see §5) |
| description / reference / comment | `label` / `provenance.source` / `provenance.justification` | a missing reference becomes `RiskSpectrum export of <project>, <date>: <kind> <id>`; a missing comment becomes the marked placeholder `MIGRATED from RiskSpectrum: no justification recorded — review required`, counted in the log |
| analysis cases, cut-offs, attributes | dropped | not model semantics; Canopy quantifies exactly |

## 4. MGL groups

Canopy rejects MGL by design. With `--mgl-to-alpha` the converter applies
the non-staggered NUREG/CR-5485 relations

    Q_k = (1 / C(m−1, k−1)) · (∏_{i≤k} ρ_i) · (1 − ρ_{k+1}) · Q_t,   ρ_1 = 1, ρ_{m+1} = 0
    α_k = C(m, k) · Q_k / Σ_j C(m, j) · Q_j

and emits the group as `alpha-factor` with `testing: non-staggered`, the
original factors recorded in the justification. Because CCF conventions
differ silently between codes (V&V finding F-1), verify one converted
group against RiskSpectrum's expanded CCF events with the cross-check
before trusting the rest.

## 5. What is refused, and why

These constructs change the logic, so a model converted without them
would quantify differently from RiskSpectrum while looking complete. The
converter exits 1 and writes nothing; `--allow-unsupported` downgrades
them to logged, dropped warnings for exploratory runs.

* **Exchange events** — Canopy has no construct that swaps one basic event
  for another under a condition. Model the alternative explicitly as a
  house-event-selected gate, or wait for the format work on the roadmap.
* **BC entries forcing a basic event TRUE/FALSE** — Canopy overrides house
  events per sequence, not basic events. Replace the forced event with a
  house event in RiskSpectrum, or add one in the converted model.
* **Frequency-type events inside fault trees** (initiator fault trees) and
  **gate-valued initiators** — Canopy initiating events are frequencies.
* **CCF groups of more than 8 members**, **UPM groups**, **MGL without
  `--mgl-to-alpha`**.
* **Duplicate sequence paths** — the validator would reject them anyway.

## 6. Proving the conversion

RiskSpectrum is the oracle for its own model. Export its results — top
probability per fault tree, frequency per sequence, and the ranked cut
sets — at the lowest cut-off it accepts, then:

```
python ci/crosscheck_rs.py converted rs-results/ --results converted.json
```

with `rs-results/` holding `fault_tree_results.csv` (`id, q`),
`sequence_results.csv` (`event_tree, sequence, frequency`), optionally
`cut_sets.csv` (`scope, rank, value, events` with events `;`-joined in
RiskSpectrum naming) and `event_map.csv` (`riskspectrum, canopy`) for
names `external_ids` cannot resolve — typically RiskSpectrum's CCF
combination events, which Canopy names `BE-<GROUP>-<idxs>`. Matching is
by RiskSpectrum id throughout. The tool exits 0 only when every value is
within tolerance (default 1e-3 relative), every id resolves, and every
RiskSpectrum cut set in the top N exists in Canopy.

Because RiskSpectrum's minimal-cut-set quantification truncates at the
cut-off, Canopy's exact value is normally slightly *above* it; a Canopy
value *below* RiskSpectrum's beyond tolerance is the signature of a
conversion defect (a lost input, a mistranslated reliability model), not
of truncation. `ci/fixtures/riskspectrum-demo-results/` shows the result
tables' shape.

A converted model is done when: the validator is clean, the cross-check
passes, `conversion-log.md` lists no dropped construct, and the
placeholder count is either zero or an accepted, tracked number.

## 7. The shadow repository

The conversion is deterministic — same export, byte-identical output — so
it can run on a schedule against the live RiskSpectrum database and commit
the result. That gives a model nobody edits in Canopy a full git history,
per-system `git log`, pull-request diffs of every RiskSpectrum change and
a ΔCDF comment on each, with RiskSpectrum untouched. It is the lowest-risk
way to put a production model under Canopy's review discipline, and the
cross-check keeps proving the shadow faithful on every run.

## 8. Scope of the evidence

The converter's fidelity is verified by `ci/test_import_riskspectrum.py`
(V&V FR-19): a hand-built table export of the demo model converts to a
model that reproduces every sequence frequency, cut set, fault-tree
probability and configuration result of `model/` to 1e-12, the output is
byte-deterministic, and each refusal rule fires. The fixture is written
to the table contract, not produced by RiskSpectrum: the first conversion
of a real export will exercise the extractor mapping, and its cross-check
is the evidence that matters for that model.
