# Limitations and roadmap

This is a working prototype that demonstrates the full git-native loop on a
small model. The gaps below are deliberate scope cuts, listed so nobody
discovers them the hard way. Roughly in priority order for a production
path.

## Quantification

**CCF expansion is implemented with scope limits.** Alpha-factor and
beta-factor models expand at load time (staggered and non-staggered
testing per NUREG/CR-5485); expansion is validated by a hand-computed unit
test and by the randomized property harness, whose oracle performs its own
independent expansion. Remaining limits: MGL groups are rejected with an
explicit error (convert to alpha factors), group size is capped at 8
members (combination events grow as 2^n; 8 matches common industry
practice, e.g. RiskSpectrum), and members of one group are assumed not to
appear in other groups.

**Uncertainty is parsed but not propagated.** Distributions
(lognormal/beta/gamma/uniform) are schema-validated and stored; the engine
quantifies point values only. Monte Carlo propagation to percentile CDF is
straightforward on the existing BDD (sample parameters, re-run the O(|BDD|)
probability pass per sample) but not yet built.

**Minimal cut sets are coherent-only.** Trees containing `not`/`xor` get
exact probabilities but no cut sets (prime implicants via Coudert–Madre
meta-products would be required). Success branches in event trees are
handled exactly for frequencies; listed sequence cut sets follow the
delete-term convention.

**No dynamic variable reordering.** DFS order from the top gate is a decent
static heuristic, but ordering is the determinant of BDD size on hard
models; sifting is the standard fix. Deep or badly-ordered models may blow
up in node count before they blow up in time.

**No garbage collection.** Dead intermediate BDD nodes stay in the arena.
Irrelevant for batch CLI runs, disqualifying for a long-lived server
process.

**Missing failure models.** `rate-periodic-test` covers idealized
(instantaneous, perfect) periodic-test standby unavailability; no
time-phased missions, no partial/imperfect test coverage, no
fire/seismic-specific constructs. Initiating-event `frequency` events
cannot appear inside fault trees (enforced).

**Event-tree constructs.** Transfers are reported, not followed — a
transferred sequence's contribution must be analyzed in the target tree
with the transfer frequency as its initiator, manually for now. No Level 2
constructs (release categories exist only as end-state strings).

**Per-sequence recompilation.** Each sequence compiles its own BDD (clean,
but wasteful); a production engine would compile each functional-event top
once per house-configuration and share the manager across sequences (needs
GC first).

## Format and tooling

**Component/module templating is absent.** Identical trains and multi-unit
sites are currently written out explicitly. A `components` mechanism
(parameterized sub-models, MEF-style) is the hardest remaining schema
design problem and the main cure for copy-paste in large models.

**Dimensional analysis is shallow.** Units are required and enum-checked;
full dimension algebra (rate × time dimensionless, alpha factors summing to
1 with tolerance) lives partly in the validator, partly nowhere yet.

**MEF import covers fault trees only (v1).** Event trees, CCF groups,
components and parameter expressions are rejected explicitly. This is
sufficient for the Aralia suite (41/43 trees cross-verified against
SCRAM); the two exceptions are scalability, not correctness: das9701
exceeds memory in our engine (the no-sifting boundary), nus9601 exceeds
it in both engines in the test environment.

**Viewer scale.** The tidy-tree layout is comfortable to a few hundred
gates per tree; beyond that it needs viewport culling and a minimap. No
visual diff mode yet (painting base-vs-head changes onto the trees is the
natural next viz feature).

**Partition checking covers generated models, not the committed model.**
The property harness verifies Σ P(sequence) = 1 on every randomized case;
a direct CI lint of the committed model's sequence tables (cheap: the
check is structural) is still worth adding.

## Regulatory reality

The full verification and validation evidence — requirements,
traceability, anomaly log, and its own honest limitations — is organized
in [verification-validation.md](verification-validation.md).

None of the above is the actual barrier to licensing use. Regulatory-grade
PSA (NRC RG 1.200 endorsement of the ASME/ANS PRA standard, or national
equivalents) expects documented verification & validation of the *software
itself* — configuration management, a validation suite against reference
problems, documented numerical methods, and an operating history. Vendors
of established codes have spent years building that pedigree. This
repository's architecture actually helps such an effort (everything is
already under configuration control, and every result is reproducible from
a tag), but the V&V program itself is a separate, substantial undertaking.
Treat this software as suitable for research, screening, teaching, and
process demonstration — not as a licensing-basis code.
