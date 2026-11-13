#!/usr/bin/env python3
"""Canonical YAML for Canopy model files (V&V FR-59): one way to write any
model, so that `dump(load(file))` is the file, byte for byte.

A model file is a function of its data: the same data always gives the
same bytes, whoever or whatever wrote it (an analyst, `canopy expand`, an
importer, later the SQL projection and Studio). `canopy canon --check`
fails on any file that is not in canonical form; `canopy canon --write`
puts it there.

Canonical form (version 1):

  comments Comments are kept, attached to what they annotate. The comment
           lines at the very top of the file are its header, kept
           verbatim. Below it, a comment on its own line belongs to the
           next key or list item and is written just above it, at its
           indentation; a comment at the end of a line belongs to the key
           or list item that starts the line and is written after it,
           two spaces away. The text after `#` is kept as written
           (trailing blanks stripped). Comments after the last key are
           written at the end, unindented.
  layout   Block style, two spaces per level, UTF-8, one final newline,
           no tabs, anchors, aliases, tags or multiple documents.
  keys     The keys of a schema object (a mapping whose keys are all
           property names of one object of schema/psa-model.schema.json,
           or of the manifest) are written in that object's property
           order — the smallest such object, the first in the schema when
           several are as small. Every other mapping (keyed by IDs or by
           free names) keeps the order it was given: that order is data
           (the columns of an event tree, the reading order of a file).
  flow     A non-empty mapping of scalars is written on one line,
           `{value: 5.0e-4, unit: per_year}`, when its keys are among
           value / unit / param or none of them is a schema property name
           (an event-tree path, house-event overrides). A non-empty list
           of scalars is written `[A, B]`. Either falls back to block
           style when the line would pass 88 columns (a comment at its
           end not counted), or when a comment is attached to one of its
           keys or items. `{}` and `[]` are the empty mapping and list.
  blanks   One blank line before a key whose value is a block (written
           over several lines) when the key is at the top two levels or
           is an entry of a mapping that is not a schema object (an
           entity among its siblings); it goes above the key's comments.
           Nowhere else.
  scalars  `true` / `false`, `null`, integers in decimal. Floats as
           analysts write them and read back exactly: scientific notation
           with the fewest digits that round-trip below 1e-2 and from 1e4
           (`3.0e-5`, `1.5e+4`), else the shortest round-trip decimal
           (`0.9787`, `24.0`); always with a dot and a signed exponent.
           Strings in double quotes with JSON-style escapes, except that
           a string of [A-Za-z0-9_.-] (and `/`, `>` or `*` after the
           first character) that is not a YAML keyword and does not start
           like a number is written plain — unless its key is free text
           (label, source, justification, name, description), which is always
           quoted.

  prose    A free-text value too long for its line is folded: `key: >-`
           (`>` when the text ends with one newline) and the words on the
           following lines, two spaces in, as many per line as fit in 88
           columns. Only text made of words separated by single spaces
           folds; anything else stays on one quoted line.

Loading is the validator's strict loader (duplicate keys refused). The
data model is JSON's: mappings with string keys, lists, strings, booleans,
integers, finite floats and null; anything else (a date, a NaN, a
non-string key) is refused.

Usage:
  canonical.py MODEL_DIR --check     exit 1 if a model file is not canonical
  canonical.py MODEL_DIR --write     rewrite the files that are not
  canonical.py FILE.yaml ... --check | --write
"""
import argparse
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

WIDTH = 88
FLOW_KEYS = {"value", "unit", "param"}
QUOTED_KEYS = {"label", "source", "justification", "name", "description"}
PLAIN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-/>*]*$")
RESERVED = {"true", "false", "yes", "no", "on", "off", "null", "y", "n", "~"}
NUMBER_LIKE = re.compile(r"^[-+.]?[0-9]")

# The manifest (model.yaml) has no JSON Schema (validate.py: MANIFEST_KEYS);
# its objects, in the order the documentation gives their keys.
MANIFEST_OBJECTS = [
    ["schema_version", "model", "includes", "configurations"],
    ["id", "name", "description", "scope", "risk_metrics"],
    ["level", "hazard", "plant_state"],
    ["id", "label", "end_states"],
    ["parameters", "house_events", "ccf_groups", "basic_events", "fault_trees",
     "event_trees"],
    ["label", "house_events", "parameters"],
]


class CanonError(Exception):
    pass


# ------------------------------------------------------------- key order
def schema_objects(schema) -> list:
    """Property-name lists of every object in the schema, in document
    order, then the manifest's."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict) and props:
                found.append(list(props))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return found + [list(o) for o in MANIFEST_OBJECTS]


_OBJECTS = None


def objects() -> list:
    global _OBJECTS
    if _OBJECTS is None:
        with open(os.path.join(ROOT, "schema", "psa-model.schema.json")) as f:
            _OBJECTS = schema_objects(json.load(f))
    return _OBJECTS


_ORDER_CACHE, _PROPERTY_NAMES = {}, None


def object_order(keys):
    """The property order of the smallest schema object holding all of
    `keys` (first in the schema among equals); None when there is none."""
    ks = frozenset(keys)
    if not ks:
        return None
    if ks not in _ORDER_CACHE:
        best = None
        if all(is_property(k) for k in ks):
            for props in objects():
                if ks <= set(props) and (best is None or len(props) < len(best)):
                    best = props
        if len(_ORDER_CACHE) > 100_000:     # ID-keyed mappings are all different
            _ORDER_CACHE.clear()
        _ORDER_CACHE[ks] = best
    return _ORDER_CACHE[ks]


def is_property(key) -> bool:
    global _PROPERTY_NAMES
    if _PROPERTY_NAMES is None:
        _PROPERTY_NAMES = {k for props in objects() for k in props}
    return key in _PROPERTY_NAMES


def ordered(m: dict) -> list:
    """(key, value) pairs of a mapping in canonical order."""
    order = object_order(m.keys())
    if order is None:
        return list(m.items())
    return [(k, m[k]) for k in order if k in m]


# --------------------------------------------------------------- scalars
def fmt_float(x: float) -> str:
    """See the module docstring. PyYAML's YAML 1.1 resolver reads `3e-05`
    or `1.5e4` as strings, hence the dot and the signed exponent."""
    if x != x or x in (float("inf"), float("-inf")):
        raise CanonError(f"non-finite number {x}")
    if x != 0.0 and (abs(x) < 1e-2 or abs(x) >= 1e4):
        for p in range(0, 18):
            t = f"{x:.{p}e}"
            if float(t) == x:
                break
        m, e = t.split("e")
        if "." not in m:
            m += ".0"
        sign, digits = e[0], e[1:].lstrip("0") or "0"
        return f"{m}e{sign}{digits}"
    s = repr(x)
    return s if "." in s else s + ".0"


def fmt_string(s: str, quote: bool) -> str:
    if (not quote and PLAIN.match(s) and s.lower() not in RESERVED
            and not NUMBER_LIKE.match(s)):
        return s
    return '"' + "".join(_escape(c) for c in s) + '"'


_ESCAPES = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\t": "\\t", "\r": "\\r"}


def _escape(c: str) -> str:
    """One character of a double-quoted scalar: itself when YAML calls it
    printable, an escape otherwise (V&V D-44: a raw DEL or C1 control is
    not readable YAML)."""
    if c in _ESCAPES:
        return _ESCAPES[c]
    n = ord(c)
    if (0x20 <= n <= 0x7E or 0xA0 <= n <= 0xD7FF or 0xE000 <= n <= 0xFFFD
            or 0x10000 <= n <= 0x10FFFF) and n not in (0x2028, 0x2029, 0xFEFF):
        return c
    return f"\\x{n:02x}" if n <= 0xFF else (f"\\u{n:04x}" if n <= 0xFFFF else f"\\U{n:08x}")


def folded(s: str, indent: int):
    """Lines of the folded form of a long free-text value at `indent`
    (first the indicator, `>-` or `>`), or None when it does not fold."""
    body = s[:-1] if s.endswith("\n") else s
    words = body.split(" ")
    if (not body or "\n" in body or any(w == "" for w in words)
            or any(_escape(c) != c for c in body if c not in '"\\')):
        return None
    pad = " " * indent
    lines, cur = [], ""
    for w in words:
        if cur and len(pad) + len(cur) + 1 + len(w) > WIDTH:
            lines.append(pad + cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    lines.append(pad + cur)
    return [">" if s.endswith("\n") else ">-"] + lines


def is_scalar(v) -> bool:
    return v is None or isinstance(v, (str, bool, int, float))


def fmt_scalar(v, key=None) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return fmt_float(v)
    if v is None:
        return "null"
    if isinstance(v, str):
        return fmt_string(v, key in QUOTED_KEYS)
    raise CanonError(f"{type(v).__name__} value {v!r} is not model data "
                     "(strings, numbers, booleans, null, mappings and lists only)")


def fmt_key(k) -> str:
    if not isinstance(k, str):
        raise CanonError(f"mapping key {k!r} is not a string")
    return fmt_string(k, False)


# ------------------------------------------------------------------ dump
class Comments:
    """Comments of a file below its header: `before[path]` (own-line
    comments above the key or item at `path`), `after[path]` (the comment
    ending its line) and `tail` (after the last key). A path is the tuple
    of keys and list indices from the root. Texts are what follows `#`."""

    def __init__(self):
        self.before, self.after, self.tail = {}, {}, []
        self._inside = None

    def any_below(self, path) -> bool:
        """Whether a comment is attached strictly below `path`."""
        if self._inside is None:
            self._inside = set()
            for p in list(self.before) + list(self.after):
                for n in range(len(p)):
                    self._inside.add(p[:n])
        return path in self._inside

    def texts(self) -> list:
        out = [c for cs in self.before.values() for c in cs]
        return sorted(out + list(self.after.values()) + list(self.tail))


def flow(v, key=None):
    """One-line form of a mapping or list of scalars, or None when the
    value is not written in flow style (whatever the width)."""
    if isinstance(v, dict):
        if not v:
            return "{}"
        if not all(is_scalar(x) for x in v.values()):
            return None
        if not (set(v) <= FLOW_KEYS or not any(is_property(k) for k in v)):
            return None
        return "{" + ", ".join(f"{fmt_key(k)}: {fmt_scalar(x, k)}"
                               for k, x in ordered(v)) + "}"
    if isinstance(v, list):
        if not v:
            return "[]"
        if not all(is_scalar(x) for x in v):
            return None
        return "[" + ", ".join(fmt_scalar(x, key) for x in v) + "]"
    raise CanonError(f"{type(v).__name__} value {v!r} is not model data")


class _Emitter:
    def __init__(self, comments: Comments):
        self.c = comments
        self.out = []

    def inline(self, prefix: str, v, path, key=None):
        """`prefix` + the one-line form of `v`, or None when `v` is a block."""
        if is_scalar(v):
            return prefix + fmt_scalar(v, key)
        if self.c.any_below(path) and v:
            return None
        f = flow(v, key)
        if f is not None and len(prefix) + len(f) <= WIDTH:
            return prefix + f
        return None

    def line(self, text: str, path) -> None:
        after = self.c.after.get(path)
        self.out.append(text + (f"  #{after}" if after is not None else ""))

    def before(self, pad: str, path) -> None:
        for text in self.c.before.get(path, ()):
            self.out.append(f"{pad}#{text}")

    def mapping(self, m: dict, indent: int, depth: int, path) -> None:
        pad = " " * indent
        entity_siblings = object_order(m.keys()) is None
        for k, v in ordered(m):
            p = path + (k,)
            head = f"{pad}{fmt_key(k)}:"
            text = self.inline(head + " ", v, p, k)
            if text is None and self.out and (depth <= 1 or entity_siblings):
                self.out.append("")
            self.before(pad, p)
            if (isinstance(v, str) and k in QUOTED_KEYS and len(text) > WIDTH
                    and p not in self.c.after):
                fold = folded(v, indent + 2)
                if fold is not None:
                    self.out.append(f"{head} {fold[0]}")
                    self.out.extend(fold[1:])
                    continue
            if text is not None:
                self.line(text, p)
                continue
            self.line(head, p)
            self.block(v, indent + 2, depth + 1, p, k)

    def block(self, v, indent: int, depth: int, path, key=None) -> None:
        if isinstance(v, dict):
            self.mapping(v, indent, depth, path)
            return
        pad = " " * indent
        for i, item in enumerate(v):
            p = path + (i,)
            self.before(pad, p)
            text = self.inline(pad + "- ", item, p, key)
            if text is not None:
                self.line(text, p)
            elif isinstance(item, dict):
                # first key on the dash line, the rest aligned under it
                mark = len(self.out)
                self.mapping(item, indent + 2, depth + 2, p)
                sub = [s for s in self.out[mark:] if s != ""]
                del self.out[mark:]
                first = sub[0]
                if first.lstrip().startswith("#"):
                    raise CanonError(f"comment above the first key of a list item "
                                     f"under {key!r}: put it above the dash")
                self.out.append(pad + "- " + first[indent + 2:]
                                + (f"  #{self.c.after[p]}" if p in self.c.after else ""))
                self.out.extend(sub[1:])
            else:
                raise CanonError(f"a list directly inside a list (under {key!r}) "
                                 "is not model data")


def dump(data, header=(), comments: Comments = None) -> str:
    """Canonical text of a model file: `header` comment lines, `data` (a
    mapping) with its `comments`."""
    if not isinstance(data, dict):
        raise CanonError("a model file holds a mapping")
    lines = []
    for h in header:
        h = h.rstrip()
        if not h.startswith("#"):
            raise CanonError(f"header line {h!r} is not a comment")
        lines.append(h)
    em = _Emitter(comments or Comments())
    em.mapping(data, 0, 0, ())
    tail = [f"#{text}" for text in em.c.tail]
    return "\n".join(lines + em.out + tail) + "\n"


# ------------------------------------------------------------------ load
def split_header(text: str):
    """(header comment lines, number of header lines)."""
    lines = text.split("\n")
    n = 0
    while n < len(lines) and lines[n].startswith("#"):
        n += 1
    return [ln.rstrip() for ln in lines[:n]], n


def scan_comments(text: str, skip: int) -> list:
    """(line index, own_line, text after `#`) of every comment from line
    `skip` on. Quote-aware; the lines of a block scalar are content."""
    found, block_indent = [], None
    for no, raw in enumerate(text.split("\n")):
        if no < skip:
            continue
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip(" "))
        if block_indent is not None:
            if stripped == "" or indent > block_indent:
                continue
            block_indent = None
        if stripped == "":
            continue
        if stripped.startswith("#"):
            found.append((no, True, stripped[1:].rstrip()))
            continue
        code, quote, i = raw, None, 0
        while i < len(raw):
            c = raw[i]
            if quote:
                if c == "\\" and quote == '"':
                    i += 1
                elif c == quote:
                    quote = None
            elif c in "\"'":
                quote = c
            elif c == "#" and raw[i - 1] in " \t":
                code = raw[:i]
                found.append((no, False, raw[i + 1:].rstrip()))
                break
            i += 1
        if quote:
            raise CanonError(f"line {no + 1}: a quoted string continues on the next "
                             "line; write it on one line")
        if re.search(r"(^|[\s:-])[|>][-+0-9]*\s*$", code):
            block_indent = indent
    return found


def node_lines(text: str) -> list:
    """(line index, path) of the first key or list item starting on each
    line, in document order."""
    import yaml
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as e:
        raise CanonError(f"YAML parse failure: {e}")
    seen, out = set(), []

    def note(node, path):
        ln = node.start_mark.line
        if ln not in seen:
            seen.add(ln)
            out.append((ln, path))

    def walk(node, path):
        if isinstance(node, yaml.MappingNode):
            for k, v in node.value:
                p = path + (k.value,)
                note(k, p)
                walk(v, p)
        elif isinstance(node, yaml.SequenceNode):
            for i, item in enumerate(node.value):
                p = path + (i,)
                note(item, p)
                walk(item, p)

    if root is not None:
        walk(root, ())
    return out


def loads(text: str):
    """(header, data, comments) of a model file's text; data by the
    validator's strict loader."""
    import yaml
    from validate import StrictLoader
    header, n = split_header(text)
    try:
        data = yaml.load(text, Loader=StrictLoader)
    except yaml.YAMLError as e:
        raise CanonError(f"YAML parse failure: {e}")
    cs = Comments()
    nodes = node_lines(text)
    by_line = dict(nodes)
    starts = [ln for ln, _ in nodes]
    import bisect
    for ln, own, ctext in scan_comments(text, n):
        if not own and ln in by_line:
            if by_line[ln] in cs.after:
                raise CanonError(f"line {ln + 1}: two comments for one key")
            cs.after[by_line[ln]] = ctext
            continue
        i = bisect.bisect_right(starts, ln)
        if i == len(starts):
            cs.tail.append(ctext)
        else:
            cs.before.setdefault(nodes[i][1], []).append(ctext)
    return header, data, cs


def canonical_text(text: str) -> str:
    """The canonical form of a model file's text: same data, same
    comments."""
    header, data, cs = loads(text)
    out = dump(data, header, cs)
    # nothing lost: the canonical text reads back as the same data, with
    # the same comments in the same places
    h2, d2, c2 = loads(out)
    if d2 != data or type_shape(d2) != type_shape(data):
        raise CanonError("internal error: canonical form reads back as different data")
    if h2 != header or (c2.before, c2.after, c2.tail) != (cs.before, cs.after, cs.tail):
        raise CanonError("a comment cannot be kept where it is (inside a flow "
                         "collection or a multi-line value?); move it onto its own line "
                         "above a key")
    return out


def type_shape(v):
    """The data with every scalar replaced by its type and mappings as
    ordered pairs: 1 and 1.0, or two orders of an ID-keyed mapping, differ."""
    if isinstance(v, dict):
        ordered_here = object_order(v.keys()) is None
        items = [(k, type_shape(x)) for k, x in v.items()]
        return ("map", tuple(items) if ordered_here else tuple(sorted(items, key=repr)))
    if isinstance(v, list):
        return ("list", tuple(type_shape(x) for x in v))
    return type(v).__name__


def model_files(model_dir: str) -> list:
    out = [os.path.join(model_dir, "model.yaml")]
    for name in ("parameters.yaml", "house-events.yaml", "ccf-groups.yaml"):
        p = os.path.join(model_dir, name)
        if os.path.exists(p):
            out.append(p)
    for sub in ("basic-events", "fault-trees", "event-trees"):
        out += sorted(glob.glob(os.path.join(model_dir, sub, "*.yaml")))
    return out


def first_difference(a: str, b: str) -> str:
    la, lb = a.split("\n"), b.split("\n")
    for n, (x, y) in enumerate(zip(la, lb), start=1):
        if x != y:
            return f"line {n}: file has {x!r}, canonical form has {y!r}"
    return (f"line {min(len(la), len(lb)) + 1}: the file "
            f"{'continues' if len(la) > len(lb) else 'ends'} where the canonical "
            f"form {'ends' if len(la) > len(lb) else 'continues'}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="+", help="a model directory, or YAML files")
    how = ap.add_mutually_exclusive_group(required=True)
    how.add_argument("--check", action="store_true")
    how.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)
    files = []
    for p in a.paths:
        if os.path.isdir(p):
            if not os.path.exists(os.path.join(p, "model.yaml")):
                print(f"canon: {p}: no model.yaml", file=sys.stderr)
                return 2
            files += model_files(p)
        else:
            files.append(p)
    bad = changed = 0
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            want = canonical_text(text)
        except (OSError, CanonError) as e:
            print(f"canon: {path}: {e}", file=sys.stderr)
            bad += 1
            continue
        if want == text:
            continue
        if a.check:
            print(f"canon: {path}: not canonical — {first_difference(text, want)}",
                  file=sys.stderr)
            bad += 1
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(want)
            print(f"canon: rewrote {path}")
            changed += 1
    if bad:
        print(f"canon: {bad} of {len(files)} file(s) "
              f"{'not canonical' if a.check else 'not written'}"
              + ("; run `python ci/canopy.py canon MODEL --write`" if a.check else ""),
              file=sys.stderr)
        return 1
    print(f"canon: {len(files)} file(s) canonical"
          + (f" ({changed} rewritten)" if a.write else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
