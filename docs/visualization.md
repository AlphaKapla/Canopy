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
