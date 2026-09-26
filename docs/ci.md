# The CI pipeline

`.github/workflows/psa.yml` runs on every pull request and on pushes to
`main`. It has two jobs: **validate**, then **quantify**.

## Job 1 — validate

```bash
python ci/validate.py model schema/psa-model.schema.json
```

Checks, in order:

1. **Strict YAML parse.** Duplicate mapping keys are rejected — this
   specifically catches the damage left by a bad merge-conflict resolution,
   where two copies of an entity survive in one file.
2. **JSON Schema validation** of every file against its entity-type schema
   (`basicEventsFile`, `faultTreeFile`, `eventTreeFile`, …). Field typos
   fail loudly because the schema sets `additionalProperties: false`
   everywhere.
3. **Reference linter**, across the merged ID space:
   dangling references (a formula naming a `BE-`/`GT-`/`HE-` that doesn't
   exist, a functional event pointing at an undefined top gate, a CCF
   member that isn't a basic event, a parameter reference with no
   parameter); duplicate IDs across files; gate cycles (with the cycle
   printed); sequence-table completeness (every functional event resolved
   in every sequence, no duplicate paths).
4. **Warnings** (non-fatal): orphaned gates and basic events never
   reachable from any top gate; sequence end states mapped to no risk
   metric; transfers to event trees not defined in the model.

Exit code 0 with warnings allowed; any error is exit 1 and blocks the PR.

## Job 2 — quantify and report

The job builds the engine (cargo-cached on `Cargo.lock`), then:

```bash
python ci/quantify.py model head.json --samples 10000 --seed 20260708
git worktree add /tmp/base <base-sha>
python ci/quantify.py /tmp/base/model base.json --samples 10000 --seed 20260708
python ci/compare.py base.json head.json > delta.md
```

Before quantifying, the job runs the **property-based validation
harness** (`ci/property_test.py`): it generates 60 random small models
(random gate DAGs with vote/NOT/XOR logic, house events, CCF groups, event
trees over shared logic, seeded for reproducibility), runs the validator
and the engine on each, and independently recomputes every result — top
probabilities, minimal cut sets (exact set equality), Birnbaum
importances, sequence frequencies, the partition property, CDF aggregation,
consequence-level importance (exact F(x=1)/F(x=0) per end state)
— by brute-force truth-table enumeration in Python, including an
independent CCF expansion. Each case is then re-issued with random
distributions (shared parameters, inline and event-level distributions, a
random CCF total and initiator frequency) and the engine's Monte Carlo
means must match the exact expectations computed from closed-form moments.
Any disagreement fails the build and preserves
the offending model for reproduction (`property-failure-*/`). Run locally
with more cases: `python ci/property_test.py --cases 500 --seed 1`.

`quantify.py` discovers every event tree in the model, runs the engine
with `--json` on each, and merges the results into one file. The engine
path defaults to `engine/target/release/canopy` and can be overridden with
the `CANOPY_BIN` environment variable or `--engine`.

Both sides are quantified with the **head engine binary**. For model PRs
that is the comparison you want (isolate the model change). A PR that
changes the engine itself gets both sides computed with the new engine —
so an engine change on an untouched model should report "quantitatively
neutral", making every engine PR a free regression test.

`compare.py` writes the markdown delta report:

* aggregate risk metrics (CDF, …) base → head with relative change,
* each metric's state-of-knowledge distribution for base and head (mean
  and 5th–95th percentiles) and the distribution of the *paired* change
  head − base. Both sides use the same N and seed, and the engine keys
  its random numbers by quantity ID, so every quantity the PR did not
  touch has the same sample in both runs: the change band reflects the
  uncertainty of the change, not Monte Carlo noise
  ([quantification.md](quantification.md#uncertainty-propagation)),
* each named configuration of `model.yaml` (house-event and parameter
  override sets), base → head, with its ratio to the head base case,
* per metric, BDD-exact Fussell–Vesely re-ranking: basic events in the top
  10 of either side whose model-wide rank or FV changed
  ([quantification.md](quantification.md#consequence-level-importance)),
* changed sequence frequencies,
* cut set changes: new, removed, and re-ranked cut sets (top 10 each).

The report is posted as a PR comment and **updated in place** on subsequent
pushes (it carries a `<!-- psa-delta -->` marker), so the thread holds one
living risk summary instead of a comment per push. `head.json`,
`base.json`, and `delta.md` are uploaded as workflow artifacts.

A sample report, from a PR that raised one pump's fail-to-start
probability 1.2e-3 → 3.6e-3:

> | metric | base (/yr) | head (/yr) | change |
> |---|---|---|---|
> | **CDF** | 9.4274e-09 | 1.1728e-08 | 🔺 +24.41% (×1.24) |
>
> **Re-ranked cut sets:** `{BE-RHR-PMP-A-FTS, BE-RHR-PMP-B-FTS}` in
> ET-SLOCA/SEQ-SLOCA-02: 7.2000e-10 → 2.1600e-09 /yr (×3)

With uncertainty, a PR halving the ECCS pump fail-to-start parameter
(1.2e-3 → 6.0e-4) adds:

> | metric | base | head | paired change head − base |
> |---|---|---|---|
> | **CDF** | 2.3151e-08 [1.0120e-09, 8.8031e-08] | 1.3386e-08 [7.1108e-10, 4.9969e-08] | -9.7655e-09 [-3.7776e-08, -2.5118e-10] |

The base and head bands overlap almost entirely, yet the paired change is
negative in more than 95% of states of knowledge.

## Reporting, not gating

`compare.py` always exits 0: whether a ΔCDF is acceptable is an engineering
judgment for the human reviewer, not a threshold script. If your process
wants hard gates (e.g. require a sign-off label when ΔCDF > 1e-7/yr), add
the threshold check in `compare.py` and a branch-protection rule — the
hook point is deliberate.

## Running the pipeline locally

Every CI step is an ordinary script; see
[getting-started.md](getting-started.md) for the local sequence. There is
no CI-only magic: the workflow file just orders the same commands you can
run by hand.

## Versioning and reproducibility

Freezing a model revision is a git tag (`rev-2026.2`). The tag pins the
model files, the schema, and `engine/Cargo.lock`; checking out the tag and
rebuilding reproduces every number bit-for-bit. Historical quantification
of any commit is `git worktree add` + `quantify.py`, exactly as the CI does
for PR bases.
