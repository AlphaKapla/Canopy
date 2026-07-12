# Quantification

The engine (`engine/`, Rust) loads the YAML model, compiles the logic into
binary decision diagrams, and computes exact probabilities, minimal cut
sets, importances, sequence frequencies, and risk metrics.

## Command line

```
canopy <model-dir> <FT-ID | ET-ID> [options]
```

| argument / option | meaning |
|---|---|
| `<model-dir>` | directory containing `model.yaml` |
| `FT-…` | quantify this fault tree |
| `ET-…` | quantify this event tree (all sequences + metrics) |
| `--house HE-ID=true\|false` | override a house event (repeatable) |
| `--mcs-limit N` | cap cut-set enumeration (default 1000) |
| `--json` | machine-readable output instead of the human report |

Examples:

```bash
canopy model FT-ECCS-INJECTION
canopy model FT-ECCS-INJECTION --house HE-ECC-TRAIN-A-OOS=true
canopy model ET-SLOCA --json > results.json
```

## Fault tree output

```
fault tree      : FT-ECCS-INJECTION (top gate GT-ECC-INJ-TOP)
basic events    : 5
BDD nodes       : 14
P(top) exact    : 1.517326e-5
minimal cut sets: 6
     7.2000e-6  {BE-ECC-PMP-A-TM, BE-ECC-PMP-B-FTS}
     ...
Birnbaum importance:
     7.9017e-3  BE-ECC-PMP-B-FTS
     ...
```

`P(top)` is **exact** — computed on the BDD, with no rare-event or
min-cut-upper-bound approximation. Cut sets are ranked by their point
probability (product of member probabilities). Birnbaum importance of an
event is P(top | event = 1) − P(top | event = 0).

## Event tree output

For each sequence the engine builds the conjunction of its functional-event
outcomes: a **failed** functional event contributes its fault-tree top
gate, a **successful** one contributes the *negation* of its top gate, and
**bypassed** events are skipped. Sequence frequency = initiating-event
frequency × exact P(conjunction).

The success-branch negation matters: it makes sequence frequencies exact
even when fault trees share basic events or support systems — the situation
real plant models are full of — instead of the common approximation that
treats success branches as probability 1.

Cut sets per sequence are reported from the failure-only logic (the
standard *delete-term* convention): the exact frequency includes the
success terms, the listed cut sets do not carry negated literals.

Per-sequence `house_events` overrides are applied for that sequence only.
Transfer sequences are reported with their frequency but excluded from risk
metrics (they belong to the target tree's analysis).

Metrics are aggregated per the manifest's `risk_metrics` mapping of end
states, e.g. `CDF = Σ frequency(sequences with end_state ∈ {CD})`.

## JSON output

With `--json`, event trees emit:

```json
{
  "type": "event_tree",
  "id": "ET-SLOCA",
  "initiating_event": {"id": "IE-SLOCA", "frequency_per_year": 5e-4},
  "sequences": [
    {"id": "SEQ-SLOCA-02",
     "frequency_per_year": 1.84e-9,
     "end_state": "CD",
     "transfer": null,
     "cut_sets": [{"frequency_per_year": 7.2e-10,
                   "events": ["BE-RHR-PMP-A-FTS", "BE-RHR-PMP-B-FTS"]}]}
  ],
  "metrics": [{"id": "CDF", "label": "Core damage frequency",
               "value_per_year": 9.43e-9}]
}
```

Fault trees emit `probability`, `minimal_cut_sets`, `birnbaum`, and
`bdd_nodes`. This format is the contract consumed by `ci/quantify.py`,
`ci/compare.py`, and `viz/build_viz.py`.

## Common-cause failure expansion

CCF groups in `ccf-groups.yaml` are expanded automatically at model load,
before any quantification. For a group of n members: each member's
probability is rescaled to its independent contribution Q₁, a combination
basic event is created for every subset of ≥ 2 members (named
`BE-<GROUP-ID>-<indices>`, e.g. `BE-CCF-ECC-PMP-FTS-1-2`), and every gate
formula reference to a member is rewritten as
`OR(member, …combinations containing it)`. Combination events therefore
appear explicitly in cut sets — a CCF-dominated result is visible as such.

Per-multiplicity probabilities follow NUREG/CR-5485:

| model / testing | Q_k |
|---|---|
| alpha-factor, staggered (default) | α_k · Q_t ⁄ C(n−1, k−1) |
| alpha-factor, non-staggered | k·α_k · Q_t ⁄ (α_t · C(n−1, k−1)), α_t = Σ k·α_k |
| beta-factor | Q₁ = (1−β)Q_t, Q_n = βQ_t |

MGL groups are rejected with an explicit error (convert to alpha factors);
group size is capped at 8 (combination events grow as 2^n; 247 events at
n=8, still trivial for the BDD engine). The alpha factors must sum to 1
(checked by both the validator and the engine).

## How it works

**Compilation.** Gate formulas compile bottom-up into a reduced ordered
BDD. House events fold to constants at compile time (a `true` house event
inside an OR removes the whole branch from the logic). Gate references are
resolved globally, memoized per gate, and cycle-checked. Variable order is
DFS discovery order from the top gate — related events end up adjacent,
which keeps intermediate BDDs small.

**Coherence.** `and`/`or`/`atleast` trees are coherent (monotone). `not`
and `xor` make a tree non-coherent: probabilities remain exact, but minimal
cut sets are skipped for non-coherent fault trees (prime implicants would
be required; see limitations).

**Probability.** One memoized pass over the shared BDD:
P(node) = p(v)·P(high) + (1−p(v))·P(low). Exact, O(|BDD|).

**Minimal cut sets.** Rauzy's minimal-solutions algorithm on the BDD: a
`minsol` transform with a `without` (⊘) operator removing subsumed
solutions, then path enumeration. Subsumption is handled correctly — for
`A OR (A AND B)` the only minimal cut set is `{A}`.

**Numbers you can check.** The repository's model has been cross-validated
against an independent brute-force truth-table evaluation (all 2ⁿ
basic-event states) to full displayed precision, including the "sequence
probabilities sum to 1" partition property.

## Open-PSA MEF export and cross-verification

`ci/export_mef.py <model-dir> <out.xml> [--expand-ccf]` exports the model
to Open-PSA MEF XML, the community exchange format consumed by SCRAM and
other engines. The exporter validates against SCRAM's RELAX NG grammar and
its stricter semantic rules (flat gates with reference-only operands —
associative nesting is flattened and other composites hoisted to
`GT-AUX-*` gates; duplicate operands deduplicated; degenerate votes
rewritten k-of-k → and, 1-of-n → or; CCF members not re-declared).

Two modes: by default CCF groups export as `<define-CCF-group>` so the
consuming engine performs its own expansion; with `--expand-ccf` this
exporter pre-expands (same math as the engine) for exact numerical
comparison. **Convention finding:** SCRAM's alpha-factor implements the
non-staggered formula; ours defaults to staggered. On the demo model,
switching our group to `testing: non-staggered` reproduces SCRAM's raw-mode
result to all displayed digits — the raw-mode difference is convention,
not error. MEF carries no frequency on initiating events in SCRAM's
grammar, so cross-comparison is done on sequence probabilities.

`ci/crosscheck_scram.py [--cases N]` runs the demo model plus N generated
models through both engines and compares every sequence probability
(tolerance 2e-5, bounded by SCRAM's 6-significant-digit report). Current
status: demo + 75 generated models across two seeds, all sequences
agreeing. The `.github/workflows/crosscheck.yml` manual workflow builds
SCRAM from source (one-line boost≥1.73 patch, documented there) and runs
this in CI on demand.

## MEF import and the Aralia benchmark

`ci/import_mef.py <in.xml> <out-model-dir> [--ignore-event-trees]` imports
MEF fault-tree models into the YAML format (gates with
and/or/not/xor/atleast, nand/nor rewritten, float-valued basic events,
house events; MEF names mapped deterministically to prefixed IDs with the
original preserved in labels). Event trees, CCF groups, components and
parameter expressions are rejected loudly rather than imported wrong.
Round trip is exact: export → import → quantify reproduces direct
quantification to 12 digits on the demo model.

`ci/benchmark_mef.py <xml-dir>` runs a directory of MEF trees through both
engines under a common timeout and memory cap. On the full Aralia suite
(43 industrial fault trees bundled with SCRAM, including non-coherent
trees with NOT logic):

* **41 of 43 agree with SCRAM on exact P(top)** to SCRAM's reported
  6 significant digits — including cea9601 (4.3M BDD nodes) and das9209
  at P = 1.058e-13, thirteen orders of magnitude down where approximate
  methods lose fidelity.
* das9701 (2226 gates): our engine exceeds 3 GiB while SCRAM solves it —
  the predicted cost of static DFS variable ordering without sifting; the
  honest scalability boundary of the current engine.
* nus9601 (1567 events): both engines exceed 3 GiB in the test container.

Practical note: SCRAM report files embed full product listings and reach
gigabytes on large trees; the benchmark passes `-l 1`, which truncates the
listing without affecting the BDD-exact probability.

## Performance notes

Nodes are 12 bytes in a flat arena addressed by `u32` indices; hash consing
guarantees each distinct sub-function is stored once. A synthetic
30,000-basic-event model (2000 × 2-of-3 trains × 5 components) builds in
~0.1 s into a ~4.7 MiB arena and quantifies exactly in ~3 ms
(`cargo run --release --example bench`).

One practical rule from that benchmark: **construction order matters**.
OR-accumulating many subsystems one at a time causes O(N²) node churn;
combining them pairwise (balanced reduction) is O(N log N). The compiler
follows the tree structure, which is naturally balanced for well-formed
models.

There is no garbage collection of dead intermediate nodes — fine for batch
runs, a known limitation for long-lived services.
