# Software Verification and Validation Report

**Software:** Canopy — git-native PSA toolchain (quantification engine,
validators, exchange-format tools)
**Version under report:** git tag `v0.2.0`; engine crate 0.2.0; model
schema 0.1.0 (with backward-compatible additions since v0.1.0: optional
`initiating_event` for transfer-only trees). Evidence for the previous
release, v0.1.0 (commit `9839e03`), is kept where marked "at v0.1.0".
**Status:** living document — any pull request that changes verified
behavior or adds/removes evidence must update this report in the same
change set, subject to the same review.

---

## 1. Purpose and scope

This report collects and organizes the verification and validation
evidence for the software in this repository: the Rust BDD quantification
engine (`engine/`), the model validator (`ci/validate.py`), the Open-PSA
MEF exchange tools (`ci/export_mef.py`, `ci/import_mef.py`), the
RiskSpectrum migration tools (`ci/import_riskspectrum.py`,
`ci/crosscheck_rs.py`), and the comparison and reporting tooling
(`ci/compare.py`, `ci/consequence_report.py`, `ci/quantify.py`,
`ci/uncertainty.py`, `ci/importance.py`, `ci/canopy.py`, `ci/property_test.py`, `ci/crosscheck_scram.py`,
`ci/crosscheck_special_functions.py`, `ci/benchmark_mef.py`).

Vocabulary follows common V&V usage: **verification** asks whether the
software correctly implements its requirements ("did we build it right");
**validation** asks whether its results are correct against independent
references ("did we build the right thing"). The structure of this report
is informed by the expectations that regulators attach to PRA software
quality (in the US frame, the software-QA expectations behind
NRC RG 1.200's endorsement of the ASME/ANS PRA standard, with
NQA-1-style configuration control), without claiming conformance to any
of them — see §9.

### 1.1 Intended-use classification

The software is classified for **research, screening, teaching, and
process demonstration**. It is **not** qualified for licensing-basis or
safety-decision use. §9 states exactly what separates the current
evidence from a licensing-grade program. Every result in this report is
reproducible from the tagged commit using the commands in Appendix A.

---

## 2. Configuration management

Configuration control is inherited from the repository design rather than
bolted on:

* The model, schema, engine source, all V&V tooling, and this report are
  under git version control in one repository. A **git tag pins the exact
  software**: source, schema version, and (via `engine/Cargo.lock`) every
  third-party dependency at exact versions, so any result regenerates
  bit-for-bit from a checkout.
* Changes reach `main` either by pull request or, since v0.1.0, as
  direct pushes by the maintainer's development agent. CI runs the same
  pipeline on both (model validation and the validator's regression
  suite, engine unit tests on the latest stable Rust and on the minimum
  supported Rust 1.75, the tooling tests, and the 60-case randomized
  property harness, §5.2, at a fixed seed). On a pull request it blocks
  merge; on a direct push it can only report, so every direct push is
  preceded by a local `python ci/canopy.py verify` whose results the
  commit message records (CLAUDE.md, rule 7). This is weaker than
  review-before-merge and is stated as such (§9).
* Derived artifacts (quantification results, reports, the model viewer)
  are never committed; they are regenerated, which eliminates the class
  of error where stored results drift from the model that produced them.
* Naming: the software was renamed to **Canopy** after tag v0.1.0
  (commit "Rename software to Canopy"). The rename changes the crate,
  binary (`canopy`), and env var (`CANOPY_BIN`) names only — no
  verified behavior. Evidence commands in Appendix A use the new
  names; regenerating from the v0.1.0 tag requires the old binary
  name `psa-bdd`.
* The defect history (§7) is the git history itself; each entry cites
  its fixing commit's subject.

---

## 3. Requirements

Functional requirements (FR) and non-functional requirements (NFR)
verified by this report. Each is testable; §8 maps them to evidence.

| ID | Requirement |
|---|---|
| FR-1 | Parse the YAML model strictly: reject duplicate keys, fail on malformed input. |
| FR-2 | Validate every model file that has a schema definition (basic events, fault trees, event trees, parameters) against the JSON Schema; unknown fields are errors. `house-events.yaml` and `ccf-groups.yaml` have no schema definition and are lint-checked only (anomaly D-4). |
| FR-3 | Enforce referential integrity: dangling references, gate cycles, duplicate IDs across files, and incomplete sequence tables are errors; so are sequence tables that do not partition the functional-event outcome space (overlapping paths, uncovered outcomes), model files that no loader reads (non-`.yaml` or hidden entity files, sub-directories, stray root YAML), missing required files, and a manifest `includes` index that disagrees with the files actually loaded. |
| FR-4 | Compute the exact top-event probability of a fault tree (no rare-event or MCUB approximation). |
| FR-5 | Compute the complete set of minimal cut sets of a coherent fault tree, with correct subsumption; a tautological function has exactly the empty cut set. |
| FR-6 | Compute Birnbaum importance P(top\|x=1) − P(top\|x=0) per basic event, in time linear in the BDD size per variable. |
| FR-7 | Support k-of-n vote gates exactly. |
| FR-8 | Compute exact probabilities for non-coherent logic (NOT/XOR); refuse to emit minimal cut sets for non-coherent logic rather than emit invalid ones (prime implicants instead, on request: FR-30). |
| FR-9 | Fold house events as compile-time constants; support per-run and per-sequence overrides. |
| FR-10 | Expand CCF groups per NUREG/CR-5485: alpha-factor (staggered and non-staggered) and beta-factor; reject MGL and oversize groups explicitly. |
| FR-11 | Quantify event-tree sequences exactly, with success branches contributing negated top gates; support bypassed events and per-sequence house overrides. Follow a transfer to an event tree of the model exactly — one row per target sequence, quantified as the conjunction of every hop's path on one BDD, house overrides accumulated along the chain (a later hop wins), nested transfers recursively, transfer cycles refused — and count its expansions, never the transfer row, in metrics and end-state groups; a transfer to a tree not in the model is reported and counted nowhere. A tree without an initiating event is transfer-only and is refused standalone. |
| FR-12 | Aggregate sequence frequencies into risk metrics per the manifest's end-state mapping, over every row except transfer rows (FR-11). |
| FR-13 | Sequence probabilities of a complete event tree partition the outcome space (sum to 1); the engine reports the sum per event tree and quantification fails when it deviates from 1 by more than 1e-9 on a tree without per-sequence house-event overrides. |
| FR-14 | Export models to Open-PSA MEF XML accepted by an independent implementation (schema-valid and semantically accepted by SCRAM). |
| FR-15 | Import MEF fault trees, alpha/beta CCF groups and complementary-fork event trees with exact fidelity (export→import round trip reproduces quantification); refuse every other construct explicitly. |
| FR-16 | Report base-vs-head risk deltas computed from two git revisions of a model. |
| FR-17 | Convert basic-event failure models (`probability`, `rate-mission`, `rate-repair`, `rate-periodic-test`) to point unavailability values using documented closed-form formulas. |
| FR-18 | Aggregate minimal cut sets (and, when present, prime implicants of non-coherent sequence logic, negated events never counted as failures) and the minimal-cut-set Fussell–Vesely measure for a named consequence (risk metric or end-state set), pooled across every qualifying sequence in every event tree, without altering any already-quantified frequency. (Exact importance for the same consequence is FR-24.) |
| FR-19 | Convert a RiskSpectrum table export into a model that quantifies identically to its hand-written equivalent (every sequence frequency, cut set, fault-tree probability and configuration result), deterministically (byte-identical re-runs), keeping the original record ids in `external_ids`; refuse every construct without a Canopy equivalent explicitly rather than approximate it, and log every numeric approximation. |
| FR-20 | Propagate state-of-knowledge uncertainty by Monte Carlo through the exact BDD: sample every quantity carrying a distribution — parameters, inline failure-model quantities, event-level distributions on `probability` events, CCF totals, initiator frequencies — once per iteration, with every event that references a quantity sharing its sample (state-of-knowledge correlation); lognormal with the point value as mean and EF = q95/q50, beta/gamma/uniform with mean equal to the point value; report mean, standard deviation, standard error of the mean and 5th/50th/95th percentiles per fault tree, sequence and metric; leave point results unchanged. *(Added after v0.1.0.)* |
| FR-21 | Monte Carlo random numbers are a pure function of (seed, quantity ID, iteration): results reproduce bit-for-bit from (tag, seed, N); separately quantified event trees combine iteration by iteration into model-wide metrics; quantities untouched by a model change keep their samples; sampled probabilities above 1 are clamped and counted, never hidden. *(Added after v0.1.0.)* |
| FR-22 | Refuse inconsistent or ambiguous uncertainty specifications, in the validator and in the engine when sampling: a point value that is not its distribution's mean (lognormal: must be positive; beta/gamma/uniform: within 1 %); invalid distribution parameters; an event-level distribution on a non-`probability` model; a quantity given two distributions; a distribution on a CCF group member. The RiskSpectrum importer places distributions accordingly and logs what it moves or drops. *(Added after v0.1.0.)* |
| FR-23 | When both sides are sampled, report each metric's distribution for base and head and, when N and seed match, the distribution of the paired change head − base. *(Added after v0.1.0.)* |
| FR-25 | Enforce dimensional consistency with one rule table, identical in the validator and the engine (which refuses to load an inconsistent model): probabilities and CCF totals `per_demand` or `dimensionless`; frequencies and initiating events `per_year`; each rate-based failure model's rate and time on the same base (`per_hour` with `hour`, `per_year` with `year`), never converted; a parameter's unit applies wherever it is referenced. *(Added after v0.1.0.)* |
| FR-26 | Provide one command-line entry point (`ci/canopy.py`) whose subcommands run the existing tools unchanged — identical output, exit codes propagated — plus `delta`, which quantifies the working-tree model and the same model at a git ref with one engine binary and compares them, always removing its worktree, and `verify`, which runs the checks required before a commit and stops at the first failure. Derived reports are reproducible: identical inputs give byte-identical output regardless of per-process hash seeds (NFR-1). *(Added after v0.1.0.)* |
| FR-28 | Offer Latin hypercube sampling as an alternative layout of the Monte Carlo deviates: per quantity, the N iterations visit N equal-probability strata once each, in an order keyed by (seed, quantity key) alone, jittered by a keyed uniform, keeping FR-21's properties (bit-for-bit reproducibility, additivity across processes, diff stability) given the same N; paired comparisons only between identical (N, seed, method). *(Added after v0.2.0.)* |
| FR-45 | In the viewer's diff (FR-40), report parameters and CCF groups as entities of their own — added, removed, or changed with the fields that changed (a parameter's value, unit, uncertainty, label, provenance; a group's model, members, total, factors, testing, factor uncertainty, label, provenance), compared exactly — with their base values; give each parameter the basic events, CCF groups and initiating events that reference it, and each basic event its parameters and CCF groups. *(Added after v0.2.0.)* |
| FR-44 | Quantify several event trees in one process on request (target `ET-A,ET-B,…` with `--json`; `quantify.py --one-process`): one model load, one compiler shared by every listed tree (use counts over all their rows; house changes between trees as between rows, FR-43), one JSON object keyed by tree ID whose values agree with each tree quantified in its own process (probabilities, frequencies, importance and Monte Carlo draws within 1e-12, identical cut-set and prime-implicant sets); refuse text output, non-event-tree targets, duplicates, unknown and transfer-only trees; single-tree output unchanged. *(Added after v0.2.0.)* |
| FR-43 | With one compiler per event tree (FR-38), when a row's house-event values differ from the previous row's, drop exactly the cached gates that reach — through their formula or any gate it references — a house event whose effective value changed (an override added, removed or changed; an override restating the current value changes nothing) and keep every other cached gate; results as with a fresh compiler per row (probabilities and frequencies within 1e-12, identical cut-set and prime-implicant sets). *(Added after v0.2.0.)* |
| FR-42 | Carry truncated event-tree results (FR-39) through the pipeline: `quantify.py --truncated CUTOFF [--order-limit K]` (and `canopy quantify` / `canopy delta`) quantifies every event tree and named configuration that way, checking on every run — as FR-13 does for sums — that each tree's own rows' probability bounds sum to an interval containing 1 and that a followed transfer's expansions' summed bounds overlap the row's own (per-sequence house overrides exempt); the delta report, the consequence report, the appendix and the viewer show bounds [lower, upper] wherever they showed values, printed outward (a value within 1e-12 relative of the printed decimal excepted), a change as the interval [L_head − U_base, U_head − L_base] (and the ratio [L_head / U_base, U_head / L_base] when L_base > 0), and a share of a bounded total as f / U .. f / L; a bounded value counts as changed when either bound moves by 1e-9 relative or more (the threshold of point values); a bound is never read or shown as a value, and exact results are reported exactly as before. *(Added after v0.2.0.)* |
| FR-41 | Offer templates as an authoring aid that never replaces the flat model: `canopy expand` writes each basic-event file generated from component types and instance files (fields, order and number notation of a hand-written file; each number reading back exactly; a GENERATED header naming the template), refusing to overwrite a hand-written file or to generate an event also defined by hand; `canopy expand --check` (CI) fails unless every generated file is exactly what its template produces today, exists, and still has a template. The engine never reads templates. *(Added after v0.2.0.)* |
| FR-40 | Show what a model change does (`viz/build_viz.py --base BASE [--base-results]`, `canopy delta --viewer`, CI artifact on pull requests): every basic event, gate, fault tree, house event, event tree and sequence added, removed or changed between two models, with the fields that changed and their base values, and sequence frequencies and metrics base → head — a relative change of 1e-9 or more counting as changed (the threshold of `ci/compare.py`), probabilities and frequencies compared only when both sides have results; exact (nothing missing, nothing spurious), deterministic, and without `--base` the model data unchanged. *(Added after v0.2.0.)* |
| FR-39 | On request (`--truncated CUTOFF` on an event tree, optionally with `--order-limit K`), give certified bounds on every sequence frequency and every metric, for coherent functional-event logic: P(sequence) = P(F) − P(F ∧ S) (F the conjunction of the failed tops, S the disjunction of the successful ones, both coherent), each side bounded as in FR-34; rows through transfers pool every hop's outcomes under the house overrides in effect at each hop; metric bounds sum their rows' bounds (transfer rows excluded); exact at cut-off 0; bounds only, never reported as a frequency; non-coherent functional events refused. *(Added after v0.2.0.)* |
| FR-38 | Compile each event tree's rows with one shared compiler by default (each functional-event top once per house-event configuration, BDD nodes shared, a collection safe point per row), deciding each row's coherence from its own tops, with results agreeing to rounding with a fresh compiler per row (`--compile per-row`, kept as the reference): probabilities, frequencies, importance within 1e-12, identical cut-set and prime-implicant sets. *(Added after v0.2.0.)* |
| FR-37 | Give model-wide importance under uncertainty: for each risk metric, the distributions (as FR-29) of the importance measures of the K events with the highest model-wide point Fussell–Vesely, from event trees sampled separately with the same N, seed and method, combined iteration by iteration (F = Σ F_t, F(x=v) = Σ F_t(x=v), a tree not depending on x contributing F_t); exactly the engine's own statistics for a single tree; refuse to combine when a tree depending on an event lacks its draws; in the PR comment, the events whose Fussell–Vesely distribution moved, base → head. *(Added after v0.2.0.)* |
| FR-36 | Propagate state-of-knowledge uncertainty on CCF factors: a group's `factor_uncertainty` (Dirichlet with parameters concentration × alpha_k — a Beta on β for a beta-factor group — whose means are the point factors) is sampled once per iteration through keyed gamma deviates `CCF-X/alpha_k` (FR-21's reproducibility and additivity kept), and every coefficient of the group is recomputed from the sampled factors with the point expansion's own formula (staggered or non-staggered), independently of the group total; point results unchanged by the block; malformed blocks refused by the validator and the engine. *(Added after v0.2.0.)* |
| FR-35 | On request (`--reorder`, `--reorder-threshold N`), reorder the variables dynamically by sifting at garbage-collection safe points without changing any result beyond rounding: probabilities, frequencies, importance and conditional frequencies within 1e-12 relative, identical cut-set and prime-implicant sets, from either static order; sifting never ends with a larger BDD than it started from; the run is reproducible bit for bit (no dependence on hash seeds); the default order and its outputs unchanged. *(Added after v0.2.0.)* |
| FR-34 | On request (`--truncated CUTOFF`, optionally with `--order-limit K`), quantify a coherent fault tree from its significant minimal cut sets instead of the exact BDD: retain exactly the minimal cut sets with probability ≥ the cut-off (and at most K events), built bottom-up without forming untruncated products; report the exact probability of their union as a lower bound on P(top) and, as an upper bound, the lower bound plus Σ P over covering terms of every dropped product not covered by a retained cut set (capped at 1); label the result as bounds, never as the probability; refuse non-coherent logic, event trees, and the combination with sampling or prime implicants. The exact method stays the default and is never replaced automatically. *(Added after v0.2.0.)* |
| FR-33 | Offer an alternative static variable order (`--order rdfs`, reverse-operand depth first) without changing any result beyond rounding: probabilities, frequencies, importance and conditional frequencies within 1e-12 relative, identical cut-set and prime-implicant sets; the default order and its outputs unchanged. *(Added after v0.2.0.)* |
| FR-32 | Generate the model's report appendices (risk metrics, initiating events, parameters, basic events, CCF groups, house events, event trees, fault-tree gates) from the model and the engine's results: every entity exactly once, every number copied from the model or the results (never recomputed), every provenance block verbatim, reproducible output. A derived artifact, never committed. *(Added after v0.2.0.)* |
| FR-31 | Quantify each named configuration of the manifest (house-event and parameter point-value overrides) next to the base case, with results identical to quantifying the model edited to say the same thing; report each configuration base → head and against the base case; validate that every referenced house event and parameter exists with a boolean or non-negative numeric value. *(Added after v0.2.0.)* |
| FR-30 | On request, list the prime implicants of a fault tree, and of the failure logic of each non-coherent event-tree sequence (delete-term convention) — minimal products of events and negated events implying the top event, each with its probability — exactly (equal to the minimal cut sets on coherent logic), optionally limited to at most K literals by a construction that is itself truncated; the same order limit applies to minimal cut sets. *(Added after v0.2.0.)* |
| FR-29 | On request, give the distributions of the consequence-level importance measures (F(x=1), F(x=0), Birnbaum, FV, RAW, RRW) of each metric's K highest point-FV events over the Monte Carlo iterations, computed per iteration by the exact cofactor method of FR-24 under the sampled inputs; ratios with a zero denominator counted, never summarized as numbers. Per event tree. *(Added after v0.2.0.)* |
| FR-27 | Collect garbage in the BDD arena (mark and compact at gate-compilation safe points, gate BDDs released after their last reference) without changing any output: every function, probability, cut set and importance identical with collection forced at every safe point and with collection disabled; survivors keep their relative order so children precede parents. *(Added after v0.2.0.)* |
| FR-24 | For every risk metric and end state of an event tree, compute the exact conditional frequencies F(x=1) and F(x=0) of every basic event x the group's sequences depend on — success branches included, no cut-set or rare-event approximation — and from them Birnbaum F(x=1) − F(x=0), Fussell–Vesely (F − F(x=0))/F, RAW F(x=1)/F and RRW F/F(x=0), reporting a ratio with a zero denominator as undefined, never as a number; the group total equals the reported metric value bit for bit. Combine these exactly across event trees into model-wide importance for a metric or end-state set (consequence report), and report Fussell–Vesely re-ranking between base and head. *(Added after v0.1.0.)* |
| NFR-1 | Any historical result is reproducible bit-for-bit from a git tag. |
| NFR-2 | Unsupported constructs fail loudly with a specific error; the software never silently approximates or omits. |

---

## 4. Verification

### 4.1 Static verification (every PR, blocking)

`ci/validate.py` verifies FR-1/2/3 on the committed model: strict YAML
parse with duplicate-key rejection, JSON Schema validation with
`additionalProperties: false` throughout, reference linting (dangling
IDs, gate cycles with the cycle printed, cross-file ID duplication,
sequence-table completeness, the partition lint, CCF factor
normalization), the file-index lint, and the uncertainty rules of FR-22.

The **partition lint** treats each sequence path as a cube over the
functional-event outcomes (bypassed = either) and requires the cubes to
be pairwise disjoint and to cover {success, failure}ⁿ: an exact cover
makes Σ P(sequence) = 1 for any fault-tree logic, so a gap or overlap —
frequency silently missing from, or double-counted in, every metric — is
caught before quantification. Overlaps are found pairwise; uncovered
outcomes by a depth-first split over the functional events, pruned where
one cube leaves all remaining events free, which also counts them. The
**file-index lint** closes a gap found while writing it (anomaly D-9):
the loaders read fixed files and the top-level `*.yaml` files of
`basic-events/`, `fault-trees/`, `event-trees/`, so any other file there
was silently ignored; such files, sub-directories, hidden model files and
stray root YAML are now errors, and `includes` in `model.yaml` must name
exactly the files loaded.

**Generated files match their templates (FR-41, every PR, blocking).**
The validate job runs `canopy expand --check`: every model file generated
from `templates/` must be byte-identical to what the templates produce
today, must exist, and must still have a template (an orphaned generated
file fails); `ci/test_expand.py` runs beside it.

**Negative testing (every PR, blocking):** `ci/test_validate.py` applies
55 targeted mutations to a copy of the demo model — one per error and
warning class: duplicate key and parse failure, unknown field, each kind
of dangling reference, gate cycle, cross-file duplicate event and gate,
undefined top gates, malformed and duplicate sequence paths, overlap,
uncovered outcome, uncovered sub-tree, CCF factor count/sum/range, CCF
factor uncertainty (not a Dirichlet, non-positive concentration, unknown
field, and a valid block that must pass clean),
undefined and single members, each FR-22 rule, each file-index rule,
missing required file and directory, the FR-25 unit rules (including a
shared parameter re-expressed in years, which must flag all four events
that use it and nothing else), unknown `model.yaml` keys (configurations
misplaced under `model:`, an unknown top-level and risk-metric key: D-22),
orphan and unmapped-end-state warnings — and requires the exit code, the specific message and, for
errors, the exact error count (so a mutation cannot pass by tripping an
unrelated check). The FR-22 case is the four-condition copy of the demo
model described here before (unknown parameter field, lognormal error
factor 0.9, event-level distribution on a `rate-mission` event, beta
mean 10× the point value): exactly those four errors. The engine refuses
the same model when `--samples` is given and quantifies it normally
without. The suite also checks the partition lint against brute-force
enumeration of all 2ⁿ outcomes on 300 random sequence tables (1–7
functional events; exact partitions built by random splitting with
bypass, then each with one sequence removed and with one random cube
added): every exact partition accepted, and on every table with a gap or
an overlap the lint's uncovered-outcome count equals the brute-force
count and an overlap is reported exactly when some outcome is covered
twice. **Negative control:** with the partition and file-index lints
disabled, the 12 cases that target them fail.

### 4.2 Unit tests (every PR, blocking)

46 distinct tests in the engine crate (the binary target runs all 46; the
library target re-runs the 37 in `bdd`, `reorder`, `uncertainty` and
`zbdd`). Expected
values are hand-computed, closed-form, or — for the special functions —
computed with SciPy 1.17.1, an implementation independent of the engine's
(corrected count history: an earlier revision double-counted the six
`bdd::tests` entries under a phantom "loader/serde tests (6)" row):

| Test | Verifies |
|---|---|
| `probability_exact` | FR-4 on a 2-train system against closed form |
| `mcs_two_train` | FR-5: exactly the four double cut sets |
| `mcs_subsumption` | FR-5: A OR (A AND B) yields only {A} |
| `vote_gate` | FR-7: 2-of-3 probability 0.028 and its 3 cut sets |
| `negation_probability` | FR-8: P(A AND NOT B) exact |
| `hash_consing_shares_structure` | BDD canonicity (identical index for identical function) |
| `ccf_tests::alpha_factor_two_pump_and` | FR-10: 2-pump staggered alpha case vs hand-derived closed form Q₂ + Q₁² − Q₂Q₁² |
| `ccf_tests::group_size_eight_beta_factor` | FR-10: group-size cap upper bound (n=8) — Q₁/Q₈ closed form, 247 combination events, intermediates exactly zero |
| `ccf_tests::beta_factor_ignores_testing_scheme` | FR-10, D-15 regression: a beta-factor group gives Q₁ = (1−β)Q_t and Q₂ = βQ_t under both testing schemes |
| `ccf_tests::group_size_nine_rejected` | FR-10: n=9 rejected explicitly (cap is 2..=8) |
| `failure_model_tests::periodic_test_unavailability` | FR-17: rate-periodic-test closed form 1 − (1 − e^−rT)/(rT) vs hand-computed value at rT=0.1 |
| `failure_model_tests::periodic_test_zero_rate_is_exact_zero` | FR-17: r=0 (or T=0) is the exact limit Q_avg=0, not the undivided 0/0 |
| `plan_cofactors_match_restrict` | FR-24: the plan cofactor P(f\|x=v) equals the probability of the restricted BDD on 200 random BDDs (AND/OR/XOR/NOT), satisfies P = p·P1 + (1−p)·P0, and is the plan value itself, bit for bit, for variables outside the support |
| `importance_tests::consequence_importance_hand_computed` | FR-24: two CD sequences over a shared event with a success branch (¬A∧B, A); F = 2.8e-4, F(A=1/0) = 1e-3/2e-4, F(B=1/0) = 1e-3/1e-4, FV/RAW/RRW/Birnbaum and ranking against closed form; the sequence independent of B enters F(B=·) unchanged; exact FV of A 0.2857 vs the minimal-cut-set 0.357 |
| `importance_tests::importance_undefined_ratios` | FR-24: F = 0 gives undefined FV/RAW/RRW; F(x=0) = 0 gives infinite (undefined) RRW, never a number |
| `unit_tests::rule_table_exhaustive` | FR-25: every unit and unit pair of every quantity group against a hand-written list of the valid ones (15 of 132); mixed-base message names both fields; missing units and unknown groups are problems |
| `zbdd::set_algebra_matches_reference` | FR-30: ZBDD union and exact difference equal Rust set operations on 300 random product families; canonical form (equal sets, equal nodes) |
| `zbdd::enumeration_limits` | FR-30: count and order limits of product enumeration; the empty product and the empty set |
| `zbdd::product_minimize_truncate_match_reference` | FR-34: ZBDD product, minimization, non-superset filter and truncation equal reference set computations on 300 random product families; truncation keeps exactly the products with fold probability ≥ cut-off and order ≤ K, and returns exactly the others as its dropped set |
| `zbdd::truncated_product_keeps_the_same_set_and_bounds_the_loss` | FR-34: on 400 random pairs of families over 8 variables, the truncated product keeps exactly what truncating the full product keeps; every other product of the full product contains a returned covering term; P(∪ full product) ≤ P(∪ kept) + Σ P(terms) by enumeration of all 256 states; no truncation returns the full product and no terms |
| `zbdd::truncation_is_exact_at_the_cutoff` | FR-34: a product exactly at the cut-off is kept and one ulp above drops it (with the dropped set named); order limits 2 and 0; the empty product survives any cut-off below 1 |
| `reorder::swaps_preserve_functions_and_invariants` | FR-35: 200 random multi-root BDDs of 2–8 variables, 40 random adjacent swaps each; after every swap every root's truth table is unchanged and every structural invariant holds (reference counts equal parent references plus root holds, unique tables hold exactly the live nodes, no redundant or out-of-order node, exact live count, level maps consistent) |
| `reorder::swap_twice_is_identity_in_size` | FR-35: swapping the same pair twice restores the size and the order |
| `reorder::sifting_is_canonical_and_never_grows` | FR-35: on 150 random multi-root BDDs, sifting never grows the BDD; the rebuilt functions keep their truth tables and probabilities (1e-12) under the returned permutation; and the result is canonical — rebuilding each function from its truth table in a fresh manager with the new order gives exactly as many nodes |
| `reorder::sifting_repairs_the_classic_bad_order` | FR-35: (x₁∧x₂) ∨ … ∨ (x₂ₘ₋₁∧x₂ₘ) under the order with all first members first needs 2^(m+1) − 2 nodes; sifting reaches the paired size 2m, m = 2..6 |
| `reorder::reordering_is_deterministic` | FR-35, NFR-1: the same input sifted in two managers (differently seeded hash tables) gives the same permutation and the same arena node for node, 60 cases |
| `reorder::export_without_sifting_is_the_same_graph` | FR-35: copy out and back with no sifting gives the identity permutation, the same size and bit-identical probabilities |
| `prime_implicants_hand_computed` | FR-30: XOR (x¬y, ¬xy), the consensus example x·y + ¬x·z (primes xy, ¬xz and the consensus yz), tautology (the empty product), contradiction (none) |
| `prime_implicants_brute_force` | FR-30: on 400 random functions of up to 6 variables, the primes equal the exhaustive enumeration of all 3ⁿ products (implicant, no removable literal); on the coherent half they equal the minimal cut sets; the truncated construction gives exactly the order ≤ k primes for k = 0..3. A mutant without the set difference fails it |
| `gc_is_invisible` | FR-27: 200 random operation sequences on a collecting BDD and a never-collecting twin (random root subsets, repeated collections): identical probabilities bit for bit, reachable sizes, paths and plans; after each collection children precede parents and the arena holds exactly the live nodes; hash consing still finds kept nodes |
| `remap_of_a_dropped_handle_panics` | FR-27: a handle that was not a root cannot be silently reused after a collection |
| `restrict_is_linear_on_shared_dags` | FR-6, D-14 regression: restrict on a 64-variable XOR chain (2⁶⁴ paths) completes, with the expected function |
| `birnbaum_from_cofactors_matches_restrict` | FR-6: Birnbaum from plan cofactors (the engine's path) equals the restricted-BDD reference on 200 random BDDs; exactly 0 outside the support |
| `prob_plan_matches_recursive_pass_exactly` | FR-20: the flattened probability plan used per Monte Carlo iteration equals the recursive pass bit for bit on 200 random BDDs (AND/OR/XOR/NOT) |
| `uncertainty::normal_quantile_reference_values` | FR-20: AS 241 Φ⁻¹ at 9 points incl. 1e-300, vs SciPy `ndtri`, ≤ 1e-14 relative |
| `uncertainty::ln_gamma_reference_values` | FR-20: Lanczos ln Γ at 7 points vs SciPy `gammaln` |
| `uncertainty::gamma_and_beta_quantile_reference_values` | FR-20: 11 gamma/beta quantiles vs SciPy `gammaincinv`/`betaincinv`, ≤ 1e-11 relative |
| `uncertainty::quantile_round_trips` | FR-20: F(F⁻¹(u)) = u on the smaller tail, 8 u values × 11 shapes, incl. the D-6 family |
| `uncertainty::inversions_converge_over_parameter_families` | FR-20: 20,000 beta and 20,000 gamma inversions over shapes 0.1–10 and means 1e-5–0.8 all converge and round-trip (regression for D-6) |
| `uncertainty::lognormal_mean_and_error_factor` | FR-20: lognormal mean = point value, q95/q50 = EF |
| `uncertainty::inconsistent_point_value_is_refused` | FR-22: mean mismatch, zero lognormal point, inverted uniform refused; the demo beta(1.5, 248.5) at 6.0e-3 accepted |
| `uncertainty::internally_tagged_yaml_parses_and_rejects_unknown_fields` | FR-22: unknown distribution fields and names are parse errors |
| `uncertainty::keyed_uniforms_are_pure_and_distinct` | FR-21: deviates independent of call order and of other keys; distinct per key and seed; strictly inside (0, 1) |
| `uncertainty::keyed_uniforms_look_uniform` | FR-21: χ² over 20 bins (200,000 draws) along iterations and across keys below the 1e-6 critical value; lag-1 correlation < 0.015 |
| `uncertainty::lhs_stratifies_every_quantity` | FR-28: for N from 1 to 4097 and several keys, the stratum map is a permutation of 0..N−1, every deviate lies in its stratum and strictly inside (0, 1) (including the top stratum at N = 2), the permutation is a pure function of (seed, key, N) and differs across keys and seeds |
| `uncertainty::percentile_type7` | FR-20: percentile definition (linear interpolation between order statistics) |

Additionally, `python ci/test_consequence_report.py` verifies FR-18's
pooling and importance arithmetic against a hand-computed two-event-tree
fixture (cut set summed across two sequences, a non-coherent sequence
flagged as untracked, exact expected coverage ratio), a prime-implicant
fixture (a product with a negated event pooled as its own entry, a prime
and a cut set with the same events pooled together, the negated event
absent from the importance table, a sequence with primes no longer
untracked, the JSON splitting events and negated events), and, end to
end, a generated non-coherent model quantified with
`quantify.py --prime-implicants` leaving no CD sequence untracked. This is a Python
tooling test, not part of the engine-crate count above.

`python ci/test_importance.py` verifies FR-24's cross-tree aggregation
(`ci/importance.py`) against a hand-computed two-event-tree fixture: the
engine's unit-test tree plus a tree over a third event, model-wide F,
F(x=1), F(x=0) and all four measures for each event (trees that do not
depend on an event contribute their F unchanged), the FV ranking, the
same answer by metric and by end-state set, infinite RRW and F = 0 as
undefined, and refusal (None) when any tree was quantified without
importance, so a partial model-wide figure is never printed.

`python ci/test_appendix.py` verifies FR-32 on the demo model (point and
sampled results) and four harness models (CCF groups, house events,
non-coherent logic, a transfer to a transfer-only tree) by parsing the
generated tables: every parameter, basic event, CCF group, house event,
sequence and gate exactly once; every basic-event probability equal to
the engine's value, every sequence frequency and metric to the results,
every CCF Q_k to the engine's combination-event probability; provenance
verbatim; identical output under three hash seeds (66 checks). An
appendix showing probabilities off by 1e-4 fails it.

`python ci/test_importance_uncertainty.py` verifies FR-37 (25 checks).
On one tree (the demo), the combination of `ci/importance.py` reproduces
the engine's own importance-uncertainty summaries bit for bit. On six
two-tree models — a harness-generated uncertain case plus a second tree
over its first functional event only, with its own lognormal initiator —
quantified by `quantify.py --samples 20000 --importance-uncertainty 4`:
the combined events are exactly each metric's model-wide top 4 by point
FV; every combined draw equals the per-iteration sum over the trees,
recomputed in the test (the second tree's metric draw standing in for 18
event rows it does not depend on); and the Monte Carlo means of all 46
model-wide F(x=1) and F(x=0) lie within 6 standard errors of their exact
expectations E[f_IE,1]·E[P_1(CD | x = v)] + E[f_IE,2]·E[P_2(CD | x = v)]
from the harness's exact-expectation oracle (a constant draw, such as
F(x=0) = 0, compared to rounding). The PR comment (`ci/compare.py`) shows
no importance-uncertainty section for unchanged results, and, when the
second initiator is raised tenfold, exactly the moved events with the
combiner's means and percentiles. Incomplete inputs are refused, results
without draws give nothing, and five misuses of the flags fail loudly.
**Negative controls:** a tree not depending on the event contributing 0
instead of its metric draw fails 8 checks; the last tree dropped from the
sums, 18; the completeness check removed, 1; the engine's two draw arrays
exchanged, 33; the selection taken from one tree's ranking instead of the
model-wide one, 6; the PR comment listing every event, or none — 1 each.

`python ci/test_ccf_uncertainty.py` verifies FR-36 against exact moments
(31 checks). For a tree over one group (all members failed, or
k of n), P(top) is expanded symbolically into a polynomial in the
multiplicity coefficients, so every raw moment E[P^j] (j ≤ 4) is a sum of
Dirichlet moments E[Π c_k^m_k] — closed form for staggered coefficients
(rising factorials), and for non-staggered ones a one-dimensional
integral (with alpha = G / Σ G the normalisation cancels:
E[Π G_k^m_k / (Σ j G_j)^M] = Π (a_k)_(m_k)/Γ(M) · ∫ t^(M−1) Π (1 + j t)^−(a_j+m_j) dt),
integrated by the trapezoid rule on a log grid and self-checked against
the closed form with every weight 1 (agreement ~1e-15). Four groups —
alpha 3 staggered, alpha 3 non-staggered (2 of 3), alpha 4 staggered with
concentration 4, beta 2 — each with 20,000 draws: Monte Carlo mean and
sample variance within 6 standard errors of the exact mean and variance
(the variance's standard error from the exact fourth central moment;
observed 0.1–2.0), the sampled quantities exactly the non-zero factors'
keys, point results byte-identical with and without the block, and four
malformed blocks refused by the engine.

`python ci/test_reorder.py` verifies FR-35 on the demo model: every
fault tree and the event tree quantified with reordering forced at every
safe point, from both static orders, gives the default P(top), sequence
frequencies, metrics, Birnbaum and importance conditional frequencies
within 1e-12 and identical cut-set sets; reordering happens (reported by
`--gc-stats`) in 4 of 6 fault-tree runs — FT-RPS is too small
to have anything live at a safe point — and on the event tree's rows;
three forced runs are byte-identical; malformed thresholds are refused.
For FR-38 it also requires `--compile shared` and `per-row` to give
byte-identical demo output, and results equal within 1e-12 with the same
cut sets when collection and reordering are forced (the shared compiler
then keeps its sifted order from row to row); a bad mode is refused
(17 checks).

`python ci/test_expand.py` verifies FR-41 (23 checks): a fixture type
(two failure modes, one with an event-level lognormal) and an instance
file (two components, one with a data override and its own provenance)
expand to a hand-written golden text byte for byte; 2,011 floats over 600
orders of magnitude plus edge values (0, 1e-2, 1e4, 5e-324, the largest
double) are written in analyst notation (`1.2e-3`, `3.0e-5`, `1.5e+4`)
and read back as the identical float; the expanded model validates and
the engine quantifies it to (2.0e-3)²; `--check` passes when up to date
and fails, naming the file and showing the literal diff, on a hand edit,
a missing file, a template change not yet expanded, an orphaned generated
file; `expand` writes a template change into every instance and refuses
to overwrite a file without the header; nine malformed inputs are
refused (two templates for one output, an unknown type, a data override
without provenance, an output outside `basic-events/`, an unknown key, a
duplicate type, a bad component id, a hand-written event also generated,
the stripped header); expansion is idempotent and byte-identical under
three hash seeds; the demo's templates are up to date and `canopy expand`
equals the script. **Negative controls** (expander mutated): overrides
ignored — 2 failed checks; the scientific-notation dot dropped (`3e-5`,
which YAML reads as a string) — 17, the expander's own read-back check
refusing to write every such file; the exponent sign dropped — 2;
`--check` comparing existence only — 3; hand-written files overwritten —
1; orphans ignored — 1. The demo's ECCS and RHR pump files were converted
to generated files (the ECCS test-and-maintenance event moved, verbatim,
to a hand-written `ecc-pumps-tm.yaml`): every engine output (the three
fault trees, the event tree, 2,000 Monte Carlo draws), the `quantify.py`
results and the report appendix are byte-identical before and after, and
the RHR file's literal diff is its two-line GENERATED header.

`python ci/test_truncated_pipeline.py` verifies FR-42 (115 checks).
`ci/bounds.py`: over 4,000 values across 600 orders of magnitude, a
printed lower bound is never above the value and an upper bound never
below it (beyond the 1e-12 tolerance), one unit apart at most; a point
value prints exactly as before (`f"{v:.4e}"`); floating-point noise
(`2.5e-6 − 1e-6` = 1.5000000000000002e-06) is not rounded outward, a real
excess of 1e-9 is; totals over mixed exact/truncated trees.
`quantify.py --truncated` on the demo at cut-offs 1e-15, 1e-9, 1e-7 and
1e-5: every sequence frequency and the CDF of the exact run lie within
the bounds, the partition bounds bracket 1, cut-off 1e-15 reproduces the
exact values to 1e-12, every named configuration is quantified truncated
and contains its exact value, `--order-limit` reaches the engine; three
refusals. The partition check on bounds, fed by a fake engine: bounds
summing to [0.9, 1.1] pass, [0.4, 0.99] and [1.01, 1.2] fail, a tree
with house overrides is only noted, a transfer's expansions overlapping
the row pass and missing it fail. `compare.py`: the change cell against
hand-computed strings (base [1.0e-6, 1.2e-6], head [2.0e-6, 2.5e-6]:
`🔺 +8.000e-07 to +1.500e-06 (×1.66–2.50)`; a decrease; a straddle, with
no arrow; identical bounds, and bounds 1e-12 apart, "—" but 1e-6 apart
not; new; an exact base; a base that may be zero, with no ratio), and
2,000 random interval pairs with true values
inside them: the printed change and ratio intervals always contain the
true change and ratio, and an arrow appears only when the whole interval
is on one side; the truncated report's header, metric row, notes and
cut-set caveat; a mixed exact/truncated report, which at cut-off 1e-7
lists no cut-set change (the same sets on both paths, D-20). The
consequence report at 1e-7 and 1e-5: total bounds equal to the sums of
the qualifying rows' bounds, no point total, every share exactly
f / upper .. f / lower, the coverage bounds, the rows with nothing
retained (SEQ-SLOCA-02 at 1e-5) listed. The appendix and the viewer's
data: intervals where values were; the viewer's metric summed over two
event trees (D-21); its diff of bounds (identical results: no change; a
different cut-off: exactly the rows whose bounds moved; an exact base
against a truncated head). `canopy quantify --truncated` equals the
script byte for byte, `canopy delta --truncated` reports bounds, and
`--order-limit` without `--truncated` is refused. The RiskSpectrum
cross-check, which compares values within a tolerance, refuses truncated
results (`test_import_riskspectrum.py`).
**Negative controls** (tooling mutated): the change interval computed
as [L_h − L_b, U_h − U_b] — 5 failed checks; bounds printed to nearest —
7; the partition check skipped on bounds — 2; the viewer's metric taken
from the first event tree (D-21 reverted) — 1; shares over the lower
bound only — 4; the appendix printing lower bounds as values — 4; a
mixed report treated as exact — 1; bound changes compared for exact
equality instead of within 1e-9 — 1. The page's rendering of bounds was
checked by hand in a browser (header, sequence cards, details, diff
block). Exact results are unaffected: on the demo, the delta report
(with configurations), the consequence report (text and JSON) and the
appendix are byte-identical to the previous tools' output, and the
viewer's embedded data differs only in its metrics entry (now model-wide,
without the per-tree importance rows the page never read).

`python ci/test_house_cache.py` verifies FR-43 (11 checks) on a
hand-computed tree: GT-P = A ∨ GT-Q, GT-Q = B ∧ HE-X, GT-R = B ∨ C (0.1,
0.2, 0.3; initiator 1e-2 /yr; HE-X default false), FE-1 on GT-P, FE-2 on
GT-R; rows (both succeed, X = true) 0.504, (FE-2 fails, no override: X
back to false) ¬A(B ∨ C) = 0.396 — a GT-P kept from the first row would
give 0.216 — and (FE-1 fails, X = false restated) A = 0.1; CDF 4.96e-3;
cut sets {B}, {C} and {A}; per row 3, 5, 5 gates compiled with 1 cached
gate dropped (GT-P, which names no house event itself) and 1 kept (GT-R),
against 3 + 3 + 2 = 8 with a fresh compiler per row; identical results
per row, with collection forced, and with reordering forced.

`python ci/test_multi_tree.py` verifies FR-44 (23 checks) on two
hand-computed event trees over shared gates (GT-P = A ∨ GT-Q, GT-Q =
B ∧ HE-X, GT-R = B ∨ C; 0.1/0.2/0.3): ET-1 (1e-2 /yr) rows 0.504, 0.396,
0.28 (X true), CDF 6.76e-3; ET-2 (1e-3 /yr) rows 0.1, 0.216 (X true),
0.504, CDF 3.16e-4 — ET-1 ends with X true and ET-2 begins without an
override, so a house value carried from one tree to the next would give
0.28 for ET-2's first row; in one process 13 gates compiled, 9 dropped
and 5 kept on house changes, against 7 + 7 in two; each tree's results
in one process equal to its own process — byte for byte for point
results, with collection forced, per row and truncated, to 1e-12 for
Monte Carlo and importance draws; five refusals; `quantify.py
--one-process` equal to the default with configurations,
`--importance-uncertainty` and `--truncated`.

`python ci/test_viz_diff.py` verifies FR-40 and FR-45 (19 checks) on a
synthetic base model and a head derived from it by twenty edits covering
every entity kind and every status (a probability, a label, a provenance
block, a house default, a fault-tree label, a gate formula, a
functional-event label, a sequence end state, a parameter's value and its
uncertainty, a CCF group's factors; an event, a second event, a house
event, a gate, a parameter and a CCF group added; an event, a house event,
a gate and a parameter removed): the diff embedded in the page is exactly
those entries (kind, id, status, changed fields) plus the two basic events
whose engine probability the parameter and factor changes moved (members
of the changed group, whose total is the changed parameter) and the
sequences whose end state or frequency changed, nothing else; each
parameter's users (basic events and CCF groups) and each event's
parameters and CCF groups are the fixture's; changed
and removed entries carry the base model's values; sequence frequencies
and the metric are the two results files' values; a relative frequency
change of 1e-12 is not flagged and 1e-6 is; with results on one side
only, probabilities, frequencies and metrics are not compared and two
notes say so, while structural changes are still reported; identical
models give an empty diff; without `--base` the page carries no diff and
the same model data; three hash seeds give byte-identical pages; and
`--base-results` without `--base` is refused. `ci/test_cli.py` checks
`canopy viz --base` byte-identical to the builder and `canopy delta
--viewer` end to end (an uncommitted probability change appears as
exactly that event's `p` and a changed CDF). **Negative controls**
(builder mutated): removals not detected — 3 failed checks; the 1e-9
threshold dropped — 1; one-sided results compared anyway — 1; sequence
frequencies not compared — 3; base values not carried — 2; for FR-45, a
parameter's uncertainty not compared — 3; CCF groups not diffed — 4; CCF
totals left out of a parameter's users — 1; parameter references found at
the top level only — 1. The page's
JavaScript is not run in CI (no browser there); it was checked by hand in
a browser on the demo with five edits and in reverse: 11 changes listed,
rings and badges on the changed gate, the edited and the added event and
all four sequences (frequency deltas +49.99% to +192.23%), the header's
CDF 2.208e-8 → 6.220e-8 (+181.68%), a gate's base formula struck through
above the head's, a removed event opening its base definition, and no
console error.

`python ci/test_truncation.py` verifies FR-34 and FR-39 on hand-computed
fixtures (40 checks): D ∨ AB ∨ AC with P = 0.1/0.2/0.01/0.05 — at cut-off 5e-3
retained {D}, {A,B}, lower 0.069 (the union, not the rare-event sum
0.07), bound 0.001, upper 0.070 around the exact 0.06976; a cut set
exactly at the cut-off (0.1 · 0.2 against 0.02) kept; at 0.0201 the bound
P(AB) + P(C) = 0.03, because C alone is below the cut-off and stands for
{A,C}; order limit 1 bound 0.021; cut-off 0 exact with a zero bound;
cut-off 0.9 nothing retained, lower 0; a 2-of-3 vote (0.1/0.2/0.3) at
0.025 bounded by [0.084, 0.104] around 0.098; house events default and
overridden; a tautology giving the empty cut set with P = 1. Refused:
non-coherent logic, event trees, `--samples`, `--prime-implicants`, and
cut-offs `abc`, `1`, `1.5`, `-0.1` or missing. The JSON has no
`probability` field. Event trees (FR-39): two functional events sharing
an event (A∨B, B∨C at 0.1/0.02/0.3, initiator 1e-2 /yr), at cut-off 0.05 —
sequence (FE1 ok, FE2 fails) in 1e-2 × [0.25, 0.32] around 0.2646 (L_F =
P(C) = 0.3, U_F = 0.32, U_G = 0.05 from the lost terms {B} and {A,C});
(FE1 fails) in [0.10, 0.12] around 0.118; (all succeed) in [0.61, 0.63]
around 0.6174; CDF in [3.5e-3, 4.4e-3]; the retained failure-logic cut
sets {C}, {A} and — retained but not listed, the all-success row ending
in `OK` (D-20) — the empty set; the probability bounds equal to these and
the partition bounds [0.96, 1.07] (FR-42); every row exact at cut-off 0; a
per-sequence house override honoured (0.118 with it, 0.1 without) at
cut-offs 0 and 0.05; a non-coherent functional event refused by name.

`python ci/test_configurations.py` verifies FR-31: every configuration
of the demo model plus an added parameter configuration, quantified by
`quantify.py --configurations`, gives output identical to quantifying a
copy of the model with the house-event defaults and parameter values
edited accordingly; BASE equals the base case; `compare.py` shows
unchanged configurations as neutral (TRAIN-A-OOS: ×43.5 the base case)
and reports a configuration-only change instead of calling the delta
neutral; unknown, negative and `--samples`-combined `--param` overrides
are refused. `test_validate.py` has two configuration lint cases.

`python ci/test_sampling.py` verifies FR-28 end to end on a tree whose
top event carries a uniform distribution, so each draw can be placed in
its stratum: with LHS, N = 1000 draws fall one per stratum and the mean
is within the deterministic bound w/(2N) of the true mean (simple random
sampling at the same N is not stratified); for both layouts a rerun is
byte-identical, an unrelated added quantity leaves the draws unchanged
and another seed changes them; `compare.py` pairs lhs with lhs and
refuses to pair srs with lhs; `--sampling` without `--samples` is
refused.

`python ci/test_units.py` verifies FR-25 end to end: for all 168
combinations of the six units over every quantity group (probability,
the three rate models, `rate-mission` with the rate taken from a
parameter, CCF totals inline and from a parameter, initiating events), a
minimal model is written and the validator must report exactly one error
naming the field, and the engine must refuse to load, exactly when a
hand-written table (independent of both implementations) says the
combination is invalid; on the 15 valid ones the engine's P(top) must
equal the failure model's closed form to 1e-12, and the viewer builder
must show the engine's `basic_event_probabilities` value exactly when
given results and its own closed form within 1e-15 without (all four
failure models; the viewer previously showed nothing for
`rate-periodic-test` and a CCF member's pre-expansion value; an old
viewer fails these checks on every valid case). Negative controls: with
the rule disabled in the engine, or in the validator, all 153 invalid
combinations are reported accepted.

`python ci/test_cli.py` verifies FR-26: every subcommand's output byte
for byte against the underlying tool (validate, quantify with and without
samples, quantify `--target` passing engine flags, report by metric and
end state, compare, viz), exit codes propagated (a broken model fails
`canopy validate`, an unknown engine target fails `canopy quantify`),
refusal of stray arguments, and `canopy delta` end to end in a throwaway
git repository holding the demo model: neutral against an unchanged HEAD,
an uncommitted change and a committed one (`--base HEAD~1`) reported as a
CDF increase with re-ranking, the same through a symlinked path (anomaly
D-12), an unknown ref refused, and the base worktree always removed.
`canopy verify` is exercised by running it: it is how the commit
introducing it was verified. `ci/test_consequence_report.py` now also runs
the report under six hash seeds on a fixture of tied events and requires
one output (anomaly D-11; the previous code gave six).

`python ci/test_transfers.py` verifies FR-11's transfer rules against a
hand-computed fixture run through the engine, the validator,
`quantify.py` and the other tools: two trees sharing an event, with the
transferring row's end state deliberately mapped to CDF. Exact
expansions (M2>T1 = A ∧ ¬(A ∨ B) = 0 exactly, where multiplying
separately quantified trees gives 0.072; 7e-4 and 3e-4 /yr), CDF 7e-4 (not
1.7e-3), partition over the tree's own rows, the expansions summing to the
transfer row, delete-term cut sets spanning both hops, BDD-exact
importance through the transfer (FV of C = −3/7), accumulated house
overrides and a later hop winning, one gate compiled under two house
configurations in the same chain (a stale gate cache would give 0 for a
7e-3 row), an unfollowed transfer counted nowhere (the D-10 regression),
a nested transfer, a transfer cycle refused by engine, validator and
`quantify.py`, standalone refusal of a transfer-only tree, an unreachable
transfer-only tree warned about, Monte Carlo bookkeeping through the
transfer (per-iteration partition, expansions = transfer row, metric
draws over the aggregated rows, E[CDF] against its closed form), and the
viewer builder, MEF exporter and consequence report accepting a
transfer-only tree. Run against the previous engine it fails, including
the D-10 section; against three mutant engines (transfer rows counted, no
house accumulation, gate cache kept across house changes) it fails 14, 6
and 4 checks respectively.

`python ci/test_import_riskspectrum.py` verifies FR-19 (13 test groups,
run in CI after the engine build): hand-computed checks of the MGL→alpha
relations (m = 3, ρ = 0.1/0.5 → α = 2.7/2.825, 0.075/2.825, 0.05/2.825;
m = 2 reducing to the beta-factor split), the periodic-test closed form
against the engine's, the id grammar (stability, collision suffixing),
the Tested-model variants (idealized / `q_mean` point value / idealized
with warning), and each refusal rule (exchange events, BC-forced basic
events, frequency events in fault trees, MGL without the flag, >8-member
groups, duplicate sequence paths) with its `--allow-unsupported`
downgrade. The round-trip leg converts `ci/fixtures/riskspectrum-demo/`
— the demo model written as a RiskSpectrum table export with the same
record ids — and requires: validator clean; byte-identical output on a
second run; every sequence frequency, every risk metric (CDF
2.208173e-8 /yr), every sequence and fault-tree cut set, and the
`HE-ECC-TRAIN-A-OOS=true` configuration result equal to `model/`'s to
1e-12 relative; and 2,000 Monte Carlo CDF draws **identical** to
`model/`'s, over the same four uncertain quantities (FR-21: distributions
are carried under the same IDs, so the keyed draws coincide). Two groups
check FR-22 distribution placement: a rate model's distribution lands on
its inline rate, a mean-inconsistent beta is dropped with a warning, a
CCF member's distribution is dropped in favour of the parameter's, and
members' common distribution moves to the group total when the total is
derived from them. A second leg runs `ci/crosscheck_rs.py` on the converted
model against RiskSpectrum-style result tables generated from `model/`
at 6 significant digits (`ci/fixtures/riskspectrum-demo-results/`):
PASS at 1e-5 relative, and FAIL — with the finding named — when a
sequence frequency is perturbed by 1 % or a CCF event mapping is
removed. The fixture is written to the table contract, not produced by
RiskSpectrum (§9).

### 4.3 Numerical methods documentation

The algorithms are documented in `docs/quantification.md` and
`docs/architecture.md`: hash-consed ROBDD with memoized apply; exact
probability by one memoized Shannon pass; minimal cut sets by Rauzy's
minimal-solutions transform with the ⊘ subsumption operator; CCF
per-multiplicity formulas with their NUREG/CR-5485 provenance; the
delete-term convention for sequence cut sets; the success-branch negation
that makes sequence frequencies exact. Uncertainty propagation
(FR-20–FR-23) is documented in `docs/quantification.md`, "Uncertainty
propagation": distribution parameterizations, keyed random numbers,
inverse-CDF sampling, special-function algorithms (AS 241; Lanczos ln Γ;
series/Lentz incomplete gamma and beta; safeguarded Newton on the
logarithm of the smaller tail), percentile definition, and the
refusal rules. Consequence-level importance (FR-24) is documented in
`docs/quantification.md`, "Consequence-level importance": the multilinear
identity that makes cofactor frequencies exact across sequences and event
trees, the cofactor pass over the flat plan, the measure definitions and
what they do and do not mean (negative FV and RAW < 1 where success
branches or transfers matter; per expanded event; point values).

---

## 5. Validation

Seven independent legs. "Independent" is meant literally: each leg uses
either a different implementation, a different algorithm, or a different
authorship lineage than the engine under test.

### 5.1 Brute-force truth-table oracle (demo model)

The full demonstration model (13 basic events pre-CCF) was evaluated by
exhaustive enumeration of all 2ⁿ basic-event states in an independent
Python implementation sharing no code with the engine. Every sequence
frequency matched to all displayed digits, before CCF
(CDF 9.427364e-9 /yr) and after CCF with an independent expansion
(CDF 2.208173e-8 /yr), and Σ P(sequence) = 1.000000000000. Validates
FR-4/9/10/11/12/13 on the demo model.

### 5.2 Randomized property harness (every PR, blocking)

`ci/property_test.py` generates random models — gate DAGs with
and/or/vote/NOT/XOR, house events, CCF groups (both testing conventions),
event trees over shared logic including bypass patterns — and checks the
engine against a brute-force oracle with its own independent CCF
expansion. Per case: validator acceptance; exact P(top); coherence-flag
correctness; **exact set equality** of minimal cut sets including the
empty-set convention; cut-probability agreement; Birnbaum spot checks;
every sequence frequency; sequence cut sets per the delete-term
convention; refusal of cut sets on non-coherent sequences; partition;
CDF aggregation. Tolerance: 1e-9 relative.

Evidence at v0.1.0: 180 cases across three seeds during bring-up, all
passing after the defects of §7 were resolved; 60 cases at a fixed seed
run on every PR since. Failing cases are preserved on disk for
reproduction. Validates FR-4 through FR-13 across the input space, not
just chosen examples.

**Uncertainty stage (FR-20/21/22).** Each case's logic is re-issued with
random distributions from a separate random stream (so the logic of case
*i* is unchanged from earlier harness versions): 1–3 shared parameters,
and per basic event a parameter reference, an inline distribution, an
event-level distribution or a constant; a parameter, inline or constant
CCF total; a lognormal or constant initiator frequency; all four
distribution families. The oracle computes the **exact expectation** of
P(top), of every sequence frequency and of CDF independently of the
engine: each basic-event probability is c·X for at most one random
quantity X (c = 1, or the CCF coefficient of its multiplicity), so each
truth-table state's weight is a polynomial in every X, whose expectation
follows from the closed-form raw moments E[Xʲ] of the four families.
Shared parameters therefore enter as E[X²], E[X³], … — the oracle checks
state-of-knowledge correlation exactly, not approximately. The engine's
sampled mean (N = 20,000) must lie within 6 standard errors of the exact
expectation. Also per case: validator acceptance of the variant, no
clamping (the generator keeps P(X > 1) negligible, which keeps the
expectation exact), per-iteration partition Σ draws(sequence) = draw(IE)
to 1e-9, CDF draws equal to the left-fold sum of CD-sequence draws, and a
byte-identical rerun.

Evidence: 180/180 cases across seeds 20260708, 424242 and 7. For the CI
seed: 363 exact-expectation checks, 283 of them on quantities with
non-zero variance, with 145 basic events sharing a parameter with at
least one other event. **Negative control:** an engine mutated to sample
each event's parameter independently (keys `PAR-X@BE-Y`, i.e. no
state-of-knowledge correlation) fails 10 of the 60 CI cases on exact
expectations — the stage detects the error that matters most. The stage
found anomalies D-6 and D-8 (§7).

**Transfer stage (FR-11, FR-12).** From a third random stream (so the
logic of case *i* is unchanged), each case gains a second event tree
ET-TEST2 over the case's gates (1–2 functional events, a random exact
partition with bypass), with or without its own initiating event, and one
random sequence of ET-TEST transfers to it, keeping its end state (CD in
most cases, so a counted transfer row would show). When the case has a
house event, per-sequence overrides flip it on the transferring row
and/or on a target row, and the target's tops favour gates that depend on
it and gates ET-TEST also uses. The oracle enumerates states once and
evaluates each hop with its accumulated house values. Checked: validator
acceptance; the row list and order; every row's frequency (1e-9
relative), end state and `transfer_path`; delete-term cut sets across
hops (exact set equality; none on non-coherent logic); the followed row's
expansion sum, probability and override flag; the partition over own
rows; CDF over the aggregated rows; every end-state group's importance
(the §5.2 comparator); ET-TEST2 standalone (refused without an
initiator, else every frequency); `quantify.py`'s tree selection; and
Monte Carlo bookkeeping (per-iteration partition, expansions = transfer
row, CDF draws = left fold of the aggregated CD rows). Evidence for the
CI seed: 60/60 cases, 155 expansion rows, 46 transferring rows with end
state CD, 10 with overrides on the transferring row and 5 on a target
row, 32 targets with their own initiator. **Negative controls:** an
engine counting transfer rows fails 46 of 60 cases; engines without house
accumulation or keeping the gate cache across house changes fail only 3
and 2 of 60 (the conditions need a house event that matters in both
trees), which is why those two rules also have deterministic
hand-computed tests (above).

**Prime implicants (FR-30).** For every case the fault tree is
quantified with `--prime-implicants` and the product set must equal the
oracle's primes, computed independently by Quine–McCluskey from the true
minterms (supports of up to 12 events); with `--order-limit 2` it must
equal their order ≤ 2 subset (so the truncated construction is checked),
each listed probability must equal Π p · Π (1 − p), and on coherent trees
the primes must equal the minimal cut sets. Evidence for the CI seed:
60/60; 56 cases checked against the oracle (28 non-coherent), 157 primes,
34 of them with negated events; 4 cases above the support limit.
The event tree is quantified with `--prime-implicants` too, and every
non-OK sequence with non-coherent failure logic must list exactly the
oracle's primes of that logic, with frequencies f_IE · Π p · Π (1 − p):
97 such sequences, 283 primes (90 with negated events) at the CI seed.
**Negative control:** an engine without the set difference in the
recursion fails all 60 cases.

**Importance under uncertainty (FR-29).** Each uncertainty variant's
event tree is also quantified with `--importance-uncertainty 100` (every
event of the CDF importance list), and for every event the sampled means
of F(x=1) and F(x=0) must lie within 6 standard errors of their exact
expectations E[f_IE]·E[P(CD | x = v)], computed by the oracle with x held
at v and its own probability factor left out (so shared parameters enter
as the exact polynomial moments, as for the other expectations). Evidence
for the CI seed: 60/60 cases, 674 exact-expectation checks (337 event
rows × F(x=1), F(x=0)). **Negative control:** an engine
that swaps the two cofactors fails 54 of 60 cases (532 mismatches).

**LHS in the uncertainty stage (FR-28).** Each uncertainty variant is
also quantified with `--sampling lhs` at the same N and seed, and the
same exact expectations must hold (P(top), every sequence, CDF; the
reported simple-random standard error bounds the LHS error up to
N/(N−1)), with the per-iteration partition. Evidence: 60/60 at the CI
seed. **Negative control:** an engine whose stratum permutation is the
identity (all quantities move through their strata in lockstep — a hidden
comonotone correlation) fails 43 of 60 cases on exact expectations.

**Garbage-collection stage (FR-27).** For every case, the fault tree
and the event tree (cut sets included) and the transfer variant's event
tree (house overrides, which clear the gate cache) are quantified twice —
collection disabled and collection forced at every safe point
(`--gc-threshold 1`) — and the JSON must be byte-identical apart from the
arena size. Evidence: 180 identity checks per 60-case run, all passing.
**Negative controls:** four engines with realistic collection bugs — an
accumulator not pinned across an operand's compilation, memo caches
surviving a collection, survivors compacted out of order, the event-tree
conjunction held outside the pinned stack — fail 34, 43, 36 and 49 of 60
cases.

**Order stage (FR-33).** Every case's fault tree and event tree, and
the transfer variant's event tree, are quantified with the default and
with the reverse-DFS variable order (`--order rdfs`), cut sets and prime
implicants included: probabilities, sequence frequencies and metrics must
agree within 1e-12 relative, cut-set and prime-implicant sets must be
identical (each probability within 1e-12), Birnbaum and the importance
conditional frequencies within rounding. 42 of the 60 CI-seed fault
trees get a different BDD under the reverse order, so the stage compares
genuinely different diagrams of the same function. Evidence: 60/60.
Since D-17 the reorder stage below also requires three runs of `--order
rdfs` to be byte-identical (reproducibility, not just agreement).

**Uncertain CCF factors (FR-36).** The uncertainty variant of a case with
a CCF group gets `factor_uncertainty` with probability 0.6 (concentration
uniform in [2, 60]); the exact-expectation oracle then expands the
group's events jointly as a polynomial in (Q_t, c_1..c_n) — the events of
multiplicity k contribute (c_k Q_t)^f (1 − c_k Q_t)^s — merges its Q_t
part with Q_t's other uses, and takes E[Π c_k^m_k] from the same exact
Dirichlet moments as `test_ccf_uncertainty.py`; every Monte Carlo check
of the stage (P(top), sequences, CDF, importance under uncertainty, LHS)
then covers sampled factors. Evidence (CI seed): 18 cases with
uncertain factors (9 staggered, 9 non-staggered; group
sizes 2 (10), 3 (4), 4 (4)); 60/60. **Negative controls** (engines mutated one at a
time; the moment test, and the uncertainty stage alone on those cases):
gamma shape without the factor (concentration only) — moment test
fails, stage 14 of 18; factors normalised by the largest deviate instead
of the sum — fails, 7 of 18; one key for every factor (comonotone
deviates) — fails, 14 of 18; coefficients not recomputed from the draw
— fails (variance 0), 3 of 18 (a wrong mean shows only through products of coefficients); the staggered formula used for sampled
non-staggered groups — fails, 7 of 18. Unmutated: 0. Models without
the block are unaffected: 93 of 93 outputs (demo and generated CCF
models, point and Monte Carlo, simple random and LHS) byte-identical to
the previous engine.

**Event-tree truncation stage (FR-39).** Every case's event tree whose
rows use only coherent functional events is quantified with
`--truncated` at cut-off 0, near the middle and near the top of its
sequences' failure-logic cut-set probabilities, and at cut-off 0 with
order limit 1. Against the oracle (enumerated): each row's frequency
bounds contain f_IE · P(row); its retained cut sets are exactly the
oracle's minimal cut sets of the conjunction of its failed tops with
P ≥ cut-off (and order ≤ 1); the failure-logic lower bound is the
probability of their union and the bounds contain P(F); the
failure-and-success bounds contain P(F ∧ ∨ S); the metric bounds contain
the exact CDF; cut-off 0 without a limit is exact (to the rounding of
P(F) − P(G)); no point frequency is reported. Trees using a non-coherent
functional event must be refused. The transfer variant is checked
against the engine's exact row frequencies (themselves checked against
the oracle by the transfer stage): every row followed, exact at cut-off
0, contained at 1e-6. Both check the partition bounds (FR-42): each
row's probability bounds contain its exact probability and, times f_IE,
give its frequency bounds bit for bit; the reported sums are the left
folds of the tree's own rows' bounds and bracket 1; each followed row
repeats its bounds and its expansions' summed bounds contain its exact
probability; an `OK` row lists no cut set (D-20), while its failure-logic
retained count still matches the oracle. Evidence (CI seed): 27 coherent trees,
106 runs, 412 rows (273 with bounds of non-zero width),
33 refused, 44 transfer-variant runs, 150 partition-bound checks, 44
followed rows; 60/60. **Negative
controls** (full harness): G formed from S alone instead of F ∧ S — 22
cases fail; success branches ignored (G empty) — 27; S's lost terms left
out of G's — 26; the cover of F's lost terms left out of G's — 4; the
per-row house overrides ignored — 1 (the transfer variant: overrides
are rare in generated models), and the hand-computed override fixture of
`test_truncation.py` catches it directly. For FR-42 (engine mutated):
the partition sums taken over the expansion rows too — 21 cases fail;
a followed row's expansion upper bound summed from the lower bounds —
15; an `OK` row listing its cut sets again (D-20 reverted) — 27, and
`test_truncation.py`'s hand fixture.

**Multi-tree stage (FR-44).** When the transfer variant's target tree has
its own initiator (half the cases), both trees are quantified in one
process (`ET-TEST,ET-TEST2`), by default and with collection forced at
every safe point, and each tree's results compared with its own process
as in the order stage (`results_differ`); `quantify.py --one-process` is
compared with the default tree by tree. Evidence (CI seed): 64 runs,
128 tree results (92 byte-identical, the others equal to rounding: a
later tree may get a different variable numbering), 32 `quantify.py`
comparisons; 60/60. **Negative controls** (engine or `quantify.py`
mutated; `test_multi_tree.py`, `test_house_cache.py`, and the full harness
for the first): house values set only for rows with overrides (so a row
inherits the previous row's, across trees too) — 12 and 4 hand checks,
11 harness cases; in one process, truncated trees all quantified as the
first — 2 checks; later trees quantified without their Monte Carlo
options — 3 checks; `quantify.py --one-process` dropping the engine flags
— 3 checks. On six event trees each using Aralia edf9204 whole, one
process takes 18.7 s against 28 s for six, byte-identical output.

**House-override stage (FR-43).** Each case's event tree is given random
per-sequence house overrides (on each row with probability 0.6, a random
non-empty subset of the model's house events with random values, from a
random stream of its own, so every other stage sees the same cases as
before) and quantified with collection forced at every safe point.
Against the oracle, under each row's own house values: every row's
frequency, and for a coherent row that is not OK its cut sets. The
shared compiler, which keeps the gates a change does not reach, is then
compared with a fresh compiler per row as in the order stage. Evidence
(CI seed): 16 event trees (the 44 cases without house events have
nothing to override), 73 rows, 53 of them with overrides; on house
changes 37 cached gates dropped and 31 kept; 60/60. **Negative controls**
(engine mutated; the full harness, `test_house_cache.py` and
`test_transfers.py`): dependencies not followed through referenced gates
— 6 cases fail, 5 hand checks; nothing ever dropped — 11 cases, 5 hand
checks, and `test_transfers.py`; the changed set taken from the new
overrides only (an override removed goes unnoticed) — 9 cases, 5 hand
checks. Control: clearing the whole cache on any change (the previous
behaviour) passes all 60 cases and fails only the hand test's gate
counts. On a benchmark tree — Aralia edf9204 whole as one functional
event and two small functional events on house events, 8 rows flipping
their overrides — 4.7 s and 0.67 GB against 11.6 s and 1.35 GB for the
previous engine, byte-identical output.

**Shared-compiler stage (FR-38).** Every case's event tree, and the
transfer variant's, is quantified with one compiler for all rows (the
default) and with a fresh compiler per row (`--compile per-row`) and
compared exactly as in the order stage. Evidence (CI seed): 120
event trees, 2 of them with byte-different output (the harness's
small trees mostly discover variables in the same order row to row, so
the stage rarely compares different BDDs — its strength is exercising
the shared paths: cache reuse across rows, cache drops on per-sequence
house overrides, transfer hops, and — through the GC and reorder stages,
which now run shared — collection and reordering across rows); 60/60.
**Negative controls** (full harness, 60 CI-seed cases): row coherence
taken from the shared compiler's global flag — 2 cases fail; the gate
cache kept across a change of house overrides — 8; XOR not counted as
non-coherent in the per-row check — 10. On a 32-row tree over five large
subtrees of Aralia edfpa14q the two modes give byte-identical output,
209 s and 232 s.

**Reorder stage (FR-35).** Every case's fault tree and event tree, and
the transfer variant's event tree, are quantified with dynamic reordering
forced at every safe point (collection at every safe point, sifting
whenever anything is live), once from the default order and once from
reverse DFS, cut sets and prime implicants included, and compared with
the default exactly as in the order stage; and three runs of each
non-default variant (reverse DFS, forced reordering, both) must be
byte-identical. Evidence (CI seed): 1402 compilations with reordering
forced, 1242 of them sifted at least once, 591 where the last sifting
shrank the BDD (so the order genuinely changed); 60/60, and the same
counts on a second run. (603 before FR-43: in the transfer variants,
gates now kept across a house change alter what is live at a safe point;
the previous behaviour, restored as a control, gives 603 again.)
**Negative controls** (engines mutated one at a time, the stage alone on
the 60 CI-seed cases, and the unit tests): the two new children of a
swapped node exchanged — 53 cases, 4 unit tests; the basic events not
renumbered after sifting — 48 cases, no unit test (the renumbering is in
the compiler); the interaction matrix ignored (every swap a relabelling)
— 42 cases, 3 unit tests; pinned handles not remapped — 32 cases, no
unit test; the rebuild using the old variable index — 54 cases, 1 unit
test; a freed node left in its unique table — 55 cases, 4 unit tests.
Unmutated: 0.

**Truncation stage (FR-34).** Every case's fault tree is quantified with
`--truncated` at cut-off 0, at a cut-off strictly between each pair of
adjacent distinct minimal-cut-set probabilities (their geometric mean,
where they differ by more than 1e-6 relative, so no product sits within
rounding of the cut-off — the exact tie is a unit test), and above the
largest, each with no order limit and with limits 1 and 2. Against the
oracle's minimal cut sets (enumerated, not the engine's): the retained
set is exactly {m : P(m) ≥ cut-off, |m| ≤ K}, each with its probability;
the lower bound equals the oracle's probability of their union
(enumerated over the truth table); the rare-event sum is Σ P(retained);
upper = min(1, lower + error bound) with a non-negative bound; the
oracle's exact P(top) lies within [lower, upper]; and, sharper, the error
bound is at least the probability of the union of *all* lost minimal cut
sets (each contains a counted term: it cannot contain a retained one);
cut-off 0 without a limit is exact with a zero bound. Non-coherent
trees must be refused. Evidence (CI seed): 30 coherent trees, 390
truncated runs (325 with a non-zero error bound, 320 with lower < exact
P(top), i.e. bounds that matter), 30 non-coherent trees refused; 60/60.
**Negative controls** (engines mutated one at a time, the stage run
alone on the 60 CI-seed cases): losses of the truncated product not
recorded fail 14 cases; an OR gate not minimized, 8; the order limit
off by one inside the product, 12; a block kept whole when its *most*
probable pair clears the cut-off, 13 (and a unit test); the lower bound
taken as the rare-event sum, 22; lost terms filtered against
themselves instead of the retained cut sets (a zero bound), 26.
Unmutated: 0. One mutant is **not** caught by this stage: a dropped
block's covering term omitted inside the truncated product passes all 60
cases. On trees this small, whole blocks are rarely dropped (the
mutant's bound differs from the correct one in 3 of 390 runs, all on one
case), and there the remaining terms still cover every lost cut set, so
no valid check on the output can object. It is caught by the unit test
`truncated_product_keeps_the_same_set_and_bounds_the_loss`, which checks
the covering property of the truncated product directly.

**Consequence-importance checks (FR-24).** On every case's event tree the
oracle makes one truth-table pass, assigns each state to the one sequence
whose path it satisfies, and accumulates, per end state, F and — with the
event's own factor removed from the state weight — F(x=1) and F(x=0) for
every basic event in the tree's support. It shares no code with the
engine's cofactor pass. Checked per end state: F; F(x=1) and F(x=0) of
every event the engine lists (1e-9 relative); Birnbaum, Fussell–Vesely,
RAW and RRW recomputed from the oracle's frequencies; the event
probability; no listed event outside the support; every support event the
engine omits (its BDDs do not depend on it) irrelevant in the oracle too
(F(x=1) = F(x=0) = F); and the CDF metric's rows identical, bit for bit,
to the CD end state's.

Evidence: 180/180 cases across seeds 20260708, 424242 and 7. For the CI
seed: 630 engine rows checked, 607 with F(x=1) ≠ F(x=0), 144 omitted-event
checks, 27 infinite-RRW cases, 294 rows with negative FV (failures that
move frequency out of an end state, mostly out of OK), in 33
non-coherent and 27 coherent event trees. **Negative controls:** an engine
mutated to compute cofactors from the failure-only logic, rescaled to the
exact sequence frequency (success branches treated as constant, the
cut-set habit), fails 57 of 60 cases, 44 of them on the CD group itself;
an engine mutated so that a sequence not depending on x contributes zero
instead of its frequency fails 28 of 60.

### 5.3 Partition property

Σ P(sequence) = 1 is asserted per generated event tree in §5.2 (against
the oracle, and the engine's own reported sum) and was confirmed on the
demo model in §5.1. This is a structural check no single-sequence
comparison provides: the sequence table covers the outcome space exactly
once. On the committed model it is enforced twice on every CI run:
structurally by the partition lint (§4.1) and numerically by
`ci/quantify.py`, which fails when the engine's reported sum deviates
from 1 by more than 1e-9 on a tree without per-sequence house-event
overrides (those change the logic per sequence, so the sum need not be 1;
they are reported instead).

### 5.4 Cross-verification against SCRAM (generated models)

`ci/crosscheck_scram.py` exports models to MEF (`--expand-ccf`) and
compares every sequence probability against SCRAM — an independently
authored BDD engine — at tolerance 2e-5 (bounded by SCRAM's
6-significant-digit report). Evidence at v0.1.0: the demo model plus 75
generated models across two seeds, **every sequence agreeing**. Evidence
for v0.2.0 (workflow run 36247632297 on commit `aa035dc`, the engine of
the release): the demo model plus 100 generated models, 444 sequences,
**every sequence agreeing**. Each tree's own rows only: the MEF export
carries no transfers (FR-11, §8). Validates FR-4/8/9/11/14 against an
implementation with no shared lineage. Since FR-30, every generated case
whose fault tree is non-coherent also has its complete prime-implicant
set compared with SCRAM's (`--prime-implicants`, no order limit, on an
FT-only export). Workflow run 36257686826 (commit `67234bf`): the demo
plus 100 generated models, 444 sequences, every sequence agreeing, and
the complete prime-implicant sets of all **56 generated non-coherent
fault trees identical** in both engines (220 products).

Convention finding (not a defect): SCRAM's alpha-factor implements the
non-staggered NUREG/CR-5485 formula; this engine defaults to staggered.
Switching the demo group to `testing: non-staggered` reproduces SCRAM's
raw-mode result to all displayed digits — both implementations are
internally correct; the convention difference is documented in
`docs/quantification.md` because it is exactly the kind of silent
between-tool discrepancy that corrupts real PSA transfers.

### 5.5 Aralia industrial benchmark suite

`ci/benchmark_mef.py` ran all 43 Aralia fault trees (the community's
standard BDD benchmark set, bundled with SCRAM; 25–1567 basic events,
coherent and non-coherent, 1036 NOT gates across the suite) through both
engines under a common timeout and memory cap:

* **41 of 43 agree on exact P(top)** to SCRAM's reported precision,
  spanning probabilities from 7.8e-1 down to **1.058e-13** (das9209) —
  the regime where approximate methods and naive floating point fail —
  and including cea9601 at 4.3 million BDD nodes.
* **das9701** (2226 gates): this engine exceeds 3 GiB where SCRAM
  succeeds. This is the predicted consequence of static DFS variable
  ordering without sifting (documented in `docs/limitations.md` *before*
  the benchmark was run) — a limitation that fails where and how it was
  documented to fail.
* **nus9601** (1567 events): both engines exceed available memory in the
  test environment; no comparison obtained.

Zero disagreements. Validates FR-4/8/15 at industrial scale.

Prime implicants against SCRAM on Aralia (`--primes 3`, workflow run
36255322868): das9601 identical (no prime of order ≤ 3 in either
engine); cea9601 not compared — SCRAM did not finish within 900 s where
Canopy lists its 924 primes of order ≤ 3 in about 12 s; das9701 beyond
both engines within the limits. The independent prime-implicant leg is
therefore the generated-model cross-check of §5.4. Re-run for
v0.2.0 in the same workflow run (4 GiB cap per side, 120 s timeout):
identical outcome — 41 agree, 0 disagree, das9701 and nus9601 incomplete
for the reasons above.

After garbage collection (FR-27), locally on macOS/arm64: das9701
quantifies with a 2.05 GB peak resident set (5.18 GB before, above the
4 GiB cap) in 21 s, P(top) = 7.446943e-2, agreeing with SCRAM's
7.44694e-2 from the run above; full Birnbaum importance on it takes 32 s
(D-14). CI re-run under the 4 GiB cap (workflow run 36250205253 on
commit `79409e8`): **42 agree, 0 disagree, 1 incomplete** — das9701
now agrees with SCRAM inside the cap (the roadmap's v0.3 criterion
"42/43"); nus9601 still exceeds memory in both engines.

**On every push (since the commit adding `ci/aralia_regression.py`).**
SCRAM's P(top) for the 42 trees it quantifies is committed as reference
data (`ci/fixtures/aralia-scram-reference.json`, with its provenance:
SCRAM commit b85b789, command, workflow run) — external reference values,
like the SciPy values of §4.2, not Canopy results. The CI job `aralia`
fetches the Aralia inputs at the same pinned SCRAM commit, quantifies
every tree with Canopy under the 4 GiB cap and a 120 s timeout, and fails
unless all 42 agree within 2e-5; wall time, BDD arena size and peak
resident memory are reported in the job summary (not gated). Local run
(macOS/arm64): 42 of 42 agree, 31 s in total. First CI run (commit
`daccb56`, Linux/x86-64): 42 of 42 agree; das9701 44.9 s with a 1.58 GB
peak resident set under the 4 GiB cap. A reference value
perturbed by 1e-4 relative is reported as a disagreement and fails the
run. The job runs twice, with the default and the reverse-DFS variable
order (FR-33): 42 of 42 agree in both (local run). This turns the
industrial-scale leg, previously on demand, into a
regression test of every change; the SCRAM build itself (for new
reference values, importance and prime implicants) stays on demand.

**Dynamic reordering against SCRAM (FR-35).** `aralia_regression.py
--reorder` quantifies every tree with `--reorder` and gates on the same
agreement: local run **42 of 42 agree** (also 42 of 42 from the
reverse-DFS order). Sifting ran on 18 trees and never ended with a larger
arena (geometric mean 0.47× the default over all 42; edf9202 9,145 nodes
instead of 1.70 million, elf9601 30 thousand instead of 2.02 million,
cea9601 190 thousand instead of 4.33 million); peak memory fell on 16
trees. It is slower — about seven times over the suite (175 s against
26 s) — and on das9701 it
stalls at 4.6 million nodes with a higher peak than without it, where
the static reverse-DFS order reaches 0.76 million. CI runs this setting
on every push (job `aralia`, 600 s timeout). nus9601 is still not
quantified with `--reorder` after one hour, from either static order
(6.2 GB and 1.5 GB resident at the limit, against 7.7 GB within 300 s
without reordering); it remains covered only by FR-34's bounds.

**Truncated quantification against SCRAM (FR-34).**
`aralia_regression.py --truncated CUTOFF` quantifies every tree by
truncated minimal cut sets and gates on SCRAM's exact P(top) lying within
Canopy's bounds (within the 2e-5 reference tolerance); non-coherent trees
must be refused, and a coherent tree refused, or a non-coherent one
quantified, fails the run. Local run at cut-off 1e-12 (macOS/arm64): **39
of 39 coherent reference trees within the bounds**, relative width
(upper − lower)/upper ≤ 1e-3 on 36 of them (median 3e-8); the three
exceptions have P(top) near or below the cut-off (das9204 2.2e-11,
das9209 1.1e-13, edf9206 8.6e-12), where the interval is correct but
wide. cea9601, das9601 and das9701 are refused as non-coherent. Most
expensive: edf9204, 4.6 million retained cut sets, about 115 s and 8 GB
(exact: 1.9 s). At cut-off 1e-10 the same 39 of 39 hold (median width
4e-6, ≤ 1e-3 on 34) in 65–76 s for the suite (two local runs, nus9601
excluded) with a 1.4 GB peak;
CI runs this setting on every push (job `aralia`, 4 GiB cap). nus9601, which neither engine quantifies
exactly, is bounded instead: at cut-off 1e-8, 12 retained cut sets and
9.939274e-6 ≤ P(top) ≤ 2.716193e-2 — certified but wide, the union bound
summing a very large number of dropped products; at 1e-10 it does not
finish within 400 s. A reference value moved 1e-3 relative outside the
bounds fails the run. Before the covering-term accounting, a numeric
version (each dropped block counted as the smaller of its two sides'
total probability, absorption ignored) gave widths hundreds of times
larger on several trees (edfpa14b: 9.1e-4 against 1.4e-6); the full-product version (products formed, minimized,
then truncated) gave similar widths but reached 8.5 GB on nus9601 at
1e-8.

**Importance against SCRAM (FR-6).** `benchmark_mef.py --importance`
compares, per basic event SCRAM reports, our Birnbaum importance with
SCRAM's Marginal Importance Factor (same definition) and our RAW with
SCRAM's, at the P(top) tolerance. SCRAM reports importance only for
events occurring in its products, so this pass limits products to order
2 (F-5). Workflow run 36250941513 (commit `e2c7029`): **34 trees agree on
3,730 events** (max relative difference 4.8e-6), 7 trees not compared
(SCRAM reported no event), and one tree, das9601 (non-coherent: 14 NOT
gates), where SCRAM's MIF has the opposite sign to ours for 32 events and
its RAW is negative — impossible for a ratio of probabilities. Adjudicated
by definition: re-quantifying das9601 with e10 at probability 1 and 0
gives P(top) = 3.734087e-2 and 3.899994e-3, a difference of +3.344088e-2 —
our Birnbaum exactly, SCRAM's with the sign flipped (F-6). The runner now
performs this adjudication itself with SCRAM alone (SCRAM's own
requantification against SCRAM's importance), so a reference
inconsistency is reported as such and only a disagreement the reference
does not resolve fails the run. First automated run (workflow run
36252065582, commit `021ebd5`): **34 trees agree on 3,763 events**, 0
disagree, 7 not compared, and for all 32 events SCRAM reports on
das9601, SCRAM's own requantification confirms our value against its
reported MIF (e.g. e10: SCRAM's P(S|e) − P(S|¬e) = 3.344091e-2, ours
3.344088e-2, its MIF −3.344090e-2).

### 5.6 Exchange-format round trip

Export (`--expand-ccf`) → import → quantify reproduces direct
quantification of all three demo fault trees to 12 digits. Validates
FR-14/15 jointly: neither direction loses semantics.

Since event trees and CCF groups import (FR-15), the round trip runs on
every property-harness case (§5.2): export with CCF groups pre-expanded,
import, validate, requantify the event tree — every sequence probability
must equal the original to 1e-12 — and, for non-staggered groups (the MEF
convention), again with the groups exported raw and re-expanded by the
engine. Evidence at the CI seed: 60/60 cases, 60 expanded and 15 raw-CCF
round trips. **Negative control:** an importer that swaps each fork's
failure and success paths fails all 60 cases. `ci/test_import_mef.py`
checks the importer on hand-written MEF files: untyped references, both
CCF encodings against a hand-computed non-staggered P(top), an event tree
with a shared end state, a collected basic event and a given initiator
frequency against hand-computed frequencies, and each refusal rule.

### 5.7 Special functions against SciPy (on demand)

`ci/crosscheck_special_functions.py` runs
`engine/examples/special_functions_grid.rs` and compares every value with
SciPy 1.17.1 (`ndtri`, `gammaincinv`, `gammainccinv`, `betaincinv`,
`gammaln`: Cephes and Boost, no shared lineage with the engine). Grid:
Φ⁻¹ at 2,314 points from 1e-300 to 1 − 1e-15; gamma quantiles for 10
shapes 0.02–5,000 and beta quantiles for 10 shape pairs (incl. Beta(200, 1),
Beta(0.5, 1e4) and the D-6 family), 317 u values each down to 1e-200;
ln Γ at 14 points. Worst relative errors: Φ⁻¹ 7.7e-16, ln Γ 1.8e-15,
gamma 1.3e-13, beta 1.9e-11 (tolerances 1e-14, 1e-13, 1e-11, 1e-10).
Values near 1 are compared on 1 − x after discounting 4 ulps of 1.0,
the precision x itself can carry there. This leg found D-5 and D-7.
Validates FR-20's sampling transforms.

---

### 5.8 Cross-toolchain reproducibility (every push)

NFR-1 promises bit-for-bit reproducibility from a tag; in practice the
toolchain a reader has will differ from the one that produced the
evidence. The CI job `toolchains` builds the engine with the minimum
supported Rust (1.75.0) and with the latest stable, runs the engine unit
tests on 1.75, checks that neither build rewrites `engine/Cargo.lock`,
and requires the two binaries' outputs to be byte-identical: the demo
model quantified by `ci/quantify.py` with 10,000 Monte Carlo samples,
and each demo fault tree with 2,000 kept draws. Evidence for v0.2.0:
identical outputs between Rust 1.75.0 and 1.93.1 on macOS/arm64 (local,
before the job existed); the job enforces the same on Linux/x86-64 on
every push from the v0.2.0 release commit on. The comparison covers the engine
build only; Python tooling results are checked on 3.9 and 3.12 by the
suites that ran on each (§4.2).

## 6. Regression strategy

Blocking on every PR and push: static verification (§4.1, including the
`test_validate.py` negative tests), the Aralia regression against SCRAM's
reference values (§5.5), unit tests (§4.2), the
60-case fixed-seed property harness (§5.2, including the uncertainty
stage and the consequence-importance checks), and the base-vs-head risk-delta report (FR-16, FR-23), which
doubles as an engine regression test: an engine-only change on an
unchanged model must report "quantitatively neutral" and, both sides
being sampled with N = 10,000 and seed 20260708, a paired change band of
exactly zero unless the change touches sampling. On demand (`workflow_dispatch`): SCRAM is built from source
(the one-line boost≥1.73 patch is scripted in the workflow) and both the
generated-model cross-check (§5.4) and the Aralia benchmark (§5.5) are
rerun; §5.7 needs SciPy and is run by hand. Recommended before tagging
any model or engine revision.

---

## 7. Anomaly log

Every anomaly found by the V&V activities, with root cause and
disposition. Findings that were not software defects are logged as F-*.

| ID | Found by | Description | Root cause | Disposition |
|---|---|---|---|---|
| D-1 | Harness design review | Event-tree path would emit minsol-derived cut sets for non-coherent sequence logic (invalid: minsol requires monotonicity) | Compiler's coherence flag computed but not consulted on the ET path | Fixed before exposure; harness asserts refusal (commit "validation: CCF expansion … fix empty-cut-set semantics") |
| D-2 | Property harness (seed 20260708, case 6) | Oracle reported zero cut sets for a tautological top; engine reported the empty cut set | Oracle enumeration started at the first non-empty subset — **oracle** defect; engine was correct | Oracle fixed; empty-set convention documented |
| D-3 | Property harness (seed 424242, cases 14/27/37) | Engine suppressed cut sets when a sequence's failure logic was tautological, leaving a dominant sequence (3.3e-3 /yr in the failing case) with no cut-set explanation, inconsistent with the fault-tree path | Over-broad guard `fail_only != ONE` on the ET path | Fixed: both paths emit the empty cut set; harness asserts consistency |
| F-1 | SCRAM cross-check (raw mode) | Demo CCF sequence differed ×1.6 between engines | Alpha-factor convention: SCRAM non-staggered vs our staggered default — both correct | Documented with reproduction (§5.4); `testing:` field selects convention |
| D-4 | Design review for uncertainty propagation | `parameters.yaml`, `house-events.yaml` and `ccf-groups.yaml` had no schema definition although the schema's description listed `parametersFile`, `houseEventsFile`, `ccfGroupsFile` and FR-2 claimed every model file was schema-validated; parameter distributions had never been validated, contradicting `limitations.md` ("schema-validated") | Definitions never written; the validator only schema-checks files with a definition | `parametersFile` added and enforced; CCF `total_probability` distributions schema-checked individually; FR-2 reworded to the actual scope; house/CCF file schemas open (`limitations.md`) |
| D-5 | SciPy grid (§5.7), during development | Gamma quantile inversion failed to converge in the far lower tail (shape 500, u < 1e-177) | Newton on F(x) − u creeps when F is exponentially steep in ln x | Residual changed to ln F − ln u on the smaller tail (nearly linear in ln x there); grid and unit tests cover it. Before release |
| D-6 | Property harness uncertainty stage (seed 20260708, 8 cases) | Beta quantile inversion failed to converge for small solutions at u > 0.5 with large β, e.g. Beta(2.5, 2e4) at u = 0.56 | The symmetry swap moved the solve onto 1 − x ≈ 1, where a relative tolerance on its logarithm is unattainable in f64; the §5.7 grid had not sampled that corner | Solve for whichever of x, 1 − x is ≤ ½ (decided exactly by I½(a,b)), with the residual on the smaller tail; regression unit test over parameter families; grid extended with the family. Before release |
| D-7 | SciPy grid (§5.7), re-run after the D-6 fix | The first D-6 fix returned 0.5 for Beta(200, 1) at u = 1e-22 (true 0.776) | `1 − u` passed through the swap rounds to 1.0 for u < 2⁻⁵³, losing the target | Both tail targets carried through the swap; the smaller, always exact, drives the residual. Before release |
| D-8 | Property harness uncertainty stage (11 cases) | "CDF draws are not the sum of CD-sequence draws" | **Harness and tooling defect**: Python ≥ 3.12 `sum()` of floats is compensated (Neumaier), not the left fold the engine performs; `ci/uncertainty.py` claimed bit-identity with the engine on the same wrong basis. Engine correct | Explicit left fold in the harness and in `ci/uncertainty.py`. Before release |
| F-4 | Demo-model review of FR-24 output | RPS basic events show RAW = 0 and FV ≈ −1.5e-5 for CDF, although RPS failure obviously matters to plant risk | Not a defect: every CD sequence of ET-SLOCA requires RPS success, and RPS failure routes to the ATWS transfer, which is excluded from metrics and not followed (FR-11) — the exact importance of the model as quantified | Documented in `docs/quantification.md` and `docs/limitations.md` (transfers entry). Transfers are now followed (FR-11), but ET-ATWS is not part of the demo model, so the observation stands until an ATWS tree is added |
| D-9 | Code review while adding the partition lint | Model files could be silently ignored by every tool: entity files named `*.yml`, files in sub-directories, and (for the validator and `quantify.py`, not the engine) hidden `*.yaml` files; the manifest's `includes` index, documented as the file index and commented "CI validates that every model file on disk is indexed and every indexed file exists", was read by no tool; a missing `parameters.yaml` crashed the validator with a traceback instead of an error | Loaders use fixed directory scans (`*.yaml`, top level), Python's `glob` skips dotfiles while Rust's `read_dir` does not, and the `includes` check was never implemented | File-index lint (FR-3): such files are errors, `includes` must name exactly the loaded files, missing required files are errors; property-harness models now index `ccf-groups.yaml`; seven `test_validate.py` cases. No committed model was affected (the demo already complied) |
| D-10 | Code review while implementing transfer following; confirmed by `test_transfers.py` against the previous engine | FR-11 said transfers are "excluded from metrics", but the engine's metric sum (and end-state groups, and `consequence_report.py`'s pooling) selected sequences by end state only: a transfer sequence whose `end_state` is mapped to a metric was counted. The exclusion held only by the naming convention `XFER-…` | Membership test on the end state alone; no test ever generated a transfer (the harness had none, the demo's transfer end state is unmapped) | Transfer rows are excluded explicitly in the engine and in `consequence_report.py`; the validator warns when a transfer row's end state is mapped to a metric; hand-computed regression (`test_transfers.py`, `test_consequence_report.py`) and the harness transfer stage. The committed demo model was not affected (its transfer end state is unmapped) |
| D-14 | Timing Aralia trees while measuring garbage collection | Fault-tree Birnbaum importance (FR-6, the default output without `--prob-only`) did not finish on Aralia baobab1 (61 basic events, a 21k-node BDD quantified in 0.0 s) after 8.5 minutes | `restrict`, used once per variable, recursed over the BDD without memoization, revisiting every path of a shared DAG: exponential. The harness's small models and the demo never exposed it; the Aralia benchmark runs `--prob-only` | Birnbaum now from plan cofactors (linear per variable, no arena growth; cross-checked against the restricted-BDD reference in a unit test); `restrict` memoized; regression test on a 64-variable XOR chain. baobab1 now 0.2 s, das9701 32 s |
| D-11 | `ci/test_cli.py`, comparing `canopy report` with `consequence_report.py` | The consequence report's order of tied rows (e.g. RHR pumps A and B, equal frequencies) changed from run to run | Ties were broken by set iteration order, which follows Python's per-process string hashing: cut-set members are frozensets. Violated NFR-1 for a derived report | Ties broken by content (cut set: sorted members; event: ID); regression in `test_consequence_report.py` under six hash seeds (the previous code gives six distinct outputs). Engine output checked separately: identical over 12 runs per demo target |
| D-12 | `ci/test_cli.py`, during development of `canopy delta` | `canopy delta` reported "quantitatively neutral" for a real change when the model path went through a symlink (macOS `/var` → `/private/var`) | git reports the resolved top level; the unresolved model path's relative form climbed out of the base worktree and pointed back at the working-tree model, so "base" and "head" were the same files | Both paths resolved; a model outside the repository is refused; an internal guard refuses a base that resolves to the working tree; regression test through a symlink. Before release |
| D-13 | Documentation review while implementing FR-25 | The README stated that CI "checks dimensional consistency (rate × mission_time must be dimensionless …)", that the strict parse rejects implicit bool/octal, and that CI quantifies through MEF and SCRAM; none was true (no tool checked units per role until FR-25; the parse rejects duplicate keys and syntax errors only; CI quantifies with the Canopy engine, SCRAM is an on-demand cross-check) | Aspirational text from the design phase never reconciled with the implementation | README rewritten to describe what runs; dimensional checks now exist (FR-25). A documentation defect, logged because the rules of §1 treat overselling as worse than silence |
| F-5 | SCRAM importance leg, first run (workflow run 36250205253) | SCRAM reported importance for only a few events per tree and for none in 16 trees; the runner counted those trees as disagreements. Every value SCRAM did report agreed with ours (26 trees, max relative difference 4.8e-6) | Not an engine defect in either code: SCRAM reports importance only for events occurring in its products, and the benchmark limits products to order 1 (`-l 1`, to keep reports from reaching gigabytes) | Importance pass run separately with `-l 2`; a tree with no reported event counts as "not compared", never as agreement; coverage (events compared per tree) printed |
| F-6 | SCRAM importance leg (workflow run 36250941513) | On Aralia das9601, SCRAM's MIF is the negative of our Birnbaum for 32 events, with negative RAW values | **Reference defect** (not ours): P(top \| e) − P(top \| ¬e) computed by re-quantification equals our value (+3.344088e-2 for e10), and a negative RAW is impossible; SCRAM's importance evidently mishandles events of this non-coherent tree (both engines agree on its P(top)) | The benchmark adjudicates importance disagreements by SCRAM's own requantification with the event at 1 and 0 and reports confirmed reference inconsistencies separately from agreement; das9601 importance therefore rests on our harness and the requantification, not on SCRAM |
| D-15 | `ci/test_import_mef.py`, hand-computing a beta-factor group imported from MEF | With `testing: non-staggered`, a beta-factor group gave Q₁ = (1−β)Q_t/(1+β) and Q₂ = 2βQ_t/(1+β) instead of the documented Q₁ = (1−β)Q_t, Q_n = βQ_t (for β = 0.2, Q_t = 0.1: 0.0667/0.0333 instead of 0.08/0.02) | The engine converted a beta group to alpha factors (α₁ = 1−β, α_n = β) and then applied the testing scheme's alpha formula; the documentation (and the beta-factor model) has no testing dependence. Never exercised: the harness generates alpha groups only, and the demo's group is alpha | Beta groups always use the staggered formula, which is the beta model exactly; unit regression test for both schemes; the validator warns that `testing` has no effect on a beta group; the MEF importer no longer sets it. Models with staggered (default) beta groups are unaffected |
| D-16 | Code review of the truncated-quantification output, before commit | `--truncated` listed retained cut sets under the wrong basic-event names (and computed their listed probabilities from the wrong events); P(top) bounds were unaffected | The truncation ZBDD used the basic-event index directly as its variable, while `Zbdd::enumerate` decodes variables with the prime-implicant literal encoding (event v as 2v, its negation as 2v + 1), halving every index | Truncation adopts the literal encoding (positive literals only; asserted when building the lower-bound BDD); the harness's truncation stage compares retained sets by event name against the oracle. Never released |
| D-17 | Investigating why a reorder-stage statistic (FR-35) varied between two identical harness runs | `--order rdfs` on event trees was not reproducible bit for bit: repeated runs of the same generated model gave different JSON in 9 of 20 cases. Results agreed within rounding, so the order stage (a 1e-12 comparison) could not see it. Violates NFR-1 for FR-33 on event trees | The event-tree path listed each row's functional-event tops by iterating a hash map, and `preorder_reverse` numbers variables in visiting order, so the numbering — hence the BDD, hence the last bits — followed Rust's per-process hash seed | Tops listed in sorted functional-event order, as the compile loop already did. The harness now requires three runs of every non-default variant (rdfs, forced reordering, both) to be byte-identical; the pre-fix engine fails that in 36 of 60 cases. Default outputs unchanged (158 of 158 byte-identical to the previous engine). In `main` since commit b79cd5d (FR-33); no release affected (v0.2.0 predates it) |
| D-18 | Benchmarking the shared compiler (FR-38) before commit | With one compiler per event tree, a 32-row tree peaked at 9.9 GB against 2.7 GB with a compiler per row | Once every functional-event top was cached, later rows compiled no gate, so they reached no collection safe point and each row's conjunction stayed in the arena | A safe point opens every row (only cached tops are roots then); 4.2 GB against 4.5 GB afterwards. Never committed |
| D-19 | Negative control of FR-37 (selection mutated to one tree's ranking) | `quantify.py --importance-uncertainty` crashed with `KeyError` when the events it printed were not all among the drawn ones | The printout iterated the model-wide ranking and looked every event up in the combined rows; with the unmutated code the two sets always coincide, so the crash was latent | The printout skips events without draws; a wrong selection is reported by `test_importance_uncertainty.py`'s selection check instead. Fixed in the FR-37 commit |
| D-20 | Comparing exact and truncated results of the demo (FR-42), before commit | The truncated event-tree path listed the empty cut set, at the initiator frequency, for the all-success row SEQ-SLOCA-01; the exact path lists no cut set for a row ending in `OK`. A delta report between the two methods showed a spurious "new cut set"; bounds and metrics were unaffected | FR-39 replicated the exact path's cut-set listing but not its guard on the `OK` end state, and the harness's truncation stage compared the listing with the oracle's minimal cut sets row by row, `OK` rows included, so it asserted the inconsistency | One rule for both paths (`lists_cut_sets`); the truncation stage expects no listed cut set on an `OK` row and checks the retained count separately; `test_truncation.py` updated. In `main` since commit acb0ce5 (FR-39); no release affected |
| D-21 | Code review of the viewer while adding bounds (FR-42) | With results for several event trees, the viewer's header showed each metric of the first event tree carrying it, not the model-wide total (the diff mode summed correctly) | `build_data` appended every tree's metric entries and the page displayed the first entry per metric ID; the demo has one event tree, so no test or use could see it | `build_data` emits one model-wide entry per metric, summed over event trees (`ci/bounds.py`); regression check with two event trees in `test_truncated_pipeline.py`. Present since the viewer showed metrics; affects multi-tree models only |
| D-22 | A negative control of FR-44 (`quantify.py --one-process` mutated to drop the engine flags) that should have failed the configuration check of `test_multi_tree.py` and did not | The test fixture declared its named configuration under `model:` instead of at the top level of `model.yaml`; every tool ignored it silently — the validator passed the model, `quantify.py --configurations` quantified zero configurations — so the check compared two empty results. A user making the same slip would get no configuration results and no error | `model.yaml` has no JSON Schema, and neither the validator nor the loaders checked its keys | The validator rejects unknown top-level, `model:` and risk-metric keys of `model.yaml` (two `test_validate.py` cases); the fixture fixed and its configuration check made non-vacuous (it asserts the configuration's hand-computed effect); the control, rerun, fails 3 checks. No committed or importer-generated manifest used an unknown key |
| F-7 | Re-running performance measurements after FR-37 | Timings measured for FR-35 (and a first FR-38 benchmark) were inflated: two nus9601 experiments started with a one-hour Python timeout had left their engines running for three hours, orphaned, holding CPU and 17 GB of swap | Not a software defect: the timeout killed the `/usr/bin/time` wrapper, not the engine it had started | Processes killed; every figure re-measured on an idle machine and corrected (reordering about 7× slower over the Aralia suite, not 6×; peak memory lower on 16 trees, not 21; shared compiler 10% faster, not 3×); results were unaffected. Long runs are now started without an intermediate wrapper |
| F-2 | Aralia benchmark | Three SCRAM "timeouts" in the first pass | SCRAM report files embed full product listings, reaching gigabytes on large trees; disk exhaustion, not solver limits | Benchmark passes `-l 1` (truncates listing; BDD probability unaffected — verified before adoption); two cases converted to AGREE |
| F-3 | SciPy comparison, during development | 11 of the 27 special-function reference values in the first draft of the unit tests were wrong beyond test tolerance (5 more differed only in the last digit) | Values typed from memory rather than computed | All reference values recomputed with SciPy and labelled with their source; §5.7 made a standing, regenerable leg so reference values are never hand-typed |

Two observations this log supports: the randomized harness found real
defects that 13 hand-written unit tests and a full-model brute-force
comparison had not (D-2, D-3), and anomalies were root-caused in both
directions — twice the reference was wrong, not the engine — which is the
discipline that keeps a validation suite honest.

---

## 8. Requirements traceability matrix

| Req | §4.1 static | §4.2 unit | §5.1 brute force | §5.2 harness | §5.4 SCRAM | §5.5 Aralia | §5.6 round trip |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| FR-1 | ✓ | ✓ | | ✓ | | | |
| FR-2 | ✓ | | | ✓ | | | |
| FR-3 | ✓ (+ `test_validate.py` mutations, partition lint vs brute force) | | | ✓ | | | |
| FR-4 | | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| FR-5 | | ✓ | | ✓ | | | |
| FR-6 | | ✓ | | ✓ | | ✓ (vs SCRAM MIF: 34 trees, 3,730 events) | |
| FR-7 | | ✓ | | ✓ | | | |
| FR-8 | | ✓ | | ✓ | ✓ | ✓ | |
| FR-9 | | | ✓ | ✓ | ✓ | | |
| FR-10 | | ✓ | ✓ | ✓ | ✓ | | |
| FR-11 | | ✓ (`test_transfers.py`) | ✓ | ✓ (+ transfer stage) | ✓ (own rows) | | |
| FR-12 | | ✓ (`test_transfers.py`) | ✓ | ✓ | | | |
| FR-13 | ✓ (partition lint; numeric sum in `quantify.py`) | | ✓ | ✓ | | | |
| FR-14 | | | | | ✓ | | ✓ |
| FR-15 | | ✓ (`test_import_mef.py`) | | ✓ (MEF round trip, 75 per run) | | ✓ | ✓ |
| FR-16 | exercised on every PR; engine-neutrality property per §6 | | | | | | |
| FR-17 | | ✓ | | | | | |
| FR-18 | `ci/test_consequence_report.py` hand-computed fixture (§4.2) | | | | | | |
| FR-19 | `ci/test_import_riskspectrum.py`: hand-computed unit checks + demo round trip to 1e-12 + cross-check PASS/FAIL (§4.2) | | | | | | |
| FR-20 | | ✓ | | ✓ (exact expectations) | | | |
| FR-21 | | ✓ | | ✓ (rerun, partition, fold) | | | ✓ (RS import: identical draws) |
| FR-22 | ✓ (negative tests) | ✓ | | ✓ (variants validate) | | | |
| FR-23 | exercised on every PR (§6); paired-band arithmetic not independently recomputed | | | | | | |
| FR-24 | | ✓ | | ✓ (every event, every end state) | | | |
| FR-25 | ✓ (4 `test_validate.py` cases) | ✓ (+ `test_units.py`, 168 combinations) | | | | | |
| FR-26 | `ci/test_cli.py` (§4.2); `canopy verify` exercised by use | | | | | | |
| FR-45 | | ✓ (`test_viz_diff.py`: parameter and CCF edits of every status in the exhaustive diff, knock-on probability changes, cross-references; 4 of 4 mutants caught; page checked by hand in a browser) | | | | | |
| FR-44 | | ✓ (`test_multi_tree.py`: hand-computed trees, house carry-over between trees, gate counts, Monte Carlo and truncated in one process, refusals, quantify.py --one-process) | | ✓ (multi-tree stage, 128 tree results vs their own process; 4 of 4 mutants caught) | | | |
| FR-43 | | ✓ (`test_house_cache.py`: hand-computed rows, transitive dependency, override removed and restated, gate counts) | | ✓ (house-override stage vs oracle, 73 rows; shared vs per-row; 3 of 3 mutants caught) | | | |
| FR-42 | | ✓ (`test_truncated_pipeline.py`: outward printing, demo bounds around the exact run at four cut-offs, partition check on bounds, change intervals vs hand strings and 2,000 random cases, report shares; `test_truncation.py` hand partition bounds; 8 of 8 tooling mutants caught; exact outputs byte-identical) | | ✓ (partition bounds and followed rows in both event-tree truncation variants, 150 checks; 3 of 3 engine mutants caught) | | | |
| FR-41 | ✓ (`expand --check` in the validate job) | ✓ (`test_expand.py`: golden text, 2,011 floats, every refusal; 6 of 6 mutants caught; demo conversion byte-identical) | | | | | |
| FR-40 | | ✓ (`test_viz_diff.py`: exhaustive diff of 13 edits, thresholds, one-sided results; `test_cli.py` delta --viewer; 5 of 5 mutants caught; page checked by hand in a browser) | | | | | |
| FR-39 | | ✓ (`test_truncation.py`: hand-computed sequence and metric bounds, house override) | | ✓ (event-tree truncation stage vs oracle, 412 rows; 5 of 5 mutants caught) | | | |
| FR-38 | | ✓ (`test_reorder.py`: demo byte identity, forced reordering to rounding) | | ✓ (shared-compiler stage, 120 trees; 3 of 3 mutants caught) | | | |
| FR-37 | | ✓ (`test_importance_uncertainty.py`: bit identity on one tree, exact expectations on two-tree models; 5 of 5 mutants caught) | | | | | |
| FR-36 | ✓ (4 `test_validate.py` cases) | ✓ (`test_ccf_uncertainty.py`: exact Dirichlet moments, mean and variance) | | ✓ (exact expectations, 18 cases; 5 of 5 mutants caught) | | | |
| FR-35 | | ✓ (+ `test_reorder.py`) | | ✓ (reorder stage, 1402 forced compilations; 6 of 6 mutants caught) | | ✓ (42/42 with `--reorder`, every push) | |
| FR-34 | | ✓ (+ `test_truncation.py`) | | ✓ (truncation stage vs oracle MCS, 390 runs; 6 of 7 mutants caught, the 7th by a unit test) | | ✓ (SCRAM's exact P(top) within the bounds, 39/39 coherent trees, every push) | |
| FR-33 | | | | ✓ (order stage, 42/60 trees with a different BDD) | | ✓ (both orders vs SCRAM, every push) | |
| FR-32 | | ✓ (`test_appendix.py`) | | | | | |
| FR-31 | ✓ (2 `test_validate.py` cases) | ✓ (`test_configurations.py`) | | | | | |
| FR-30 | | ✓ | | ✓ (Quine–McCluskey oracle, 56 cases) | ✓ (generated non-coherent trees, complete sets) | (das9601 only; SCRAM times out on cea9601) | |
| FR-29 | | | | ✓ (exact expectations of F(x=1), F(x=0)) | | | |
| FR-28 | | ✓ (+ `test_sampling.py`) | | ✓ (exact expectations under LHS) | | | |
| FR-27 | | ✓ | | ✓ (GC stage, 180 identity checks/run) | | ✓ (das9701 2.05 GB peak, local) | |
| NFR-1 | enforced by design (§2); §5.8 cross-toolchain bit identity (CI, every push); this report regenerates from tag v0.2.0 | | | | | | |
| NFR-2 | ✓ (MGL, oversize CCF, importer scope, unknown fields — all loud errors) | ✓ | | ✓ | | | |

Coverage gaps visible in the matrix are stated in §9 rather than papered
over: FR-6 is compared with SCRAM only for events occurring in SCRAM's
order-2 products (and not on das9601, F-6); FR-16's delta *content* is exercised but not
independently recomputed; FR-17 rests on the unit test alone (the
property harness generates raw probabilities directly and does not
exercise failure-model conversion, matching how rate-mission/rate-repair
were already validated before this test existed); FR-18 rests on its own
fixture test alone and is arithmetic over numbers already validated
elsewhere in this matrix (per-sequence frequencies and cut sets), not an
independent quantification path — see `docs/limitations.md` for the
cut-set-overlap caveat on the importance figures it produces. FR-19's
round trip proves the converter against a hand-built export of the demo
model, i.e. against this repository's own understanding of the table
contract; agreement with an actual RiskSpectrum project is established
per model by `ci/crosscheck_rs.py` against that project's exported
results, and no such run has been performed yet. FR-20 is validated
exactly for **means** only: no leg checks sampled percentiles against an
exact reference (they are order statistics with no stated confidence
interval), no independent engine's Monte Carlo is compared (the MEF
exporter does not emit distributions, so SCRAM's uncertainty analysis is
not yet usable as a leg), and the harness uses `probability` events only,
so failure-model conversion under sampling rests on the shared-formula
design (the Monte Carlo path calls the same `fm_value` as the point path,
checked bit for bit at the point inputs on every run). The special
functions (§5.7) are checked against SciPy on demand, not in CI. FR-11's
transfer following has no independent-engine leg: the MEF exporter does
not carry transfers, so SCRAM compares each tree's own rows only. FR-24
rests on the unit tests and the harness: no independent engine's
event-tree importance is compared (SCRAM's importance analysis is per
fault tree), the cross-tree sum is verified against a hand-computed
fixture only (the harness generates one event tree per case), and the
PR-comment re-ranking is exercised but not independently recomputed.
FR-34's retained sets and lower bounds are checked exactly against the
oracle only on the harness's small trees; at industrial scale the check
is that SCRAM's exact value lies within the bounds — which also holds
for any wider interval, so it does not test the bounds' tightness, and
no independent engine's truncated cut-set list is compared.

---

## 9. Limitations of this V&V program

Validated scope excludes, per `docs/limitations.md`: CCF factor
distributions other than one Dirichlet per group, cut sets under
uncertainty, importance under uncertainty outside each metric's
model-wide top K or for end-state groups, CCF member- or group-level
importance aggregates, MGL CCF groups, prime implicants on trees of
das9701's size (FR-30 is validated on generated trees and das9601),
time-phased missions, MEF event-tree constructs other than
complementary forks, truncated quantification of non-coherent
logic, and exact results past the current memory boundary
(nus9601, which dynamic reordering does not bring within reach either;
for coherent fault trees FR-34 gives certified bounds there, not exact
values). (An earlier revision of this sentence still listed
Latin hypercube sampling, importance under uncertainty, prime implicants
and MEF event-tree/CCF import as excluded, and das9701 as the memory
boundary, after FR-28, FR-29, FR-30, FR-15 and FR-27 had brought them
into scope.) No claim in this report
extends to those. The RiskSpectrum converter (FR-19) is validated
against a hand-built table export, not against a RiskSpectrum-produced
one: the SQL extractor is a mapping skeleton with no schema filled in,
the MGL→alpha conversion follows the non-staggered NUREG/CR-5485
relations without confirmation that RiskSpectrum's MGL implementation
uses the same convention, and the Tested-model point-value fallback
reproduces RiskSpectrum's number only when the export carries `q_mean`.
Each of these is settled by the cross-check on the first real model.

What separates this evidence from a licensing-grade program is
organizational, not just technical, and should be stated plainly:

1. **Independence.** All V&V here was performed by the developing party.
   Since v0.1.0 most development and V&V — the v0.2.0 features, their
   tests and this report's updates — has been done by an AI coding agent
   (Claude) under the maintainer's direction, committing directly to
   `main` (§2). The evidence is machine-checkable and regenerable, but
   it has had no independent human review.
   A qualified program requires independent review and ideally an
   independent V&V organization.
2. **Procedures.** There is no approved SQA plan, no documented review
   and approval records, no formal requirements specification preceding
   implementation (§3 was reverse-engineered from behavior), no training
   or role qualifications.
3. **Operating history.** Established codes carry years of documented
   use; this software has none.
4. **Standard conformance.** No conformance assessment against NQA-1,
   IEEE 1012, or the PRA standard's software expectations has been
   performed; this report borrows their structure, not their authority.

The repository's architecture makes closing these gaps cheaper than usual
— configuration control, regression automation, and reproducibility are
already in place — but they remain open, and the intended-use
classification of §1.1 stands until they are closed.

---

## Appendix A — Evidence regeneration

From a checkout of tag `v0.2.0`, with Python 3.9+ (CI uses 3.12; the
evidence above was produced on 3.9.6 locally and 3.12 in CI) and Rust
1.75+ (the committed lockfile is format v3, which 1.75 reads; a newer
Cargo must not be allowed to rewrite it):

```bash
pip install pyyaml jsonschema
cargo build --release --manifest-path engine/Cargo.toml
cargo test  --release --manifest-path engine/Cargo.toml        # §4.2
python ci/test_consequence_report.py                            # §4.2, FR-18
python ci/test_importance.py                                    # §4.2, FR-24
python ci/test_transfers.py                                     # §4.2, FR-11/12
python ci/test_units.py                                         # §4.2, FR-25
python ci/test_cli.py                                           # §4.2, FR-26
python ci/test_sampling.py                                      # §4.2, FR-28
python ci/test_import_mef.py                                    # §4.2, FR-15
python ci/test_configurations.py                                # §4.2, FR-31
python ci/test_appendix.py                                      # §4.2, FR-32
python ci/test_truncation.py                                    # §4.2, FR-34, FR-39, FR-42
python ci/test_reorder.py                                       # §4.2, FR-35
python ci/test_ccf_uncertainty.py                               # §4.2, FR-36
python ci/test_importance_uncertainty.py                        # §4.2, FR-37
python ci/test_viz_diff.py                                      # §4.2, FR-40, FR-45
python ci/canopy.py expand --check                              # §4.1, FR-41
python ci/test_expand.py                                        # §4.2, FR-41
python ci/test_truncated_pipeline.py                            # §4.2, FR-42
python ci/test_house_cache.py                                   # §4.2, FR-43
python ci/test_multi_tree.py                                    # §4.2, FR-44
# §5.5 Aralia regression (inputs: SCRAM commit b85b789, input/Aralia)
python ci/aralia_regression.py <path-to-scram>/input/Aralia
python ci/aralia_regression.py <path-to-scram>/input/Aralia --reorder --timeout 600   # FR-35 (CI)
python ci/aralia_regression.py <path-to-scram>/input/Aralia --truncated 1e-10   # FR-34 (CI)
python ci/aralia_regression.py <path-to-scram>/input/Aralia --truncated 1e-12   # FR-34 (§5.5 figures)
python ci/canopy.py verify                                      # all of the above + harness
python ci/test_import_riskspectrum.py                           # §4.2, FR-19

python ci/validate.py model schema/psa-model.schema.json       # §4.1
python ci/test_validate.py                                      # §4.1 negative tests
python ci/property_test.py --cases 60 --seed 20260708          # §5.2 (+ uncertainty stage, FR-24 checks)
python ci/property_test.py --cases 60 --seed 424242            # §5.2
python ci/property_test.py --cases 60 --seed 7                 # §5.2

# §5.7 special functions vs SciPy (on demand)
pip install scipy
python ci/crosscheck_special_functions.py

# Demo uncertainty figures quoted in docs/quantification.md
python ci/quantify.py model head.json --samples 10000 --seed 20260708

# §5.4/§5.5 need SCRAM on PATH; build recipe (incl. the boost patch)
# is scripted in .github/workflows/crosscheck.yml
python ci/crosscheck_scram.py --cases 25 --seed 20260708
python ci/crosscheck_scram.py --cases 50 --seed 99
python ci/benchmark_mef.py <path-to-scram>/input/Aralia --timeout 120

# §5.6 round trip
python ci/export_mef.py model /tmp/rt.xml --expand-ccf
python ci/import_mef.py /tmp/rt.xml /tmp/rt-model --ignore-event-trees
```

The §5.1 brute-force oracle is embedded in the property harness
(`Oracle` class in `ci/property_test.py`); the demo-model instance of it
is reconstructible in a few lines using that class against `model/`.
