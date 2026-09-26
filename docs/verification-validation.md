# Software Verification and Validation Report

**Software:** Canopy — git-native PSA toolchain (quantification engine,
validators, exchange-format tools)
**Version under report:** git tag `v0.1.0` (commit `9839e03`); engine
crate 0.1.0; model schema 0.1.0
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
* All changes arrive by pull request. CI blocks merge on: model
  validation, the full engine unit-test suite, and the 60-case randomized
  property harness (§5.2) at a fixed seed.
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
| FR-6 | Compute Birnbaum importance P(top\|x=1) − P(top\|x=0) per basic event. |
| FR-7 | Support k-of-n vote gates exactly. |
| FR-8 | Compute exact probabilities for non-coherent logic (NOT/XOR); refuse to emit cut sets for non-coherent logic rather than emit invalid ones. |
| FR-9 | Fold house events as compile-time constants; support per-run and per-sequence overrides. |
| FR-10 | Expand CCF groups per NUREG/CR-5485: alpha-factor (staggered and non-staggered) and beta-factor; reject MGL and oversize groups explicitly. |
| FR-11 | Quantify event-tree sequences exactly, with success branches contributing negated top gates; support bypassed events and per-sequence house overrides. Follow a transfer to an event tree of the model exactly — one row per target sequence, quantified as the conjunction of every hop's path on one BDD, house overrides accumulated along the chain (a later hop wins), nested transfers recursively, transfer cycles refused — and count its expansions, never the transfer row, in metrics and end-state groups; a transfer to a tree not in the model is reported and counted nowhere. A tree without an initiating event is transfer-only and is refused standalone. |
| FR-12 | Aggregate sequence frequencies into risk metrics per the manifest's end-state mapping, over every row except transfer rows (FR-11). |
| FR-13 | Sequence probabilities of a complete event tree partition the outcome space (sum to 1); the engine reports the sum per event tree and quantification fails when it deviates from 1 by more than 1e-9 on a tree without per-sequence house-event overrides. |
| FR-14 | Export models to Open-PSA MEF XML accepted by an independent implementation (schema-valid and semantically accepted by SCRAM). |
| FR-15 | Import MEF fault trees with exact fidelity (export→import round trip reproduces quantification). |
| FR-16 | Report base-vs-head risk deltas computed from two git revisions of a model. |
| FR-17 | Convert basic-event failure models (`probability`, `rate-mission`, `rate-repair`, `rate-periodic-test`) to point unavailability values using documented closed-form formulas. |
| FR-18 | Aggregate minimal cut sets and the minimal-cut-set Fussell–Vesely measure for a named consequence (risk metric or end-state set), pooled across every qualifying sequence in every event tree, without altering any already-quantified frequency. (Exact importance for the same consequence is FR-24.) |
| FR-19 | Convert a RiskSpectrum table export into a model that quantifies identically to its hand-written equivalent (every sequence frequency, cut set, fault-tree probability and configuration result), deterministically (byte-identical re-runs), keeping the original record ids in `external_ids`; refuse every construct without a Canopy equivalent explicitly rather than approximate it, and log every numeric approximation. |
| FR-20 | Propagate state-of-knowledge uncertainty by Monte Carlo through the exact BDD: sample every quantity carrying a distribution — parameters, inline failure-model quantities, event-level distributions on `probability` events, CCF totals, initiator frequencies — once per iteration, with every event that references a quantity sharing its sample (state-of-knowledge correlation); lognormal with the point value as mean and EF = q95/q50, beta/gamma/uniform with mean equal to the point value; report mean, standard deviation, standard error of the mean and 5th/50th/95th percentiles per fault tree, sequence and metric; leave point results unchanged. *(Added after v0.1.0.)* |
| FR-21 | Monte Carlo random numbers are a pure function of (seed, quantity ID, iteration): results reproduce bit-for-bit from (tag, seed, N); separately quantified event trees combine iteration by iteration into model-wide metrics; quantities untouched by a model change keep their samples; sampled probabilities above 1 are clamped and counted, never hidden. *(Added after v0.1.0.)* |
| FR-22 | Refuse inconsistent or ambiguous uncertainty specifications, in the validator and in the engine when sampling: a point value that is not its distribution's mean (lognormal: must be positive; beta/gamma/uniform: within 1 %); invalid distribution parameters; an event-level distribution on a non-`probability` model; a quantity given two distributions; a distribution on a CCF group member. The RiskSpectrum importer places distributions accordingly and logs what it moves or drops. *(Added after v0.1.0.)* |
| FR-23 | When both sides are sampled, report each metric's distribution for base and head and, when N and seed match, the distribution of the paired change head − base. *(Added after v0.1.0.)* |
| FR-25 | Enforce dimensional consistency with one rule table, identical in the validator and the engine (which refuses to load an inconsistent model): probabilities and CCF totals `per_demand` or `dimensionless`; frequencies and initiating events `per_year`; each rate-based failure model's rate and time on the same base (`per_hour` with `hour`, `per_year` with `year`), never converted; a parameter's unit applies wherever it is referenced. *(Added after v0.1.0.)* |
| FR-26 | Provide one command-line entry point (`ci/canopy.py`) whose subcommands run the existing tools unchanged — identical output, exit codes propagated — plus `delta`, which quantifies the working-tree model and the same model at a git ref with one engine binary and compares them, always removing its worktree, and `verify`, which runs the checks required before a commit and stops at the first failure. Derived reports are reproducible: identical inputs give byte-identical output regardless of per-process hash seeds (NFR-1). *(Added after v0.1.0.)* |
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

**Negative testing (every PR, blocking):** `ci/test_validate.py` applies
46 targeted mutations to a copy of the demo model — one per error and
warning class: duplicate key and parse failure, unknown field, each kind
of dangling reference, gate cycle, cross-file duplicate event and gate,
undefined top gates, malformed and duplicate sequence paths, overlap,
uncovered outcome, uncovered sub-tree, CCF factor count/sum/range,
undefined and single members, each FR-22 rule, each file-index rule,
missing required file and directory, the FR-25 unit rules (including a
shared parameter re-expressed in years, which must flag all four events
that use it and nothing else), orphan and unmapped-end-state warnings — and requires the exit code, the specific message and, for
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

27 distinct tests in the engine crate (the binary target runs all 27; the
library target re-runs the 19 in `bdd` and `uncertainty`). Expected
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
| `ccf_tests::group_size_nine_rejected` | FR-10: n=9 rejected explicitly (cap is 2..=8) |
| `failure_model_tests::periodic_test_unavailability` | FR-17: rate-periodic-test closed form 1 − (1 − e^−rT)/(rT) vs hand-computed value at rT=0.1 |
| `failure_model_tests::periodic_test_zero_rate_is_exact_zero` | FR-17: r=0 (or T=0) is the exact limit Q_avg=0, not the undivided 0/0 |
| `plan_cofactors_match_restrict` | FR-24: the plan cofactor P(f\|x=v) equals the probability of the restricted BDD on 200 random BDDs (AND/OR/XOR/NOT), satisfies P = p·P1 + (1−p)·P0, and is the plan value itself, bit for bit, for variables outside the support |
| `importance_tests::consequence_importance_hand_computed` | FR-24: two CD sequences over a shared event with a success branch (¬A∧B, A); F = 2.8e-4, F(A=1/0) = 1e-3/2e-4, F(B=1/0) = 1e-3/1e-4, FV/RAW/RRW/Birnbaum and ranking against closed form; the sequence independent of B enters F(B=·) unchanged; exact FV of A 0.2857 vs the minimal-cut-set 0.357 |
| `importance_tests::importance_undefined_ratios` | FR-24: F = 0 gives undefined FV/RAW/RRW; F(x=0) = 0 gives infinite (undefined) RRW, never a number |
| `unit_tests::rule_table_exhaustive` | FR-25: every unit and unit pair of every quantity group against a hand-written list of the valid ones (15 of 132); mixed-base message names both fields; missing units and unknown groups are problems |
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
| `uncertainty::percentile_type7` | FR-20: percentile definition (linear interpolation between order statistics) |

Additionally, `python ci/test_consequence_report.py` verifies FR-18's
pooling and importance arithmetic against a hand-computed two-event-tree
fixture (cut set summed across two sequences, a non-coherent sequence
flagged as untracked, exact expected coverage ratio). This is a Python
tooling test, not part of the engine-crate count above.

`python ci/test_importance.py` verifies FR-24's cross-tree aggregation
(`ci/importance.py`) against a hand-computed two-event-tree fixture: the
engine's unit-test tree plus a tree over a third event, model-wide F,
F(x=1), F(x=0) and all four measures for each event (trees that do not
depend on an event contribute their F unchanged), the FV ranking, the
same answer by metric and by end-state set, infinite RRW and F = 0 as
undefined, and refusal (None) when any tree was quantified without
importance, so a partial model-wide figure is never printed.

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
generated models across two seeds, **every sequence agreeing**. Validates
FR-4/8/9/11/14 against an implementation with no shared lineage.

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

### 5.6 Exchange-format round trip

Export (`--expand-ccf`) → import → quantify reproduces direct
quantification of all three demo fault trees to 12 digits. Validates
FR-14/15 jointly: neither direction loses semantics.

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

## 6. Regression strategy

Blocking on every PR: static verification (§4.1, including the
`test_validate.py` negative tests), unit tests (§4.2), the
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
| D-11 | `ci/test_cli.py`, comparing `canopy report` with `consequence_report.py` | The consequence report's order of tied rows (e.g. RHR pumps A and B, equal frequencies) changed from run to run | Ties were broken by set iteration order, which follows Python's per-process string hashing: cut-set members are frozensets. Violated NFR-1 for a derived report | Ties broken by content (cut set: sorted members; event: ID); regression in `test_consequence_report.py` under six hash seeds (the previous code gives six distinct outputs). Engine output checked separately: identical over 12 runs per demo target |
| D-12 | `ci/test_cli.py`, during development of `canopy delta` | `canopy delta` reported "quantitatively neutral" for a real change when the model path went through a symlink (macOS `/var` → `/private/var`) | git reports the resolved top level; the unresolved model path's relative form climbed out of the base worktree and pointed back at the working-tree model, so "base" and "head" were the same files | Both paths resolved; a model outside the repository is refused; an internal guard refuses a base that resolves to the working tree; regression test through a symlink. Before release |
| D-13 | Documentation review while implementing FR-25 | The README stated that CI "checks dimensional consistency (rate × mission_time must be dimensionless …)", that the strict parse rejects implicit bool/octal, and that CI quantifies through MEF and SCRAM; none was true (no tool checked units per role until FR-25; the parse rejects duplicate keys and syntax errors only; CI quantifies with the Canopy engine, SCRAM is an on-demand cross-check) | Aspirational text from the design phase never reconciled with the implementation | README rewritten to describe what runs; dimensional checks now exist (FR-25). A documentation defect, logged because the rules of §1 treat overselling as worse than silence |
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
| FR-6 | | | | ✓ | | | |
| FR-7 | | ✓ | | ✓ | | | |
| FR-8 | | ✓ | | ✓ | ✓ | ✓ | |
| FR-9 | | | ✓ | ✓ | ✓ | | |
| FR-10 | | ✓ | ✓ | ✓ | ✓ | | |
| FR-11 | | ✓ (`test_transfers.py`) | ✓ | ✓ (+ transfer stage) | ✓ (own rows) | | |
| FR-12 | | ✓ (`test_transfers.py`) | ✓ | ✓ | | | |
| FR-13 | ✓ (partition lint; numeric sum in `quantify.py`) | | ✓ | ✓ | | | |
| FR-14 | | | | | ✓ | | ✓ |
| FR-15 | | | | | | ✓ | ✓ |
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
| NFR-1 | enforced by design (§2); this report regenerates from tag v0.1.0 | | | | | | |
| NFR-2 | ✓ (MGL, oversize CCF, importer scope, unknown fields — all loud errors) | ✓ | | ✓ | | | |

Coverage gaps visible in the matrix are stated in §9 rather than papered
over: FR-6 rests on the harness alone (no independent-engine importance
comparison yet); FR-16's delta *content* is exercised but not
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

---

## 9. Limitations of this V&V program

Validated scope excludes, per `docs/limitations.md`: Latin hypercube
sampling, uncertainty on CCF alpha/beta factors, importance measures and
cut sets under uncertainty, CCF member- or group-level importance
aggregates, MGL CCF groups, prime implicants for
non-coherent cut sets, time-phased missions, MEF event-tree/CCF import,
and models past the das9701 memory boundary. No claim in this report
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

From a checkout of tag `v0.1.0`, with Python 3.10+, Rust 1.75+:

```bash
pip install pyyaml jsonschema
cargo build --release --manifest-path engine/Cargo.toml
cargo test  --release --manifest-path engine/Cargo.toml        # §4.2
python ci/test_consequence_report.py                            # §4.2, FR-18
python ci/test_importance.py                                    # §4.2, FR-24
python ci/test_transfers.py                                     # §4.2, FR-11/12
python ci/test_units.py                                         # §4.2, FR-25
python ci/test_cli.py                                           # §4.2, FR-26
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
