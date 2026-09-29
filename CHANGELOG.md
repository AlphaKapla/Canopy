# Changelog

Each release is a git tag; the verification and validation evidence for
it is `docs/verification-validation.md` at that tag (requirement IDs
FR-*/NFR-* and anomaly IDs D-*/F-* below refer to that report).

## Unreleased

- Importance cofactors (fault-tree Birnbaum, consequence importance) in
  one sweep of each plan instead of two passes per variable: edfpa14q
  with Birnbaum 2.06 s -> 0.30 s, the 32-row benchmark tree with
  importance 24.5 s -> 9.2 s; `--cofactors per-variable` keeps the
  reference method (FR-50).
- Truncated event trees reuse a gate's truncated sets across rows whose
  house-event overrides agree on the house events it reaches (memo keyed
  by those values instead of the whole configuration) (FR-49).
- Viewer diff mode reports named configurations (label, house-event and
  parameter overrides) as entities (FR-48).
- Relative truncation cut-off for fault trees: `--truncated-relative R`
  retains every minimal cut set with P >= R x P(top), the absolute
  cut-off R x L taken from a lower bound L on P(top) by an estimation
  pass (FR-47).
- Fixed: FR-46's tightened bound could make `--truncated` run
  indefinitely on a lost set with astronomically many products (das9209
  at 1e-20); the split is now step-limited (V&V D-24).
- Tighter truncation upper bounds: the retained cut sets' union with the
  lost terms, computed exactly on a BDD within `--upper-budget N` nodes
  (default 1,048,576; 0 = the previous sum bound), the remaining lost
  terms summed; `upper_bound_method` reports how each bound was obtained
  (FR-46). nus9601's interval is not narrowed.
- Viewer diff mode reports parameters and CCF groups as entities (value,
  uncertainty, factors, members, ... changed/added/removed), with a
  parameter's users and an event's parameters and CCF group as links
  (FR-45).
- Several event trees in one engine process: target `ET-A,ET-B,...`
  (with `--json`) loads the model once and shares one compiler, so fault
  trees used by several event trees are compiled once;
  `quantify.py --one-process` uses it (opt-in: later trees agree with
  their standalone results to rounding) (FR-44).
- Fixed: unknown keys in `model.yaml` (e.g. `configurations` misplaced
  under `model:`) were silently ignored; the validator now rejects them
  (V&V D-22).
- House-event changes between event-tree rows recompile only the gates
  that reach a changed house event (transitively); other cached gates are
  kept (FR-43). `--gc-stats` reports gates compiled, dropped and kept.
- Truncated results through the pipeline: `quantify.py --truncated
  CUTOFF [--order-limit K]` (also `canopy quantify` / `canopy delta`)
  quantifies every event tree and configuration by truncated cut sets and
  checks the partition on the bounds; the delta report, consequence
  report, appendix and viewer show bounds `[lower, upper]` (printed
  outward) and changes as rigorous intervals (FR-42). The engine's
  truncated event-tree JSON gains per-row probability bounds, `partition`
  and `followed` bounds.
- Fixed: the truncated event-tree path listed the empty cut set for an
  `OK` row, which the exact path never lists (V&V D-20); the viewer's
  header showed the first event tree's metric instead of the model-wide
  total on multi-tree models (V&V D-21).
- Templates as an authoring aid: `templates/` component types and
  instance files, `canopy expand` writes the model's basic-event files
  (committed, GENERATED header), `canopy expand --check` in CI fails on
  drift; the demo's pump files are now generated (engine outputs
  byte-identical) (FR-41).
- The PR comment shows importance under uncertainty: per metric, the
  top-10 events whose FV distribution moved, base -> head (CI quantifies
  both sides with `--importance-uncertainty 10`).
- Viewer diff mode: `build_viz.py --base BASE [--base-results]` and
  `canopy delta --viewer` paint added/removed/changed entities, frequency
  and metric changes onto the trees; CI uploads it on every pull request
  (FR-40).
- Truncated quantification of event trees: `--truncated CUTOFF` on an ET
  gives certified bounds on every sequence frequency and metric, from
  P(seq) = P(F) − P(F ∧ S) with both coherent sides truncated (FR-39).
- One compiler per event tree (`--compile shared`, default; `per-row`
  keeps the old behaviour): functional-event tops compiled once per house
  configuration and cached across rows, a GC safe point per row,
  per-row coherence decided from the model (FR-38).
- Model-wide importance under uncertainty: `quantify.py --samples N
  --importance-uncertainty K` selects each metric's model-wide top K by
  exact FV, samples every tree with the engine's new `--importance-events
  LIST` (per-iteration draws of F(x=1), F(x=0)) and combines them per
  iteration (`ci/importance.py`); shown by quantify.py and the consequence
  report (FR-37).
- Uncertainty on CCF factors: `factor_uncertainty: {distribution:
  dirichlet, concentration: N}` on a group — a Dirichlet on the alphas
  (a Beta on β) with the point factors as means, sampled through keyed
  gammas `CCF-X/alpha_k`; validator rules; appendix shows it (FR-36).
- Dynamic variable reordering, `--reorder` (`--reorder-threshold N`):
  Rudell sifting with CUDD's growth bound and interaction matrix, run at
  garbage-collection safe points in a separate reference-counted manager,
  functions rebuilt with index = new level; results to rounding,
  reproducible bit for bit (FR-35). Aralia: 42/42 agree, arena geometric
  mean 0.47x (edf9202 1.7M -> 9k nodes), about 7x slower; CI runs it.
- Fixed D-17: `--order rdfs` on event trees numbered variables in hash
  order, so repeated runs could differ in the last bits (never in a
  release); the harness now checks byte-identical reruns of every
  non-default variant.
- Truncated quantification for coherent fault trees, `--truncated CUTOFF`
  (with `--order-limit K`): exactly the minimal cut sets above the
  cut-off, built bottom-up with truncation inside the ZBDD product, and
  rigorous bounds on P(top) — lower: exact probability of their union;
  upper: plus the union bound over the dropped products not covered by a
  retained cut set (FR-34). CI checks SCRAM's exact P(top) lies within
  the bounds on every coherent Aralia tree.
- BDD garbage collection: mark and compact at gate-compilation safe points,
  gate BDDs released after their last reference; invisible in every output
  (FR-27). Aralia das9701 peak memory 5.2 GB → 2.1 GB.
- Fixed D-14: fault-tree Birnbaum importance was exponential on shared
  BDDs (Aralia baobab1 never finished); now linear per variable.
- Latin hypercube sampling, `--sampling lhs` (FR-28).
- Importance under uncertainty, `--importance-uncertainty K`: per event
  tree, distributions of FV, RAW, RRW, Birnbaum for each metric's K
  highest-FV events (FR-29).
- `--order rdfs`: reverse-DFS static variable order, results order-
  invariant (harness order stage; Aralia against SCRAM in both orders)
  (FR-33).
- Report appendices generated from the model and the engine's results,
  `ci/appendix.py` / `canopy appendix`; uploaded by CI per commit (FR-32).
- CI job `aralia`: the 42 Aralia trees quantified on every push against
  committed SCRAM reference values (gating), with time/nodes/memory in
  the job summary.
- Named configurations quantified: `quantify.py --configurations`
  (house-event and parameter overrides, point values), engine `--param`,
  PR comment section; validator lint (FR-31).
- MEF import of alpha/beta CCF groups and event trees (complementary
  forks); untyped `<event>` references resolved; IDs that already follow
  the grammar are kept (FR-15).
- Fixed D-15: a beta-factor group with `testing: non-staggered` used the
  non-staggered alpha formula; beta groups now ignore `testing`.
- Prime implicants for non-coherent fault trees and event-tree sequences,
  `--prime-implicants`, with `--order-limit K` (truncated construction);
  ZBDD-based (FR-30); `quantify.py --prime-implicants`; pooled by the
  consequence report.
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
