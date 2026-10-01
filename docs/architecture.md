# Architecture

## Repository layout

```
model/                     the model: YAML source of truth
  model.yaml               manifest: metadata, file index, configurations
  parameters.yaml          named constants with units & provenance
  house-events.yaml        boolean configuration flags
  ccf-groups.yaml          common-cause failure groups
  basic-events/*.yaml      basic events (split per system by convention)
  fault-trees/*.yaml       fault trees (1..N per file; one ID space)
  event-trees/*.yaml       event trees (flat sequence tables)
schema/
  psa-model.schema.json    JSON Schema, entity type per $defs entry
engine/                    Rust quantifier
  src/bdd.rs               the BDD engine proper
  src/model.rs             YAML loader / probability resolution
  src/main.rs              CLI, fault-tree & event-tree quantification
  examples/bench.rs        30k-event synthetic scale benchmark
ci/
  validate.py              strict parse + schema + reference linter
  quantify.py              run engine on every event tree → merged JSON
  compare.py               base-vs-head markdown risk delta
  property_test.py         randomized engine-vs-oracle validation
  export_mef.py            Open-PSA MEF XML exporter
  crosscheck_scram.py      engine-vs-SCRAM cross-verification
  import_mef.py            MEF importer (fault trees, CCF, event trees)
  scram_suite_regression.py  importer + engine vs SCRAM's published test values
  benchmark_mef.py         Aralia/MEF benchmark runner (both engines)
viz/
  build_viz.py             model (+results) → single-file HTML viewer
  template.html            the viewer application
.github/workflows/psa.yml  the PR pipeline
```

Dataflow:

```
YAML model ──validate.py──▶ ok/fail
    │
    └──engine──▶ results JSON ──compare.py──▶ PR risk-delta comment
                     │
                     └──build_viz.py──▶ psa-viewer.html
```

Derived artifacts (results, delta reports, the viewer) are never committed.

## Design decisions and their reasons

**Mappings keyed by immutable ID, never lists.** The single biggest
git-friendliness decision: reordering is a no-op diff, one added entity
touches only its own lines, and merge conflicts shrink to genuinely
conflicting edits.

**One global ID space, file layout free.** The loader merges every indexed
file; CI enforces cross-file ID uniqueness. Consequence: an entity can move
between files with *zero semantic diff*, so teams can start with one file
per entity type and split per system later without migration.

**Structured formulas.** `{and: [A, B]}` instead of `"A AND B"`: no
expression parser, no precedence bugs, recursive schema validation for
free, and diffs show exactly which operand changed.

**Flat sequence tables for event trees.** Nested branch structures diff
terribly (one insertion re-indents everything below). The table is the
source; the staircase is a rendering; CI checks the table instead of
trusting a drawing.

**Units and provenance mandatory.** `{value, unit}` everywhere; `source` +
`justification` required on every number. Diff + blame + provenance =
complete audit trail.

**No YAML anchors, no implicit typing.** Anchors make diffs lie about what
changed; implicit typing turns `NO` into `false`. Reuse is explicit
(`{param: …}`), and the schema's closed enums and
`additionalProperties: false` fail closed on anything ambiguous.

**Configurations are runtime, not edits.** House events fold in at
quantification time (`--house`), so "what if train A is out?" never
touches the committed model.

## Engine internals

The core is a classic reduced ordered BDD with hash consing, chosen because
BDD quantification is *exact* — the reason serious PSA codes moved beyond
cut-set-based approximations.

* **Node arena.** A node is 12 bytes — `(var, low, high)` as three `u32` —
  in one contiguous `Vec`. Node references are indices, not pointers: half
  the size on 64-bit, cache-friendly traversal, and trivially serializable.
* **Unique table.** `mk(var, low, high)` hash-conses: identical
  sub-functions exist once, equality is index comparison, and the two BDD
  reduction rules (redundant-test elision, sharing) hold by construction.
* **Apply cache.** Memoizes `(op, f, g) → result` with operand-order
  normalization for commutative ops, giving the standard O(|f|·|g|)
  Shannon-expansion apply.
* **Vote gates** build by dynamic programming over (input index, k still
  needed).
* **Probability** is one memoized pass; **Birnbaum** is two cofactor
  restrictions and two probability passes.
* **Minimal cut sets** use Rauzy's minimal-solutions transform with the
  `⊘` (without) subsumption operator, then path enumeration — valid for
  coherent logic; the compiler tracks coherence and the CLI degrades
  gracefully for non-coherent trees.
* **Variable ordering** is DFS discovery order from the top gate. Ordering
  is *the* determinant of BDD size on hard models; production engines add
  dynamic reordering (sifting) — see limitations.

Why Rust specifically: the engine's performance is dominated by node-table
memory layout and hashing. Rust gives compact arena storage with no GC
pauses mid-traversal and fearless use of `u32` indices, while `serde`
handles the YAML/JSON edges. The synthetic benchmark
(`engine/examples/bench.rs`) quantifies a 30,000-basic-event model exactly
in milliseconds from a ~5 MiB arena.

## The trust chain

Correctness rests on six independent legs: the Aralia industrial
benchmark suite, 41/43 trees agreeing with SCRAM on exact P(top) (the
other two are memory boundaries, not disagreements), plus an exact
export→import round trip; cross-verification against
SCRAM, an independent BDD engine, via MEF export (demo + 75 generated
models, every sequence probability agreeing); a randomized property-test
harness run in CI on every PR (generated models, brute-force oracle with
its own CCF expansion, exact cut-set equality — it caught two real
empty-cut-set semantics defects during its own bring-up); unit tests with hand-computed
known answers (subsumption, vote gates, non-coherent probability), an
independent brute-force truth-table evaluator over the full model (all 2ⁿ
states, no shared code with the engine) matching every sequence frequency,
and the partition property (Σ P(sequence) = 1) confirming the event tree
covers the outcome space exactly once.
