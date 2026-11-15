#!/usr/bin/env python3
"""Bring a model to the current version of the format (FR-62).

    python ci/migrate.py [MODEL]            what would change (nothing is written)
    python ci/migrate.py [MODEL] --write    rewrite the model
    python ci/migrate.py [MODEL] --to V     stop at version V
    python ci/migrate.py --list             the versions and the steps between them

A model states the version of the format it is written in
(`schema_version` in model.yaml). The validator reads only the current
one (`ci/schema_version.py`); this tool rewrites an older model, one
published step after another, each step a function registered in `STEPS`
for one pair of consecutive versions.

What a step may do: change the content of model files and add files.
What the tool guarantees around it:

  * all or nothing: every refusal comes before the first write;
  * the result is written through the canonical writer (FR-59), comments
    staying with their key; a comment whose key a step removed is
    reported, never moved; only files whose content changed are rewritten;
  * a model migrated to the current version is validated (`validate.py`,
    in a scratch copy) before anything is written; a result that does not
    validate is refused with the validator's messages — the step is wrong,
    or the model was not valid in its own version;
  * a file generated from a template (FR-41) is never rewritten: a step
    that changes one is refused, naming the file (the template has to be
    brought to the new format by hand, then `canopy expand`);
  * no downgrade, no unknown version, no step missing from the chain.

There is one version today (0.1.0), hence no step: on a current model the
tool says so and exits 0. The machinery is exercised by
`ci/test_migrate.py` with chains of its own.

Writing a step: `def step(docs)` receives {relative path: parsed content}
of every model file, changes it in place (it may add a path), and returns
a list of notes for the user. It must not touch `schema_version`: the
tool sets it.
"""
import argparse
import copy
import difflib
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import canonical  # noqa: E402
import schema_version  # noqa: E402

GENERATED = "# GENERATED"
SCHEMA = os.path.join(ROOT, "schema", "psa-model.schema.json")

# (from, to) -> (one-line description, function); consecutive versions only
STEPS = {}


class MigrationError(Exception):
    pass


def chain(start: str, to: str, versions, steps) -> list:
    """The steps from `start` to `to`, or a refusal."""
    if not isinstance(start, str) or start not in versions:
        raise MigrationError(schema_version.version_problem(start, versions))
    if to not in versions:
        raise MigrationError(f"--to {to}: not a version of the format "
                             f"({', '.join(versions)})")
    i, j = versions.index(start), versions.index(to)
    if i > j:
        raise MigrationError(f"the model is at {start}, newer than {to}: "
                             "a model is never migrated backwards")
    out = []
    for a, b in zip(versions[i:j], versions[i + 1:j + 1]):
        if (a, b) not in steps:
            raise MigrationError(f"no migration is registered from {a} to {b}")
        out.append((a, b) + tuple(steps[(a, b)]))
    return out


def read_model(model_dir: str) -> dict:
    """{relative path: (text, header, data, comments)} of the model files."""
    if not os.path.isfile(os.path.join(model_dir, "model.yaml")):
        raise MigrationError(f"{model_dir}: no model.yaml")
    out = {}
    for path in canonical.model_files(model_dir):
        rel = os.path.relpath(path, model_dir).replace(os.sep, "/")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        try:
            out[rel] = (text,) + tuple(canonical.loads(text))
        except canonical.CanonError as e:
            raise MigrationError(f"{rel}: {e}")
    return out


def validate(texts: dict, model_dir: str, schema: str = SCHEMA):
    """Run the validator on the model as it would be written; None if valid,
    else its output. Files outside the model files are copied as they are."""
    tmp = tempfile.mkdtemp(prefix="canopy-migrate-")
    try:
        scratch = os.path.join(tmp, "model")
        shutil.copytree(model_dir, scratch)
        for rel, text in texts.items():
            path = os.path.join(scratch, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        r = subprocess.run([sys.executable, os.path.join(HERE, "validate.py"), scratch, schema],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return None
        return (r.stdout + r.stderr).replace(scratch + os.sep, "").strip()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def plan(model_dir: str, to=None, versions=None, steps=None, schema: str = SCHEMA) -> dict:
    """What migrating `model_dir` to `to` would do. Writes nothing.
    Returns {"from", "to", "steps": [(a, b, description)], "texts": {rel: new
    text of each file that changes}, "old": {rel: text or None}, "notes"}."""
    versions = versions or schema_version.VERSIONS
    steps = STEPS if steps is None else steps
    to = to or versions[-1]
    files = read_model(model_dir)
    manifest = files["model.yaml"][2]
    if not isinstance(manifest, dict):
        raise MigrationError("model.yaml is not a mapping")
    start = manifest.get("schema_version")
    todo = chain(start, to, versions, steps)
    result = {"from": start, "to": to, "steps": [s[:3] for s in todo], "texts": {},
              "old": {}, "notes": []}
    if not todo:
        return result
    docs = {rel: copy.deepcopy(f[2]) for rel, f in files.items()}
    for a, b, description, function in todo:
        before = set(docs)
        try:
            notes = function(docs) or []
        except MigrationError:
            raise
        except Exception as e:           # a step's failure is a refusal, not a traceback
            raise MigrationError(f"{a} -> {b} failed on this model: {type(e).__name__}: {e}")
        gone = before - set(docs)
        if gone:
            raise MigrationError(f"{a} -> {b} removed {', '.join(sorted(gone))}: "
                                 "a migration never deletes a file")
        if not isinstance(docs.get("model.yaml"), dict) \
                or docs["model.yaml"].get("schema_version") != a:
            raise MigrationError(f"{a} -> {b} changed schema_version itself")
        docs["model.yaml"]["schema_version"] = b
        result["notes"] += [f"{a} -> {b}: {n}" for n in notes]
    for rel in sorted(docs):
        old = files.get(rel)
        if old is not None and docs[rel] == old[2] \
                and canonical.type_shape(docs[rel]) == canonical.type_shape(old[2]):
            continue
        if old is not None and old[0].startswith(GENERATED):
            raise MigrationError(
                f"{rel} is generated from a template and the migration changes it: "
                "bring the template to the new format, run `canopy expand`, then migrate")
        header, comments = (old[1], old[3]) if old else ((), None)
        try:
            text = canonical.dump(docs[rel], header, comments)
        except canonical.CanonError as e:
            raise MigrationError(f"{rel}: {e}")
        if comments is not None:
            lost = canonical.dropped_comments(docs[rel], comments)
            if lost:
                result["notes"].append(f"{rel}: {lost} comment(s) dropped: the key they "
                                       "annotated is gone")
        result["texts"][rel] = text
        result["old"][rel] = old[0] if old else None
    if to == versions[-1]:
        problems = validate(result["texts"], model_dir, schema)
        if problems:
            raise MigrationError(
                f"the model migrated to {to} does not pass the validator; nothing "
                f"written:\n{problems}")
    else:
        result["notes"].append(f"{to} is not the current version ({versions[-1]}): "
                               "the result was not validated")
    return result


def migrate(model_dir: str, to=None, write: bool = False, **kw) -> dict:
    """`plan`, then write the changed files when `write`."""
    result = plan(model_dir, to, **kw)
    if write:
        for rel, text in result["texts"].items():
            path = os.path.join(model_dir, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
    return result


def diff(result: dict) -> str:
    out = []
    for rel in sorted(result["texts"]):
        old = result["old"][rel]
        out += difflib.unified_diff(
            (old or "").split("\n"), result["texts"][rel].split("\n"),
            f"a/{rel}" if old is not None else "/dev/null", f"b/{rel}", lineterm="")
    return "\n".join(out)


def listing(versions=None, steps=None) -> str:
    versions = versions or schema_version.VERSIONS
    steps = STEPS if steps is None else steps
    lines = [f"model format versions (current: {versions[-1]}):"]
    for i, v in enumerate(versions):
        lines.append(f"  {v}")
        if i + 1 < len(versions):
            d = steps.get((v, versions[i + 1]))
            lines.append(f"    -> {versions[i + 1]}: {d[0] if d else 'NO MIGRATION REGISTERED'}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("model", nargs="?", default="model")
    ap.add_argument("--to", help="target version (default: the current one)")
    ap.add_argument("--write", action="store_true", help="rewrite the model files")
    ap.add_argument("--list", action="store_true", help="list versions and steps")
    a = ap.parse_args(argv)
    if a.list:
        print(listing())
        return 0
    try:
        result = migrate(a.model, a.to, a.write)
    except MigrationError as e:
        print(f"migrate: {e}", file=sys.stderr)
        return 1
    if not result["steps"]:
        print(f"migrate: {a.model} is at {result['from']}"
              + (", the current version" if result["to"] == schema_version.CURRENT else "")
              + ": nothing to do")
        return 0
    for a_, b_, description in result["steps"]:
        print(f"migrate: {a_} -> {b_}: {description}")
    for n in result["notes"]:
        print(f"migrate: note: {n}", file=sys.stderr)
    if a.write:
        print(f"migrate: wrote {len(result['texts'])} file(s): "
              + ", ".join(sorted(result["texts"])))
    else:
        print(diff(result))
        print(f"migrate: {len(result['texts'])} file(s) would change; nothing written "
              "(pass --write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
