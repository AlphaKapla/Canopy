#!/usr/bin/env python3
"""Tests for ci/canonical.py (FR-59): the canonical form of model files.

  * a hand-written canonical document covering every rule is a fixed
    point, and a messy rendering of the same data (keys out of order,
    block where flow is canonical, odd spacing and quoting, a folded text
    wrapped elsewhere) is rewritten into exactly it, comments in place;
  * numbers: every float is written so that both YAML readers get the
    same float back, in the documented notation;
  * strings: quoting rules, and every string reads back as itself;
  * random data (300 documents mixing schema objects, ID-keyed mappings,
    lists, every scalar type): dump -> load gives the same data, types
    and ID order included, and the text is a fixed point;
  * comments are kept with the key or list item they annotate (one
    inside a flow collection moves out with it), and `#` inside a quoted
    string or a block scalar is not a comment;
  * refusals: a date, a non-string key, a NaN, lists nested three deep, a
    duplicate key, a quoted string over two lines;
  * key order comes from the schema: every object of the schema and of
    the manifest orders its own keys as declared;
  * the command: --check fails with the first difference, --write fixes
    it, and the committed model is canonical.

Usage: python ci/test_canonical.py
"""
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import canonical as cn  # noqa: E402
import validate  # noqa: E402

CANONICAL = '''\
# Header, kept as written:
#   indented text   and   spacing
fault_trees:

  # a comment above an entity stays above it
  FT-B:
    label: "Second tree, written first: ID order is data"
    top_gate: GT-TOP  # a comment at the end of a line stays there
    gates:

      GT-TOP:
        label: "5"
        formula:
          or:
            - BE-ONE
            # above a list item
            - BE-TWO  # on a list item
            - GT-VOTE

      GT-VOTE:
        label: "yes"
        formula:
          atleast:
            k: 2
            of: [BE-A, BE-B, BE-C]

  FT-A:
    label: >-
      A label long enough that it does not fit on its line and is folded at eighty-eight
      columns, word by word
    top_gate: GT-A
    gates:

      GT-A:
        label: "pass-through"
        formula: BE-ONE
# after the last key
'''

MESSY = '''\
# Header, kept as written:
#   indented text   and   spacing
fault_trees:
  # a comment above an entity stays above it
  FT-B:
    gates:
      GT-TOP:
        formula: {or: [BE-ONE,
            # above a list item
            BE-TWO,   # on a list item
            GT-VOTE]}
        label: '5'
      GT-VOTE:
        formula:
          atleast: {of: [BE-A, BE-B, BE-C], k: 2}
        label: "yes"
    top_gate:   GT-TOP       # a comment at the end of a line stays there
    label: 'Second tree, written first: ID order is data'
  FT-A:
    top_gate: "GT-A"
    label: >-
      A label long enough that it does not fit on its line
      and is folded at eighty-eight columns,
      word by word
    gates: {GT-A: {formula: BE-ONE, label: pass-through}}
# after the last key
'''

EVENT_TREE = '''\
event_tree:
  id: ET-X
  label: "An event tree"

  initiating_event:
    id: IE-X
    label: "Initiator"
    frequency:
      value: 5.0e-4
      unit: per_year
      uncertainty:
        distribution: lognormal
        error_factor: 6.0
    provenance:
      source: "A source"
      justification: "A reason"

  functional_events:

    FE-B:
      label: "Second column first"
      top_gate: GT-B

    FE-A:
      label: "First"
      top_gate: GT-A

  sequences:

    SEQ-1:
      path: {FE-B: success, FE-A: failure}
      end_state: CD
      house_events: {HE-X: true}

    SEQ-2:
      path: {FE-B: failure, FE-A: bypassed}
      end_state: OK
'''


def main() -> int:
    failures = []

    def check(cond, msg):
        (print(f"  ok  {msg}") if cond else failures.append(msg))

    def refused(text, needle, msg):
        try:
            cn.canonical_text(text)
            failures.append(f"{msg}: accepted")
        except cn.CanonError as e:
            check(needle in str(e), f"refused: {msg} ({str(e)[:70]})")

    # --- fixtures
    for name, doc in (("fault trees", CANONICAL), ("event tree", EVENT_TREE)):
        check(cn.canonical_text(doc) == doc, f"{name}: the canonical document is a fixed point")
    got = cn.canonical_text(MESSY)
    check(got == CANONICAL, "a messy rendering of the same data is rewritten into the "
          "canonical document, comments in place"
          + ("" if got == CANONICAL else ": " + cn.first_difference(got, CANONICAL)))
    h, d, c = cn.loads(CANONICAL)
    check(list(d["fault_trees"]) == ["FT-B", "FT-A"]
          and list(d["fault_trees"]["FT-B"]["gates"]) == ["GT-TOP", "GT-VOTE"]
          and d["fault_trees"]["FT-B"]["gates"]["GT-TOP"]["label"] == "5"
          and d["fault_trees"]["FT-B"]["gates"]["GT-VOTE"]["label"] == "yes"
          and len(h) == 2 and c.tail == [" after the last key"]
          and c.after[("fault_trees", "FT-B", "top_gate")].strip().startswith("a comment at")
          and ("fault_trees", "FT-B", "gates", "GT-TOP", "formula", "or", 1) in c.before,
          "ID order, the strings \"5\" and \"yes\", header, and each comment's key are read back")
    et = cn.loads(EVENT_TREE)[1]["event_tree"]
    check(list(et["functional_events"]) == ["FE-B", "FE-A"]
          and et["initiating_event"]["frequency"]["value"] == 5.0e-4
          and et["sequences"]["SEQ-1"]["house_events"] == {"HE-X": True},
          "event tree: column order kept, 5.0e-4 a float, overrides in flow")
    # a quantity without uncertainty is written in flow; with it, in block
    q = cn.dump({"parameters": {"PAR-X": {"label": "x", "value": {"value": 3e-5,
                                                                  "unit": "per_hour"}}}})
    check("    value: {value: 3.0e-5, unit: per_hour}\n" in q,
          "a quantity of scalars on one line: {value: 3.0e-5, unit: per_hour}")
    wide = cn.dump({"x": {"members": [f"BE-LONG-NAME-{i}" for i in range(9)]}})
    check("members:\n    - BE-LONG-NAME-0\n" in wide and all(len(l) <= 88 for l in wide.split("\n")),
          "a list too long for 88 columns goes to block style")
    edge = "x: [" + ", ".join(["BE-AAAAAA"] * 7)
    edge += ", BE-" + "A" * (88 - len(edge) - 6) + "]"
    check(len(edge) == 88 and cn.dump(yaml.safe_load(edge)) == edge + "\n"
          and cn.dump(yaml.safe_load(edge[:-1] + "A]")).count("\n") == 9,
          "exactly 88 columns stays on one line, 89 does not")

    words = " ".join(f"w{'o' * (n % 9)}rd{n}" for n in range(120))
    lines = cn.dump({"x": {"FT-A": {"label": words}}}).split("\n")[4:-1]
    check(lines and all(len(l) <= 88 for l in lines)
          and all(len(a) + 1 + len(b.split()[0]) > 88 for a, b in zip(lines, lines[1:]))
          and " ".join(l.strip() for l in lines) == words,
          "folded text: every line within 88 columns and as full as the next word allows")

    # --- numbers
    xs = [0.0, 1.0, 24.0, 0.9787, 0.01, 0.0099, 9999.5, 1e4, 1.5e4, 3e-5, 1.2e-3, 6e-3,
          5e-4, 1e-300, 1.7976931348623157e308, 5e-324, 0.1 + 0.2, 1 / 3, -2.5e-7, -3.0,
          123456.789, 1e22, 8760.0]
    r = random.Random(20260708)
    xs += [r.uniform(-1, 1) * 10 ** r.randint(-30, 30) for _ in range(2000)]
    bad = [x for x in xs if yaml.safe_load(cn.fmt_float(x)) != x
           or not isinstance(yaml.safe_load(cn.fmt_float(x)), float)
           or float(cn.fmt_float(x)) != x]
    check(not bad, f"{len(xs)} floats read back exactly, as floats ({bad[:3]})")
    check([cn.fmt_float(x) for x in (1.2e-3, 3e-5, 6e-3, 1.5e4, 0.9787, 24.0, 0.01, 1e22)]
          == ["1.2e-3", "3.0e-5", "6.0e-3", "1.5e+4", "0.9787", "24.0", "0.01", "1.0e+22"],
          "notation: 1.2e-3, 3.0e-5, 6.0e-3, 1.5e+4, 0.9787, 24.0, 0.01, 1.0e+22")
    check(cn.dump({"a": 1, "b": 1.0, "c": True, "d": None}) == "a: 1\nb: 1.0\nc: true\nd: null\n",
          "1 stays an integer, 1.0 a float, true and null as such")

    # --- strings
    strs = ["BE-X", "per_year", "a b", "", " ", "true", "No", "null", "~", "1", "1e3", "-x",
            ".5", "a:b", "a: b", "a #b", "#a", "é–ü", 'q"q', "back\\slash", "line\nbreak",
            "tab\there", "{a}", "[a]", "a,b", "*a", "&a", "!a", "%a", "@a", "`a", "a/b>c*",
            "2026-01-01", "0x1F", "0o7", "+1", "y", "N", "on", "OFF", "\x7f", "a" * 200]
    bad = [s for s in strs if yaml.safe_load(cn.dump({"k": s}))["k"] != s
           or yaml.safe_load(cn.dump({"k": [s]}))["k"] != [s]
           or yaml.safe_load(cn.dump({"label": s}))["label"] != s
           or list(yaml.safe_load(cn.dump({s: 1}))) != [s]]
    check(not bad, f"{len(strs)} awkward strings read back as themselves, as value, list "
          f"item, free text and key ({bad[:3]})")
    check(cn.dump({"unit": "per_year", "label": "per_year", "k": "true", "m": "12", "n": 1})
          == 'unit: per_year\nlabel: "per_year"\nk: "true"\nm: "12"\n"n": 1\n',
          "plain identifiers; free text, keywords (as keys too) and number-like strings "
          "quoted")

    # --- random documents
    props = sorted({k for o in cn.objects() for k in o})
    ids = [f"{p}-{n}" for p in ("BE", "GT", "FT", "PAR", "HE", "SEQ") for n in ("A", "B", "C1")]

    def rnd(depth):
        t = r.random()
        if depth > 3 or t < 0.45:
            return r.choice([r.choice(strs), r.choice(ids), r.randint(-5, 5), True, False,
                             None, r.uniform(0, 1) * 10 ** r.randint(-9, 6), 2.0])
        if t < 0.65:
            items = [rnd(depth + 1) for _ in range(r.randint(0, 4))]
            return [x for x in items if not isinstance(x, list)]
        keys = r.sample(props, r.randint(0, 4)) if r.random() < 0.5 else \
            r.sample(ids + ["free name", "value"], r.randint(0, 4))
        return {k: rnd(depth + 1) for k in keys}

    bad, changed = [], 0
    for n in range(300):
        doc = {k: rnd(0) for k in r.sample(props + ids, r.randint(1, 5))}
        try:
            text = cn.dump(doc)
            back = cn.loads(text)[1]
            ok = (back == doc and cn.type_shape(back) == cn.type_shape(doc)
                  and cn.canonical_text(text) == text
                  and yaml.load(text, Loader=validate.StrictLoader) == doc)
            changed += yaml.safe_dump(doc) != text
        except cn.CanonError as e:
            ok = False
            text = str(e)
        if not ok:
            bad.append((doc, text))
    check(not bad and changed > 250, f"300 random documents: dump -> load is the same data "
          f"(types and ID order), the text a fixed point ({bad[:1]})")

    # --- comments
    doc = ('basic_events:\n\n  BE-A:\n    label: "a # not a comment"  # a comment\n'
           '    provenance:\n      source: "s"\n      justification: >-\n'
           + "        " + " ".join(["word"] * 14) + "\n        # not a comment: folded text\n")
    got = cn.loads(doc)
    check(got[2].after == {("basic_events", "BE-A", "label"): " a comment"}
          and not got[2].before and not got[2].tail
          and got[1]["basic_events"]["BE-A"]["provenance"]["justification"].endswith(
              "# not a comment: folded text")
          and got[1]["basic_events"]["BE-A"]["label"] == "a # not a comment",
          "`#` inside a quoted string or a folded text is not a comment")
    check(cn.canonical_text(doc) == doc.replace("word\n        # not", "word #\n        not")
          or cn.loads(cn.canonical_text(doc))[1] == got[1],
          "... and the text reads back unchanged after rewriting")
    refused('a:\n  b: "x\n    y"\n', "one line", "a quoted string over two lines")
    check(cn.canonical_text("a: [1, 2,  # c\n  3]\n") == "a: [1, 2, 3]  # c\n",
          "a comment inside a flow list moves to the end of its key's line")

    # --- refusals
    refused("a: 2026-01-01\n", "not model data", "a date")
    refused("a: {1: x}\n", "not a string", "a non-string key")
    refused("a: .nan\n", "non-finite", "a NaN")
    refused("a: [[[1, 2]]]\n", "list directly inside a list", "a list of lists of lists")
    check(cn.canonical_text("a: [[1, 2], [3]]\n") == "a:\n  - [1, 2]\n  - [3]\n",
          "a list of lists of scalars: one per line")
    refused("a: 1\na: 2\n", "duplicate key", "a duplicate key")
    refused("- a\n- b\n", "holds a mapping", "a list at the top")

    # --- key order from the schema
    bad = []
    for o in cn.objects():
        shuffled = list(o)
        r.shuffle(shuffled)
        got = [k for k, _ in cn.ordered({k: 1 for k in shuffled})]
        want = cn.object_order(o)
        if got != want or set(want) != set(o):
            bad.append((o, got))
    check(not bad and len(cn.objects()) > 20,
          f"each of the {len(cn.objects())} objects of the schema and manifest orders its "
          f"keys as one declared object does ({bad[:1]})")
    check(cn.object_order(["top_gate", "label"])[:2] == ["label", "top_gate"]
          and cn.object_order(["param"]) == ["param"]
          and cn.object_order(["unit", "value"]) == ["value", "unit", "uncertainty"]
          and cn.object_order(["BE-A", "label"]) is None
          and cn.object_order([]) is None,
          "smallest object holding the keys; none for ID-keyed mappings")
    check(validate.MANIFEST_KEYS == set(cn.MANIFEST_OBJECTS[0]),
          "the manifest's keys are the validator's MANIFEST_KEYS")

    # --- command
    tmp = tempfile.mkdtemp(prefix="psa-canon-")
    try:
        run = lambda *a: subprocess.run([sys.executable, os.path.join(HERE, "canonical.py"), *a],
                                        capture_output=True, text=True)
        p = run(os.path.join(ROOT, "model"), "--check")
        check(p.returncode == 0 and "12 file(s) canonical" in p.stdout,
              f"the committed model is canonical ({(p.stdout + p.stderr).strip()[-80:]})")
        m = os.path.join(tmp, "model")
        shutil.copytree(os.path.join(ROOT, "model"), m)
        f = os.path.join(m, "parameters.yaml")
        text = open(f).read()
        open(f, "w").write(text.replace("parameters:\n", "parameters:\n\n\n", 1))
        p = run(m, "--check")
        check(p.returncode == 1 and "parameters.yaml: not canonical — line" in p.stderr
              and "--write" in p.stderr, "--check fails on an extra blank line, naming the line")
        p = run(m, "--write")
        check(p.returncode == 0 and "1 rewritten" in p.stdout and open(f).read() == text
              and run(m, "--check").returncode == 0, "--write restores the file; --check passes")
        open(f, "w").write("parameters: [\n")
        check(run(m, "--write").returncode == 1 and open(f).read() == "parameters: [\n",
              "an unreadable file is reported and left alone")
        p = subprocess.run([sys.executable, os.path.join(HERE, "canopy.py"), "canon"],
                           capture_output=True, text=True, cwd=ROOT)
        check(p.returncode == 0 and "12 file(s) canonical" in p.stdout,
              "`canopy canon` checks the committed model")
        check(run(tmp, "--check").returncode == 2, "a directory without model.yaml is refused")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("canonical: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
