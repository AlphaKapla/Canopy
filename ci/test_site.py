#!/usr/bin/env python3
"""Tests for ci/build_site.py (FR-67): the documentation site.

  * every page of the site is written, with the style sheet and the
    .nojekyll marker; building twice gives byte-identical output;
  * every link inside the site resolves to a file that exists, no link
    to a Markdown file survives, an anchor is kept, and a link to a
    repository file that is not part of the site becomes a link to that
    file on GitHub at the revision being built;
  * the navigation is on every page, carries the revision, marks the
    current page and offers every other page and viewer;
  * the content is really rendered: tables become tables, fenced code
    becomes <pre>, and each page's own first heading is its document's;
  * the viewers are built when a model is given (needs the engine), and
    the pages link to them;
  * refusals: a model directory that does not exist, a --model argument
    without '=', a document missing from the repository.

Usage: python ci/test_site.py          (the viewer part needs the engine)
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import build_site as bs  # noqa: E402

ENGINE = os.environ.get("CANOPY_BIN") or os.path.join(
    ROOT, "engine", "target", "release", "canopy")


def files(d):
    out = {}
    for root, _, names in os.walk(d):
        for n in names:
            p = os.path.join(root, n)
            with open(p, "rb") as f:
                out[os.path.relpath(p, d).replace(os.sep, "/")] = f.read()
    return out


def main() -> int:
    failures = []
    tmp = tempfile.mkdtemp(prefix="canopy-site-")

    def check(cond, msg):
        (print(f"  ok  {msg}") if cond else failures.append(msg))

    # --- docs only
    out = os.path.join(tmp, "site")
    written = bs.build(out, revision="v9.9.9-test", models=[])
    have = files(out)
    want = {t for _, t, _, _ in bs.PAGES} | {"style.css", ".nojekyll"}
    check(set(have) == want and len(written) == len(want),
          f"{len(bs.PAGES)} pages, the style sheet and .nojekyll are written")

    again = os.path.join(tmp, "site2")
    bs.build(again, revision="v9.9.9-test", models=[])
    check(files(again) == have, "building twice gives byte-identical output")

    # --- links
    dangling, md_links, anchors, blobs = [], [], 0, []
    for name, raw in have.items():
        if not name.endswith(".html"):
            continue
        text = raw.decode("utf-8")
        for url in re.findall(r'href="([^"]+)"', text):
            if url.startswith("http"):
                if "/blob/v9.9.9-test/" in url:
                    blobs.append(url)
                continue
            if ".md" in url:
                md_links.append((name, url))
            path, _, frag = url.partition("#")
            if frag:
                anchors += 1
            if path and not os.path.exists(os.path.join(out, path)):
                dangling.append((name, url))
    check(not dangling, "every link inside the site resolves"
          + (f": {dangling[:4]}" if dangling else ""))
    check(not md_links, "no link to a Markdown file survives"
          + (f": {md_links[:4]}" if md_links else ""))
    check(anchors > 0, f"anchors are kept ({anchors} links carry one)")
    check(all("/blob/v9.9.9-test/" in u for u in blobs),
          f"every link to a repository file outside the site carries the revision "
          f"({len(blobs)} of them; today's documents link only to each other, so the "
          f"rewriting itself is checked on its own below)")

    # a link that the site does own is rewritten to the site's page
    index = have["index.html"].decode("utf-8")
    check('href="docs.html"' in index,
          "the README's link to docs/index.md points at the site's page")
    body = bs.rewrite_links('<a href="model-format.md#units">u</a>',
                            "docs/index.md", "r1", bs.REPO_URL)
    check(body == '<a href="model-format.html#units">u</a>',
          "a relative link with an anchor is rewritten, anchor kept")
    body = bs.rewrite_links('<a href="../schema/psa-model.schema.json">s</a>',
                            "docs/index.md", "r1", bs.REPO_URL)
    check(body == f'<a href="{bs.REPO_URL}/blob/r1/schema/psa-model.schema.json">s</a>',
          "a link out of the site becomes a GitHub link at the revision")
    body = bs.rewrite_links('<a href="https://x.test/a">x</a><a href="#top">t</a>',
                            "README.md", "r1", bs.REPO_URL)
    check(body == '<a href="https://x.test/a">x</a><a href="#top">t</a>',
          "an absolute link and a bare anchor are left alone")

    # --- navigation
    missing = []
    for name, raw in have.items():
        if not name.endswith(".html"):
            continue
        text = raw.decode("utf-8")
        if "v9.9.9-test" not in text or f'href="{name}" aria-current="page"' not in text:
            missing.append(name)
        for _, target, _, _ in bs.PAGES:
            if f'href="{target}"' not in text:
                missing.append(f"{name} -> {target}")
    check(not missing, "every page carries the revision, marks itself current and "
                       "links to every other page" + (f": {missing[:3]}" if missing else ""))

    # --- rendering
    vv = have["verification-validation.html"].decode("utf-8")
    fmt = have["model-format.html"].decode("utf-8")
    check(vv.count("<table>") > 5 and "<th>" in vv, "tables are rendered as tables")
    check("<pre><code>" in fmt and "basic_events:" in fmt,
          "fenced code blocks are rendered")
    check(have["reference-readme.html"].decode("utf-8").count("<h1") == 1
          and "generic PWR" in have["reference-readme.html"].decode("utf-8"),
          "the reference model's own README is a page of the site")
    check("<h1" in index and "Canopy" in index, "the landing page is the README")

    # --- viewers
    if os.path.exists(ENGINE):
        out3 = os.path.join(tmp, "site3")
        w = bs.build(out3, revision="v9.9.9-test", models=[("demo", "model")],
                     engine=ENGINE)
        page = os.path.join(out3, "viewer", "demo.html")
        check(os.path.exists(page) and os.path.getsize(page) > 50000
              and "viewer/demo.html" in w,
              f"a viewer is built for each model "
              f"({os.path.getsize(page) // 1024 if os.path.exists(page) else 0} KiB)")
        check('href="viewer/demo.html"' in open(
                  os.path.join(out3, "index.html"), encoding="utf-8").read(),
              "the navigation links to the viewer")
    else:
        print(f"  --  viewers skipped: engine not built ({ENGINE})")

    # --- refusals
    def refused(name, needle, **kw):
        d = os.path.join(tmp, "bad")
        try:
            bs.build(d, **kw)
            failures.append(f"refused: {name}: accepted")
        except bs.SiteError as e:
            check(needle in str(e), f"refused: {name} ({str(e).splitlines()[0][:60]})")

    refused("a model directory that does not exist", "no such model directory",
            models=[("x", "no-such-model")])
    pages = bs.PAGES[:]
    bs.PAGES.append(("docs/nothing-here.md", "nope.html", "Nope", "Start"))
    try:
        refused("a document missing from the repository", "no such document", models=[])
    finally:
        bs.PAGES[:] = pages
    r = subprocess.run([sys.executable, os.path.join(HERE, "build_site.py"),
                        os.path.join(tmp, "x"), "--model", "oops"],
                       capture_output=True, text=True)
    check(r.returncode == 2 and "expected NAME=DIR" in r.stderr,
          "refused: a --model argument without '=' (usage, exit 2)")
    r = subprocess.run([sys.executable, os.path.join(HERE, "build_site.py"),
                        os.path.join(tmp, "cli"), "--no-viewers", "--revision", "abc123"],
                       capture_output=True, text=True)
    check(r.returncode == 0 and "0 viewer(s)" in r.stdout and "abc123" in r.stdout,
          "`build_site.py --no-viewers` builds the documentation alone")

    shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print(f"\n{len(failures)} FAILED:")
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("\nsite: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
