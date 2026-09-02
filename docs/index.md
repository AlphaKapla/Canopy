# Canopy — documentation

Canopy treats a probabilistic safety assessment model the way modern
engineering treats source code. The model — event trees, fault trees, basic
events, parameters, house events, CCF groups — is authored as plain YAML
files in a git repository. Validation runs in CI on every change,
quantification is a build step, and every risk result is reproducible from a
git commit. There is no database: the repository *is* the model, and git
provides the versioning, history, diffing, branching, review, and audit
trail that conventional PSA tools lack.

The consequence that motivates the whole design: when an analyst changes a
failure rate or restructures a fault tree, the change arrives as a pull
request. Reviewers see exactly which lines changed, `git blame` answers who
changed a number and when, the provenance block answers why, and the CI
pipeline posts the quantitative consequence — ΔCDF, changed sequence
frequencies, re-ranked cut sets — as a comment on the PR before anyone
approves it.

## Documentation map

| Document | Contents |
|---|---|
| [getting-started.md](getting-started.md) | Installation, building the engine, first quantification |
| [model-format.md](model-format.md) | Complete YAML format reference: every entity type and field |
| [quantification.md](quantification.md) | Engine CLI, algorithms, JSON output format |
| [ci.md](ci.md) | The CI pipeline: validation, risk-delta reports, workflow reference |
| [visualization.md](visualization.md) | Building and using the interactive model viewer |
| [architecture.md](architecture.md) | Repository layout, design decisions, engine internals |
| [limitations.md](limitations.md) | Known gaps, deliberate scope cuts, regulatory caveats |
| [riskspectrum-import.md](riskspectrum-import.md) | Migrating a RiskSpectrum PSA model: table export, converter, cross-check |
| [verification-validation.md](verification-validation.md) | The V&V report: requirements, evidence, anomaly log, traceability matrix |

## The pieces at a glance

```
model/     the PSA model: YAML source of truth, one ID space
schema/    JSON Schema enforced by CI on every file
engine/    Rust BDD quantifier: exact P(top), cut sets, sequences, CDF
ci/        validate.py, quantify.py, compare.py — the pipeline scripts
viz/       generates a self-contained interactive HTML model viewer
.github/   the workflow wiring it together on every pull request
docs/      you are here
```

A model revision is a git tag. The tag pins the model files, the schema
version, and (through `engine/Cargo.lock`) the exact quantification engine,
so any historical result can be regenerated bit-for-bit.

## Status

This is a working prototype demonstrating the full loop — authored YAML →
validation → exact BDD quantification → PR risk-delta review →
visualization — on an illustrative PWR model fragment. It is not a
licensed, V&V'd PSA code. Read [limitations.md](limitations.md) before
considering any regulatory use.
