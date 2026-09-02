# Canopy — git-native probabilistic safety assessment

Canopy treats a PSA model as source code: authored YAML in git,
validated in CI, quantified exactly by a BDD engine, reviewed as pull
requests with automated risk-delta reports.

## Schema conventions

Full documentation lives in [`docs/`](docs/index.md): getting started,
the complete model-format reference, quantification engine guide, CI
pipeline, viewer, architecture, and known limitations.

This repository layout treats the PSA model as *source code*: authored YAML,
validated in CI, quantified by a build step. Derived artifacts (cut sets,
quantified sequence frequencies, reports) are **never committed** here.

## Repository layout

```
model/
  model.yaml            # manifest: metadata, file index, configurations
  parameters.yaml       # named constants & mission times (with units)
  house-events.yaml     # boolean configuration flags
  ccf-groups.yaml       # common-cause failure groups
  basic-events/         # one file per system (diff locality)
    ecc-pumps.yaml
  fault-trees/          # one file per fault tree
    ft-eccs-injection.yaml
  event-trees/          # one file per initiating event
    et-sloca.yaml
schema/
  psa-model.schema.json # JSON Schema used by CI validation
```

## Design rules (the ones that make git diffs meaningful)

1. **Everything is a mapping keyed by stable ID.** Never a positional list of
   objects. Reordering entries must produce an empty diff of meaning; adding
   one basic event must touch exactly the lines of that event.

2. **IDs are immutable and namespaced by prefix.** `BE-` basic event, `GT-`
   gate, `FT-` fault tree, `ET-` event tree, `FE-` functional event, `IE-`
   initiating event, `HE-` house event, `PAR-` parameter, `CCF-` CCF group.
   Renaming an ID is a schema-checked, deliberate operation (CI's reference
   linter fails on any dangling reference).

3. **No YAML anchors/aliases, no implicit typing.** All scalars that could be
   ambiguous are quoted or structured. Reuse happens through explicit
   references (`{param: PAR-...}`), never through YAML `&anchor`/`*alias` —
   anchors make diffs lie about what changed.

4. **Every physical quantity carries a unit.** `{value: 3.0e-5, unit: per_hour}`.
   CI rejects unitless rates and checks dimensional consistency
   (rate × mission_time must be dimensionless, initiating-event frequency must
   be per_year, etc.).

5. **Every number has provenance.** `source` (document reference) and
   `justification` (why this value, why this distribution) are required on
   basic events and parameters. `git blame` then answers *who/when*; the
   provenance block answers *why/from where*.

6. **File layout is a team convention, not a format rule.** Every file holds
   a mapping of one-or-many entities (`fault_trees:`, `basic_events:`, ...);
   the loader merges all indexed files into one ID space and file boundaries
   carry no meaning. Small models can live in a single file per entity type.
   Large models should split (per tree or per system) because that is what
   makes `git log -- <file>` give per-system history, keeps PR diffs local,
   reduces merge-conflict surface between analysts working on different
   systems, and lets CODEOWNERS route review to the right system engineer.
   CI enforces uniqueness of IDs across files, so a tree can be moved between
   files with zero semantic diff.

7. **Formulas are structured, not strings.** A gate is
   `formula: {or: [A, B]}`, not `"A OR B"`. No expression parser, no operator
   precedence bugs, trivially schema-validatable.

## What CI checks (in order)

1. YAML strict parse (fail on duplicate keys, tabs, implicit bool/octal).
2. JSON Schema validation of every file against `schema/psa-model.schema.json`.
3. Reference linter: every ID referenced anywhere resolves; no cycles through
   gates; every CCF member exists; every functional event points at a defined
   top gate; no orphaned definitions (warning).
4. Unit/dimension checks.
5. Compile to Open-PSA MEF XML → quantify (e.g. SCRAM) → post risk-metric
   deltas (ΔCDF, changed cut sets) as a PR comment.

## CI pipeline (implemented)

`.github/workflows/psa.yml` runs on every PR:

1. `ci/validate.py` — strict YAML parse (duplicate-key detection catches bad
   merge resolutions), JSON Schema validation, reference linter (dangling
   IDs, gate cycles, duplicate IDs across files, orphans, sequence-table
   completeness).
2. Builds `engine/` (Rust BDD quantifier) and quantifies every event tree on
   the PR head *and* the base commit (via `git worktree`).
3. `ci/compare.py` posts a risk-delta comment on the PR: ΔCDF per metric,
   changed sequence frequencies, and new / removed / re-ranked cut sets.
   The comment is updated in place on subsequent pushes.

Run the same pipeline locally:

```
python ci/validate.py model schema/psa-model.schema.json
cargo build --release --manifest-path engine/Cargo.toml
python ci/quantify.py model head.json
git worktree add /tmp/base main && python ci/quantify.py /tmp/base/model base.json
python ci/compare.py base.json head.json
```

## Consequence report: cut sets and importance for CD

The classic PSA review tables — dominant minimal cut sets and basic-event
importance for a consequence such as core damage — pooled across every
qualifying sequence in every event tree:

```
python ci/quantify.py model head.json
python ci/consequence_report.py head.json --metric CDF --model model
```

On the demo model this prints the CDF total (2.2082e-8 /yr), the ranked
cut-set table (dominated by the ECC pump CCF pair at 57.9%), and a
basic-event importance table (minimal-cut-set Fussell–Vesely). Variants:

```
python ci/consequence_report.py head.json --end-state CD          # no model.yaml lookup
python ci/consequence_report.py head.json --metric CDF --model model --json --top 15
```

Importance here is the standard cut-set-based Fussell–Vesely measure, not
the BDD-exact Birnbaum the engine reports per fault tree — see
`docs/limitations.md` for the overlap/coverage caveat.

## Visualization

`viz/build_viz.py` compiles the model into a single self-contained
interactive HTML viewer (fault tree diagrams with logic-gate glyphs,
event-tree staircase with sequence frequencies, search, click-through
navigation from functional events to their fault trees, provenance in the
details panel). The viewer is a derived artifact — regenerate it, never
commit it:

```
python ci/quantify.py model results.json          # optional, adds numbers
python viz/build_viz.py model psa-viewer.html --results results.json
# Open visualizer in a browser (no server needed):
open psa-viewer.html                   # if Mac 
xdg-open psa-viewer.html               # if Linux  
./psa-viewer.html                      # if Windows
```

Works offline; suitable as a CI artifact or a gh-pages deploy per tag.

## Importing from RiskSpectrum

`ci/import_riskspectrum.py` converts a RiskSpectrum PSA model — read from
the project database by a mapping-driven SQL extractor, or exported with
the RiskSpectrum PSA Macro — into a Canopy model, and `ci/crosscheck_rs.py`
proves the conversion against RiskSpectrum's own results by record id:

```
python ci/import_riskspectrum.py rs-export/ converted --metric CDF=CD
python ci/validate.py converted schema/psa-model.schema.json
python ci/quantify.py converted converted.json
python ci/crosscheck_rs.py converted rs-results/ --results converted.json
```

Constructs Canopy cannot represent are refused, not approximated; every
approximation is logged in `converted/conversion-log.md`. See
[docs/riskspectrum-import.md](docs/riskspectrum-import.md).

## Versioning

A model revision = a git tag (e.g. `rev-2026.2`). The tag pins the exact
model, the schema version, and (via lockfile) the quantification engine
version, so any historical result is reproducible bit-for-bit.
