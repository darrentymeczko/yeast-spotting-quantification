"""
spotting_sheet.py -- laying a comparison sheet out to fit the space it has.

A comparison sheet is four pieces: a text header, the spot montage, the graph
and a one-line footer. Their shapes vary a lot. The lab's design gives a tall,
narrow montage next to a 12-strain graph, so they sit best side by side. A
96-well plate gives a wide montage and a 24-strain graph, so side by side
both would be squeezed into a thin strip; stacked, each gets the full width.

So no arrangement is fixed. `layout` tries both and keeps whichever shows the
montage and graph largest in a given box. The pipeline uses it to write the
sheet. The review window uses it again for its own pane, so the sheet is
rearranged for the window instead of shrunk into it.

Where each piece sits is written into the PNG as a text chunk (`KEY`). That is
what lets the review take the montage back out exactly, swap in a redrawn
graph, or rearrange the pieces, without guessing at panel boundaries.

Pillow only -- no numpy, no matplotlib -- because the review window imports this
to browse results on a machine without the science stack.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

#: PNG text-chunk key holding the piece boxes.
KEY = "spotting-sheet"
#: Box the pipeline lays a sheet out in, in pixels: big enough that the 400 dpi
#: montage and graph are barely downsampled, and a screen-like 16:10 shape.
SHEET_BOX = (3200, 2000)
#: Gap between the montage and the graph, as a fraction of their shared edge.
GAP_FRAC = 0.03
#: Gap between a text strip and the panels, in sheet pixels before scaling.
TEXT_GAP = 12

MODES = ("side", "stacked")
PANELS = ("montage", "graph")


@dataclass
class Layout:
    mode: str
    size: tuple                     # (width, height) in pixels
    boxes: dict = field(default_factory=dict)   # name -> (x0, y0, x1, y1)
    scale: float = 1.0              # panel scale, relative to the larger panel
    area: float = 0.0               # pixels of montage + graph shown

    def to_json(self, extra: "dict | None" = None) -> str:
        return json.dumps({"version": 1, "mode": self.mode,
                           "boxes": {k: list(v) for k, v in self.boxes.items()},
                           **(extra or {})})


def _content(mode: str, m: tuple, g: "tuple | None"):
    """Panel rectangles at a common height (side) or width (stacked).

    In "source" pixels: the larger panel keeps its own size and the other is
    brought to match it, so neither loses resolution to the shared edge.
    """
    mw, mh = m
    if g is None:
        return {"montage": (0.0, 0.0, mw, mh)}, (mw, mh)
    gw, gh = g
    if mode == "side":
        h0 = max(mh, gh)
        a, b = mw * h0 / mh, gw * h0 / gh
        gap = GAP_FRAC * h0
        return ({"montage": (0.0, 0.0, a, h0),
                 "graph": (a + gap, 0.0, a + gap + b, h0)}, (a + gap + b, h0))
    w0 = max(mw, gw)
    a, b = mh * w0 / mw, gh * w0 / gw
    gap = GAP_FRAC * w0
    return ({"montage": (0.0, 0.0, w0, a),
             "graph": (0.0, a + gap, w0, a + gap + b)}, (w0, a + gap + b))


def layout(sizes: dict, box: tuple, *, max_scale: float = 1.0,
           text_scale: tuple = (1.0, 1.0), modes=MODES) -> Layout:
    """Arrange the pieces to show the panels as large as `box` allows.

    `sizes` is {name: (w, h)} for "montage" and optionally "graph", "header",
    "footer". The panels are never scaled past `max_scale` times their source
    size. Text strips are scaled by the panel scale, held within `text_scale`
    (lo, hi), so the header stays legible when the panels get small, and never
    wider than the box.
    """
    bw, bh = max(1, box[0]), max(1, box[1])
    m, g = sizes["montage"], sizes.get("graph")
    hd, ft = sizes.get("header", (0, 0)), sizes.get("footer", (0, 0))
    lo, hi = text_scale
    text_w = max(hd[0], ft[0], 1)

    best = None
    for mode in (modes if g is not None else ("side",)):
        rects, (cw, ch) = _content(mode, m, g)
        ts = lo
        for _ in range(3):
            gaps = TEXT_GAP * ts * ((hd[1] > 0) + (ft[1] > 0))
            avail = bh - ts * (hd[1] + ft[1]) - gaps
            s = max(1e-3, min(max_scale, bw / cw, avail / ch))
            ts = min(max(s, lo), hi, bw / text_w)
        area = s * s * sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in rects.values())
        if best is None or area > best[0] * 1.0001:
            best = (area, mode, rects, (cw, ch), s, ts)

    area, mode, rects, (cw, ch), s, ts = best
    boxes, y = {}, 0
    if hd[1]:
        boxes["header"] = (0, 0, round(hd[0] * ts), round(hd[1] * ts))
        y = boxes["header"][3] + round(TEXT_GAP * ts)
    for name, (x0, y0, x1, y1) in rects.items():
        boxes[name] = (round(x0 * s), y + round(y0 * s),
                       round(x1 * s), y + round(y1 * s))
    y += round(ch * s)
    if ft[1]:
        y += round(TEXT_GAP * ts)
        boxes["footer"] = (0, y, round(ft[0] * ts), y + round(ft[1] * ts))
        y = boxes["footer"][3]
    width = max(b[2] for b in boxes.values())
    return Layout(mode, (width, y), boxes, s, area)


def best_montage(pieces: dict, box: tuple, **kw) -> dict:
    """`pieces` with whichever montage -- as photographed or turned -- lets
    `layout` show montage and graph largest in `box`.

    A 12 x 8 plate beside a wide graph leaves the box half empty; turned to
    8 x 12 the same plate may stand beside it, or stack under it, using the
    space. Without a turned copy, `pieces` comes back as it is.
    """
    turned = pieces.get("montage_rot")
    base = {k: v for k, v in pieces.items()
            if k not in ("montage_rot", "montage_clean", "graph_h")}
    if turned is None:
        return base
    options = [base, dict(base, montage=turned)]
    scored = [layout({k: v.size for k, v in p.items()}, box, **kw).area
              for p in options]
    return options[scored.index(max(scored))]


def assemble(pieces: dict, lay: Layout):
    """Paste the pieces into a white sheet at the boxes `lay` gives them."""
    from PIL import Image

    sheet = Image.new("RGB", lay.size, "white")
    for name, (x0, y0, x1, y1) in lay.boxes.items():
        im = pieces[name].convert("RGB")
        size = (max(1, x1 - x0), max(1, y1 - y0))
        if im.size != size:
            im = im.resize(size, Image.LANCZOS)
        sheet.paste(im, (x0, y0))
    return sheet


#: Private PNG chunk types carrying the panels at full resolution. Lower-case
#: first letter = ancillary, so every other PNG reader skips them; the sheet
#: still opens anywhere as the picture it is.
PANEL_CHUNKS = {"montage": b"spMn", "graph": b"spGr",
                # The montage without its quantified-row outline, when it has
                # one: the aligned view cuts spots from this, since a large
                # spot runs underneath the outline.
                "montage_clean": b"spMc",
                # The montage a quarter turn round, labels re-placed upright
                # (spotting_montage.rotate_plate): the layout may use it
                # instead when that shape fills the space better.
                "montage_rot": b"spMr",
                # The graph on its side (strains down the left, upright):
                # what the review's Rotate option shows in place of `graph`.
                "graph_h": b"spGh"}
#: Longest side a carried panel is kept at.
PANEL_MAX = 4096


def _png_bytes(im) -> bytes:
    import io

    from PIL import Image

    im = im.convert("RGB")
    scale = min(1.0, PANEL_MAX / max(im.size))
    if scale < 1.0:
        im = im.resize((round(im.width * scale), round(im.height * scale)),
                       Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def save(sheet, lay: Layout, path: Path, extra: "dict | None" = None,
         panels: "dict | None" = None) -> Path:
    """Write the sheet with its piece boxes recorded in the PNG.

    `extra` rides along in the same record: the montage's spot map and the
    graph's tick map, and the dilution level quantified. `panels`, the
    montage and graph at their own resolution, are carried in private chunks:
    the sheet has to shrink them to fit, and the review's other views are
    built from these instead, so they are as sharp as the screen allows.
    """
    from PIL.PngImagePlugin import PngInfo

    info = PngInfo()
    info.add_text(KEY, lay.to_json(extra))
    for name, im in (panels or {}).items():
        if name in PANEL_CHUNKS and im is not None:
            # After the image data, so opening the sheet to look at it never
            # has to read past them.
            info.add(PANEL_CHUNKS[name], _png_bytes(im), after_idat=True)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path, format="PNG", pnginfo=info)
    return path


def _trim(im, pad: int = 6):
    """Crop a text strip to its ink, so its blank margin takes no space."""
    from PIL import ImageChops, ImageOps

    gray = ImageOps.invert(im.convert("L"))
    bbox = ImageChops.subtract(gray, gray.point(lambda _: 8)).getbbox()
    if bbox is None:
        return None
    x0, y0, x1, y1 = bbox
    return im.crop((max(0, x0 - pad), max(0, y0 - pad),
                    min(im.width, x1 + pad), min(im.height, y1 + pad)))


def _record(sheet) -> dict:
    try:
        got = json.loads(getattr(sheet, "info", {}).get(KEY) or "")
        return got if isinstance(got, dict) else {}
    except (ValueError, TypeError):
        return {}


def native_panels(sheet) -> dict:
    """The full-resolution panels `save` carried in the sheet's file.

    Read straight from the file's chunks, which Pillow itself skips. Empty
    for a sheet written without them, or one not opened from a file.
    """
    import io
    import struct

    from PIL import Image

    path = getattr(sheet, "filename", None)
    if not path:
        return {}
    want = {v: k for k, v in PANEL_CHUNKS.items()}
    out = {}
    try:
        with open(path, "rb") as fh:
            if fh.read(8) != b"\x89PNG\r\n\x1a\n":
                return {}
            while True:
                head = fh.read(8)
                if len(head) < 8:
                    break
                length, ctype = struct.unpack(">I4s", head)
                if ctype in want:
                    data = fh.read(length)
                    with Image.open(io.BytesIO(data)) as im:
                        out[want[ctype]] = im.convert("RGB")
                    fh.seek(4, 1)                       # CRC
                elif ctype == b"IEND":
                    break
                else:
                    fh.seek(length + 4, 1)
    except (OSError, struct.error, ValueError):
        return {}
    return out


def mode_of(sheet) -> "str | None":
    """The arrangement a sheet written by `save` was laid out in."""
    return _record(sheet).get("mode")


def extra_of(sheet) -> dict:
    """What `save` was given as `extra`: spot map, tick map, level."""
    return {k: v for k, v in _record(sheet).items()
            if k not in ("version", "mode", "boxes")}


def png_record(im, key: str) -> "dict | None":
    """A JSON text chunk another writer left in a PNG (spots, ticks)."""
    try:
        got = json.loads(getattr(im, "info", {}).get(key) or "")
        return got if isinstance(got, dict) else None
    except (ValueError, TypeError):
        return None


def pieces_of(sheet) -> "dict | None":
    """The pieces of a sheet written by `save`, or None for an older sheet."""
    raw = getattr(sheet, "info", {}).get(KEY)
    if not raw:
        return None
    try:
        boxes = json.loads(raw)["boxes"]
    except (ValueError, KeyError, TypeError):
        return None
    out = {}
    for name, box in boxes.items():
        piece = sheet.crop(tuple(int(v) for v in box))
        if name in ("header", "footer"):
            piece = _trim(piece)
            if piece is None:
                continue
        out[name] = piece
    return out if "montage" in out else None


def arrange(pieces: dict, box: tuple, **kw):
    """Lay `pieces` out for `box` and assemble them: (image, Layout)."""
    sizes = {k: v.size for k, v in pieces.items()}
    lay = layout(sizes, box, **kw)
    return assemble(pieces, lay), lay


def _shrink(im, box: tuple):
    """`im` scaled down to fit `box`; never enlarged."""
    from PIL import Image

    im = im.convert("RGB")
    scale = min(box[0] / im.width, box[1] / im.height, 1.0)
    if scale < 1.0:
        im = im.resize((max(1, int(im.width * scale)),
                        max(1, int(im.height * scale))), Image.LANCZOS)
    return im


def _all_pieces(sheet) -> "dict | None":
    """The sheet's pieces, with its full-resolution panels in place of the
    shrunk copies wherever it carries them."""
    pieces = pieces_of(sheet)
    if pieces is not None:
        pieces.update(native_panels(sheet))
    return pieces


# ---------------------------------------------------------------------------
# The ways the review shows a sheet
# ---------------------------------------------------------------------------
#
#   "fit"      montage and graph and nothing else, arranged -- side by side
#              or stacked, the montage turned or not -- to fill the box
#   "aligned"  the graph with every strain's replicate spots in a column
#              directly under its name
#
# Either can be rotated: the graph is swapped for its twin drawn on its side,
# strains down the left with upright names, and Aligned then puts each
# strain's replicates in a row to the left of its name. That twin is drawn by
# the plotting code rather than made by turning the picture, which would lay
# every name on its side.
#
# Both are built from the sheet's own pixels. Nothing is re-measured and
# nothing re-plotted, so they work for any candidate, including a graph the
# review has just redrawn from corrections.

VIEWS = ("fit", "aligned")


class ViewUnavailable(Exception):
    """This sheet does not carry what the requested view is built from."""


def _graph_for(pieces: dict, rotate: bool):
    """The sheet's graph, or its sideways twin when `rotate`."""
    if not rotate:
        return pieces.get("graph")
    if pieces.get("graph_h") is None:
        raise ViewUnavailable(
            "this sheet has no sideways graph -- re-run the time course to "
            "get the rotated view")
    return pieces["graph_h"]


def panels_only(pieces: dict, box: tuple, rotate: bool = False):
    """Montage and graph, as large as `box` allows, in whichever arrangement
    and montage orientation does that best."""
    panels = {k: pieces[k] for k in ("montage", "montage_rot") if k in pieces}
    graph = _graph_for(pieces, rotate)
    if graph is not None:
        panels["graph"] = graph
    im, _ = arrange(best_montage(panels, box), box)
    return im


def _font(size: int):
    from PIL import ImageFont

    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:                           # Pillow < 10.1
        return ImageFont.load_default()


#: How far past a spot's centre its crop reaches, as a fraction of half the
#: spot pitch: just short of the midpoint to the next spot, so a well-grown
#: spot is never clipped and a neighbour's edge never creeps in.
SPOT_REACH = 0.95
#: A pixel whose channels differ by more than this is an overlay (the amber
#: outline marking the quantified row), never plate: the montage is grey.
OVERLAY_SAT = 30


def _cut(montage, box, clip, fill=None):
    """`box` cut from the montage, kept inside `clip`.

    Whatever falls outside `clip` -- past the block edge, into a label lane --
    is filled with the plate's own background colour, as are overlay pixels,
    so the cut reads as uninterrupted plate. Returns (image, fill colour).
    """
    from PIL import Image, ImageChops

    x0, y0, x1, y1 = (int(round(v)) for v in box)
    cx0, cy0, cx1, cy1 = (int(round(v)) for v in clip)
    ix0, iy0 = max(x0, cx0, 0), max(y0, cy0, 0)
    ix1, iy1 = min(x1, cx1, montage.width), min(y1, cy1, montage.height)
    inner = (montage.crop((ix0, iy0, ix1, iy1))
             if ix1 > ix0 and iy1 > iy0 else None)
    if fill is None:
        fill = _background(inner) if inner is not None else (0, 0, 0)
    out = Image.new("RGB", (max(1, x1 - x0), max(1, y1 - y0)), fill)
    if inner is not None:
        out.paste(inner, (ix0 - x0, iy0 - y0))
    r, g, b = out.split()
    spread = ImageChops.subtract(ImageChops.lighter(ImageChops.lighter(r, g), b),
                                 ImageChops.darker(ImageChops.darker(r, g), b))
    mask = spread.point(lambda v: 255 if v > OVERLAY_SAT else 0)
    if mask.getbbox():
        out.paste(fill, mask=mask)
    return out, fill


def _background(im) -> tuple:
    """The plate's background grey in `im`: its dark end, not its spots."""
    hist = im.convert("L").histogram()
    total, seen = sum(hist), 0
    for v, n in enumerate(hist):
        seen += n
        if seen >= 0.2 * total:
            return (v, v, v)
    return (0, 0, 0)


def _strain_strip(group: list, montage, ms: float):
    """One strain's replicates as a single upright strip, replicate 1 on top.

    Replicates spotted side by side are cut out together, as the one piece
    of plate they are, so the agar between them is the real agar. A run along
    a row is turned upright -- rotated, never mirrored. Replicates that are
    not neighbours (other blocks, other plates) are each cut close and set
    edge to edge, so no white shows between them either.

    Returns (strip, [centre y of each replicate in the strip, in rep order]).
    """
    from PIL import Image

    group = sorted(group, key=lambda s: s["rep"])
    r = sorted(s["r"] for s in group)[len(group) // 2] * ms
    e = max(1, int(round(SPOT_REACH * r)))
    pts = [(s["x"] * ms, s["y"] * ms) for s in group]
    step = 2 * r                                 # one spot pitch
    whole = (0, 0, montage.width, montage.height)
    clips = [[v * ms for v in s["clip"]] if s.get("clip") else whole
             for s in group]

    def run_along(axis):
        """True when the spots form one unbroken line along `axis`."""
        other = 1 - axis
        if any(abs(p[other] - pts[0][other]) > 0.25 * r for p in pts):
            return False
        pos = sorted(p[axis] for p in pts)
        return all(0.75 * step <= b - a <= 1.25 * step
                   for a, b in zip(pos, pos[1:]))

    if len(pts) > 1 and (run_along(0) or run_along(1)):
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        box = (int(min(xs) - e), int(min(ys) - e),
               int(max(xs) + e), int(max(ys) + e))
        clip = (max(c[0] for c in clips), max(c[1] for c in clips),
                min(c[2] for c in clips), min(c[3] for c in clips))
        strip, _ = _cut(montage, box, clip)
        if run_along(0):
            # Along a row: rep 1 goes to the top.
            first_left = pts[0][0] < pts[-1][0]
            strip = strip.transpose(Image.ROTATE_270 if first_left
                                    else Image.ROTATE_90)
            along = [p[0] - box[0] for p in pts]
            if not first_left:
                along = [(box[2] - box[0]) - a for a in along]
        else:
            first_top = pts[0][1] < pts[-1][1]
            if not first_top:
                strip = strip.transpose(Image.ROTATE_180)
            along = [p[1] - box[1] for p in pts]
            if not first_top:
                along = [(box[3] - box[1]) - a for a in along]
        return strip, along

    # Not neighbours: each cut to its own cell, edge to edge, on one
    # background colour so the joins do not show.
    side = 2 * e
    strip = Image.new("RGB", (side, side * len(pts)))
    fill = None
    for i, ((x, y), clip) in enumerate(zip(pts, clips)):
        tile, fill = _cut(montage, (x - e, y - e, x - e + side, y - e + side),
                          clip, fill)
        strip.paste(tile, (0, i * side))
    return strip, [e + i * side for i in range(len(pts))]


def aligned(pieces: dict, extra: dict, rotate: bool = False):
    """The graph with each strain's replicate spots next to its name.

    One band of plate: for each strain, its replicate spots as a continuous
    strip (see `_strain_strip`), the strips meeting edge to edge with no gap
    and the whole band on the plate's own background colour. Only the dilution
    level that was quantified is shown -- those are the spots the points above
    it came from. Upright, the band runs below the strain names, one column
    per strain, replicate 1 on top. `rotate` uses the sideways graph: the band
    runs down the left of the names, one row per strain, replicate 1 leftmost.
    """
    from PIL import Image, ImageDraw

    graph = _graph_for(pieces, rotate)
    montage = pieces.get("montage_clean") or pieces.get("montage")
    gmap = extra.get("graph_h_map" if rotate else "graph_map")
    smap = extra.get("montage_map")
    if graph is None or montage is None or not gmap or not smap:
        raise ViewUnavailable(
            "this sheet was drawn before spot and strain positions were "
            "recorded -- re-run the time course to get the aligned view")

    gs = graph.width / float(gmap["size"][0])
    ms = montage.width / float(smap["size"][0])
    axis = "y" if rotate else "x"
    ticks = {t["name"]: t[axis] * gs for t in gmap["ticks"]}
    pitch = (float(gmap.get("pitch") or 0) * gs
             or (graph.height if rotate else graph.width) / max(len(ticks), 1))

    level = extra.get("level")
    spots = [s for s in smap["spots"] if s.get("strain") in ticks]
    if level is not None and any(s.get("level") == level for s in spots):
        spots = [s for s in spots if s.get("level") == level]
    by_strain: dict = {}
    for s in spots:
        by_strain.setdefault(s["strain"], []).append(s)
    if not by_strain:
        raise ViewUnavailable("no spot on this sheet belongs to a graphed strain")
    strips = {}
    for name, group in by_strain.items():
        strip, centres = _strain_strip(group, montage, ms)
        if rotate:                      # replicate 1 on the left, not the top
            strip = strip.transpose(Image.ROTATE_90)
        reps = [s["rep"] for s in sorted(group, key=lambda s: s["rep"])]
        strips[name] = (strip, centres, reps)

    # Each strain's cell runs from halfway to the previous tick to halfway to
    # the next, so neighbours meet with no gap, and anything a strip does not
    # cover is the plate's own background colour -- no white inside the band.
    order = sorted(strips, key=lambda n: ticks[n])
    at = [ticks[n] for n in order]
    edges = ([at[0] - pitch / 2] + [(a + b) / 2 for a, b in zip(at, at[1:])]
             + [at[-1] + pitch / 2])
    edges = [int(round(e)) for e in edges]
    agar = _background(montage)
    gap = max(6, int(pitch * 0.12))             # white between names and spots
    font = _font(max(9, int(pitch * 0.24)))

    placed = {}
    for i, name in enumerate(order):
        strip, centres, reps = strips[name]
        span = max(1, edges[i + 1] - edges[i])
        f = span / (strip.height if rotate else strip.width)
        placed[name] = (strip.resize((max(1, round(strip.width * f)),
                                      max(1, round(strip.height * f)))
                                     if rotate else
                                     (span, max(1, round(strip.height * f))),
                                     Image.LANCZOS),
                        [c * f for c in centres], reps)

    if not rotate:
        band_h = max(s.height for s, _, _ in placed.values())
        band_x0, band_x1 = max(0, edges[0]), min(graph.width, edges[-1])
        out = Image.new("RGB", (graph.width, graph.height + gap + band_h),
                        "white")
        out.paste(graph, (0, 0))
        top = graph.height + gap
        ImageDraw.Draw(out).rectangle((band_x0, top, band_x1 - 1,
                                       top + band_h - 1), fill=agar)
        for i, name in enumerate(order):
            out.paste(placed[name][0], (edges[i], top))
        # Replicate numbers down the left, beside the band, at the replicate
        # centres of the first strain's strip.
        draw = ImageDraw.Draw(out)
        _, centres, reps = placed[order[0]]
        for rep, y in zip(reps, centres):
            draw.text((band_x0 - max(4, int(pitch * 0.08)), top + y),
                      f"R{rep}", fill=(90, 90, 90), font=font, anchor="rm")
        return out

    # Sideways: the band to the left of the names, one row per strain, with a
    # strip of room above it for the replicate numbers.
    band_w = max(s.width for s, _, _ in placed.values())
    head = int(pitch * 0.5)
    band_y0, band_y1 = max(0, edges[0]), min(graph.height, edges[-1])
    out = Image.new("RGB", (band_w + gap + graph.width, graph.height + head),
                    "white")
    out.paste(graph, (band_w + gap, head))
    ImageDraw.Draw(out).rectangle((0, head + band_y0, band_w - 1,
                                   head + band_y1 - 1), fill=agar)
    for i, name in enumerate(order):
        out.paste(placed[name][0], (0, head + edges[i]))
    draw = ImageDraw.Draw(out)
    _, centres, reps = placed[order[0]]
    for rep, x in zip(reps, centres):
        draw.text((x, head + band_y0 - max(4, int(pitch * 0.08))), f"R{rep}",
                  fill=(90, 90, 90), font=font, anchor="ms")
    return out


def view(sheet, mode: str, box: tuple, rotate: bool = False):
    """`sheet` shown as `mode` (one of VIEWS), fitted to `box`.

    `rotate` shows the graph on its side. Raises ViewUnavailable when the
    sheet cannot be shown that way; an older sheet without recorded pieces is
    only ever shown whole, as it was written.
    """
    if mode == "stacked":               # its name before it learned to turn
        mode = "fit"
    pieces = _all_pieces(sheet)
    if pieces is None:
        if mode == "fit" and not rotate:
            return _shrink(sheet, box)
        raise ViewUnavailable(
            "this sheet was drawn before its panels were recorded -- re-run "
            "the time course to get this view")
    if mode == "fit":
        return panels_only(pieces, box, rotate)
    if mode == "aligned":
        return _shrink(aligned(pieces, extra_of(sheet), rotate), box)
    raise ValueError(f"unknown view {mode!r}; expected one of {VIEWS}")