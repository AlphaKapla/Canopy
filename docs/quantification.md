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
| `--param PAR-ID=value` | override a parameter's point value, in its own unit (repeatable; not with `--samples`) |
| `--mcs-limit N` | cap cut-set enumeration (default 1000) |
| `--prime-implicants` | also list prime implicants (the cut sets of non-coherent logic, with negated events): for a fault tree, of its top event (equal to the minimal cut sets when coherent); for an event tree, of the failure logic of each non-OK sequence whose logic is non-coherent |
| `--order-limit K` | list only cut sets / prime implicants with at most K literals (prime implicants are then built truncated, not filtered); with `--truncated`, drop cut sets of more than K events |
| `--truncated CUTOFF` | coherent logic, instead of the exact BDD: build the minimal cut sets with probability ≥ CUTOFF bottom-up and report **bounds** — on P(top) for a fault tree, on every sequence frequency and metric for an event tree ([below](#truncated-quantification-bounds)) — for models too large for the exact method |
| `--prob-only` | skip cut sets and importance — Birnbaum on fault trees, consequence importance on event trees (large or imported trees) |
| `--json` | machine-readable output instead of the human report |
| `--samples N` | also propagate parameter uncertainty by Monte Carlo, N iterations ([below](#uncertainty-propagation)) |
| `--seed S` | seed for `--samples` (default 20260708; always echoed in the output) |
| `--importance-uncertainty K` | with `--samples`, on an event tree: distributions of the importance measures of each metric's K highest-FV events ([below](#consequence-level-importance)) |
| `--importance-events LIST` | with `--samples`, on an event tree: the same for exactly these basic events (comma-separated; those each metric depends on), with the per-iteration draws `draws_if_true` / `draws_if_false`, so trees can be combined model-wide ([below](#consequence-level-importance)) |
| `--sampling srs\|lhs` | with `--samples`: simple random sampling (default) or Latin hypercube sampling ([below](#uncertainty-propagation)) |
| `--keep-samples` | with `--json`, also emit every draw (P(top), each sequence, the initiator) |
| `--gc-threshold N` | collect garbage once the BDD arena exceeds N nodes (default 4,194,304; `0` disables collection) — never changes a result |
| `--gc-stats` | report collections and arena sizes on stderr |
| `--reorder` | dynamic variable reordering: sift the order whenever the live BDD passes a threshold (65,536 nodes, then twice the size the last sifting left) — a different BDD for the same function, usually much smaller, at a cost in time ([below](#performance-notes)) |
| `--reorder-threshold N` | `--reorder` with this first threshold (live nodes; `0` sifts at every collection — used by the tests) |
| `--compile shared\|per-row` | event trees: one compiler (BDD manager, gate cache) for every row of the tree (default), or a fresh one per row as before FR-38 — results agree to rounding ([below](#performance-notes)) |
| `--order dfs\|rdfs` | variable order: basic events numbered as compilation discovers them (default), or depth first with operands visited last-to-first — a different BDD for the same function, often much smaller, sometimes larger |

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
min-cut-upper-bound approximation.

**Prime implicants** (`--prime-implicants`). Minimal cut sets exist only
for coherent logic; a tree with `not` or `xor` has *prime implicants*
instead — minimal products of events and negated events that imply the
top event (a negated event reads "this component works"). The engine
builds them as a zero-suppressed BDD over literals (event v as variable
2v, its negation as 2v + 1) by the Coudert–Madre recursion: for
f = ite(x, f1, f0) and the consensus g = f0 ∧ f1,
PI(f) = PI(g) ∪ x·(PI(f1) ∖ PI(g)) ∪ ¬x·(PI(f0) ∖ PI(g)). With
`--order-limit K` the construction itself is truncated to products of at
most K literals (PI_k(f) = PI_k(g) ∪ x·(PI_{k−1}(f1) ∖ PI_{k−1}(g)) ∪
¬x·(…)), which is exact and far cheaper than enumerating the full set.
Each listed prime carries its probability Π p(events) · Π (1 − p(negated
events)). On a coherent tree the primes are exactly the minimal cut sets.
Note that the order counts negated literals too: on Aralia das9601 no
prime has fewer than six literals, because the top event needs many
components working as well as a few failing. Cut sets are ranked by their point
probability (product of member probabilities). Birnbaum importance of an
event is P(top | event = 1) − P(top | event = 0).

## Truncated quantification (bounds)

For a coherent fault tree too large for the exact BDD, `--truncated
CUTOFF` (optionally with `--order-limit K`) quantifies from the
significant minimal cut sets instead, and says exactly how much it may
have lost:

```
canopy model FT-ECCS-INJECTION --truncated 1e-6

method          : truncated minimal cut sets, cut-off 1e-6
retained        : 4 minimal cut sets (rare-event sum 3.830439e-5)
P(top) bounds   : 3.829072e-5 <= P(top) <= 4.049933e-5
```

(the exact value is 4.048284e-5; the three dropped cut sets have
probability below 1e-6).

*What is retained.* Gates are evaluated bottom-up into sets of products
(zero-suppressed BDDs over the basic events): a basic event is {{e}}, OR
is union, AND is the pairwise product, vote gates follow the recursion
at-least-k(x₁…xₙ) = x₁·at-least-(k−1)(x₂…) ∪ at-least-k(x₂…); every set
is minimized (supersets removed). Products with probability below the
cut-off, or with more than K events, are dropped as soon as they appear —
inside the pairwise product itself, so the untruncated product is never
built. A product built from a dropped one contains it, so it can be no
more probable and no shorter: the retained set is **exactly the minimal
cut sets with P ≥ cut-off (and order ≤ K)**, the same set the exact
method would list after filtering. A product's probability is the
product of its events' probabilities in variable order, and a product
exactly at the cut-off is kept.

*Lower bound.* The retained cut sets each imply the top event, so the
probability of their union — computed exactly on a BDD built from them,
not by the rare-event sum — is a lower bound on P(top). It is reported as
`probability_lower_bound`; the rare-event sum is reported beside it for
comparison.

*Upper bound.* Every dropped block is recorded as a set of covering
terms: products such that each lost scenario contains one of them (a
dropped product itself, or, for a block of pairs x ∪ y dropped at once,
the side x or y with the smaller total probability). Terms that contain
another term, or contain a retained cut set (whose scenarios are already
in the lower bound), are removed; the sum of the remaining terms'
probabilities is `truncation_error_bound`, and P(top) ≤ lower + bound
(the union bound), reported as `probability_upper_bound` (capped at 1).
This is a rigorous bound on what truncation lost, not an estimate — and
it can be loose where very many products fall just below the cut-off.

Truncation is refused for non-coherent logic (`not`/`xor`: dropping a
product that contains a negated event is not conservative) and together
with `--samples` or `--prime-implicants`. It is opt-in: the default
remains the exact BDD, and nothing chooses between the two methods
automatically.

*Event trees* (`canopy model ET-… --truncated CUTOFF`). A sequence is
F ∧ ¬S, where F is the conjunction of its failed functional-event tops
and S the disjunction of its successful ones — non-coherent because of
the negation. But F and G = F ∧ S are both coherent, and F ∧ ¬S and G
partition F, so P(sequence) = P(F) − P(G) exactly, and the truncated
bounds of the two give
P(sequence) ∈ [max(0, L_F − U_G), min(1, U_F − L_G)] — exact at cut-off 0,
rigorous at any cut-off. G's lost terms are those of S, of the product
F × S, and, for "a lost term of F together with a retained cut set of S",
the cover of the truncated product of F's lost terms with S (smaller
terms than F's own). A row reached through transfers is the conjunction
of all its hops (their failures and successes pooled), each hop's tops
built under the house-event overrides in effect there; metric bounds are
the sums of their rows' bounds (transfer rows excluded, as always). Each
row lists the retained minimal cut sets of its failure logic F — the same
listing convention as the exact path, so none for a row ending in `OK`
(V&V D-20) — as frequencies. Every functional
event a row uses must be coherent, or the tree is refused. JSON: per
sequence `probability_lower_bound` / `probability_upper_bound`,
`frequency_lower_bound` / `frequency_upper_bound` (the probability bounds
× the initiator frequency), the bounds of
`failure_logic` (F) and `failure_and_success_logic` (G), and `cut_sets`;
on a followed transfer row, `followed` with the row's own probability
bounds and the summed bounds of its expansions; per metric
`value_lower_bound` / `value_upper_bound` — no point
frequency or metric value, deliberately; and `partition`, the sums of the
tree's own rows' lower and upper probability bounds. The rows partition
the outcome space, so those sums bracket 1 (unless a row overrides house
events) and a followed row's expansions bracket the same probability as
the row: a check on the bounds that `ci/quantify.py` runs on every
truncated result. The demo's ET-SLOCA at cut-off
1e-7 gives CDF in [2.191e-8, 2.208e-8] around the exact 2.208e-8.

*Through the pipeline* (FR-42). `ci/quantify.py MODEL OUT.json
--truncated CUTOFF [--order-limit K]` quantifies every event tree (and,
with `--configurations`, every named configuration) this way and checks
the partition bounds above; `canopy quantify` and `canopy delta` take the
same flags. The reporting tools read the results through `ci/bounds.py`,
where an exact value is the interval [v, v] and a bound is never read as
a value: `ci/compare.py` (the PR comment) shows each metric,
configuration and sequence as [lower, upper] and the change as the
interval head − base, [L_head − U_base, U_head − L_base] — rigorous, so
two runs at the same cut-off whose bounds overlap show a change interval
straddling zero, with a 🔺/🔽 only when the whole interval is on one side;
bounds agreeing within 1e-9 relative (the threshold for point values)
show "—". The consequence report prints the total as
bounds and every share of it (a cut set's, the coverage, the
minimal-cut-set FV) as the range f / upper .. f / lower, and lists the
qualifying sequences with no cut set retained; the appendix and the
viewer show bounds where values were. Printed bounds are rounded outward
(a lower bound down, an upper bound up), except that a value within
1e-12 relative of the printed decimal — the bounds' own floating-point
error — prints as that decimal. There is no importance, uncertainty or
prime-implicant listing on this path; a model with a non-coherent
functional event must still be quantified exactly.

JSON (`--json`): `method: "truncated-mcs"`, `cutoff`, `order_limit` (when
given), `probability_lower_bound`, `probability_upper_bound`,
`truncation_error_bound`, `retained_cut_sets`, `rare_event_sum`,
`minimal_cut_sets` (retained, most probable first, up to `--mcs-limit`),
`bdd_nodes` (of the lower-bound BDD) and `basic_event_probabilities`.
There is deliberately no `probability` field: nothing downstream should
mistake a bound for the exact value.

When to use it: the exact method is faster on every tree it can handle
(Aralia edf9204, P(top) = 0.525, is exact in 1.9 s; truncated at 1e-12 it
retains 4.6 million cut sets and takes about two minutes). Truncation is
the fallback when the BDD does not fit — and on a tree like Aralia
nus9601, where no Canopy method completes exactly, it gives a certified
interval rather than a number (see the Aralia section below).

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
success terms, the listed cut sets do not carry negated literals. When
that failure logic is itself non-coherent (a `not` or `xor` inside the
fault trees) there are no minimal cut sets; `--prime-implicants` lists
its prime implicants instead (`prime_implicants` on the sequence, each
`{frequency_per_year, events, negated}`), under the same delete-term
convention. `ci/quantify.py --prime-implicants` requests them for every
event tree, and `ci/consequence_report.py` then pools them with the cut
sets.

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
`bdd_nodes`; with `--prime-implicants` also `prime_implicants` (each
`{probability, events, negated}`), and `order_limit` when one was
given. Both fault and event trees emit
`basic_event_probabilities`: every basic event's point probability as the
engine uses it — after failure-model conversion and CCF expansion (a CCF
member's value is its independent part Q₁; combination events are
listed too) — so other tools display the engine's numbers rather than
recomputing them. This format is the contract consumed by `ci/quantify.py`,
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
- They are **point values** unless `--importance-uncertainty K` is given
  (below).

**Importance under uncertainty** (`--samples N --importance-uncertainty
K`). For each metric, the K events with the highest point Fussell–Vesely
are re-evaluated in every Monte Carlo iteration: F(x=1) and F(x=0) by the
same plan cofactors, under that iteration's sampled probabilities and
initiator frequency, and F from the iteration's metric draw (the same
sums, bit for bit). Each importance row of those events gains an
`uncertainty` object with the distribution (mean, standard deviation,
standard error, 5th/50th/95th percentiles) of F(x=1), F(x=0), Birnbaum,
Fussell–Vesely, RAW and RRW; a ratio is summarized over the iterations
where its denominator is non-zero and the others are counted
(`iterations_with_zero_frequency`, `…_if_false`). The point measures are
unchanged. Cost: two plan passes per iteration per selected event per
member sequence, hence the K bound. On the demo model (N = 2000) the ECCS
pump CCF event's FV is 0.58 at point values but 0.51 on average, with a
90% band of [0.21, 0.74].

**Model-wide importance under uncertainty** (`ci/quantify.py --samples N
--importance-uncertainty K`). The K events with the highest model-wide
point Fussell–Vesely of each metric are selected from a point pass (exact,
summed over event trees); every tree is then sampled with
`--importance-events` for the union of those events, which adds the
per-iteration draws of F(x=1) and F(x=0) to its rows. Because the random
numbers are keyed by (seed, quantity, iteration), iteration i is the same
state of knowledge in every tree, so `ci/importance.py` forms the
model-wide F = Σ F_t and F(x=v) = Σ F_t(x=v) iteration by iteration — a
tree that does not depend on x contributes its metric draw F_t — and
summarizes the measures exactly as the engine does (for a single tree the
result is bit-identical to the engine's own). It refuses to combine when a
tree depends on an event but carries no draws for it. `quantify.py`
prints the distributions and `ci/consequence_report.py --metric` tabulates
them (JSON: `importance_uncertainty`). `ci/compare.py` adds them to the PR
comment — for each metric, the events whose Fussell–Vesely distribution
moved, mean [5th, 95th percentile] base → head (paired sampling makes an
unchanged model's draws identical, so only real changes appear); CI
quantifies both sides with `--importance-uncertainty 10`.

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
| beta-factor (any testing; `testing` is ignored, with a validator warning) | Q₁ = (1−β)Q_t, Q_n = βQ_t |

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
| a group's `factor_uncertainty` | `CCF-…/alpha_k`, one per non-zero factor | a Gamma(concentration × alpha_k, 1) deviate G_k; the sampled factors are alpha_k = G_k / Σ G_j (a Dirichlet draw) |
| an initiating-event `frequency` | `IE-…` | the initiator frequency (/yr) |

**One sample per quantity per iteration.** In iteration *i* each quantity
is sampled once and every basic event that uses it sees that sample: two
pump events referencing `PAR-ECC-PMP-FTS` fail-to-start with the *same*
probability in a given iteration (state-of-knowledge correlation). This is
what makes the mean of a redundant pair E[X²] rather than E[X]², so the
Monte Carlo mean of a model with shared parameters is above its point
value — on the demo model, 2.34e-8 /yr against 2.21e-8 /yr. CCF events
follow the sampled Q_t of their group (Q_k = coefficient × Q_t). The
coefficients are the point expansion's unless the group has
`factor_uncertainty`: then each iteration draws the factors from their
Dirichlet (a Beta for a beta-factor group) through one keyed gamma deviate
per non-zero factor, and recomputes every coefficient of the group with
the same function the point expansion uses (so a sampled combination
event and the members' independent parts move together, as the model
requires). Factors and total are independent.

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

**Latin hypercube sampling** (`--sampling lhs`). The unit interval of
each quantity is cut into N equal-probability strata; iteration *i* uses
stratum π(*i*) with a uniform jitter inside it, u = (π(*i*) + v)/N, where
π is a permutation of 0..N−1 keyed by (seed, quantity key) alone (a
Fisher–Yates shuffle driven by keyed uniforms) and v is the keyed uniform
of simple random sampling. Every quantity visits each of its strata
exactly once; permutations of different quantities are independent, so
quantities are correlated only through shared parameters, as with simple
random sampling. All the keyed properties above carry over, with one
addition: an LHS draw depends on N, so pairing and per-tree summation
need the same N, seed and method (`ci/compare.py` checks all three). The
estimator is unbiased and its variance is at most N/(N−1) times the
simple-random variance (Owen 1997), typically much less; the reported
`std_error_of_mean` is the simple-random formula, so for LHS it is
conservative up to that factor. On the demo model at N = 1000, the
standard deviation of the CDF mean over 40 seeds is 1.44e-9 with LHS
against 2.05e-9 with simple random sampling.

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
MEF models into the YAML format: fault trees (gates with
and/or/not/xor/atleast, nand/nor rewritten, float-valued basic events,
house events, untyped `<event>` references resolved by definition), CCF
groups (alpha-factor, imported non-staggered as in MEF/SCRAM, and
beta-factor, with `<float>` distribution and factors), and event trees
whose forks each have two paths collecting a formula and its negation —
the formula becomes the functional event's top gate (a pass-through gate
when it is not a gate reference), each path to a `<sequence>` becomes a
row whose end state is the MEF sequence name, and each end state gets a
risk metric. MEF names map deterministically to prefixed IDs (names that
already follow the ID grammar are kept, so Canopy's own exports return
their IDs), with originals in labels and `external_ids`. Everything else
is refused loudly. Round trip is exact: every harness-generated model
exported (CCF pre-expanded, or raw for non-staggered groups), imported
and requantified reproduces every sequence probability to 1e-12.

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
  Truncated quantification (`--truncated`) bounds it instead: at cut-off
  1e-8, 12 minimal cut sets are retained and
  9.939274e-6 ≤ P(top) ≤ 2.716193e-2 — a certified interval, but a wide
  one, because the union bound sums a very large number of dropped
  products (about 30 s and 3–5 GB locally; at 1e-10 it does not finish
  within 400 s).

On the other trees, `python ci/aralia_regression.py <dir> --truncated
1e-12` checks that SCRAM's exact P(top) lies within Canopy's bounds:
all 39 coherent trees with a reference value do, with relative bound
widths (upper − lower)/upper of at most 1e-3 on 36 of them. The other
three have P(top) within an order of magnitude of the cut-off or below
it (das9204 2.2e-11, das9209 1.1e-13, edf9206 8.6e-12), so most of their
probability sits in cut sets below the cut-off and the interval is
honest but wide (relative width ≥ 0.96). The three non-coherent trees
with a reference are refused, as designed. CI runs the same check at
cut-off 1e-10 on every push (about 70 s for the suite locally, 1.4 GB
peak; nus9601 is reported, not gated).

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

**Garbage collection.** Compiling a large tree leaves most intermediate
nodes dead. At each gate-compilation safe point, once the arena exceeds
the threshold (default 4,194,304 nodes; afterwards twice the surviving
count), the engine marks the nodes reachable from its roots — every
compiled gate still in use and every pinned partial result — and compacts
the survivors in their original order, so children still precede
parents, the flat probability plan stays valid, and every function is
unchanged: collection is invisible in the output, which the property
harness checks by forcing a collection at every safe point. A compiled
gate is released once its last reference (counted before compilation) is
consumed. On Aralia das9701 (2226 gates) peak memory falls from 5.2 GB
to 2.1 GB with an identical P(top); small models never reach the
threshold.

**Variable order.** BDD size depends on the variable order, which is the
order basic events are numbered in. The default numbers them as
compilation discovers them, depth first; `--order rdfs` numbers them in a
depth-first pass that visits every formula's operands last-to-first. The
function, and so every result, is the same to rounding (the property
harness checks both orders on every case, and CI checks both against
SCRAM on the Aralia suite). On Aralia, reverse-DFS gives the smaller BDD
on 25 of 42 trees (geometric-mean size 0.66× the default): das9701 needs
0.76 million nodes instead of 6.8 million, edf9202 8.5 thousand instead of
413 thousand, but edf9203 needs 877 thousand instead of 160 thousand. The
default is kept so historical results stay bit-identical; try `rdfs` on a
tree that is slow or large.

**Dynamic reordering (sifting).** `--reorder` changes the order while
compiling. At a garbage-collection safe point, once the live BDD exceeds
65,536 nodes (later: twice the size the previous sifting left), the live
functions — every compiled gate still in use and every pinned partial
result, the collector's roots — are copied into a separate sifting
manager (`engine/src/reorder.rs`) with reference counts and one unique
table per variable. There each variable, largest level first, is moved
through the order by in-place swaps of adjacent levels (Rudell's
algorithm) and left where the BDD was smallest; a move in one direction
stops once the BDD exceeds 1.2× the best size seen, and two variables that
never occur in the same root's support are exchanged by relabelling alone
(an interaction matrix, as in CUDD). The sifted functions are then
rebuilt, children first, into a fresh arena in which each variable's
index is its new level, and the engine renumbers its basic events
accordingly, so every other algorithm keeps "order = index". Every
decision depends on the graph alone and nodes are renumbered by a
structural traversal, so a run reproduces bit for bit. Results agree with
the static orders to rounding (checked on every harness case with
reordering forced at every safe point, and on the Aralia suite in CI).

On the Aralia suite `--reorder` sifts 18 of the 42 trees (the others
never reach the threshold) and never ends with a larger arena: geometric
mean 0.47× the default over all 42, with edf9202 at 9.1 thousand nodes
instead of 1.7 million, elf9601 30 thousand instead of 2.0 million and
cea9601 190 thousand instead of 4.3 million; peak memory falls on 16
trees (edf9204 927 → 236 MB). The price is time — the suite takes about
seven times longer (175 s against 26 s; cea9601 8.3 s instead of 0.9 s) —
and it is not a remedy everywhere: on das9701 sifting stalls at 4.6
million nodes and takes 140 s instead of 20 s, with a higher peak (2.4 GB
instead of 1.9 GB), where `--order rdfs` alone
reaches 0.76 million in seconds (`--order rdfs --reorder`: 2.1 million,
0.9 GB). On nus9601 it does not finish within an hour from either static
order. Use it when a tree is memory-bound under both static orders.

**One compiler per event tree.** An event tree's rows share one compiler
(`--compile shared`, the default since FR-38): each functional-event top
is compiled once per house-event configuration and cached across rows
(use counts cover every row's references, so a top stays cached until its
last row), BDD nodes are shared, and each row only builds its own
conjunction. A collection safe point opens every row, so the previous
rows' conjunctions do not accumulate. Whether a row's logic is coherent
(which decides between minimal cut sets and prime implicants) is decided
per row from the model — no NOT or XOR in any of its non-bypassed tops —
since a shared compiler has seen other rows' gates. `--compile per-row`
keeps the previous behaviour (a fresh compiler per row); results agree to
rounding and are usually byte-identical (rows mostly discover variables in
the same order). The gain is the avoided recompilation, which is modest
when the conjunctions dominate: on a 32-row tree whose five functional
events are large subtrees of Aralia edfpa14q, 209 s instead of 232 s,
peak 4.2 GB instead of 4.5 GB, byte-identical output.

**Importance on large trees.** Fault-tree Birnbaum importance is computed
from plan cofactors — two passes over the flat plan per variable of the
support, exactly 0 outside it — the same method as consequence-level
importance. (An earlier path through unmemoized `restrict` was
exponential on shared DAGs: Aralia baobab1 never finished; V&V anomaly
D-14.)
