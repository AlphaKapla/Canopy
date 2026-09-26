# Getting started

## Prerequisites

Three tools, all standard:

| Tool | Version | Used for |
|---|---|---|
| Python | 3.10+ | validation, CI scripts, viewer build |
| Rust (cargo) | 1.75+ | building the quantification engine |
| git | any recent | the model's version control and history |

Install the Python dependencies and build the engine once:

```bash
pip install pyyaml jsonschema
cargo build --release --manifest-path engine/Cargo.toml
```

The engine binary lands at `engine/target/release/canopy`. Note that
`cargo build` only compiles — it produces no results by itself.

## First quantification

All commands run from the repository root. Validate first — it is fast and
catches broken references, schema violations, and gate cycles before you
spend time quantifying:

```bash
python ci/validate.py model schema/psa-model.schema.json
```

Quantify a fault tree (exact top probability, minimal cut sets ranked by
probability, Birnbaum importances):

```bash
engine/target/release/canopy model FT-ECCS-INJECTION
```

Quantify an event tree (sequence frequencies, dominant cut sets per
sequence, risk metrics such as CDF):

```bash
engine/target/release/canopy model ET-SLOCA
```

The first argument is the model directory (the one containing
`model.yaml`); the second is any `FT-` or `ET-` ID — the engine routes on
the prefix. Results print to the terminal and are never written into the
repository: derived artifacts are regenerated, not committed.

## Exploring configurations

House events are boolean flags folded into the logic at quantification
time, so plant-configuration studies need no YAML edits:

```bash
engine/target/release/canopy model FT-ECCS-INJECTION \
    --house HE-ECC-TRAIN-A-OOS=true
```

`--house` can be repeated. The named configurations in `model.yaml`
(`configurations:`) document standard alignments as sets of house-event
values; apply one by passing its overrides as `--house` flags.

## Previewing a change's risk impact

Before opening a pull request you can run the same base-vs-head comparison
CI will post:

```bash
python ci/canopy.py delta --base main --samples 10000 --seed 20260708
```

which is shorthand (with guaranteed worktree cleanup) for

```bash
python ci/quantify.py model head.json --samples 10000 --seed 20260708
git worktree add /tmp/base main
python ci/quantify.py /tmp/base/model base.json --samples 10000 --seed 20260708
python ci/compare.py base.json head.json     # markdown delta report
git worktree remove /tmp/base
```

Drop `--samples`/`--seed` for point values only; keep them identical on
both sides for the paired uncertainty band.

## Viewing the model interactively

```bash
python ci/quantify.py model results.json                # optional
python viz/build_viz.py model psa-viewer.html --results results.json
```

Open `psa-viewer.html` in any browser — a single self-contained file, no
server required. See [visualization.md](visualization.md).

## A typical editing session

1. Branch: `git checkout -b update/rhr-pump-data`.
2. Edit the YAML — say, a basic event's value in
   `model/basic-events/rhr-pumps.yaml` — and update its `provenance` block
   to cite the new data source.
3. `python ci/validate.py model schema/psa-model.schema.json`.
4. Optionally preview the risk delta as above.
5. Commit, push, open a PR. CI validates, quantifies both sides, and posts
   the ΔCDF comment for the reviewer.
6. Merge; tag a release (`git tag rev-2026.3`) when a model revision is
   frozen.
