#!/usr/bin/env python3
"""Tests for the format version and `canopy migrate` (FR-62).

There is one published version today, so the real chain has no step; the
machinery is exercised with a chain of the test's own that ends at the
real current version: the demo model is first rewritten into two
invented older layouts (0.0.8: parameters say `val`, gates say `desc`;
0.0.9: parameters say `value`, gates still `desc`), then migrated.

  * the pin: the declared format's digest is the one recorded for the
    current version; every version has a pin; every pair of consecutive
    versions has a step; the JSON Schema names the current version; the
    digest ignores the schema's layout and sees a property added;
  * the validator reads only the current version: older, unknown,
    missing, unquoted;
  * a current model: nothing to do, nothing written;
  * the old model migrated over two steps is the demo model's canonical
    form byte for byte; a dry run writes nothing and shows the diff;
    `--to` an intermediate version stops there and says the result was
    not validated; only files whose content changes are rewritten;
    comments stay with their key, one whose key a step removes is
    reported; a step may add a file;
  * refusals, each with nothing written: unknown, missing and non-string
    version; downgrade; unknown target; a step missing from the chain; a
    step that raises, deletes a file, or sets schema_version itself; a
    step that changes a generated file; a result the validator rejects
    (its messages shown); a directory without model.yaml;
  * the command, through `canopy migrate`.

Usage: python ci/test_migrate.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import canonical as cn  # noqa: E402
import migrate as mg  # noqa: E402
import schema_version as sv  # noqa: E402

MODEL = os.path.join(ROOT, "model")
SCHEMA = os.path.join(ROOT, "schema", "psa-model.schema.json")
VERSIONS = ["0.0.8", "0.0.9", sv.CURRENT]


def rename(d: dict, old: str, new: str) -> dict:
    """`d` with key `old` called `new`, in the same place."""
    return {(new if k == old else k): v for k, v in d.items()}


def step_val(docs):
    """0.0.8 -> 0.0.9: a parameter's `val` is `value`."""
    ps = docs["parameters.yaml"]["parameters"]
    for k in ps:
        ps[k] = rename(ps[k], "val", "value")
    return [f"{len(ps)} parameter(s) renamed"]


def step_desc(docs):
    """0.0.9 -> current: a gate's `desc` is `label`."""
    for rel, doc in docs.items():
        for ft in (doc.get("fault_trees") or {}).values():
            for g in ft["gates"]:
                ft["gates"][g] = rename(ft["gates"][g], "desc", "label")


STEPS = {("0.0.8", "0.0.9"): ("parameters: val -> value", step_val),
         ("0.0.9", sv.CURRENT): ("gates: desc -> label", step_desc)}


def read_tree(d):
    out = {}
    for root, _, names in os.walk(d):
        for n in names:
            p = os.path.join(root, n)
            with open(p, encoding="utf-8") as f:
                out[os.path.relpath(p, d).replace(os.sep, "/")] = f.read()
    return out


def main() -> int:
    failures = []
    tmp = tempfile.mkdtemp(prefix="canopy-mgtest-")

    def check(cond, msg):
        (print(f"  ok  {msg}") if cond else failures.append(msg))

    canon = {rel: cn.canonical_text(text) for rel, text in read_tree(MODEL).items()}

    def old_model(name, version="0.0.8", change=None):
        """The demo model rewritten into an invented older layout."""
        d = os.path.join(tmp, name)
        shutil.copytree(MODEL, d)
        for path in cn.model_files(d):
            rel = os.path.relpath(path, d).replace(os.sep, "/")
            with open(path, encoding="utf-8") as f:
                header, data, cs = cn.loads(f.read())
            if rel == "model.yaml":
                data["schema_version"] = version
            if rel == "parameters.yaml" and version == "0.0.8":
                for k in data["parameters"]:
                    data["parameters"][k] = rename(data["parameters"][k], "value", "val")
            for ft in (data.get("fault_trees") or {}).values():
                for g in ft["gates"]:
                    ft["gates"][g] = rename(ft["gates"][g], "label", "desc")
            if change:
                change(rel, data)
            with open(path, "w", encoding="utf-8") as f:
                f.write(cn.dump(data, header, cs))
        return d

    def run(d, **kw):
        kw.setdefault("versions", VERSIONS)
        kw.setdefault("steps", STEPS)
        return mg.migrate(d, **kw)

    # --- the pin
    pins = sv.pins()
    check(pins[sv.CURRENT]["format_sha256"] == sv.format_digest(),
          f"the declared format is the one pinned for {sv.CURRENT} (a change to the schema, "
          "the manifest keys or the unschemed field lists needs a new version: "
          "ci/schema_version.py)")
    check(list(pins) == sv.VERSIONS and sv.CURRENT == sv.VERSIONS[-1]
          and len(set(sv.VERSIONS)) == len(sv.VERSIONS), "every version has a pin, in order")
    check(all((a, b) in mg.STEPS for a, b in zip(sv.VERSIONS, sv.VERSIONS[1:]))
          and all(k in set(zip(sv.VERSIONS, sv.VERSIONS[1:])) for k in mg.STEPS),
          "every pair of consecutive versions has a migration, and no other is registered")
    with open(SCHEMA) as f:
        schema = json.load(f)
    check(f"/{sv.CURRENT}/" in schema["$id"] and f"v{sv.CURRENT}" in schema["title"],
          "the JSON Schema names the current version")
    alt = os.path.join(tmp, "schema.json")
    with open(alt, "w") as f:
        json.dump(schema, f, indent=7, sort_keys=True)
    same = sv.format_digest(alt)
    schema["$defs"]["parameter" if "parameter" in schema["$defs"] else sorted(
        schema["$defs"])[0]].setdefault("properties", {})["colour"] = {"type": "string"}
    with open(alt, "w") as f:
        json.dump(schema, f)
    check(same == sv.format_digest() and sv.format_digest(alt) != same,
          "the digest ignores the schema's layout and sees a property added")

    # --- version_problem and the validator
    vp = lambda v: sv.version_problem(v, VERSIONS) or ""  # noqa: E731
    check(vp(sv.CURRENT) == "" and "run `python ci/canopy.py migrate --write`" in vp("0.0.9")
          and "older" in vp("0.0.8") and "not a version this Canopy knows" in vp("9.9.9")
          and "missing" in vp(None) and "quoted string" in vp(0.1),
          "version_problem: current, older (migrate), unknown, missing, not a string")

    def validator(name, text):
        d = os.path.join(tmp, name)
        shutil.copytree(MODEL, d)
        p = os.path.join(d, "model.yaml")
        with open(p, encoding="utf-8") as f:
            t = f.read()
        line = f'schema_version: "{sv.CURRENT}"\n'
        assert line in t
        with open(p, "w", encoding="utf-8") as f:
            f.write(t.replace(line, text))
        r = subprocess.run([sys.executable, os.path.join(HERE, "validate.py"), d, SCHEMA],
                           capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr

    for name, text, needle in (
            ("v-unknown", 'schema_version: "0.0.1"\n', "not a version this Canopy knows"),
            ("v-missing", "", "schema_version is missing"),
            ("v-float", "schema_version: 0.1\n", "must be a quoted string")):
        rc, out = validator(name, text)
        check(rc != 0 and needle in out, f"the validator refuses a model whose version is "
              f"{name[2:]}: {needle}")
    rc, out = validator("v-current", f'schema_version: "{sv.CURRENT}"\n')
    check(rc == 0, "the validator accepts the current version")

    # --- a current model
    d = os.path.join(tmp, "current")
    shutil.copytree(MODEL, d)
    before = read_tree(d)
    r = mg.migrate(d, write=True)
    check(r["steps"] == [] and r["texts"] == {} and read_tree(d) == before
          and (r["from"], r["to"]) == (sv.CURRENT, sv.CURRENT),
          "a current model: no step, nothing written")

    # --- two steps, to the demo model byte for byte
    d = old_model("old")
    before = read_tree(d)
    check("    val: 3.0e-5\n" in before["parameters.yaml"] and "desc:" in before[
        "fault-trees/ft-rps.yaml"] and 'schema_version: "0.0.8"' in before["model.yaml"],
        "the invented 0.0.8 layout: parameters say val, gates say desc")
    r = run(d)
    check(read_tree(d) == before and r["steps"] == [
        ("0.0.8", "0.0.9", "parameters: val -> value"),
        ("0.0.9", sv.CURRENT, "gates: desc -> label")],
        "a dry run names the steps and writes nothing")
    check(sorted(r["texts"]) == ["fault-trees/ft-eccs-injection.yaml",
                                 "fault-trees/ft-rhr.yaml", "fault-trees/ft-rps.yaml",
                                 "model.yaml", "parameters.yaml"]
          and all(r["texts"][rel] == canon[rel] for rel in r["texts"]),
          "the migrated files are the demo model's canonical text, byte for byte; "
          "files the steps do not touch are not rewritten")
    check("-    val: 3.0e-5" in mg.diff(r) and "+    value: 3.0e-5" in mg.diff(r)
          and f'+schema_version: "{sv.CURRENT}"' in mg.diff(r),
          "the dry run's diff shows the change")
    check(r["notes"] == ["0.0.8 -> 0.0.9: 3 parameter(s) renamed"], "a step's notes are reported")
    run(d, write=True)
    check(read_tree(d) == canon, "written: the whole model is the demo model, byte for byte")
    check(run(d)["steps"] == [], "migrating again: nothing to do")

    d = old_model("mid")
    r = run(d, to="0.0.9", write=True)
    after = read_tree(d)
    check([s[:2] for s in r["steps"]] == [("0.0.8", "0.0.9")]
          and 'schema_version: "0.0.9"' in after["model.yaml"]
          and "    value: 3.0e-5\n" in after["parameters.yaml"]
          and "desc:" in after["fault-trees/ft-rps.yaml"]
          and r["notes"][-1] == f"0.0.9 is not the current version ({sv.CURRENT}): the result "
                                "was not validated",
          "--to an intermediate version stops there and says it was not validated")
    run(d, write=True)
    check(read_tree(d) == canon, "and the second step from there reaches the same model")

    # --- comments
    def commented(rel, data):
        pass

    d = old_model("comments")
    p = os.path.join(d, "parameters.yaml")
    with open(p, encoding="utf-8") as f:
        t = f.read()
    t = t.replace("  PAR-ECC-PMP-FR:\n", "  # under review\n  PAR-ECC-PMP-FR:\n", 1)
    t = t.replace("    val: 24.0\n", "    val: 24.0  # one day\n", 1)
    t = t.replace("    unit: hour\n", "    unit: hour  # SI would be seconds\n", 1)
    with open(p, "w", encoding="utf-8") as f:
        f.write(t)
    r = run(d, write=True)
    after = read_tree(d)["parameters.yaml"]
    check("  # under review\n  PAR-ECC-PMP-FR:\n" in after
          and "    unit: hour  # SI would be seconds\n" in after
          and "# one day" not in after
          and "parameters.yaml: 1 comment(s) dropped: the key they annotated is gone" in r["notes"],
          "comments stay with their key; the one on a renamed key is reported, not moved")

    # --- a step may add a file
    def step_add(docs):
        step_desc(docs)
        docs["basic-events/extra.yaml"] = {"basic_events": {"BE-EXTRA": dict(
            docs["basic-events/rps.yaml"]["basic_events"]["BE-RPS-LOGIC-FAIL"])}}

    d = old_model("add", "0.0.9")
    r = run(d, write=True, steps={**STEPS, ("0.0.9", sv.CURRENT): ("adds a file", step_add)})
    extra = read_tree(d).get("basic-events/extra.yaml", "")
    check("basic-events/extra.yaml" in r["texts"] and r["old"]["basic-events/extra.yaml"] is None
          and extra.startswith("basic_events:\n\n  BE-EXTRA:\n")
          and cn.canonical_text(extra) == extra,
          "a step may add a file; it is written in canonical form")

    # --- refusals
    def refused(name, needle, version="0.0.8", change=None, **kw):
        d = old_model(name, version, change)
        before = read_tree(d)
        try:
            run(d, write=True, **kw)
            failures.append(f"refused: {name}: accepted")
        except mg.MigrationError as e:
            check(needle in str(e) and read_tree(d) == before,
                  f"refused, nothing written: {name} ({str(e).splitlines()[0][:80]})")

    refused("unknown version", "not a version this Canopy knows", "0.0.1")
    refused("version not a string", "quoted string", 0.1)
    refused("downgrade", "never migrated backwards", "0.0.9", to="0.0.8")
    refused("unknown target", "not a version of the format", to="7.0.0")
    refused("missing step", "no migration is registered from 0.0.9",
            steps={("0.0.8", "0.0.9"): STEPS[("0.0.8", "0.0.9")]})

    def no_version(rel, data):
        if rel == "model.yaml":
            del data["schema_version"]
    refused("missing version", "schema_version is missing", change=no_version)

    def raises(docs):
        raise KeyError("gates")
    refused("a step that raises", "0.0.8 -> 0.0.9 failed on this model: KeyError",
            steps={**STEPS, ("0.0.8", "0.0.9"): ("raises", raises)})

    def deletes(docs):
        step_val(docs)
        del docs["ccf-groups.yaml"]
    refused("a step that deletes a file", "a migration never deletes a file",
            steps={**STEPS, ("0.0.8", "0.0.9"): ("deletes", deletes)})

    def sets_version(docs):
        step_val(docs)
        docs["model.yaml"]["schema_version"] = "0.0.9"
    refused("a step that sets schema_version", "changed schema_version itself",
            steps={**STEPS, ("0.0.8", "0.0.9"): ("sets the version", sets_version)})

    def touches_generated(docs):
        step_desc(docs)
        be = docs["basic-events/ecc-pumps.yaml"]["basic_events"]
        for k in be:
            be[k]["label"] = "changed"
    refused("a step that changes a generated file",
            "basic-events/ecc-pumps.yaml is generated from a template", "0.0.9",
            steps={**STEPS, ("0.0.9", sv.CURRENT): ("touches a generated file",
                                                    touches_generated)})

    def dangling(docs):
        step_desc(docs)
        del docs["parameters.yaml"]["parameters"]["PAR-ECC-PMP-FR"]
    d = old_model("invalid", "0.0.9")
    before = read_tree(d)
    try:
        run(d, write=True, steps={**STEPS, ("0.0.9", sv.CURRENT): ("drops a parameter", dangling)})
        failures.append("refused: a result the validator rejects: accepted")
    except mg.MigrationError as e:
        check("does not pass the validator; nothing written" in str(e)
              and "PAR-ECC-PMP-FR" in str(e) and "canopy-migrate-" not in str(e)
              and read_tree(d) == before,
              "refused, nothing written: a result the validator rejects, its messages shown")
    try:
        mg.migrate(os.path.join(tmp, "nowhere"))
        failures.append("refused: no model.yaml: accepted")
    except mg.MigrationError as e:
        check("no model.yaml" in str(e), "refused: a directory without model.yaml")

    # --- the command
    tool = [sys.executable, os.path.join(HERE, "canopy.py"), "migrate"]
    r = subprocess.run(tool + [os.path.join(tmp, "current")], capture_output=True, text=True)
    check(r.returncode == 0 and f"is at {sv.CURRENT}, the current version: nothing to do"
          in r.stdout, "`canopy migrate` on a current model: nothing to do, exit 0")
    r = subprocess.run(tool + ["--list"], capture_output=True, text=True)
    check(r.returncode == 0 and f"current: {sv.CURRENT}" in r.stdout,
          "`canopy migrate --list` names the versions")
    d = old_model("cli")
    r = subprocess.run(tool + [d], capture_output=True, text=True)
    check(r.returncode == 1 and "not a version this Canopy knows" in r.stderr,
          "`canopy migrate` on a version it does not know: exit 1 on stderr")
    check("0.0.8" not in mg.listing() and "NO MIGRATION REGISTERED" in mg.listing(
        ["1", "2"], {}) and "-> 0.0.9: parameters: val -> value" in mg.listing(VERSIONS, STEPS),
        "the listing shows each step, and a missing one")

    shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print(f"\n{len(failures)} FAILED:")
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("\nmigrate: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
