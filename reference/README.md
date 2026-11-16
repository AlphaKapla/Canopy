# Reference model: a generic PWR, Level 1, internal events, at power

`REF-PWR-L1-INTERNAL` is the model the tools are exercised on. It is a
complete, connected Level 1 internal-events PSA (Probabilistic Safety
Assessment) of a generic two-train pressurised water reactor, written in
Canopy's own format:

| | |
|---|---|
| initiating events | 7 (transient, loss of feedwater, loss of offsite power, small / medium / large LOCA, steam generator tube rupture) |
| event trees | 8 — the seven above plus a transfer-only tree for the transient without scram |
| sequences | 40 rows, 65 after the transfers are followed |
| fault trees | 24: 17 system trees over five support and eight front-line systems, plus 7 variant trees (27 duplicated gates) for the loss of offsite power |
| gates | 73, including one two-of-three vote |
| basic events | 115, of which 110 are generated from 76 component instances of 21 component types |
| common-cause groups | 11 (nine alpha-factor pairs, one three-member group, one beta-factor pair), each with a Dirichlet distribution on its factors |
| human failure events | 4 |
| parameters | 33 |
| risk metrics | 2 (core damage frequency; core damage with the containment bypassed) |
| configurations | 5 (base, three maintenance alignments, one data sensitivity) |

```bash
python ci/canopy.py validate reference
python ci/canopy.py quantify reference reference.json
python ci/canopy.py report reference.json --metric CDF --model reference
python ci/canopy.py viz reference ref-viewer.html --results reference.json
```

Quantification is exact (no truncation) and takes well under a second.

## What this model is for, and what it is not

It exists so that the tools have something of a realistic shape to work
on: a second regression fixture beside the demo fragment, an example of
how a model of this size is laid out, and the surface on which a review
of a model change can be rehearsed end to end.

**The data is illustrative.** Every number is of the order of magnitude
published for industry-average generic data (NUREG/CR-6928 for component
performance and initiating-event frequencies, the NRC common-cause
failure database for the factors, SPAR-H screening values for the human
error probabilities), but nothing is transcribed from a specific table,
and no number has been reconciled against a source document. The
provenance block of every entity says so. **Do not take a number out of
this model for any real purpose.** The plant is generic: it matches no
licensee's design, and its systems are deliberately simple.

The quantified core damage frequency is about **7.4 × 10⁻⁶ per year**,
which is in the range a real PWR Level 1 internal-events model reports.
That is a property of the generic data, not evidence that the model is
right.

## Deliberate simplifications

Each of these is a modelling choice made to keep the model readable, or a
limitation of Canopy today (`docs/limitations.md`). None of them is
hidden in the data.

* **No branch-scoped boundary conditions.** A success criterion that
  depends on the sequence is written as a second set of gates. The
  loss-of-offsite-power tree points at the `-LOOP` gates of
  `fault-trees/ft-loop.yaml`, where the grid is already lost, and the
  station-blackout branch points at `FT-AFW-SBO`, where only the
  turbine-driven feedwater train is left. Canopy has no way to set a
  house event on a branch and keep the model exportable (V&V D-33), so
  the duplication is explicit. Only gates are duplicated: every basic
  event, parameter and common-cause group is shared.
* **No time phases.** A DC train is taken as unavailable if its bus, its
  battery or its charger fails. Crediting the battery alone for part of
  the mission and the charger for the rest needs a time-phased mission,
  which Canopy does not have.
* **No recovery actions.** Nothing is credited after the first failure
  of a function: no diesel repair, no cross-tie, no alternate feed.
* **One conditional factor.** The reactivity conditions of a transient
  without scram are a conditional split fraction, not a failure; Canopy's
  native format has no split fractions, so it is a basic event in
  `basic-events/conditional.yaml`.
* **No dependency between human actions.** The four human failure events
  are independent. No sequence of this model needs two of them, so the
  question does not arise here — but Canopy could not answer it if it
  did.
* **Level 1 only.** `CD-BYPASS` is a core-damage end state that records
  a containment bypass. There is no containment event tree and there are
  no release categories.
* **Accumulator injection is not modelled** in the large-break tree; it
  is assumed to succeed.

## How the model is laid out

```
reference/
  model.yaml                  the manifest: metrics, file index, configurations
  parameters.yaml             every number, once
  house-events.yaml           two maintenance flags, both false
  ccf-groups.yaml             the common-cause groups
  basic-events/               13 generated files + human actions + conditional
  fault-trees/                17 system trees + 7 loss-of-offsite-power variants
  event-trees/                one file per tree
```

Most basic-event files are **generated**: a component type states a
failure mode's data once (`reference-templates/component-types.yaml`), an
instance file lists the components of that type, and

```bash
python ci/canopy.py expand reference --templates reference-templates
```

writes the files in full. They are committed and reviewed literally, and
CI fails if they drift from their template. The two hand-written files
are the human failure events and the conditional factor, neither of which
is equipment.

`CODEOWNERS.example` maps review responsibility onto those files: one
system per file means GitHub can ask the electrical engineers to review a
change to the diesel generators and the human-reliability analyst to
review a change to an operator action, without anyone assigning
reviewers by hand. It is an example, not in force; copy it to
`.github/CODEOWNERS` and put real team names in it.

Two files are worth reading first for the shape of the model:
`fault-trees/ft-ac-power.yaml`, which every system depends on, and
`event-trees/et-loop.yaml`, where the sequence-dependent success criteria
are.

## The dependency structure

What makes this model behave like a real one is that the support systems
are shared:

```
DC power  ->  diesel control, reactor trip breakers, turbine-driven feedwater
AC power  ->  service water -> component cooling -> injection and heat removal
                            -> motor-driven feedwater, boration, instrument air
instrument air -> pressuriser relief valves, steam generator isolation
```

One consequence is worth pointing out, because it is the kind of thing a
PSA is for: the **DC train A battery charger** comes second in the model
by Fussell-Vesely importance, behind only a diesel generator failing to
run and ahead of every pump and valve. It supplies the control power of diesel A *and* of the
turbine-driven feedwater train, so losing it in a loss of offsite power
removes one AC train and the blackout-proof feedwater train at the same
time. There is no such pairing on train B, and the asymmetry shows up as
a two-event cut set on one side and a three-event cut set on the other.
Nothing in the data says that; it falls out of the logic.

The flip side is the warning on every model of this kind: that result is
only as good as the assumption that the turbine-driven train takes its
control power from train A. One line of one gate decides it.
