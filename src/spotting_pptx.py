#!/usr/bin/env python3
"""
spotting_pptx.py -- collect the figures into a PowerPoint deck.

One slide per treatment-set combination, in the layout of the deck Darren
supplied: a blank 13.33 x 7.5in widescreen slide carrying two pictures and no
text -- the spot montage on the left, the Prism-style graph on the right, both
run to the full slide height.

    python spotting_pptx.py                      # every combination it can pair
    python spotting_pptx.py --combo 4 K-OAc      # just one
    python spotting_pptx.py --out talk.pptx

Reads what the batch run already wrote; it measures nothing itself:

    Results/montages/montage_set<N>_<TREATMENT>.png
    Results/figures/spotting_Set_<N>_<TREATMENT>.png
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The repository root: source lives in src/, but the photo folder and
# everything a run writes live beside it, not inside it.
PROJECT_ROOT = HERE.parent

# The supplied deck: 13.33 x 7.50 in, montage at x=0.00 w=6.47, graph at
# x=6.43 w=6.90, both spanning the full height with no gutter and no title.
SLIDE_W_IN = 13.333
SLIDE_H_IN = 7.5
LEFT_W_IN = 6.47          # nominal half-widths, matching the example
RIGHT_X_IN = 6.43
RIGHT_W_IN = 6.90


def _fit(img_w: int, img_h: int, box_x: float, box_w: float) -> tuple:
    """Scale an image to the full slide height inside its half, keeping its
    aspect ratio, and centre it horizontally in that half.

    The example deck stretched slightly; preserving the ratio instead avoids
    distorting a plate image, which would misrepresent spot shape.
    """
    scale = SLIDE_H_IN / img_h
    w = img_w * scale
    h = SLIDE_H_IN
    if w > box_w:                      # too wide: fit the width instead
        scale = box_w / img_w
        w, h = box_w, img_h * scale
    return box_x + (box_w - w) / 2.0, (SLIDE_H_IN - h) / 2.0, w, h


# Slide order: set first, then treatment -- set 1 on every medium, then set 2,
# and so on. Media run fermentable before respiring so each set opens on the
# permissive control condition.
TREATMENT_ORDER = ["GLU", "GLY", "K-OAc"]


def sort_key(combo: tuple) -> tuple:
    set_id, treatment = combo
    try:
        n = int(set_id)
    except ValueError:
        n = 10 ** 6
    try:
        t = TREATMENT_ORDER.index(treatment)
    except ValueError:          # an unlisted medium sorts after the known ones
        t = len(TREATMENT_ORDER)
    return (n, t, treatment)


def find_pairs(results: Path) -> list:
    """Every (set, treatment) that has BOTH a montage and a graph."""
    mont_dir = results / "montages"
    fig_dir = results / "figures"
    montages, graphs = {}, {}

    for p in sorted(mont_dir.glob("montage_set*_*.png")):
        m = re.match(r"montage_set([^_]+)_(.+)\.png$", p.name)
        if m:
            montages[(m.group(1), m.group(2))] = p

    for p in sorted(fig_dir.glob("spotting_Set_*.png")):
        m = re.match(r"spotting_Set_([^_]+)_(.+)\.png$", p.name)
        if m:
            # R's safe() turns every run of non-alphanumerics into "_", so
            # "K-OAc" survives but a treatment with a space would not.
            graphs[(m.group(1), m.group(2))] = p

    pairs = []
    for key in sorted(set(montages) & set(graphs), key=sort_key):
        pairs.append((key, montages[key], graphs[key]))

    only_m = sorted(set(montages) - set(graphs), key=sort_key)
    only_g = sorted(set(graphs) - set(montages), key=sort_key)
    for key in only_m:
        print(f"  (skipping set {key[0]} {key[1]}: montage but no graph)")
    for key in only_g:
        print(f"  (skipping set {key[0]} {key[1]}: graph but no montage)")
    return pairs


def build(pairs: list, out_path: Path) -> Path:
    from pptx import Presentation
    from pptx.util import Inches
    from PIL import Image

    pres = Presentation()
    pres.slide_width = Inches(SLIDE_W_IN)
    pres.slide_height = Inches(SLIDE_H_IN)
    blank = pres.slide_layouts[6]        # "Blank" -- no placeholders

    for (set_id, treatment), mont, graph in pairs:
        slide = pres.slides.add_slide(blank)
        for path, box_x, box_w in ((mont, 0.0, LEFT_W_IN),
                                   (graph, RIGHT_X_IN, RIGHT_W_IN)):
            with Image.open(path) as im:
                iw, ih = im.size
            x, y, w, h = _fit(iw, ih, box_x, box_w)
            slide.shapes.add_picture(str(path), Inches(x), Inches(y),
                                     Inches(w), Inches(h))
        # Not shown on the slide -- the example carries no text -- but it makes
        # the deck navigable and searchable.
        slide.notes_slide.notes_text_frame.text = f"Set {set_id} {treatment}"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pres.save(str(out_path))
    return out_path


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path, nargs="?", default=PROJECT_ROOT / "Spotting Assays")
    ap.add_argument("--combo", nargs=2, metavar=("SET", "TREATMENT"),
                    help="Only this combination, e.g. --combo 4 K-OAc")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output .pptx (default <folder>/Results/spotting_figures.pptx)")
    args = ap.parse_args(argv)

    results = args.folder / "Results"
    if not results.is_dir():
        print(f"No Results folder in {args.folder}. Run the analysis first.",
              file=sys.stderr)
        return 2

    pairs = find_pairs(results)
    if args.combo:
        want = (args.combo[0], args.combo[1])
        pairs = [p for p in pairs if p[0] == want]
        if not pairs:
            print(f"No montage+graph pair for set {want[0]} {want[1]}.",
                  file=sys.stderr)
            return 2
    if not pairs:
        print("Nothing to build: no combination has both a montage and a graph.\n"
              "  Draw the montages (say yes at the prompt, or run "
              "spotting_montage.py) and make sure the R figures were written.",
              file=sys.stderr)
        return 1

    out = args.out or (results / "spotting_figures.pptx")
    build(pairs, out)
    print(f"  wrote {out}  ({len(pairs)} slide(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
