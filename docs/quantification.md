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
| `--prob-only` | skip cut sets and importance — Birnbaum on fault trees, consequence importance on event trees (large or imported trees) |
| `--json` | machine-readable output instead of the human report |
| `--samples N` | also propagate parameter uncertainty by Monte Carlo, N iterations ([below](#uncertainty-propagation)) |
| `--seed S` | seed for `--samples` (default 20260708; always echoed in the output) |
| `--keep-samples` | with `--json`, also emit every draw (P(top), each sequence, the initiator) |

Examples:

```bash
canopy model FT-ECCS-INJECTION
canopy model FT-ECCS-INJECTION --house HE-ECC-TRAIN-A-OOS=true
canopy model ET-SLOCA --json > results.json
canopy model ET-SLOCA --samples 10000 --seed 20260708
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
**Transfers.** A sequence with `transfer: ET-X` hands off to another event
tree. When ET-X is in the model the transfer is **followed**: the row
expands into one row per sequence T of ET-X, identified `SEQ-S>SEQ-T`, each
quantified exactly as the conjunction of both paths on one BDD,

```
frequency(S>T) = f_IE · P( path(S) ∧ path(T) )
```

so events and support systems shared between the trees are handled
exactly (multiplying separately quantified trees would not be). Transfers
inside ET-X are followed recursively; a transfer cycle is an error.
Per-sequence house overrides accumulate along the chain: T's logic is
evaluated with S's overrides, then T's own (T wins on conflict). The
expansions carry the end states of ET-X and count in the metrics; the
transfer row itself stays listed with its frequency but counts in **no**
metric or end-state group. A transfer whose target is not in the model is
reported and not followed, and likewise counts nowhere. (Before V&V
anomaly D-10 was fixed, a transfer row whose end state happened to be
mapped to a metric was counted.)

An event tree may omit `initiating_event`: it is then **transfer-only**,
quantified solely through the trees that transfer into it. The engine
refuses to quantify it standalone and `ci/quantify.py` skips it. A target
tree that has its own initiator is also quantified standalone with that
initiator; that is correct when the initiator stands for the target's own
initiating events, and double counting when it only stands for the
transfer.

Metrics are aggregated per the manifest's `risk_metrics` mapping of end
states, e.g. `CDF = Σ frequency(sequences with end_state ∈ {CD})`.

Under each metric the human report prints the ten basic events with the
highest BDD-exact Fussell–Vesely importance for this tree
([below](#consequence-level-importance)).

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
     "transfer_path": null,
     "followed": null,
     "cut_sets": [{"frequency_per_year": 7.2e-10,
                   "events": ["BE-RHR-PMP-A-FTS", "BE-RHR-PMP-B-FTS"]}]}
  ],
  "metrics": [{"id": "CDF", "label": "Core damage frequency",
               "value_per_year": 9.43e-9}]
}
```

A row reached through transfers has `transfer_path`, the list of hops
(`{"event_tree", "sequence"}`) from this tree's sequence to the final one.
A followed transfer row has `followed: {sum_probability, probability,
per_sequence_house_overrides}`: its expansions sum to its own probability
when the target tables partition and no expansion hop overrides house
events (`ci/quantify.py` checks this to 1e-9 relative).

Event trees also emit `partition: {sum_probability,
per_sequence_house_overrides}`: the sum of the probabilities of the tree's
own sequences (transfer rows included, expansions not), which is 1 up to rounding for a table that partitions
the outcome space, unless per-sequence house-event overrides change the
logic of some sequences. `ci/quantify.py` fails when it deviates from 1 by
more than 1e-9 on a tree without overrides.

Fault trees emit `probability`, `minimal_cut_sets`, `birnbaum`, and
`bdd_nodes`. This format is the contract consumed by `ci/quantify.py`,
`ci/compare.py`, and `viz/build_viz.py`.

With `--samples`, every point field above is unchanged and an
`uncertainty` object is added: on the fault tree (summary of P(top)); on
each sequence and each metric (`mean`, `std`, `std_error_of_mean`, `p05`,
`p50`, `p95`); and at the event-tree level (`samples`, `seed`, `sampling`,
`clamped_probabilities`, and `quantities`, the list of uncertain quantities
actually sampled with their distribution and point value). Each metric's
`uncertainty.draws` holds all N draws, always, so per-tree runs can be
summed into model-wide metrics; `--keep-samples` adds `draws` to sequences
and fault trees and `initiating_event_draws` to the event tree.

Unless `--prob-only` is given, each metric also carries an `importance`
list and the event tree an `end_states` list (one entry per end state:
`id`, `frequency_per_year`, `importance`). An importance row is:

```json
{"event": "BE-CCF-ECC-PMP-FTS-1-2", "probability": 2.556e-5,
 "frequency_if_true_per_year": 5.0e-4,
 "frequency_if_false_per_year": 9.30e-9,
 "birnbaum_per_year": 4.9998e-4, "fussell_vesely": 0.5787,
 "raw": 22642.0, "rrw": 2.3738}
```

Rows list every basic event the group's sequence BDDs depend on, ranked by
Fussell–Vesely; a ratio is `null` where its denominator is zero (`rrw`
with F(x=0) = 0 < F means infinite: the group cannot occur without the
event).

## Consequence-level importance

For a group of sequences G — a risk metric or an end state — and a basic
event x with probability p, the engine reports the exact conditional
frequencies

```
F_G(x=v) = Σ_{j ∈ G} f_IE · P_j(x=v),   v ∈ {1, 0}
```

where P_j is the full sequence function, success branches (negated tops)
included. Every sequence probability is multilinear in p, so
P_j = p·P_j(x=1) + (1−p)·P_j(x=0) exactly, and a sum of sequences inherits
the identity: F(x=1) and F(x=0) are the exact cofactors of the group
frequency, not cut-set estimates. The measures follow:

| measure | definition |
|---|---|
| Birnbaum (/yr) | F(x=1) − F(x=0) |
| Fussell–Vesely | (F − F(x=0)) / F |
| RAW | F(x=1) / F |
| RRW | F / F(x=0) |

Each P_j(x=v) is one pass over the sequence's flat probability plan with
the nodes labelled x replaced by their v-child (the Shannon cofactor), so
F(x=0) is computed directly, never as a difference, and keeps full
relative precision when it is tiny (RRW of a dominant event). A sequence
whose BDD does not depend on x contributes its frequency to both
conditional values. Cost: two plan passes per (sequence, basic event);
the BDD arena does not grow. The group total is summed in the same order
as the metric value and checked bit for bit against it on every run.

**Across event trees** the same identity makes aggregation exact:
F = Σ_t F_t and F(x=v) = Σ_t F_t(x=v), where a tree that does not depend
on x contributes F_t to both. `ci/importance.py` does this sum;
`ci/consequence_report.py` prints the model-wide table for a metric or an
end-state set, and `ci/compare.py` reports Fussell–Vesely re-ranking in
the PR comment.

What the measures mean, stated plainly:

- They are **exact** for the model as quantified, including success
  branches. The minimal-cut-set Fussell–Vesely (sum of cut sets containing
  x over the total) omits success terms and double-counts overlapping cut
  sets; the consequence report prints it next to the exact value for
  comparison. On the demo model they agree to within 0.1 percentage point;
  they diverge where success branches are not negligible.
- They can be **negative or below 1** where success branches matter: an
  event whose failure moves frequency out of the group (for instance into
  a transfer or a different end state) has RAW < 1 and FV < 0. On the demo
  model the RPS events have RAW = 0 for CDF: every CD sequence requires RPS
  success, and RPS failure routes to the ATWS transfer, whose target tree
  is not part of the demo model, so that frequency is counted nowhere.
- They are **per basic event after CCF expansion**: the CCF combination
  events (`BE-CCF-…-1-2`) and the members' independent parts are ranked
  separately; no member- or group-level aggregate is reported.
- They are **point values**: no importance under uncertainty.

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

## Uncertainty propagation

`--samples N` propagates the state-of-knowledge (epistemic) uncertainty
declared in the model's `uncertainty:` blocks through the exact BDD, by
Monte Carlo. Point results are computed first, exactly as without the
flag, and are not affected.

**What is sampled.** Every quantity that carries a distribution, keyed by
its stable ID:

| where the `uncertainty:` block sits | quantity key | distribution of |
|---|---|---|
| a parameter in `parameters.yaml` | `PAR-…` | the parameter value |
| an inline failure-model quantity | `BE-…/rate`, `BE-…/value`, … | that quantity |
| a basic event (sibling of `failure_model`) | `BE-…` | the event probability — `probability` models only |
| an inline CCF `total_probability` | `CCF-…/total_probability` | the group total Q_t |
| an initiating-event `frequency` | `IE-…` | the initiator frequency (/yr) |

**One sample per quantity per iteration.** In iteration *i* each quantity
is sampled once and every basic event that uses it sees that sample: two
pump events referencing `PAR-ECC-PMP-FTS` fail-to-start with the *same*
probability in a given iteration (state-of-knowledge correlation). This is
what makes the mean of a redundant pair E[X²] rather than E[X]², so the
Monte Carlo mean of a model with shared parameters is above its point
value — on the demo model, 2.34e-8 /yr against 2.21e-8 /yr. CCF events
follow the sampled Q_t of their group (Q_k = coefficient × Q_t, with the
same coefficients as the point expansion); alpha and beta factors are not
sampled.

**Keyed random numbers.** The uniform deviate for a quantity in iteration
*i* is a pure function of (seed, quantity key, *i*) — a SplitMix64-based
hash, not a sequential stream — turned into a sample by the distribution's
inverse CDF. Consequences, by construction:

- *Reproducible*: (model tag, seed, N) determines every draw bit for bit.
- *Additive across processes*: `ci/quantify.py` runs one engine process per
  event tree; iteration *i* draws the same values in each, so the
  model-wide metric is the iteration-by-iteration sum of per-tree draws.
- *Diff-stable*: adding, removing or reordering an unrelated quantity
  changes no other quantity's samples.
- *Paired comparisons*: base and head of a pull request share the samples
  of every quantity the change did not touch (common random numbers), and a
  changed distribution is coupled comonotonically (same *u*, new quantile).
  `ci/compare.py` therefore reports the distribution of the paired change
  head − base, whose band is not swamped by Monte Carlo noise.

**Distributions.** The point value of a quantity is the **mean** of its
distribution:

| distribution | fields | parameterization |
|---|---|---|
| `lognormal` | `error_factor` (> 1) | mean = the point value; EF = 95th percentile / median, so σ = ln(EF)/1.6449 and μ = ln(mean) − σ²/2 |
| `beta` | `alpha`, `beta` | mean α/(α+β) must equal the point value (within 1%) |
| `gamma` | `shape`, `scale` | mean shape × scale must equal the point value (within 1%) |
| `uniform` | `lower`, `upper` | mean (lower+upper)/2 must equal the point value (within 1%) |

A sampled probability above 1 (possible for lognormal or gamma on a
probability) is set to 1 and counted in `clamped_probabilities`; it is
never hidden.

**Refused rather than guessed** (the engine when sampling, and
`ci/validate.py` always): a point value that is not its distribution's
mean; an event-level distribution on anything but a `probability` model
(for rate models the distribution belongs on the rate); a distribution
given both on an event and on its input; a distribution on a CCF group
member (the member's probability derives from the group total, so it would
be silently unused).

**Output.** Mean, sample standard deviation, standard error of the mean,
and the 5th/50th/95th percentiles (linear interpolation between order
statistics, NumPy's default), per fault tree, sequence and metric.
Metric draws aggregate exactly the rows the point values do (expansions
of followed transfers, never transfer rows).

**Numerics.** Normal quantiles use Wichura's AS 241; gamma and beta
quantiles invert the regularized incomplete functions (series and Lentz
continued fractions) by safeguarded Newton on the logarithm of the smaller
tail probability. `ci/crosscheck_special_functions.py` compares a dense
grid against SciPy (worst relative errors: normal 8e-16, ln Γ 2e-15,
gamma 1e-13, beta 2e-11). Each iteration re-runs the O(|BDD|) probability
pass through a flattened, topologically ordered copy of the BDD that is
bit-identical to the recursive pass; the demo event tree runs 10,000
iterations in ~0.05 s.

**Choosing N.** The standard error of the mean is reported; percentiles
of heavy-tailed results (large error factors) need more samples than the
mean. CI uses N = 10,000, seed 20260708.

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
