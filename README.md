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
   The validator and the engine enforce one dimensional rule table
   (probabilities per_demand or dimensionless, frequencies per_year, a rate
   and its time on the same base); units are never converted
   ([model-format.md](docs/model-format.md#quantities-units-references)).

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

## CI pipeline

`.github/workflows/psa.yml` runs on every PR and every push to `main`:

1. `ci/validate.py` — strict YAML parse (duplicate keys and syntax errors
   fail; implicit typing such as `yes` for a string is caught where the
   schema expects another type), JSON Schema validation, the file-index
   lint (no model file silently ignored), reference lint (dangling IDs,
   gate and transfer cycles, duplicate IDs across files, sequence tables
   that must partition the outcome space), the dimensional rules, the
   uncertainty rules, orphan warnings; then the validator's own
   regression suite (`ci/test_validate.py`).
2. Builds `engine/` (Rust BDD quantifier), runs its unit tests, the
   randomized property harness against a brute-force oracle, and the
   tooling tests.
3. Quantifies every event tree on the PR head *and* the base commit (via
   `git worktree`), with the same Monte Carlo seed on both sides.
4. `ci/compare.py` posts a risk-delta comment on the PR: ΔCDF per metric,
   the uncertainty bands and paired change band, Fussell–Vesely
   re-ranking, changed sequence frequencies, and new / removed /
   re-ranked cut sets. The comment is updated in place on subsequent
   pushes.

SCRAM cross-verification and the Aralia benchmark run on demand
(`.github/workflows/crosscheck.yml`), not on every PR.

## The `canopy` command

One entry point for the whole toolchain (`python ci/canopy.py …`; a thin
dispatcher, so each subcommand is exactly the underlying tool):

```
python ci/canopy.py validate                 # schema + lint
python ci/canopy.py quantify -o results.json # every event tree
python ci/canopy.py quantify --target FT-RHR # one tree, engine flags pass through
python ci/canopy.py report --metric CDF      # consequence report
python ci/canopy.py delta                    # working tree vs HEAD, as CI would post it
python ci/canopy.py delta --base main --samples 10000 --seed 20260708
python ci/canopy.py viz -o psa-viewer.html --results results.json
python ci/canopy.py appendix --results results.json   # report appendices (markdown)
python ci/canopy.py verify                   # every check required before a commit
```

`delta` quantifies the working-tree model and the same model at a git ref
with one engine binary and compares them, cleaning up its worktree;
`verify` runs the engine tests, the validator and its suite, every tooling
test and the property harness, stopping at the first failure.

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
BDD-exact basic-event importance table (Fussell–Vesely, RAW, RRW,
Birnbaum, with the minimal-cut-set FV beside it for comparison). Variants:

```
python ci/consequence_report.py head.json --end-state CD          # no model.yaml lookup
python ci/consequence_report.py head.json --metric CDF --model model --json --top 15
```

Importance is exact: computed from BDD cofactors of every qualifying
sequence (success branches included) and summed across event trees — see
`docs/quantification.md`, "Consequence-level importance". The PR comment
reports Fussell–Vesely re-ranking between base and head.

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
