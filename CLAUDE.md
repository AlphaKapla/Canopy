# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Canopy is a git-native Probabilistic Safety Assessment (PSA) tool. PSA models (nuclear/industrial risk models with fault trees and event trees) are authored as YAML source code, validated in CI, and quantified exactly using a Rust BDD (Binary Decision Diagram) engine. Derived artifacts (cut sets, frequencies, HTML viewer) are never committed.

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
# Fault tree
engine/target/release/canopy model FT-RHR
engine/target/release/canopy model FT-RHR --json

# Event tree
engine/target/release/canopy model ET-SLOCA --json

# With house event overrides
engine/target/release/canopy model ET-SLOCA --house HE-TRAIN-A-OOS=true --json
```

### Quantify all event trees (writes merged JSON)
```bash
python ci/quantify.py model head.json
# Override engine path: CANOPY_BIN=... python ci/quantify.py model head.json
```

### Full local CI pipeline (validate → build → quantify → compare base vs head)
```bash
python ci/validate.py model schema/psa-model.schema.json
cargo build --release --manifest-path engine/Cargo.toml
python ci/quantify.py model head.json
git worktree add /tmp/base main
python ci/quantify.py /tmp/base/model base.json
python ci/compare.py base.json head.json
```

### Property-based tests (engine vs brute-force oracle)
```bash
python ci/property_test.py --cases 60 --seed 20260708
# Runs N randomized PSA models through both the engine and a Python truth-table oracle
```

### Visualization
```bash
python ci/quantify.py model results.json   # optional, adds frequencies to viewer
python viz/build_viz.py model psa-viewer.html --results results.json
open psa-viewer.html
```

### Rust engine tests and benchmark
```bash
cargo test --manifest-path engine/Cargo.toml
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
- Event trees use flat sequence tables (not nested branches) for diffability.
- House events are runtime configuration (`--house HE-ID=true`), never committed state.

### Validation layer (`ci/validate.py`)
Single-pass Python script: strict YAML parse (duplicate-key detection) → JSON Schema → reference linter (dangling IDs, gate cycles, CCF membership, sequence path completeness, unit dimensions) → orphan warnings. Exit 0 = clean.

### Quantification engine (`engine/src/`)
Rust BDD engine. Key files:
- `bdd.rs` — core ROBDD: flat node arena (`Vec<Node>` of 12-byte `(var,low,high)` triples with `u32` indices), hash consing unique table, apply cache, `minsol` (Rauzy minimal solutions), `enumerate_paths`, `probability`, `birnbaum`.
- `model.rs` — YAML loader: merges all indexed files into one ID space, resolves `{param: ...}` references, expands CCF groups (alpha-factor and beta-factor models per NUREG/CR-5485).
- `main.rs` — CLI + `Compiler` struct that walks formulas and builds BDD nodes, then drives fault-tree and event-tree quantification.

Variable ordering is DFS discovery order from the top gate (no dynamic reordering — see `docs/limitations.md`). The engine tracks coherence: `NOT`/`XOR` gates set `coherent = false`, which suppresses `minsol` (minimal cut sets require coherent logic).

### CI pipeline (`.github/workflows/psa.yml`)
Two jobs: `validate` (schema + lint) then `quantify` (build engine → property tests → quantify head → quantify base via `git worktree` → post risk-delta as PR comment, updating in place on re-push).

### Cross-verification tools (`ci/`)
- `export_mef.py` / `import_mef.py` — Open-PSA MEF XML round-trip
- `crosscheck_scram.py` — compare engine results against SCRAM (independent BDD engine)
- `property_test.py` — randomized model generation + Python truth-table oracle; checks exact probability, cut sets, Birnbaum importance, partition property (Σ P(sequence) = 1), and CCF expansion end-to-end
- `benchmark_mef.py` — Aralia/MEF benchmark runner
