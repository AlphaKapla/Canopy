#!/usr/bin/env python3
"""The versions of the model format, and what a model's version means (FR-62).

`VERSIONS` lists every published version of the format, oldest first;
`CURRENT` is the one this Canopy reads and writes. A model states its
version in `model.yaml` (`schema_version`); the validator refuses any
other than `CURRENT` (`version_problem`), and `ci/migrate.py` rewrites a
model of an older version step by step.

The format is pinned: `format_digest()` is a SHA-256 over the JSON Schema
(as data: its layout does not count) and the field lists the tools keep in
code for what the schema does not cover (the manifest's keys). `ci/fixtures/schema-versions.json`
records that digest for every version. `ci/test_migrate.py` fails when
the format changes without a new version — so a change to the format is
a deliberate act: add the version here, record its digest, and register
the migration from the previous one in `ci/migrate.py`.

The digest sees the declared format, not every rule: a rule the validator
enforces in code (a reference check, a unit rule) can change without
changing it.
"""
import hashlib
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCHEMA = os.path.join(ROOT, "schema", "psa-model.schema.json")
PINS = os.path.join(HERE, "fixtures", "schema-versions.json")

VERSIONS = ["0.1.0", "0.2.0"]
CURRENT = VERSIONS[-1]


def version_problem(version, versions=None):
    """None when `version` is the current one, else why not and what to do."""
    versions = versions or VERSIONS
    current = versions[-1]
    if version is None:
        return f"schema_version is missing (this Canopy reads {current})"
    if not isinstance(version, str):
        return (f"schema_version must be a quoted string such as \"{current}\", "
                f"not {version!r}")
    if version == current:
        return None
    if version in versions:
        return (f"schema_version {version} is older than the format this Canopy reads "
                f"({current}): run `python ci/canopy.py migrate --write`")
    return (f"schema_version {version!r} is not a version this Canopy knows "
            f"({', '.join(versions)}): the model was written for another Canopy")


def format_digest(schema_path: str = SCHEMA) -> str:
    """SHA-256 of the declared model format (see the module docstring)."""
    import canonical
    import validate
    with open(schema_path, encoding="utf-8") as f:
        schema = json.load(f)
    declared = {
        "schema": schema,
        "manifest_keys": sorted(validate.MANIFEST_KEYS),
        "manifest_model_keys": sorted(validate.MANIFEST_MODEL_KEYS),
        "manifest_objects": canonical.MANIFEST_OBJECTS,
    }
    text = json.dumps(declared, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pins() -> dict:
    with open(PINS, encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, HERE)
    print(f"{CURRENT}  {format_digest()}")
