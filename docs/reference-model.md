# The reference model

Canopy ships two models. `model/` is a fragment — eleven basic events,
one event tree — small enough to read in one sitting and to hand-compute.
`reference/` is a whole generic pressurised water reactor: seven
initiating events, eight event trees, 24 fault trees, 115 basic events,
eleven common-cause groups, 73 gates. It is the model the tools are
exercised on at a realistic size.

`reference/README.md` describes the model itself: its systems, its data,
and every simplification it makes. **Read it before taking any number out
of the model** — the data is illustrative generic data, not a plant data
set. This page is about the model's role in the project.

## Using it

```bash
python ci/canopy.py validate reference
python ci/canopy.py quantify reference reference.json
python ci/canopy.py report reference.json --metric CDF --model reference
python ci/canopy.py viz reference ref-viewer.html --results reference.json
python ci/canopy.py quantify reference unc.json --samples 10000 --seed 20260708
python ci/canopy.py quantify reference cfg.json --configurations cfg-runs.json
python ci/canopy.py sql load reference && python ci/canopy.py sql path reference
```

Exact quantification of all seven trees takes well under a second; 10,000
Monte Carlo samples take about seven seconds; a truncated run at a cut-off
of 1e-12 takes about half a minute and returns bounds 0.08 % wide.

Editing it is editing any model, with two extra habits:

```bash
python ci/canopy.py expand reference --templates reference-templates   # after a template change
python ci/canopy.py canon model reference --write                     # after any model edit
python ci/canopy.py reference                                         # its own checks
```

`reference/CODEOWNERS.example` is a per-system ownership map for the
model's files: copy it to `.github/CODEOWNERS`, replace the team names,
and a pull request that touches the service-water tree asks the
mechanical engineers for a review on its own.

## Why there are two

A fragment and a plant-sized model fail differently, and the project
needs both kinds of failure to show up in CI.

The fragment is where a result can be checked by hand. Most of the V&V
evidence for the engine's arithmetic is hand-computed references on
`model/` or on randomly generated models small enough for a brute-force
truth table.

The reference model is where *shape* failures show up: a model where
twenty-four fault trees share five support systems, where the same gate
is referenced from three trees, where a sequence-dependent success
criterion has to be written twice, where the common-cause groups overlap
the trains that the event trees branch on. None of that is visible in a
fragment. It is also the only model in the repository large enough for
the viewer's culling, the SQL projection's size and the report's ranking
to mean anything.

## What CI checks on it

`ci/test_reference_model.py` (the `quantify` job, and `canopy verify`)
holds **no expected numbers**. A stored figure would be a derived
artifact, and every data change would have to touch a test. What it
checks are properties:

* it validates with no error and no warning, is in canonical form, and
  its generated files match their templates;
* every event tree's sequence probabilities sum to one; no sequence
  frequency exceeds its initiating-event frequency; each metric total is
  the sum of its end states' frequencies, re-summed independently of the
  reporting code;
* the loss-of-offsite-power variant gates are exactly their originals
  with the variants substituted — so the one place where the model
  duplicates logic cannot drift;
* every parameter is referenced, every basic event is named by a gate,
  every common-cause member exists, no provenance is a placeholder;
* the model round-trips through Open-PSA MEF (with `--expand-ccf`, the
  only form comparable across the staggered/non-staggered convention
  boundary) to 1e-12 on all 65 sequence probabilities, and through the
  SQL working copy byte for byte;
* each of the five configurations quantifies, and none of them lowers
  the core damage frequency below the base case;
* a Monte Carlo run brackets every metric's point value between its 5th
  and 95th percentiles;
* the viewer builds.

What catches a *numeric* regression is the risk-delta comment the CI
pipeline already posts on every pull request: base versus head, on the
committed models.

## What it is not evidence of

The reference model is a fixture, not a validation case. It has no
independent oracle: no published study to compare against, no second code
that has quantified the same plant. Its contribution to the V&V evidence
(§5 of the V&V report) is that the tools handle a model of this shape at
all, and that the invariants above hold on it — not that its numbers are
right. The validation legs that compare Canopy against something
independent are the brute-force oracle, SCRAM, the Aralia suite and
SCRAM's own published test values.
