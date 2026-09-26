# Changelog

Each release is a git tag; the verification and validation evidence for
it is `docs/verification-validation.md` at that tag (requirement IDs
FR-*/NFR-* and anomaly IDs D-*/F-* below refer to that report).

## Unreleased

- BDD garbage collection: mark and compact at gate-compilation safe points,
  gate BDDs released after their last reference; invisible in every output
  (FR-27). Aralia das9701 peak memory 5.2 GB → 2.1 GB.
- Fixed D-14: fault-tree Birnbaum importance was exponential on shared
  BDDs (Aralia baobab1 never finished); now linear per variable.
- Latin hypercube sampling, `--sampling lhs` (FR-28).
- Importance under uncertainty, `--importance-uncertainty K`: per event
  tree, distributions of FV, RAW, RRW, Birnbaum for each metric's K
  highest-FV events (FR-29).
- Prime implicants for non-coherent fault trees, `--prime-implicants`,
  with `--order-limit K` (truncated construction); ZBDD-based (FR-30).
- SCRAM cross-check extended to importance (Birnbaum vs MIF, RAW) on the
  Aralia suite; `import_mef.py` records original names in `external_ids`.

## v0.2.0 — the complete analyst's table

Every number a reviewer expects from an incumbent code, on the exact BDD
path, with the V&V evidence extended for each.

**Quantification**
- Monte Carlo uncertainty propagation with state-of-knowledge correlation:
  keyed, bit-for-bit reproducible random numbers; lognormal, beta, gamma,
  uniform; base/head paired change band in the PR comment (FR-20–FR-23).
- BDD-exact consequence-level importance — Fussell–Vesely, RAW, RRW,
  Birnbaum — per metric and end state, success branches included, summed
  exactly across event trees; Fussell–Vesely re-ranking in the PR comment
  (FR-24).
- Event-tree transfers followed exactly (one BDD per expansion, house
  overrides accumulated along the chain, cycles refused); transfer-only
  trees without an initiating event (FR-11).
- `rate-periodic-test` failure model; CCF group cap raised to 8.

**Model checking**
- Partition lint: every sequence table must cover the functional-event
  outcome space exactly once; the engine reports Σ P(sequence) and
  `quantify.py` fails on a deviation (FR-3, FR-13).
- File-index lint: no model file can be silently ignored; `includes` must
  name exactly the files loaded (FR-3).
- Dimensional rules, one table in the validator and the engine: a rate
  and its time on the same base, never converted (FR-25).

**Tooling**
- `python ci/canopy.py`: one entry point — `validate`, `quantify`,
  `report`, `compare`, `delta` (working tree vs a git ref, end to end),
  `viz`, `verify` (every check required before a commit) (FR-26).
- RiskSpectrum table-export importer, by-id cross-check, SQL extractor
  skeleton (FR-19).
- Consequence report: pooled minimal cut sets plus the exact importance
  table (FR-18, FR-24).

**Fixed**
- D-10: a transfer sequence whose end state was mapped to a metric was
  counted in it (engine and consequence report).
- D-11: the consequence report's order of tied rows depended on Python's
  per-process hashing.
- D-9: model files named `*.yml`, in sub-directories or hidden were
  silently ignored; the documented `includes` check did not exist.
- D-13: README claims about dimensional checks and the CI pipeline that
  did not match the implementation.
- D-4 to D-8: see the anomaly log (special-function inversions, schema
  coverage, compensated summation in the harness).

**Verification added**: validator regression suite (46 mutation cases,
partition lint against brute force), exhaustive unit-rule test (168
combinations, engine and validator), hand-computed transfer, importance
and CLI suites, property-harness uncertainty, importance and transfer
stages with negative controls, CI job requiring bit-identical results
from Rust 1.75 and the latest stable.

**Compatibility**: every v0.1.0 model remains valid (schema 0.1.0 with
additions); the demo model's CDF is unchanged at 2.208173e-8 /yr. Event
tree JSON gained fields (`importance`, `end_states`, `partition`,
`transfer_path`, `followed`); no field was removed.

## v0.1.0

The git-native loop on a PWR fragment: YAML model format, validator,
Rust ROBDD engine (exact probabilities, minimal cut sets, Birnbaum,
k-of-n, NOT/XOR, event trees with negated success branches, CCF
expansion), CI risk-delta comments, MEF export/import, HTML viewer, and
the V&V report (Aralia 41/43 exact against SCRAM).
