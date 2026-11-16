#!/usr/bin/env python3
"""Build the documentation site: rendered docs plus a live model viewer (FR-67).

    python ci/build_site.py out/site --revision v0.3.0
    python ci/build_site.py out/site --no-viewers          # docs only, no engine
    python ci/build_site.py out/site --model demo=model --model ref=reference

Renders every document of the repository that is written for a reader
(the README, the docs directory, the changelog, the roadmap, the
reference model's own README) to HTML, rewrites the links between them,
and builds the interactive viewer for each model beside them, so that a
tag can be published as a site: the documentation as it was at that
revision, and the model as it was quantified there.

The site is a derived artifact. It is never committed: the workflow
`.github/workflows/pages.yml` builds it on a tag and deploys it to
GitHub Pages, and `ci/test_site.py` checks it.

A link to a file that is not part of the site (`CLAUDE.md`, a schema, a
script) is rewritten to that file on GitHub at the revision being built,
so no link dangles and none silently points at the wrong version.

Needs `markdown` (pip install markdown). Rendering is deliberately the
only dependency: everything else here is link rewriting and a style
sheet.
"""
import argparse
import html
import os
import re
import shutil
import subprocess
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPO_URL = "https://github.com/AlphaKapla/Canopy"

# source path -> (output file, nav title, nav group). Order is nav order.
PAGES = [
    ("README.md", "index.html", "Canopy", "Start"),
    ("docs/index.md", "docs.html", "Documentation map", "Start"),
    ("docs/getting-started.md", "getting-started.html", "Getting started", "Start"),
    ("docs/model-format.md", "model-format.html", "Model format", "Authoring"),
    ("docs/reference-model.md", "reference-model.html", "The reference model",
     "Authoring"),
    ("reference/README.md", "reference-readme.html", "Reference model: the model itself",
     "Authoring"),
    ("docs/riskspectrum-import.md", "riskspectrum-import.html", "RiskSpectrum import",
     "Authoring"),
    ("docs/quantification.md", "quantification.html", "Quantification", "Tools"),
    ("docs/visualization.md", "visualization.html", "Visualisation", "Tools"),
    ("docs/ci.md", "ci.html", "The CI pipeline", "Tools"),
    ("docs/architecture.md", "architecture.html", "Architecture", "Tools"),
    ("docs/verification-validation.md", "verification-validation.html",
     "V&V report", "Assurance"),
    ("docs/limitations.md", "limitations.html", "Limitations", "Assurance"),
    ("ROADMAP.md", "roadmap.html", "Roadmap", "Assurance"),
    ("CHANGELOG.md", "changelog.html", "Changelog", "Assurance"),
]
MODELS = [("demo", "model"), ("reference", "reference")]

CSS = """
:root {
  --bg: #fbfbf9; --fg: #1a1a18; --muted: #5c5c56; --rule: #dedcd4;
  --accent: #1f5c3a; --code-bg: #f1f0ea; --nav-bg: #f4f3ed;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #15161a; --fg: #e6e5e0; --muted: #a2a19a; --rule: #32343a;
    --accent: #7cc49b; --code-bg: #1e2026; --nav-bg: #1a1c21;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
.wrap { display: flex; min-height: 100vh; align-items: stretch; }
nav {
  width: 16rem; flex: 0 0 16rem; background: var(--nav-bg);
  border-right: 1px solid var(--rule); padding: 1.5rem 1rem; overflow-y: auto;
}
nav .brand { font-weight: 600; font-size: 1.1rem; display: block; margin-bottom: .2rem; }
nav .rev { color: var(--muted); font-size: .8rem; font-family: ui-monospace, monospace; }
nav h2 {
  font-size: .72rem; text-transform: uppercase; letter-spacing: .08em;
  color: var(--muted); margin: 1.4rem 0 .4rem;
}
nav a { display: block; padding: .18rem 0; color: var(--fg); text-decoration: none; }
nav a:hover { color: var(--accent); }
nav a[aria-current] { color: var(--accent); font-weight: 600; }
main { flex: 1 1 auto; min-width: 0; padding: 2.5rem 2rem 6rem; }
article { max-width: 46rem; margin: 0 auto; }
article > :first-child { margin-top: 0; }
h1, h2, h3, h4 { line-height: 1.25; margin: 2rem 0 .6rem; }
h1 { font-size: 1.9rem; } h2 { font-size: 1.4rem; } h3 { font-size: 1.1rem; }
h2 { border-bottom: 1px solid var(--rule); padding-bottom: .25rem; }
a { color: var(--accent); }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .88em; }
code { background: var(--code-bg); padding: .1em .32em; border-radius: 3px; }
pre { background: var(--code-bg); padding: .9rem 1rem; border-radius: 5px; overflow-x: auto; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; display: block; overflow-x: auto; }
th, td { border: 1px solid var(--rule); padding: .35rem .6rem; text-align: left; vertical-align: top; }
th { background: var(--code-bg); }
/* a Markdown table written without a header renders an empty header row */
thead tr:has(th:empty):not(:has(th:not(:empty))) { display: none; }
blockquote { border-left: 3px solid var(--rule); margin: 1rem 0; padding: .2rem 1rem; color: var(--muted); }
hr { border: none; border-top: 1px solid var(--rule); margin: 2rem 0; }
footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--rule);
         color: var(--muted); font-size: .85rem; }
@media (max-width: 52rem) {
  .wrap { flex-direction: column; }
  nav { width: auto; flex: none; border-right: none; border-bottom: 1px solid var(--rule); }
  main { padding: 1.5rem 1rem 4rem; }
}
"""


class SiteError(Exception):
    pass


def nav_html(current: str, revision: str, viewers: list) -> str:
    out = [f'<nav><a class="brand" href="index.html">Canopy</a>'
           f'<span class="rev">{html.escape(revision)}</span>']
    group = None
    for _, target, title, g in PAGES:
        if g != group:
            out.append(f"<h2>{html.escape(g)}</h2>")
            group = g
        cur = ' aria-current="page"' if target == current else ""
        out.append(f'<a href="{target}"{cur}>{html.escape(title)}</a>')
    if viewers:
        out.append("<h2>Model viewer</h2>")
        for name in viewers:
            out.append(f'<a href="viewer/{name}.html">{html.escape(name)}</a>')
    out.append("</nav>")
    return "".join(out)


def rewrite_links(body: str, source: str, revision: str, repo_url: str) -> str:
    """Point every link at its page in the site, or at GitHub for the rest."""
    by_source = {s: t for s, t, _, _ in PAGES}
    base = os.path.dirname(source)

    def one(m):
        attr, url = m.group(1), m.group(2)
        if re.match(r"^(?:[a-z+.-]+:|//|#|mailto:)", url, re.I):
            return m.group(0)
        path, _, frag = url.partition("#")
        frag = "#" + frag if frag else ""
        if not path:
            return m.group(0)
        resolved = os.path.normpath(os.path.join(base, urllib.parse.unquote(path)))
        resolved = resolved.replace(os.sep, "/")
        if resolved in by_source:
            return f'{attr}"{by_source[resolved]}{frag}"'
        if path.endswith(".md") or os.path.exists(os.path.join(ROOT, resolved)):
            return f'{attr}"{repo_url}/blob/{revision}/{resolved}{frag}"'
        return m.group(0)

    return re.sub(r'\b(href=)"([^"]*)"', one, body)


def render(source: str, revision: str, repo_url: str) -> str:
    try:
        import markdown
    except ImportError:
        raise SiteError("the `markdown` package is needed to render the docs "
                        "(pip install markdown)")
    path = os.path.join(ROOT, source)
    if not os.path.exists(path):
        raise SiteError(f"{source}: no such document")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    body = markdown.markdown(text, extensions=[
        "tables", "fenced_code", "sane_lists", "attr_list", "toc", "md_in_html"])
    return rewrite_links(body, source, revision, repo_url)


def page(title: str, body: str, current: str, revision: str, viewers: list,
         repo_url: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — Canopy</title>
<link rel="stylesheet" href="style.css">
</head><body><div class="wrap">
{nav_html(current, revision, viewers)}
<main><article>
{body}
<footer>Canopy documentation, built from
<a href="{repo_url}/tree/{html.escape(revision)}">{html.escape(revision)}</a>.
This site is a derived artifact and is never committed. Quantified results shown
in the viewer are those of this revision.</footer>
</article></main></div></body></html>
"""


def build_viewer(name: str, model_dir: str, out_dir: str, engine: str) -> str:
    """Quantify a model and build its viewer into out_dir/viewer/<name>.html."""
    import tempfile
    md = os.path.join(ROOT, model_dir)
    if not os.path.isdir(md):
        raise SiteError(f"{model_dir}: no such model directory")
    tmp = tempfile.mkdtemp(prefix="canopy-site-")
    try:
        results = os.path.join(tmp, "r.json")
        q = [sys.executable, os.path.join(HERE, "quantify.py"), md, results]
        if engine:
            q += ["--engine", engine]
        r = subprocess.run(q, capture_output=True, text=True)
        if r.returncode != 0:
            raise SiteError(f"{model_dir}: quantification failed:\n{r.stdout}{r.stderr}")
        page_path = os.path.join(out_dir, "viewer", f"{name}.html")
        os.makedirs(os.path.dirname(page_path), exist_ok=True)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "viz", "build_viz.py"),
                            md, page_path, "--results", results],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SiteError(f"{model_dir}: viewer build failed:\n{r.stdout}{r.stderr}")
        return page_path
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def build(out_dir: str, revision: str = "main", models=None, engine: str = "",
          repo_url: str = REPO_URL) -> dict:
    """Write the site; returns {output file: bytes written}."""
    models = MODELS if models is None else models
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    written = {}
    viewers = [n for n, _ in models]
    for source, target, title, _ in PAGES:
        body = render(source, revision, repo_url)
        text = page(title, body, target, revision, viewers, repo_url)
        with open(os.path.join(out_dir, target), "w", encoding="utf-8") as f:
            f.write(text)
        written[target] = len(text.encode("utf-8"))
    with open(os.path.join(out_dir, "style.css"), "w", encoding="utf-8") as f:
        f.write(CSS.strip() + "\n")
    written["style.css"] = len(CSS)
    with open(os.path.join(out_dir, ".nojekyll"), "w") as f:
        f.write("")                      # the site is already HTML; skip Jekyll
    written[".nojekyll"] = 0
    for name, model_dir in models:
        p = build_viewer(name, model_dir, out_dir, engine)
        written[os.path.relpath(p, out_dir).replace(os.sep, "/")] = os.path.getsize(p)
    return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out")
    ap.add_argument("--revision", default="main",
                    help="the tag or commit the site is built from (shown on it)")
    ap.add_argument("--model", action="append", default=[], metavar="NAME=DIR",
                    help="a model to build a viewer for (default: demo and reference)")
    ap.add_argument("--no-viewers", action="store_true",
                    help="documentation only; needs no engine")
    ap.add_argument("--engine", default="", help="engine binary for the viewers")
    ap.add_argument("--repo-url", default=REPO_URL)
    a = ap.parse_args(argv)
    models = []
    for spec in a.model:
        if "=" not in spec:
            print(f"build_site: --model {spec}: expected NAME=DIR", file=sys.stderr)
            return 2
        name, _, d = spec.partition("=")
        models.append((name, d))
    if a.no_viewers:
        models = []
    elif not models:
        models = MODELS
    try:
        written = build(a.out, a.revision, models, a.engine, a.repo_url)
    except SiteError as e:
        print(f"build_site: {e}", file=sys.stderr)
        return 1
    total = sum(written.values())
    print(f"built {a.out}: {len(written)} file(s), {total // 1024} KiB "
          f"({len(PAGES)} page(s), {len(models)} viewer(s)), revision {a.revision}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
