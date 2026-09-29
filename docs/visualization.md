# The model viewer

`viz/build_viz.py` compiles the model into a **single self-contained HTML
file** — an interactive viewer with no server, no build toolchain, and no
network dependency. It follows the repository's derived-artifact rule: the
viewer is regenerated from the model, never committed (it is in
`.gitignore`).

## Building

```bash
# structure only
python viz/build_viz.py model psa-viewer.html

# with quantification results (sequence frequencies, CDF readout)
python ci/quantify.py model results.json
python viz/build_viz.py model psa-viewer.html --results results.json
```

Open `psa-viewer.html` in any browser. Because it is one file it travels
well: attach it to a CI run as an artifact, publish it to GitHub Pages per
model tag, or email it to a reviewer.

The header's risk metrics are model-wide: each is summed over every event
tree in the results (before V&V anomaly D-21, a model with several event
trees showed the first tree's value). Results from `ci/quantify.py
--truncated` show every sequence frequency and metric as bounds
`[lower, upper]`, rounded outward, and a `TRUNCATED` readout with the
cut-off ([quantification.md](quantification.md#truncated-quantification-bounds)).

## Navigating

**Left rail** — every event tree and fault tree, searchable. Typing filters
the list; pressing Enter on a full gate or basic-event ID jumps straight to
it inside its containing tree.

**Fault tree view** — top-down diagram. Each gate carries its logic-shape
glyph (D-shape AND, chevron OR, hexagon k/n vote, circle-bar NOT); basic
events show their point probability, house events their default state.
With `--results`, probabilities are the engine's own values (after CCF
expansion, so a CCF member shows its independent part); without, the
viewer computes each failure model's closed form itself and marks CCF
members as "before CCF expansion".
Double-click a gate to collapse or expand its subtree. Gates that appear in
more than one place (shared logic, transfers) carry a ↺ badge — the DAG is
drawn as a tree with repeats, the convention analysts expect.

**Event tree view** — the staircase, rendered from the flat sequence table.
Initiating event on the left with its frequency; functional-event columns
whose **headers click through to the underlying fault tree**; success
branches run level (green), failures drop (red), bypassed segments are
dashed. Each sequence ends in a chip showing its ID, end state, and — when
results are loaded — its frequency.

**Details panel** — click anything. Basic events show probability, failure
model, system, and the full provenance block (source and justification), so
"why is this number what it is" is one click away. Gates show their formula
with every referenced ID as a clickable link. Sequences show their full
path, end state, and frequency.

**Canvas** — drag to pan, scroll to zoom, `Fit view` to reframe,
`Expand all` to reopen collapsed subtrees.

## Diff mode: what a change does to the model

```bash
python viz/build_viz.py model psa-viewer.html --results head.json \
    --base /path/to/base/model --base-results base.json
python ci/canopy.py delta --viewer psa-viewer.html      # working tree vs HEAD
```

With `--base`, the viewer shows the head model with its changes relative
to the base model painted on: every basic event, gate, fault tree, house
event, event tree, sequence, parameter, CCF group and named
configuration **added**, **removed** or **changed**. The
builder computes the difference exactly (entity by entity, field by
field) and embeds it; the page renders it:

- **CHANGES** heads the left rail — every change, with the fields that
  changed (`p`, `formula`, `provenance`, `end_state`, `freq`, …). Click
  one to open it in its tree; a removed entity opens its base definition.
  Trees that contain a change carry a badge in their own list.
- In the diagrams, changed and added gates, events and sequences get a
  dashed ring and a text badge (`Δ` changed, `+` added — the colour is a
  second cue, never the only one); a sequence whose frequency changed
  shows the relative change (`Δ+65.60%`); a changed initiator is marked
  too. Removed entities are not in the head diagram; they are listed.
- The details panel of a changed entity starts with a **CHANGED SINCE
  BASE** block: each changed field's base value, struck through, above
  its head value (with the relative change for probabilities and
  frequencies).
- The header shows the change counts and each risk metric
  `base → head (Δ%)` — or, for truncated results, the bounds on each side;
  a sequence's changed bounds appear as the field `frequency bounds`.
- A parameter (`PAR-`) or CCF group (`CCF-`) is listed with the fields
  that changed — a parameter's `value`, `unit`, `uncertainty`, `label`,
  `provenance`; a group's `model`, `members`, `total_probability`,
  `factors`, `testing`, `factor_uncertainty`, `label`, `provenance` —
  compared exactly, since they are inputs (FR-45). Its details panel shows
  its definition and, for a parameter, **USED BY**: the basic events, CCF
  groups and initiating events that reference it, as links; a basic
  event's panel links its parameters and CCF group. The basic events whose
  probability a parameter or CCF change moved are listed as changed `p`
  too (when both sides have results, or neither).
- A named configuration of `model.yaml` is listed with its changed
  `label`, `house_events` or `parameters` overrides (FR-48); its panel
  shows the overrides, linked to the house events and parameters.

A probability, frequency or metric (for bounds: either bound) counts as
changed at a relative difference of 1e-9 or more — `ci/compare.py`'s threshold — so an edit
that only re-rounds unrelated results through a different variable order
does not light up the whole model. Probabilities and frequencies are
compared only like for like: when only one side has results, the builder
compares the structure and says in a note that the numbers were not
compared.

On a pull request, CI builds this viewer against the PR's base and
uploads it as `psa-viewer.html` in the run's `quantification-results`
artifact — the risk-delta comment says how much the metrics moved, the
viewer shows where in the model and why.

## Color semantics

Color encodes entity kind and outcome, not decoration:

| color | meaning |
|---|---|
| amber | basic events (component failures) |
| steel blue | gates / logic |
| violet | house events, transfers |
| green | success branches, OK end states |
| red | failure branches, core-damage end states, initiating events |
| cyan (mono) | numeric readouts (probabilities, frequencies, CDF) |

## Scale and known edges

The tidy-tree layout is comfortable to a few hundred gates per tree. Very
large single trees will render but become slow to lay out; the planned
upgrades are viewport culling and a minimap. The viewer requires a
reasonably current browser (SVG + ES2019); no external fonts or scripts are
fetched, so it works fully offline.
