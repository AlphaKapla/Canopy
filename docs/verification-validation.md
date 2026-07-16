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
MEF exchange tools (`ci/export_mef.py`, `ci/import_mef.py`), and the
comparison and reporting tooling (`ci/compare.py`,
`ci/consequence_report.py`, `ci/property_test.py`, `ci/crosscheck_scram.py`,
`ci/benchmark_mef.py`).

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
| FR-2 | Validate every model file against the JSON Schema; unknown fields are errors. |
| FR-3 | Enforce referential integrity: dangling references, gate cycles, duplicate IDs across files, and incomplete sequence tables are errors. |
| FR-4 | Compute the exact top-event probability of a fault tree (no rare-event or MCUB approximation). |
| FR-5 | Compute the complete set of minimal cut sets of a coherent fault tree, with correct subsumption; a tautological function has exactly the empty cut set. |
| FR-6 | Compute Birnbaum importance P(top\|x=1) − P(top\|x=0) per basic event. |
| FR-7 | Support k-of-n vote gates exactly. |
| FR-8 | Compute exact probabilities for non-coherent logic (NOT/XOR); refuse to emit cut sets for non-coherent logic rather than emit invalid ones. |
| FR-9 | Fold house events as compile-time constants; support per-run and per-sequence overrides. |
| FR-10 | Expand CCF groups per NUREG/CR-5485: alpha-factor (staggered and non-staggered) and beta-factor; reject MGL and oversize groups explicitly. |
| FR-11 | Quantify event-tree sequences exactly, with success branches contributing negated top gates; support bypassed events, per-sequence house overrides, and transfers (excluded from metrics). |
| FR-12 | Aggregate sequence frequencies into risk metrics per the manifest's end-state mapping. |
| FR-13 | Sequence probabilities of a complete event tree partition the outcome space (sum to 1). |
| FR-14 | Export models to Open-PSA MEF XML accepted by an independent implementation (schema-valid and semantically accepted by SCRAM). |
| FR-15 | Import MEF fault trees with exact fidelity (export→import round trip reproduces quantification). |
| FR-16 | Report base-vs-head risk deltas computed from two git revisions of a model. |
| FR-17 | Convert basic-event failure models (`probability`, `rate-mission`, `rate-repair`, `rate-periodic-test`) to point unavailability values using documented closed-form formulas. |
| FR-18 | Aggregate minimal cut sets and basic-event importance for a named consequence (risk metric or end-state set), pooled across every qualifying sequence in every event tree, without altering any already-quantified frequency. |
| NFR-1 | Any historical result is reproducible bit-for-bit from a git tag. |
| NFR-2 | Unsupported constructs fail loudly with a specific error; the software never silently approximates or omits. |

---

## 4. Verification

### 4.1 Static verification (every PR, blocking)

`ci/validate.py` verifies FR-1/2/3 on the committed model: strict YAML
parse with duplicate-key rejection, JSON Schema validation with
`additionalProperties: false` throughout, reference linting (dangling
IDs, gate cycles with the cycle printed, cross-file ID duplication,
sequence-table completeness, CCF factor normalization). Negative testing:
deliberately broken references and malformed factors produce errors and a
non-zero exit (verified during development; regenerable per Appendix A).

### 4.2 Unit tests (every PR, blocking)

11 tests in the engine crate, all with hand-computed expected values
(corrected count: an earlier revision of this table double-counted the six
`bdd::tests` entries below under a phantom "loader/serde tests (6)" row
that never existed as separate tests):

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

Additionally, `python ci/test_consequence_report.py` verifies FR-18's
pooling and importance arithmetic against a hand-computed two-event-tree
fixture (cut set summed across two sequences, a non-coherent sequence
flagged as untracked, exact expected coverage ratio). This is a Python
tooling test, not part of the engine-crate count above.

### 4.3 Numerical methods documentation

The algorithms are documented in `docs/quantification.md` and
`docs/architecture.md`: hash-consed ROBDD with memoized apply; exact
probability by one memoized Shannon pass; minimal cut sets by Rauzy's
minimal-solutions transform with the ⊘ subsumption operator; CCF
per-multiplicity formulas with their NUREG/CR-5485 provenance; the
delete-term convention for sequence cut sets; the success-branch negation
that makes sequence frequencies exact.

---

## 5. Validation

Six independent legs. "Independent" is meant literally: each leg uses
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

### 5.3 Partition property

Σ P(sequence) = 1 is asserted per generated event tree in §5.2 and was
confirmed on the demo model in §5.1. This is a structural check no
single-sequence comparison provides: the sequence table covers the
outcome space exactly once.

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

---

## 6. Regression strategy

Blocking on every PR: static verification (§4.1), unit tests (§4.2), the
60-case fixed-seed property harness (§5.2), and the base-vs-head
risk-delta report (FR-16), which doubles as an engine regression test:
an engine-only change on an unchanged model must report "quantitatively
neutral". On demand (`workflow_dispatch`): SCRAM is built from source
(the one-line boost≥1.73 patch is scripted in the workflow) and both the
generated-model cross-check (§5.4) and the Aralia benchmark (§5.5) are
rerun. Recommended before tagging any model or engine revision.

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
| F-2 | Aralia benchmark | Three SCRAM "timeouts" in the first pass | SCRAM report files embed full product listings, reaching gigabytes on large trees; disk exhaustion, not solver limits | Benchmark passes `-l 1` (truncates listing; BDD probability unaffected — verified before adoption); two cases converted to AGREE |

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
| FR-3 | ✓ | | | ✓ | | | |
| FR-4 | | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| FR-5 | | ✓ | | ✓ | | | |
| FR-6 | | | | ✓ | | | |
| FR-7 | | ✓ | | ✓ | | | |
| FR-8 | | ✓ | | ✓ | ✓ | ✓ | |
| FR-9 | | | ✓ | ✓ | ✓ | | |
| FR-10 | | ✓ | ✓ | ✓ | ✓ | | |
| FR-11 | | | ✓ | ✓ | ✓ | | |
| FR-12 | | | ✓ | ✓ | | | |
| FR-13 | | | ✓ | ✓ | | | |
| FR-14 | | | | | ✓ | | ✓ |
| FR-15 | | | | | | ✓ | ✓ |
| FR-16 | exercised on every PR; engine-neutrality property per §6 | | | | | | |
| FR-17 | | ✓ | | | | | |
| FR-18 | `ci/test_consequence_report.py` hand-computed fixture (§4.2) | | | | | | |
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
cut-set-overlap caveat on the importance figures it produces.

---

## 9. Limitations of this V&V program

Validated scope excludes, per `docs/limitations.md`: uncertainty
propagation (point estimates only), MGL CCF groups, prime implicants for
non-coherent cut sets, time-phased missions, MEF event-tree/CCF import,
and models past the das9701 memory boundary. No claim in this report
extends to those.

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

python ci/validate.py model schema/psa-model.schema.json       # §4.1
python ci/property_test.py --cases 60 --seed 20260708          # §5.2
python ci/property_test.py --cases 60 --seed 424242            # §5.2
python ci/property_test.py --cases 60 --seed 7                 # §5.2

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
