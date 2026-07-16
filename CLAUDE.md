# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Canopy is a git-native Probabilistic Safety Assessment (PSA) tool. PSA models (nuclear/industrial risk models with fault trees and event trees) are authored as YAML source code, validated in CI, and quantified exactly using a Rust BDD (Binary Decision Diagram) engine. Derived artifacts (cut sets, frequencies, HTML viewer) are never committed.

## Non-negotiable rules

This is safety software; these override convenience. Do not skip them even for "trivial" changes.

1. **Test before committing.** Any change under `engine/` or `ci/` requires: `cargo test --release --manifest-path engine/Cargo.toml` AND `python ci/property_test.py --cases 60 --seed 20260708`, both green, *actually run* — never assumed.
2. **V&V living-document rule.** Any change to verified behavior (algorithms, output semantics, validated tools) must update `docs/verification-validation.md` **in the same commit**: requirements (§3), evidence (§4/§5), the traceability matrix (§8), and Appendix A commands as applicable.
3. **Anomaly log.** Any defect found — in the engine, the oracle, or the harness itself — gets an entry in V&V §7 (found-by, root cause, disposition), including cases where the *reference* was wrong, not the engine.
4. **Provenance rule.** Any change to a numeric value in `model/` must update that entity's `provenance` block (source + justification) in the same commit.
5. **Documentation honesty.** Closing a gap must delete/amend the corresponding `docs/limitations.md` entry in the same PR. Docs describe the software as it *is*; overselling is worse than silence here.
6. **Never commit derived artifacts**: quantification results, `delta.md`, `psa-viewer.html`, `engine/target/`, `property-failure-*/`. Regenerate, don't store.
7. **Commit messages carry verification evidence** (what was run, key numbers). See `git log` for the house style.
8. **Cargo.lock pins are deliberate** (V&V NFR-1: bit-for-bit reproducibility from a tag). Do not bump Rust dependencies casually; a dep bump is a reviewed change with test evidence, not housekeeping.

## Validation status (the bar any change must keep clearing)

Six independent evidence legs, detailed in `docs/verification-validation.md`:
unit tests with hand-computed references → brute-force truth-table oracle →
partition property (Σ P(seq) = 1) → randomized property harness (in CI per PR)
→ SCRAM cross-verification (76 models, every sequence) → Aralia industrial
suite **41/43 exact P(top) agreement** + exact MEF round trip (12 digits).
The two Aralia exceptions are memory boundaries, not disagreements
(das9701 = our no-sifting limit; nus9601 = both engines).

## Commands

### Validation (Python, no build needed)
```bash
pip install pyyaml jsonschema
python ci/validate.py model schema/psa-model.schema.json
```

### Build the engine
```bash
cargo build --release --manifest-path engine/Cargo.toml
# Binary lands at engine/target/release/canopy
```

### Quantify a single fault tree or event tree
```bash
engine/target/release/canopy model FT-RHR
engine/target/release/canopy model FT-RHR --json
engine/target/release/canopy model ET-SLOCA --json
engine/target/release/canopy model ET-SLOCA --house HE-TRAIN-A-OOS=true --json
# --prob-only skips cut sets + Birnbaum (for big/imported trees)
# --mcs-limit N caps enumeration; 0 skips cut sets entirely
```

### Quantify all event trees (writes merged JSON)
```bash
python ci/quantify.py model head.json
# Override engine path: CANOPY_BIN=... python ci/quantify.py model head.json
```

### Consequence report: minimal cut sets + basic-event importance for CD/CDF etc.
```bash
python ci/quantify.py model head.json
python ci/consequence_report.py head.json --metric CDF --model model
# or by raw end-state, no model.yaml lookup needed:
python ci/consequence_report.py head.json --end-state CD --json
```
Pools every qualifying sequence's cut sets (across all event trees) into one
ranked table, plus a minimal-cut-set Fussell-Vesely importance table per
basic event. This is a post-processing aggregation over already-exact
per-sequence numbers, not a new engine algorithm — see the module docstring
for the coverage/overlap caveat. Tested by `ci/test_consequence_report.py`
(hand-computed fixture).

### Full local CI pipeline (validate → build → quantify → compare base vs head)
```bash
python ci/validate.py model schema/psa-model.schema.json
cargo build --release --manifest-path engine/Cargo.toml
python ci/quantify.py model head.json
git worktree add /tmp/base main
python ci/quantify.py /tmp/base/model base.json
python ci/compare.py base.json head.json
git worktree remove /tmp/base
```

### Property-based tests (engine vs brute-force oracle)
```bash
python ci/property_test.py --cases 60 --seed 20260708
# Failing cases are preserved in property-failure-seed<S>-case<N>/ for repro
```

### Cross-verification against SCRAM (needs `scram` on PATH; build recipe in .github/workflows/crosscheck.yml)
```bash
python ci/crosscheck_scram.py --cases 25
python ci/benchmark_mef.py <scram>/input/Aralia --timeout 120
```

### Visualization
```bash
python ci/quantify.py model results.json   # optional, adds frequencies to viewer
python viz/build_viz.py model psa-viewer.html --results results.json
open psa-viewer.html
```

### Rust engine tests and benchmark
```bash
cargo test --release --manifest-path engine/Cargo.toml
cargo run --release --manifest-path engine/Cargo.toml --example bench
```

## Architecture

### Data flow
```
YAML model/ ──validate.py──> ok/fail
     |
     └──canopy (Rust)──> results JSON ──compare.py──> risk-delta markdown
                              |
                              └──build_viz.py──> psa-viewer.html (artifact)
```

### Model layer (`model/`)
YAML source of truth. Everything is a **mapping keyed by stable, prefixed ID** — never a positional list. This makes git diffs meaningful (one added entity = one diff hunk).

ID namespace prefixes enforced by CI:
- `BE-` basic events, `GT-` gates, `FT-` fault trees, `ET-` event trees
- `FE-` functional events, `IE-` initiating events, `HE-` house events
- `PAR-` parameters, `CCF-` CCF groups

Key design constraints:
- No YAML anchors/aliases (they make diffs lie). Reuse via `{param: PAR-...}` references.
- Every physical quantity has a `unit` field; every number has `source` + `justification`.
- Gate formulas are structured (`{or: [A, B]}`), not strings — no expression parser.
- Single-operand `and`/`or` is rejected by schema; a pass-through gate is a bare ID.
- Event trees use flat sequence tables (not nested branches) for diffability.
- House events are runtime configuration (`--house HE-ID=true`), never committed state.
- File layout is a team convention, not a format rule: files hold 1..N entities,
  the loader merges one global ID space, entities move between files with zero
  semantic diff. Loader requires `parameters.yaml`, `house-events.yaml`,
  `basic-events/`, `fault-trees/` to exist even if minimal.

### Validation layer (`ci/validate.py`)
Single-pass Python script: strict YAML parse (duplicate-key detection) → JSON Schema → reference linter (dangling IDs, gate cycles, CCF membership + alpha-sum, sequence path completeness) → orphan warnings. Exit 0 = clean.

### Quantification engine (`engine/src/`)
Rust BDD engine. Key files:
- `bdd.rs` — core ROBDD: flat node arena (`Vec<Node>` of 12-byte `(var,low,high)` triples with `u32` indices), hash consing unique table, apply cache, `minsol` (Rauzy minimal solutions), `enumerate_paths`, `probability`, `birnbaum`.
- `model.rs` — YAML loader: merges all indexed files into one ID space, resolves `{param: ...}` references, expands CCF groups (alpha-factor and beta-factor models per NUREG/CR-5485).
- `main.rs` — CLI + `Compiler` struct that walks formulas and builds BDD nodes, then drives fault-tree and event-tree quantification.

Variable ordering is DFS discovery order from the top gate (no dynamic reordering — see `docs/limitations.md`). The engine tracks coherence: `NOT`/`XOR` gates set `coherent = false`, which suppresses `minsol` (minimal cut sets require coherent logic) — on BOTH the fault-tree and event-tree paths.

### CI pipeline (`.github/workflows/psa.yml`)
Two jobs: `validate` (schema + lint) then `quantify` (build engine → property tests → quantify head → quantify base via `git worktree` → post risk-delta as PR comment, updating in place on re-push). Comparison is **reporting, not gating**: `compare.py` always exits 0; acceptability of a ΔCDF is the reviewer's judgment.

### Cross-verification tools (`ci/`)
- `export_mef.py` / `import_mef.py` — Open-PSA MEF XML round-trip
- `crosscheck_scram.py` — compare engine results against SCRAM (independent BDD engine)
- `property_test.py` — randomized model generation + Python truth-table oracle; checks exact probability, cut sets, Birnbaum importance, partition property (Σ P(sequence) = 1), and CCF expansion end-to-end
- `benchmark_mef.py` — Aralia/MEF benchmark runner

## Hard-won knowledge (gotchas that cost real debugging time)

- **BDD construction order matters**: the arena has NO garbage collection, so
  linearly OR-accumulating N subtrees causes O(N²) node churn (observed: 40M
  nodes vs 402k). Combine collections with balanced pairwise reduction.
- **Empty-cut-set convention**: a tautological function (e.g. a true house
  event in an OR) has exactly ONE minimal cut set — the empty set. Both the
  fault-tree and event-tree paths must emit it (V&V anomaly log D-2/D-3;
  the harness asserts this).
- **Sequence semantics**: success branches contribute *negated* top gates
  (frequencies are exact even with shared basic events); listed sequence cut
  sets follow the delete-term convention (failure logic only).
- **CCF conventions**: Canopy defaults to the *staggered* alpha-factor
  formula; SCRAM implements *non-staggered*. `testing: non-staggered` on a
  group reproduces SCRAM exactly. Never compare raw-mode CCF results across
  engines without checking the convention. MGL is rejected by design.
- **CCF expansion naming**: combination events are `BE-<GROUP-ID>-<idxs>`
  (e.g. `BE-CCF-ECC-PMP-FTS-1-2`); the property oracle and MEF exporter
  replicate this exactly — keep all three in sync if it ever changes.
- **SCRAM interop**: SCRAM's MEF reader is stricter than the published
  grammar — flat gates only (exporter hoists composites to `GT-AUX-*`),
  no duplicate operands, no degenerate votes (k-of-k, 1-of-n), CCF members
  not re-declared in model-data, no frequency on initiating events (compare
  sequence *probabilities*, ×IE frequency externally). SCRAM report files
  embed full product listings and reach **gigabytes** on large trees —
  always pass `-l 1` (probability is BDD-exact and unaffected).
- **Building SCRAM** on modern toolchains needs a one-line boost≥1.73 patch
  (`BOOST_THROW_EXCEPTION_CURRENT_FUNCTION` → `BOOST_CURRENT_FUNCTION`),
  scripted in `.github/workflows/crosscheck.yml`.
- **Subprocess diagnostics**: when a tool invokes another as subprocess,
  surface stderr in failure messages, not just stdout (an empty error
  message once hid a `ModuleNotFoundError` in CI for a full run).

## Roadmap (agreed priorities, see docs/limitations.md)

1. Uncertainty propagation (Monte Carlo over the existing O(|BDD|)
   probability pass; distributions already parsed and schema-validated).
2. Dynamic variable reordering (sifting) — the das9701 memory boundary.
3. BDD garbage collection (prerequisite for a long-lived service and for
   sharing one manager across event-tree sequences).
4. Prime implicants (Coudert–Madre) for non-coherent cut sets.
5. MEF event-tree/CCF import; component/module templating in the YAML format.
6. Viewer: base-vs-head visual diff mode; partition check as a CI lint on
   the committed model.