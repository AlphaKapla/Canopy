# Editing a model in an editor

A Canopy model is YAML, so any editor will do. With the YAML language
server (the `redhat.vscode-yaml` extension in VS Code, or
`yaml-language-server` in any editor that speaks LSP) it also tells you
what a file may contain while you type, and the repository ships the
snippets for the entities you write most.

## What the repository ships

```
schema/editor/*.schema.json   one entry point per kind of model file
.vscode/settings.json         which schema applies to which files (six globs)
.vscode/canopy.code-snippets  14 snippets, prefix `be-`, `par`, `gate-`, …
.vscode/extensions.json       the extensions this repository expects
```

Open the repository in VS Code, accept the recommended extensions, and
every kind of model file but the manifest is checked as you type: a missing
`provenance`, a misspelled `failure_model.type`, an ID that breaks its
prefix rule, a `unit` outside the closed enum. Hovering a key shows what
it is for.

In another editor, point your YAML language server at the same globs;
the entry points under `schema/editor/` are ordinary JSON Schema files
and carry no `$id`, so the relative reference inside them resolves
against their own location.

## What the editor checks, and what it does not

The editor sees one file at a time, so it can only check the shape of
that file. Everything that spans entities or files stays with
`ci/validate.py`:

| checked while you type | checked by `python ci/canopy.py validate` |
|---|---|
| required fields, unknown fields, types | every referenced ID exists |
| ID prefixes (`BE-`, `GT-`, `PAR-`, …) | gate cycles, transfer cycles |
| the closed `unit` and `failure_model.type` enums | the dimensional rules between a rate and its time |
| the shape of a formula | a sequence table that is an exact cover |
| a distribution's own fields | the point value equal to the distribution's mean |
| | CCF membership, alpha factors summing to one |
| | the file index, and files no tool would read |

**A file the editor is happy with is not yet a valid model.** Run the
validator before you commit; CI runs it on every push either way.

## One file has no editor schema

`model.yaml` is checked in `ci/validate.py`, not by the JSON Schema
(V&V D-22), so the manifest gets no live validation. Every other kind of
model file does, house events and common-cause groups since format 0.2.0
(D-4).

## The snippets

Type the prefix and press Tab. Every snippet expands to a fragment that
already carries the fields the format requires — including the
`provenance` block, which is the field most easily forgotten and the one
the validator refuses to do without.

| prefix | what it writes |
|---|---|
| `be-prob`, `be-rate`, `be-test` | a basic event: probability on demand, a rate over a mission, a standby component between periodic tests |
| `par`, `par-beta` | a parameter with a lognormal or a beta distribution |
| `gate-or`, `gate-and`, `gate-vote` | a gate |
| `ft` | a fault tree with a top gate and two train gates |
| `et` | a whole event-tree file, sequence table included |
| `seq` | one more sequence row |
| `ccf` | an alpha-factor common-cause group |
| `he` | a house event |
| `prov` | a provenance block |

`ci/test_editor_support.py` expands every snippet with its default
placeholders and validates the result against the schema of the file it
belongs in, so a snippet cannot drift into writing something the format
does not accept.

## After editing

```bash
python ci/canopy.py canon model reference --write   # one written form (FR-59)
python ci/canopy.py validate reference
python ci/canopy.py delta                           # the risk delta of your change
```

If you edited a file with a `# GENERATED` header, edit its template
instead and run `python ci/canopy.py expand`; CI fails if a generated
file and its template disagree.
