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
→ SCRAM cross-verification (demo + 100 generated models, every sequence;
prime-implicant sets of the non-coherent ones) → Aralia industrial suite
**42/43 exact P(top) agreement** (every push, committed SCRAM references)
+ exact MEF round trip (12 digits). The one Aralia exception, nus9601, is
beyond both engines (Canopy bounds it by truncation instead).

## Commands

### One entry point: `python ci/canopy.py`
```bash
python ci/canopy.py verify            # the non-negotiable checks, in order (use before committing)
python ci/canopy.py verify --quick    # fast subset while iterating (NOT sufficient to commit)
python ci/canopy.py delta             # working-tree model vs HEAD, as the PR comment would show
python ci/canopy.py validate | quantify | report | compare | viz | appendix   # thin wrappers
```
Thin dispatcher (FR-26): each subcommand runs the tool below unchanged;
`ci/test_cli.py` checks byte-identical output and `delta` end to end.

### Validation (Python, no build needed)
```bash
pip install pyyaml jsonschema
python ci/validate.py model schema/psa-model.schema.json
python ci/test_validate.py      # validator regression suite
python ci/canopy.py expand      # regenerate model files from templates/ (then commit them)
python ci/canopy.py expand --check   # CI: generated files must match their templates
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
engine/target/release/canopy model ET-A,ET-B --json   # several trees, one process, one compiler (FR-44)
engine/target/release/canopy model ET-SLOCA --house HE-ECC-TRAIN-A-OOS=true --json
# --prob-only skips cut sets + Birnbaum (for big/imported trees)
# --mcs-limit N caps enumeration; 0 skips cut sets entirely
engine/target/release/canopy model ET-SLOCA --samples 10000 --seed 20260708
# --reorder: dynamic sifting (memory for time; results to rounding)
# CCF factor uncertainty: factor_uncertainty: {distribution: dirichlet,
#   concentration: N} on a group; tests: python ci/test_ccf_uncertainty.py
# Monte Carlo over parameter uncertainty; --keep-samples emits every draw;
# --sampling lhs for Latin hypercube (pairing needs same N, seed, method)
# --importance-uncertainty K: FV/RAW/RRW/Birnbaum distributions for each
# metric's K highest-FV events (event trees, per tree)
```

### Quantify all event trees (writes merged JSON)
```bash
python ci/quantify.py model head.json
python ci/quantify.py model head.json --samples 10000 --seed 20260708  # + uncertainty
python ci/quantify.py model head.json --prime-implicants   # non-coherent sequences: primes
python ci/quantify.py model head.json --configurations cfg.json   # named configurations
python ci/quantify.py model head.json --samples 10000 --importance-uncertainty 10   # model-wide importance distributions
python ci/quantify.py model head.json --truncated 1e-12   # bounds, not values (FR-42); reports read them via ci/bounds.py
python ci/quantify.py model head.json --one-process   # all event trees in one engine process, one compiler (FR-44)
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
ranked table, plus a BDD-exact basic-event importance table (FV, RAW, RRW,
Birnbaum; minimal-cut-set FV beside it for comparison). The engine emits,
per metric and per end state, exact conditional frequencies F(x=1)/F(x=0)
from cofactor passes over each sequence's flat plan; `ci/importance.py`
sums them across event trees (exact: frequencies are multilinear). Tested
by `ci/test_consequence_report.py`, `ci/test_importance.py` (hand-computed
fixtures), engine unit tests, and the property harness oracle.

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
# (uncertainty-stage failures in ...-case<N>-uncertainty/); --mc-samples 0
# skips the Monte Carlo stage for a quick logic-only run
```

### Special functions vs SciPy (on demand, needs `pip install scipy`)
```bash
python ci/crosscheck_special_functions.py
```

### Aralia regression (no SCRAM needed: reference values committed; CI job `aralia` on every push)
```bash
python ci/aralia_regression.py <scram-checkout>/input/Aralia   # 42 trees vs ci/fixtures/aralia-scram-reference.json
python ci/aralia_regression.py <scram-checkout>/input/Aralia --truncated 1e-10   # SCRAM's P(top) within the bounds
```

### Truncated quantification (coherent fault trees too large for the exact BDD)
```bash
engine/target/release/canopy model FT-RHR --truncated 1e-12 [--order-limit K] --json
engine/target/release/canopy model ET-SLOCA --truncated 1e-9 --json   # sequence + metric bounds
# retained = exactly the MCS with P >= cutoff; probability_lower_bound (exact
# union of them) <= P(top) <= probability_upper_bound; no "probability" field
python ci/test_truncation.py     # hand-computed bounds + every refusal
```

### Cross-verification against SCRAM (needs `scram` on PATH; build recipe in .github/workflows/crosscheck.yml)
```bash
python ci/crosscheck_scram.py --cases 25
python ci/benchmark_mef.py <scram>/input/Aralia --timeout 120
```

### Import a RiskSpectrum model and cross-check it (docs/riskspectrum-import.md)
```bash
python ci/extract_riskspectrum_sql.py my-mapping.yaml rs-export.json   # or Macro/Excel -> CSV dir
python ci/import_riskspectrum.py rs-export.json converted --metric CDF=CD [--mgl-to-alpha]
python ci/validate.py converted schema/psa-model.schema.json
python ci/quantify.py converted converted.json
python ci/crosscheck_rs.py converted rs-results/ --results converted.json
python ci/test_import_riskspectrum.py     # unit + demo round trip (needs the engine)
```
Refuses logic-changing constructs (exchange events, BC-forced basic events,
initiator fault trees, >8-member/UPM/MGL groups) unless --allow-unsupported;
every approximation lands in converted/conversion-log.md. Same ID grammar
as import_mef.py; originals kept in `external_ids`.

### Visualization
```bash
python ci/quantify.py model results.json   # optional, adds frequencies to viewer
python viz/build_viz.py model psa-viewer.html --results results.json
open psa-viewer.html
python ci/canopy.py delta --viewer psa-viewer.html   # changes vs HEAD painted on the trees
python ci/test_viz_diff.py                           # the diff data, exhaustively
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
- `templates/` (outside `model/`) is an authoring aid only (FR-41):
  `python ci/canopy.py expand` writes basic-event files carrying a
  GENERATED header; they are committed and reviewed literally, and CI
  (`expand --check`) fails if they drift. Never hand-edit a generated
  file; edit the template and expand. The engine never reads templates.
- File layout is a team convention, not a format rule: files hold 1..N entities,
  the loader merges one global ID space, entities move between files with zero
  semantic diff. Loader requires `parameters.yaml`, `house-events.yaml`,
  `basic-events/`, `fault-trees/` to exist even if minimal.

### Validation layer (`ci/validate.py`)
Single-pass Python script: strict YAML parse (duplicate-key detection) → JSON Schema → file-index lint (no silently ignored files; `includes` = files loaded) → reference linter (dangling IDs, gate cycles, CCF membership + alpha-sum, sequence path completeness, partition = exact cover of FE outcomes, transfer cycles) → unit rules (FR-25) → orphan warnings. Exit 0 = clean. Regression suite: `ci/test_validate.py` (55 mutation cases + partition lint vs brute force; runs in the CI validate job). `model.yaml` has no JSON Schema: its keys are checked in `validate.py` (`MANIFEST_KEYS`, V&V D-22) — add a key there when the manifest format grows.

### Quantification engine (`engine/src/`)
Rust BDD engine. Key files:
- `bdd.rs` — core ROBDD: flat node arena (`Vec<Node>` of 12-byte `(var,low,high)` triples with `u32` indices), hash consing unique table, apply cache, `minsol` (Rauzy minimal solutions), `enumerate_paths`, `probability`, `birnbaum`.
- `model.rs` — YAML loader: merges all indexed files into one ID space, resolves `{param: ...}` references, expands CCF groups (alpha-factor and beta-factor models per NUREG/CR-5485). Keeps each basic event's probability recipe so `Sampler` can recompute it from sampled inputs.
- `uncertainty.rs` — distributions, keyed counter-based uniforms, inverse-CDF sampling (AS 241, incomplete gamma/beta inversion), summary statistics.
- `main.rs` — CLI + `Compiler` struct that walks formulas and builds BDD nodes, then drives fault-tree and event-tree quantification.

Variable ordering is DFS discovery order from the top gate by default; `--order rdfs` (static, FR-33) and `--reorder` (dynamic sifting, FR-35) are opt-in alternatives — see `docs/limitations.md`. The engine tracks coherence: `NOT`/`XOR` gates set `coherent = false`, which suppresses `minsol` (minimal cut sets require coherent logic) — on BOTH the fault-tree and event-tree paths.

### CI pipeline (`.github/workflows/psa.yml`)
Four jobs: `validate` (schema + lint), `toolchains` (Rust 1.75 vs stable bit identity), `aralia` (42 industrial trees vs committed SCRAM references: exact in both static orders, with `--reorder`, and SCRAM's value inside the truncated bounds), and `quantify` (build engine → unit and tooling tests → property harness → quantify head → quantify base via `git worktree` → post risk-delta as PR comment, updating in place on re-push). Comparison is **reporting, not gating**: `compare.py` always exits 0; acceptability of a ΔCDF is the reviewer's judgment.

### Cross-verification tools (`ci/`)
- `export_mef.py` / `import_mef.py` — Open-PSA MEF XML round-trip
- `crosscheck_scram.py` — compare engine results against SCRAM (independent BDD engine)
- `property_test.py` — randomized model generation + Python truth-table oracle; checks exact probability, cut sets, Birnbaum importance, consequence-level importance (F(x=1)/F(x=0) per end state), partition property (Σ P(sequence) = 1), and CCF expansion end-to-end
- `benchmark_mef.py` — Aralia/MEF benchmark runner
- `import_riskspectrum.py` / `extract_riskspectrum_sql.py` / `crosscheck_rs.py` —
  RiskSpectrum table export → Canopy model, mapping-driven DB extractor, and
  by-id cross-check against RiskSpectrum results (tests:
  `test_import_riskspectrum.py`, fixtures under `ci/fixtures/`)

## Hard-won knowledge (gotchas that cost real debugging time)

- **BDD construction order matters**: linearly OR-accumulating N subtrees
  causes O(N²) node churn (observed: 40M nodes vs 402k). Combine
  collections with balanced pairwise reduction.
- **GC pinning rule** (FR-27): `Compiler::maybe_gc` runs at the start of
  every gate compilation and RENUMBERS nodes. Any BDD handle held in a
  local across a `compile`/`compile_ref` call must be on `Compiler::pinned`
  (see `fold`, the vote-gate inputs, the event-tree conj/fail_only) and be
  read back afterwards. The harness forces GC at every safe point
  (`--gc-threshold 1`) and requires byte-identical JSON — keep it that way.
- **Event-tree rows share one compiler** (FR-38): a row's coherence
  (cut sets vs prime implicants) is `formula_coherent` over ITS tops, never
  `Compiler::coherent` (which has seen other rows); a `maybe_gc()` opens
  every row (rows with all tops cached reach no other safe point — without
  it a 32-row benchmark peaked at 9.9 GB). `--compile per-row` is the
  reference the harness compares against. A house-value change drops only
  the cached gates whose `gate_house_deps` (transitive, memoized) meet a
  house event whose *effective* value changed (FR-43) — any new way for a
  gate to depend on a house event must be reflected there.
- **Reordering renumbers variables at safe points** (FR-35): with
  `--reorder`, `Compiler::maybe_reorder` runs right after a collection
  and changes BOTH node handles (same roots as GC: `pinned` and
  `gate_cache`) AND variable indices (`be_of_var` / `var_of_be` are
  permuted). Never hold a variable index, a var-ordered list or a BDD
  handle across a `compile`/`compile_ref` call unless it is re-derived
  afterwards. The sifter (`reorder.rs`) must never let hash iteration
  order reach a decision or a node number (`reordering_is_deterministic`).
- **Never recurse over a shared BDD without a memo** (`restrict` was
  exponential: V&V D-14). Per-variable passes go through `ProbPlan`.
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
- **Uncertainty keys are model IDs**: a quantity's Monte Carlo samples are a
  pure function of (seed, key, iteration), where the key is `PAR-X`,
  `BE-X/<field>`, `BE-X`, `CCF-X/total_probability` or `IE-X`. Renaming an
  ID changes its samples (and nothing else); per-tree runs sum per
  iteration only because of this; never introduce a sequential RNG stream.
- **Point path and sampled path share arithmetic**: failure-model formulas
  live in one function (`fm_value`) and CCF probabilities are
  `coeff × Qt` with the historical operation order; the coefficients come
  from one function (`model.rs::ccf_coefficients`) whether the factors
  are the point ones or a Dirichlet draw (FR-36: keys `CCF-X/alpha_k`,
  one gamma deviate per non-zero factor, normalised). `Sampler::new` checks
  bit-identity at the point inputs on every run — keep it that way.
- **Units are checked, never converted** (FR-25): one rule table in
  `model.rs::unit_problem` and `validate.py::unit_problem` (keep them
  identical; `ci/test_units.py` cross-checks all 168 combinations). The
  engine refuses mixed time bases at load; it does no unit arithmetic.
- **Transfers are followed** when the target tree is in the model: rows
  `SEQ-S>SEQ-T` = conjunction of both paths on one BDD (exact with shared
  events), house overrides accumulated along the chain (later hop wins,
  gate cache dropped on change). Transfer rows are NEVER aggregated —
  followed or not (V&V D-10: they used to count if their end state was
  mapped). A tree without `initiating_event` is transfer-only: refused
  standalone, skipped by quantify.py. MEF export does not carry transfers.
- **Consequence importance can be negative / RAW < 1**: exact cofactors
  include success branches, so an event whose failure moves frequency out
  of a group (another end state, an unfollowed transfer) has FV < 0 and
  RAW < 1. On the demo, RPS events have RAW = 0 for CDF (V&V F-4). Not a bug.
- **Never let set/dict iteration order reach output**: Python string
  hashing is per-process, so ties must be broken by content (V&V D-11).
  Resolve symlinks before comparing paths with git's (D-12).
- **Truncation bounds rest on two invariants** (FR-34): every set the
  `Truncator` returns is minimized and truncated, and every cut set of a
  gate contains a retained product or a recorded lost term
  (`Zbdd::product_truncated` returns covering terms, not the dropped
  products). The upper bound is only valid for coherent logic; a product
  is kept iff its ascending-order fold probability is >= cut-off
  (shortcuts use a 1e-9 margin so reassociation never flips a decision).
- **Bounds are never values** (FR-42): truncated results carry
  `*_lower_bound`/`*_upper_bound` and no `frequency_per_year` /
  `value_per_year`. Reporting code reads numbers only through
  `ci/bounds.py` (an exact value is [v, v]); printed bounds round
  outward (`fmt_bound`). The exact and truncated ET paths share the
  cut-set listing rule `lists_cut_sets` (no cut sets for `OK` rows, V&V D-20).
- **Python >= 3.12 `sum()` of floats is compensated**, not a left fold: use
  `ci/uncertainty.py::fold_sum` wherever a result must match the engine
  bit for bit (V&V anomaly D-8).
- **Loaders scan fixed paths, not `includes`**: engine and Python read
  top-level `*.yaml` of the entity dirs; Rust `read_dir` sees dotfiles,
  Python `glob` does not. The file-index lint (V&V D-9) is what keeps
  `includes` honest and forbids files that would be silently skipped.
- **Subprocess diagnostics**: when a tool invokes another as subprocess,
  surface stderr in failure messages, not just stdout (an empty error
  message once hid a `ModuleNotFoundError` in CI for a full run).

## Roadmap (agreed priorities, see docs/limitations.md)

1. ~~Uncertainty propagation~~ — done (FR-20–FR-23); ~~LHS~~ (FR-28),
   ~~importance under uncertainty~~ (FR-29 per event tree, FR-37
   model-wide via quantify.py --importance-uncertainty K),
   ~~CCF-factor uncertainty~~ (FR-36, Dirichlet) — done, and shown in the
   PR comment (compare.py, CI runs --importance-uncertainty 10).
   ~~BDD-exact consequence-level importance~~ — done (FR-24);
   ~~partition lint~~ — done; ~~transfers followed~~ — done (FR-11);
   ~~dimensional checks~~ — done (FR-25); ~~single `canopy` CLI~~ — done
   (FR-26).
2. ~~Dynamic variable reordering (sifting)~~ — done, opt-in `--reorder`
   (FR-35): arena geo-mean 0.47x on Aralia, never larger, ~7x slower;
   stalls on das9701 (rdfs is better there); does not crack nus9601.
   Remaining: lower-bound pruning / group sifting, reordering inside a
   single exploding gate (only at safe points today), automatic choice.
3. ~~BDD garbage collection~~ — done (FR-27): mark-and-compact at gate safe
   points + gate release by reference count. ~~Shared manager across
   event-tree sequences~~ — done (FR-38, `--compile shared` default; modest
   gain where conjunctions dominate); ~~invalidate only house-dependent
   gates~~ — done (FR-43); ~~share across event trees~~ — done on request
   (FR-44, `ET-A,ET-B` / `quantify.py --one-process`; not default: later
   trees agree to rounding). Next: the same selective reuse in the
   truncated path's per-configuration memo; reusing row conjunctions.
4. ~~Prime implicants~~ — done for fault trees and event-tree sequences
   (FR-30, ZBDD, truncated by order); remaining: cost on das9701-size trees.
   ~~Truncated quantification with bounds~~ — done for coherent fault
   trees (FR-34) and event trees (FR-39: P(seq) = P(F) − P(F ∧ S), both
   coherent), ~~through quantify.py, reports and viewer~~ (FR-42,
   `--truncated`, bounds shown outward, partition checked on bounds);
   remaining: relative cut-off, automatic exact/truncated selection (an
   open decision), tighter upper bounds, a per-tree exact/truncated mix.
5. MEF event-tree/CCF import. ~~Component templating~~ — done as an
   authoring aid (FR-41, option D: templates/ expand into committed,
   literally reviewed model files; CI checks); remaining: templates for
   gates/modules, CCF groups.
6. ~~Viewer base-vs-head visual diff~~ — done (FR-40: `build_viz.py
   --base`, `canopy delta --viewer`, CI artifact on PRs); ~~partition
   check as a CI lint~~ — done (validator); ~~diffing CCF groups and
   parameters as entities~~ — done (FR-45). Remaining: viewport culling /
   minimap for very large trees; named configurations as diff entities.