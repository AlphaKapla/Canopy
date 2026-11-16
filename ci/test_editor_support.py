#!/usr/bin/env python3
"""Tests for the editor support (FR-68): schemas, globs and snippets.

  * the entry points under `schema/editor/` cover exactly the file kinds
    the JSON Schema defines, carry no `$id` (so their relative reference
    resolves against their own location), and point at a definition that
    exists;
  * each one accepts every committed file of its kind in both models and
    rejects a file of another kind and a mutilated one -- the same
    verdicts `ci/validate.py` reaches through the same definitions;
  * the globs in `.vscode/settings.json` name schemas that exist and
    match every model file of those kinds in the repository, and match no
    file of another kind;
  * every snippet expands -- placeholders replaced by their defaults --
    to YAML that parses and validates against the schema of the file it
    belongs in, so a snippet cannot write something the format refuses;
  * the two kinds with no schema are the two documented ones, and the
    documentation says so.

Usage: python ci/test_editor_support.py
"""
import fnmatch
import glob
import json
import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

SCHEMA = os.path.join(ROOT, "schema", "psa-model.schema.json")
EDITOR = os.path.join(ROOT, "schema", "editor")
SETTINGS = os.path.join(ROOT, ".vscode", "settings.json")
SNIPPETS = os.path.join(ROOT, ".vscode", "canopy.code-snippets")
NO_SCHEMA = ("house-events.yaml", "ccf-groups.yaml")

# where a snippet's text belongs: the lines above it, and its indentation
CONTEXT = {
    "be-prob": ("basic_events:", 2, "basic-events"),
    "be-rate": ("basic_events:", 2, "basic-events"),
    "be-test": ("basic_events:", 2, "basic-events"),
    "par": ("parameters:", 2, "parameters"),
    "par-beta": ("parameters:", 2, "parameters"),
    "gate-or": ("fault_trees:\n  FT-X:\n    label: \"x\"\n    top_gate: GT-X\n    gates:",
                6, "fault-trees"),
    "gate-and": ("fault_trees:\n  FT-X:\n    label: \"x\"\n    top_gate: GT-X\n    gates:",
                 6, "fault-trees"),
    "gate-vote": ("fault_trees:\n  FT-X:\n    label: \"x\"\n    top_gate: GT-X\n    gates:",
                  6, "fault-trees"),
    "ft": ("fault_trees:", 2, "fault-trees"),
    "et": ("", 0, "event-trees"),
    "seq": ("event_tree:\n  id: ET-X\n  label: \"x\"\n  functional_events:\n"
            "    FE-A: {label: \"a\", top_gate: GT-A}\n  sequences:", 4, "event-trees"),
    "ccf": ("ccf_groups:", 2, None),
    "he": ("house_events:", 2, None),
    "prov": ("parameters:\n  PAR-X:\n    label: \"x\"\n    value: 1.0\n    unit: hour",
             4, "parameters"),
}

PLACEHOLDER = re.compile(r"\$\{(\d+)(?::([^{}]*))?\}|\$\{(\d+)\|([^|]*)\|[^}]*\}|\$(\d+)")


def expand(body) -> str:
    """A snippet body with every placeholder replaced by its default."""
    text = "\n".join(body)

    def one(m):
        if m.group(1) is not None:
            return m.group(2) or f"X{m.group(1)}"
        if m.group(3) is not None:
            return m.group(4).split(",")[0]     # the first of a choice placeholder
        return ""                      # $0, the final cursor
    return PLACEHOLDER.sub(one, text).rstrip("\n")


def validator(kind):
    import jsonschema
    from referencing import Registry, Resource
    with open(SCHEMA) as f:
        main = Resource.from_contents(json.load(f))
    with open(os.path.join(EDITOR, f"{kind}.schema.json")) as f:
        wrapper = json.load(f)
    reg = Registry().with_resource("../psa-model.schema.json", main)
    return jsonschema.Draft202012Validator(wrapper, registry=reg)


def main() -> int:
    failures = []

    def check(cond, msg):
        (print(f"  ok  {msg}") if cond else failures.append(msg))

    with open(SCHEMA) as f:
        schema = json.load(f)
    defs = {k for k in schema["$defs"] if k.endswith("File")}
    kinds = sorted(os.path.basename(p)[: -len(".schema.json")]
                   for p in glob.glob(os.path.join(EDITOR, "*.schema.json")))

    # --- the entry points
    refs, problems = {}, []
    for kind in kinds:
        with open(os.path.join(EDITOR, f"{kind}.schema.json")) as f:
            d = json.load(f)
        if "$id" in d:
            problems.append(f"{kind}: has an $id, so its relative $ref would not resolve")
        ref = d.get("$ref", "")
        m = re.fullmatch(r"\.\./psa-model\.schema\.json#/\$defs/(\w+)", ref)
        if not m or m.group(1) not in defs:
            problems.append(f"{kind}: $ref {ref!r} does not name a definition of the schema")
        else:
            refs[kind] = m.group(1)
        if not d.get("title") or not d.get("description"):
            problems.append(f"{kind}: no title or description")
    check(not problems, f"{len(kinds)} entry points, each without an $id and pointing at "
                        f"a definition that exists" + (f": {problems}" if problems else ""))
    check(set(refs.values()) == defs,
          f"the entry points cover exactly the file kinds the schema defines "
          f"({', '.join(sorted(defs))})")

    # --- they accept the committed models and reject what they should
    files = {"basic-events": [], "fault-trees": [], "event-trees": [], "parameters": []}
    for model in ("model", "reference"):
        for kind in ("basic-events", "fault-trees", "event-trees"):
            files[kind] += sorted(glob.glob(os.path.join(ROOT, model, kind, "*.yaml")))
        files["parameters"].append(os.path.join(ROOT, model, "parameters.yaml"))
    n = 0
    rejected = []
    for kind, paths in files.items():
        v = validator(kind)
        for p in paths:
            with open(p, encoding="utf-8") as f:
                doc = yaml.safe_load(f)
            errs = [e.message for e in v.iter_errors(doc)]
            n += 1
            if errs:
                rejected.append(f"{os.path.relpath(p, ROOT)}: {errs[0]}")
    check(not rejected, f"all {n} committed files of the four kinds validate"
          + (f": {rejected[:3]}" if rejected else ""))

    v = validator("basic-events")
    other = yaml.safe_load(open(os.path.join(ROOT, "model", "parameters.yaml")))
    check(list(v.iter_errors(other)), "a file of another kind is rejected")
    doc = yaml.safe_load(open(os.path.join(ROOT, "model", "basic-events", "rps.yaml")))
    first = next(iter(doc["basic_events"]))
    del doc["basic_events"][first]["provenance"]
    check(any("provenance" in e.message for e in v.iter_errors(doc)),
          "a basic event without provenance is rejected")
    doc["basic_events"]["NOT-A-BE-ID"] = doc["basic_events"].pop(first)
    check(list(v.iter_errors(doc)), "an identifier that breaks its prefix rule is rejected")

    # --- the globs
    with open(SETTINGS) as f:
        settings = json.load(f)
    mapping = settings["yaml.schemas"]
    bad = [s for s in mapping if not os.path.exists(os.path.join(ROOT, s))]
    check(not bad, f"every schema named in .vscode/settings.json exists"
          + (f": {bad}" if bad else ""))
    check({os.path.basename(s)[: -len(".schema.json")] for s in mapping} == set(kinds),
          "the settings map every entry point")
    unmatched, miscast = [], []
    for kind, paths in files.items():
        want = f"./schema/editor/{kind}.schema.json"
        for p in paths:
            rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
            hit = [s for s, globs in mapping.items()
                   if any(fnmatch.fnmatch(rel, g.replace("**/", "*")) or
                          fnmatch.fnmatch(rel, g) or
                          fnmatch.fnmatch("/" + rel, g.replace("**", "*"))
                          for g in globs)]
            if not hit:
                unmatched.append(rel)
            elif hit != [want]:
                miscast.append(f"{rel} -> {hit}")
    check(not unmatched and not miscast,
          f"every committed file of those kinds is matched by its own glob and no other"
          + (f": unmatched {unmatched[:3]} miscast {miscast[:3]}"
             if unmatched or miscast else ""))
    for name in NO_SCHEMA:
        hit = [s for s, globs in mapping.items() if any(fnmatch.fnmatch(name, g) or
               fnmatch.fnmatch("model/" + name, g.replace("**/", "")) for g in globs)]
        if hit:
            failures.append(f"{name} has no schema but is matched by {hit}")
    check(all(not any(fnmatch.fnmatch("model/" + name, g) for globs in mapping.values()
                      for g in globs) for name in NO_SCHEMA),
          "the two kinds with no schema are matched by no glob "
          f"({', '.join(NO_SCHEMA)})")

    # --- the snippets
    with open(SNIPPETS) as f:
        snippets = json.load(f)
    prefixes = {s["prefix"]: s for s in snippets.values()}
    check(set(prefixes) == set(CONTEXT),
          f"{len(prefixes)} snippets, each with a place in a model file"
          + (f"; unplaced {sorted(set(prefixes) ^ set(CONTEXT))}"
             if set(prefixes) != set(CONTEXT) else ""))
    bad = [p for p, s in prefixes.items()
           if s.get("scope") != "yaml" or not s.get("description")]
    check(not bad, "every snippet is scoped to YAML and described"
          + (f": {bad}" if bad else ""))
    broken = []
    for prefix, snip in sorted(prefixes.items()):
        head, indent, kind = CONTEXT[prefix]
        text = expand(snip["body"])
        if "${" in text or re.search(r"\$\d", text):
            broken.append(f"{prefix}: a placeholder survived expansion")
            continue
        block = "\n".join((" " * indent + ln) if ln.strip() else ln
                          for ln in text.split("\n"))
        doc_text = (head + "\n" if head else "") + block + "\n"
        try:
            doc = yaml.safe_load(doc_text)
        except yaml.YAMLError as e:
            broken.append(f"{prefix}: does not parse as YAML: {e}")
            continue
        if kind is None:
            if not isinstance(doc, dict):
                broken.append(f"{prefix}: does not expand to a mapping")
            continue
        errs = [e.message for e in validator(kind).iter_errors(doc)]
        if errs:
            broken.append(f"{prefix}: {errs[0]}")
    check(not broken, f"every snippet expands to YAML its schema accepts"
          + (f": {broken[:4]}" if broken else ""))

    # a snippet that would write an invalid fragment must be caught
    saved = prefixes["par"]["body"]
    try:
        prefixes["par"]["body"] = [ln for ln in saved if "provenance" not in ln
                                   and "source" not in ln and "justification" not in ln]
        text = expand(prefixes["par"]["body"])
        block = "\n".join("  " + ln if ln.strip() else ln for ln in text.split("\n"))
        doc = yaml.safe_load("parameters:\n" + block + "\n")
        check(list(validator("parameters").iter_errors(doc)),
              "the snippet check would catch a snippet that forgot provenance")
    finally:
        prefixes["par"]["body"] = saved

    # --- the documented gap
    with open(os.path.join(ROOT, "docs", "editor.md"), encoding="utf-8") as f:
        doc = f.read()
    check(all(n in doc for n in NO_SCHEMA) and "D-45" in doc,
          "the documentation names the two kinds with no editor schema and why")

    if failures:
        print(f"\n{len(failures)} FAILED:")
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("\neditor support: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
