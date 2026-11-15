#!/usr/bin/env python3
"""The viewer's page logic, in a real browser (FR-64): large diagrams.

Builds the viewer for the demo model, for the demo model against a
modified base (diff mode), and for a synthetic fault tree of about 1,900
basic events and 1,000 gates (some 10,000 node instances once unfolded),
opens each in headless Chromium (Playwright) and checks:

  * a small tree is drawn whole, as before: every node laid out is in
    the page with its ID; a click shows its details and marks it
    selected; a double-click folds a gate and another unfolds it; an
    event tree shows its sequences; no minimap when everything is in
    view; the diff rings of changed entities are drawn;
  * a large tree: laid out whole, but only what meets the viewport is in
    the page (a small fraction of the nodes); it opens at the top gate at
    a scale where nodes can be told apart, as plain rectangles (level of
    detail), and the minimap is shown and painted;
  * culling is complete and sound at full detail: every laid-out node
    whose box meets the viewport is in the page, and nothing far outside
    it is — at five places in the tree: the view set in the middle, a
    node put at the top-left and at the bottom-right corner, after a
    drag, and after a click on the minimap; on a small tree zoomed in, a
    click on the minimap centres the view on the point clicked;
  * the budget on repeated subtrees: lowered for the test, subtrees
    already drawn elsewhere start folded, the page says so, and a
    double-click unfolds one;
  * the page raises no error.

Needs Playwright with Chromium (`pip install playwright && python -m
playwright install chromium`). Without it the test says so and exits 0,
unless --require (CI), where a missing browser is a failure.

Usage: python ci/test_viz_page.py [--require]
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODEL = os.path.join(ROOT, "model")

# what is in the page for the nodes that meet the viewport (no margin), and
# how far outside it the farthest drawn node is, from the page's own layout
AUDIT = """() => {
  const r = svg.getBoundingClientRect();
  const v = {x0: -view.x / view.k, y0: -view.y / view.k,
             x1: (r.width - view.x) / view.k, y1: (r.height - view.y) / view.k};
  const drawn = new Map();
  for (const e of document.querySelectorAll('#world [data-id]'))
    drawn.set(e.getAttribute('data-id'), (drawn.get(e.getAttribute('data-id')) || 0) + 1);
  let expected = 0, missing = 0;
  const need = new Map();
  for (const n of scene.nodes)
    if (n.x + 84 >= v.x0 && n.x - 84 <= v.x1 && n.y + 46 >= v.y0 && n.y <= v.y1){
      expected++; need.set(n.id, (need.get(n.id) || 0) + 1);
    }
  for (const [id, k] of need) if ((drawn.get(id) || 0) < k) missing++;
  let far = 0;
  for (const e of document.querySelectorAll('#world [data-id]')){
    const b = e.getBoundingClientRect();
    far = Math.max(far, r.left - b.right, b.left - r.right, r.top - b.bottom, b.top - r.bottom);
  }
  return {expected, missing, far, inPage: document.querySelectorAll('#world [data-id]').length,
          centre: (v.x0 + v.x1) / 2};
}"""


def main() -> int:
    require = "--require" in sys.argv[1:]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("test_viz_page: Playwright is not installed"
              + ("" if require else "; SKIPPED (the page logic was not checked)"))
        return 1 if require else 0

    failures = []
    tmp = tempfile.mkdtemp(prefix="canopy-vizpage-")

    def check(cond, msg):
        (print(f"  ok  {msg}") if cond else failures.append(msg))

    def run(*cmd):
        r = subprocess.run([sys.executable] + list(cmd), capture_output=True, text=True)
        if r.returncode:
            raise SystemExit(f"{' '.join(cmd)} failed:\n{r.stdout}{r.stderr}")

    build = os.path.join(ROOT, "viz", "build_viz.py")
    demo = os.path.join(tmp, "demo.html")
    run(build, MODEL, demo)
    base = os.path.join(tmp, "base")
    shutil.copytree(MODEL, base)
    p = os.path.join(base, "basic-events", "rps.yaml")
    with open(p, encoding="utf-8") as f:
        t = f.read()
    assert "value: 2.0e-5" in t
    with open(p, "w", encoding="utf-8") as f:
        f.write(t.replace("value: 2.0e-5", "value: 1.0e-5", 1))
    diff = os.path.join(tmp, "diff.html")
    run(build, MODEL, diff, "--base", base)
    run(os.path.join(HERE, "generate_fault_tree.py"), os.path.join(tmp, "g.xml"),
        "--structure", "psa", "--basic-events", "2000", "--seed", "1")
    run(os.path.join(HERE, "import_mef.py"), os.path.join(tmp, "g.xml"), os.path.join(tmp, "big"))
    big = os.path.join(tmp, "big.html")
    run(build, os.path.join(tmp, "big"), big)

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as e:             # Playwright without its browser
            print(f"test_viz_page: Chromium cannot be started ({str(e).splitlines()[0]})"
                  + ("" if require else "; SKIPPED (the page logic was not checked)"))
            shutil.rmtree(tmp, ignore_errors=True)
            return 1 if require else 0
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        state = lambda: page.evaluate("canopyView()")  # noqa: E731
        settle = lambda: page.wait_for_timeout(120)    # noqa: E731

        # --- a small tree: whole, as before
        page.goto("file://" + demo)
        page.wait_for_selector("#navlist .nav-item")
        page.evaluate("show('FT-ECCS-INJECTION')")
        s = state()
        ids = page.evaluate("[...document.querySelectorAll('#world .n-id')].map(e => e.textContent)")
        check(s["laid"] == s["drawnNodes"] == len(ids) == page.locator("#world .node-card").count()
              and s["laid"] > 5 and not s["lod"] and not s["minimap"]
              and "GT-ECC-INJ-TOP" in ids,
              f"a small fault tree is drawn whole ({s['laid']} nodes, each with its ID), "
              "at full detail, without a minimap")
        page.locator("#world g[data-id='GT-ECC-TRAIN-A']").first.click()
        settle()
        check("GT-ECC-TRAIN-A" in page.inner_text("#details")
              and page.locator("#world .node-card.sel").count() == 1,
              "a click shows the node's details and marks it selected")
        page.locator("#world g[data-id='GT-ECC-TRAIN-A']").first.dblclick()
        settle()
        folded = state()["laid"]
        page.locator("#world g[data-id='GT-ECC-TRAIN-A']").first.dblclick()
        settle()
        check(folded < s["laid"] and state()["laid"] == s["laid"],
              f"a double-click folds a gate ({s['laid']} -> {folded} nodes), another unfolds it")
        # zoomed in, the minimap appears, and a click in it centres the view there
        page.evaluate("() => { show('FT-ECCS-INJECTION'); view.k = 3; applyView(); }")
        settle()
        mm = page.locator("#minimap").bounding_box()
        page.mouse.click(mm["x"] + mm["width"] * 0.25, mm["y"] + mm["height"] * 0.5)
        settle()
        got = page.evaluate(
            "() => { const r = svg.getBoundingClientRect(), b = scene.bounds; return {"
            "cx: (r.width / 2 - view.x) / view.k, cy: (r.height / 2 - view.y) / view.k, "
            "b, shown: canopyView().minimap}; }")
        b = got["b"]                  # canvas 440 x 280, 8 px of padding, axes independent
        want_x = b["x"] + (0.25 * 440 - 8) / (440 - 16) * b["width"]
        want_y = b["y"] + (0.5 * 280 - 8) / (280 - 16) * b["height"]
        check(got["shown"] and abs(got["cx"] - want_x) < 8 and abs(got["cy"] - want_y) < 4,
              f"zoomed in, a click at (25%, 50%) of the minimap centres the view on that "
              f"point of the diagram (x {got['cx']:.0f} for {want_x:.0f}, "
              f"y {got['cy']:.0f} for {want_y:.0f})")
        page.evaluate("show('ET-SLOCA')")
        check("SEQ-SLOCA-01" in page.evaluate("document.querySelector('#svg').textContent")
              and not state()["minimap"]
              and state()["laid"] is None,
              "an event tree shows its sequences; no minimap when all of it is in view")
        page.goto("file://" + diff)
        page.wait_for_selector("#navlist .nav-item")
        page.evaluate("show('FT-RPS')")
        check(page.locator("#world .diff-ring").count() >= 1,
              "diff mode: a changed basic event carries its ring in the fault tree")

        # --- a large tree
        page.goto("file://" + big)
        page.wait_for_selector("#navlist .nav-item")
        ms = page.evaluate("() => { const t = performance.now(); "
                           "show(Object.keys(M.fault_trees)[0]); return performance.now() - t; }")
        s = state()
        check(s["laid"] > 5000 and s["inPage"] < s["laid"] / 10 and s["drawnNodes"] > 20,
              f"a large tree is laid out whole ({s['laid']} nodes) with {s['inPage']} elements "
              f"in the page (it took {ms:.0f} ms to show)")
        check(abs(s["k"] - 0.12) < 1e-9 and s["lod"]
              and page.locator("#world .node-dot").count() == s["drawnNodes"]
              and page.locator("#world .node-card").count() == 0,
              "it opens at the top gate at scale 0.12, nodes as plain rectangles")
        painted = page.evaluate(
            "() => { const d = mctx.getImageData(0, 0, mini.width, mini.height).data; let n = 0; "
            "for (let i = 3; i < d.length; i += 4) if (d[i]) n++; return n; }")
        check(s["minimap"] and painted > 2000,
              f"the minimap is shown and painted ({painted} pixels)")

        def audit(where):
            settle()
            a = page.evaluate(AUDIT)
            check(a["expected"] > 0 and a["missing"] == 0 and a["far"] <= 121 * 1.0 + 1,
                  f"culling at full detail, {where}: all {a['expected']} nodes meeting the "
                  f"viewport are in the page ({a['inPage']} drawn), none farther than the "
                  f"margin outside it")
            return a

        page.evaluate("() => { const n = scene.nodes[Math.floor(scene.nodes.length / 3)]; "
                      "view.k = 1; view.x = 500 - n.x; view.y = 300 - n.y; applyView(); }")
        a1 = audit("view set in the middle of the tree")
        page.evaluate("() => { const n = scene.nodes[Math.floor(scene.nodes.length / 3)]; "
                      "view.y = 10 - n.y; view.x = 20 - n.x + 84; applyView(); }")
        audit("a node at the top-left corner of the viewport")
        page.evaluate("() => { const n = scene.nodes[Math.floor(scene.nodes.length / 3)]; "
                      "const r = svg.getBoundingClientRect(); view.y = r.height - 10 - n.y; "
                      "view.x = r.width - 20 - n.x + 84; applyView(); }")
        audit("a node at the bottom-right corner of the viewport")
        page.evaluate("() => { const n = scene.nodes[Math.floor(scene.nodes.length / 3)]; "
                      "view.x = 500 - n.x; view.y = 300 - n.y; applyView(); }")
        settle()
        check(not state()["lod"] and page.locator("#world .node-card").count() == a1["inPage"],
              "at scale 1 the nodes are drawn in full")
        box = page.locator("#svg").bounding_box()
        page.mouse.move(box["x"] + 600, box["y"] + 500)
        page.mouse.down()
        page.mouse.move(box["x"] + 200, box["y"] + 480, steps=8)
        page.mouse.up()
        a2 = audit("after dragging 400 px")
        check(abs((a2["centre"] - a1["centre"]) - 400) < 2, "dragging moves the view by the drag")
        mm = page.locator("#minimap").bounding_box()
        page.mouse.click(mm["x"] + mm["width"] * 0.9, mm["y"] + mm["height"] * 0.5)
        a3 = audit("after a click near the right end of the minimap")
        extent = page.evaluate("[scene.bounds.x, scene.bounds.width]")
        where = (a3["centre"] - extent[0]) / extent[1]
        check(0.85 < where < 0.95,
              f"a click on the minimap moves the view to the place clicked ({where:.2f} of "
              "the width)")

        # --- the budget on repeated subtrees
        full = state()["laid"]
        page.evaluate("() => { FT_BUDGET = 1500; render(); }")
        s = state()
        note = page.inner_text("#scale-note")
        check(s["folded"] > 0 and 1500 <= s["laid"] < full
              and "repeated subtrees start folded" in note,
              f"past the budget, subtrees already drawn start folded ({full} -> {s['laid']} "
              f"nodes, {s['folded']} folded), and the page says so")
        target = page.evaluate(
            "() => { const n = scene.nodes.find(n => n.collapsedHere); view.k = 1; "
            "view.x = 500 - n.x; view.y = 300 - n.y; applyView(); return n.id; }")
        settle()
        page.locator(f"#world g[data-id='{target}']").first.dblclick()
        settle()
        check(state()["laid"] > s["laid"], "a double-click unfolds a folded subtree")

        check(errors == [], "the page raised no error"
              + ("" if not errors else ": " + "; ".join(errors[:3])))
        browser.close()

    shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print(f"\n{len(failures)} FAILED:")
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("\nviewer page: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
