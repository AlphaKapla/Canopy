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

**Uncertainty propagation is Monte Carlo over the model's quantities.**
Distributions are propagated through the exact BDD with state-of-knowledge
correlation ([quantification.md](quantification.md#uncertainty-propagation)),
by simple random or Latin hypercube sampling (`--sampling lhs`), over
parameters, inline quantities, event probabilities, initiator
frequencies, CCF totals and — as a Dirichlet (Beta for β) — CCF factors.
Not yet: a CCF factor distribution other than the Dirichlet with one
concentration (no per-factor spread, no correlation between a group's
total and its factors, no MGL-parameter distributions);
distributions other than lognormal/beta/gamma/uniform (normal, log-uniform,
histogram, discrete are dropped to point values by the RiskSpectrum
importer, with a warning); cut-set frequencies under uncertainty;
importance under uncertainty for end-state groups (only per risk metric)
or for events outside each metric's model-wide top K (10 in CI);
and uncertainty on the pooled cut-set table of the consequence report. Percentiles are sample percentiles with no confidence interval;
only the mean carries a standard error.

**Prime implicants: on request, and costly on large non-coherent
trees.** Minimal cut sets are listed for coherent logic;
for trees containing `not`/`xor`, `--prime-implicants` lists the prime
implicants (products of events and negated events), optionally limited
to order K by `--order-limit` ([quantification.md](quantification.md#fault-tree-output)).
The construction needs a consensus BDD per node: it completes on Aralia
cea9601 (order ≤ 3 in 12 s) but not on das9701 within minutes even at
order 2. Event-tree sequences with non-coherent failure logic get prime
implicants on request too (`ci/quantify.py --prime-implicants`); the
consequence report pools them with the cut sets (negated events shown as
¬, never counted in the minimal-cut-set FV), but the PR comment's cut-set
diff does not list them. Success
branches in event trees are handled exactly for frequencies; listed
sequence cut sets follow the delete-term convention.

**Consequence-level importance is exact but point-valued and per
expanded event.** Birnbaum, Fussell–Vesely, RAW and RRW for a metric or
end state are computed from exact BDD cofactors across every qualifying
sequence and summed across event trees
([quantification.md](quantification.md#consequence-level-importance)).
Distributions under uncertainty exist per metric, per tree and
model-wide (see the uncertainty entry for their limits). Not yet: member-
or group-level aggregates for CCF groups (combination events are ranked individually); importance
for fault-tree-level groupings (system importance). The pooled cut-set
table in `ci/consequence_report.py` remains a cut-set-based listing
(delete-term convention; overlapping cut sets can push its "coverage"
above 100%), and the minimal-cut-set Fussell–Vesely it prints beside the
exact value is the familiar approximation, kept for comparison.

**Variable ordering: two static orders and opt-in sifting, no automatic
choice.** The default orders basic events as compilation discovers them
(depth first); `--order rdfs` visits operands last-to-first; `--reorder`
sifts the order dynamically
([quantification.md](quantification.md#performance-notes)). None is a
safe default: reverse-DFS gives a smaller BDD on 25 of 42 Aralia trees
but a larger one on others (edf9203 5.5×); sifting never ends larger
(geometric mean 0.47×) but costs about seven times the time over the suite,
and on das9701 it stalls where reverse-DFS succeeds. The engine chooses
neither per tree, and the default stays the discovery order so historical
results are bit-identical. Sifting is plain Rudell sifting with CUDD's
growth bound and interaction matrix, without its lower-bound pruning,
group sifting or symmetric sifting, and it runs only at collection safe
points, so one gate whose BDD explodes between two safe points is not
helped. Nor does it reach nus9601: with `--reorder`, from either static order,
the tree is still not quantified after an hour (6.2 GB and 1.5 GB
resident at the limit); truncated bounds (FR-34) remain the only Canopy
result for it.

**Truncated quantification: coherent logic only, opt-in, bounds can be
wide.** `--truncated CUTOFF` retains exactly the minimal cut sets above
the cut-off and brackets P(top) of a fault tree, or every sequence
frequency and metric of an event tree, between rigorous bounds
([quantification.md](quantification.md#truncated-quantification-bounds));
`ci/quantify.py --truncated` carries them through the consequence report,
the appendix, the viewer and the delta report (FR-42). Not yet:
non-coherent fault trees or functional events (a model with one must be
quantified exactly, whole — there is no per-tree mix), a cut-off
relative to P(top), importance, uncertainty or prime implicants on the
truncated path, and any automatic choice between the exact and the
truncated method: the CI pipeline always quantifies exactly. An event-tree
sequence's interval is the difference of two truncated computations, so
it is at least as wide as both errors together. The upper bound is the union bound over the dropped
products: tight on the Aralia trees the exact method also solves
(relative width ≤ 1e-3 on 36 of 39 at cut-off 1e-12), but on nus9601 —
the tree it exists for — the interval at cut-off 1e-8 is
[9.94e-6, 2.72e-2], and lower cut-offs do not finish within 400 s. Memory
follows the retained set and the recorded dropped terms; the ZBDD arena
is not garbage-collected (edf9204 at cut-off 1e-12: 4.6 million retained
cut sets, 8 GB, about two minutes, where the exact method takes 1.9 s).

**Garbage collection is batch-oriented.** The engine collects dead BDD
nodes (mark and compact) at gate-compilation safe points once the arena
passes a threshold, and releases each compiled gate once its last
reference is consumed ([quantification.md](quantification.md#performance-notes)).
Not collected: nodes created after compilation (minimal-cut-set
extraction), and the arena is not shared across event trees or
kept between runs — a long-lived service would also need collection at
query time.

**Missing failure models.** `rate-periodic-test` covers idealized
(instantaneous, perfect) periodic-test standby unavailability; no
time-phased missions, no partial/imperfect test coverage, no
fire/seismic-specific constructs. Initiating-event `frequency` events
cannot appear inside fault trees (enforced).

**Event-tree constructs.** Transfers to event trees in the model are
followed exactly ([quantification.md](quantification.md#event-tree-output));
a transfer to a tree that is not in the model is reported and counted
nowhere, which is what the demo model's `SEQ-SLOCA-04 → ET-ATWS` does (hence
RAW = 0 for the RPS events there). Transfers are not exported to MEF
(`ci/export_mef.py` writes a transfer sequence as an ordinary one), so the
SCRAM cross-check covers each tree's own rows only; transfer expansions
rest on the hand-computed tests and the property harness. Whether a target
tree's own initiator should also be quantified standalone is the
modeller's call (omit it for a transfer-only tree); the RiskSpectrum
importer keeps every initiator and warns. No Level 2 constructs (release
categories exist only as end-state strings).

**One compiler per event tree, not per model.** The rows of an event tree
share one compiler, so each functional-event top is compiled once, and a
change of per-sequence house overrides recompiles only the gates that
reach a changed house event (FR-38, FR-43); but every event tree (and every
`ci/quantify.py` engine process) still builds its own, truncated
quantification still memoizes its sets per house configuration as a
whole, and the conjunction of each row is still built from scratch — on trees where those conjunctions
dominate, sharing saves little (10% on the benchmark in
[quantification.md](quantification.md#performance-notes)).

## Format and tooling

**Two model files have no JSON Schema.** `house-events.yaml` and
`ccf-groups.yaml` are checked by the reference linter only (CCF
`total_probability` distributions are schema-checked individually);
`parameters.yaml` gained a schema with uncertainty propagation (V&V
anomaly D-4).

**Dimensional checks are a rule table, not unit algebra.** Every quantity's
unit is checked against its role by one table enforced identically in the
validator and the engine ([model-format.md](model-format.md#quantities-units-references));
units are never converted, so a model must express a rate and its time in
the same base. Not covered: the units of distribution parameters (a gamma
`scale` or uniform bounds are taken in the quantity's unit), and CCF
factors, which are dimensionless by construction (the alpha-sum check
allows a tolerance of 1e-2).

**MEF import covers fault trees, alpha/beta CCF groups and event trees
of one shape.** Event trees import when every fork has exactly two paths
collecting a formula and its negation (the shape Canopy exports and the
usual shape of success/failure trees); other fork shapes, instructions
such as `set-house-event`, named branches, transfers, MGL groups,
components and parameter expressions are refused explicitly. The SCRAM
dialect carries no initiating-event frequency, so the importer uses 1 /yr
and says so. On the Aralia suite 42/43 trees agree with SCRAM; nus9601
exceeds memory in both engines in the test environment.

**RiskSpectrum import is verified on a hand-built export, not a real
one.** `ci/import_riskspectrum.py` reads a neutral table export
([riskspectrum-import.md](riskspectrum-import.md)); the SQL extractor that
produces it from a project database is a mapping-driven skeleton whose
table and column names must be filled in by someone with the schema, and
no real RiskSpectrum export has been through the pipeline yet. Constructs
without a Canopy equivalent are refused rather than approximated: exchange
events, boundary conditions that force a basic event, initiator fault
trees, CCF groups above 8 members or of UPM type, and MGL unless
`--mgl-to-alpha` is given (a non-staggered NUREG/CR-5485 conversion that
must be checked against RiskSpectrum's expanded CCF events, since
conventions differ between codes). A Tested reliability model with
repair-time, test-duration or first-test terms is emitted as a point value
(RiskSpectrum's `q_mean` when exported, otherwise the idealized
`rate-periodic-test`) with a warning; normal, log-uniform, histogram and
discrete distributions are dropped to point values with a warning.

**Templates cover basic events only.** `templates/` (FR-41) generates
basic events from component types; gates, CCF groups, parameters, event
trees and whole modules are written by hand — so identical trains and
multi-unit sites still repeat their logic, and parameterized sub-models
(the main cure for copy-paste in large models) remain the hardest open
format question. Types do not inherit from
each other, overrides replace a failure mode's fields whole (no partial
merge of a failure model), and `expand` reports but never deletes an
orphaned generated file.

**Viewer scale and diff scope.** The tidy-tree layout is comfortable to a
few hundred gates per tree; beyond that it needs viewport culling and a
minimap. The diff mode (`--base`) marks what changed but does not draw
removed entities in the diagrams (they are listed), does not diff CCF
groups, parameters or named configurations as entities of their own
(their effect shows on the basic events and results they change), and
its page logic is checked by hand in a browser, not in CI (the data it
renders is tested).


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
