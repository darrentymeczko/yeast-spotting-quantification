"""Comparison sheets: arranged to the panels' shapes, and re-arrangeable.

The sheet used to be a fixed side-by-side grid. A 96-well montage beside a
24-strain graph then left both squeezed into a thin strip, and the review's
graph swap -- which assumed the graph started 47.5% of the way across --
painted over the right third of the spots.
"""

import pytest

Image = pytest.importorskip("PIL.Image")

import spotting_sheet as ss  # noqa: E402


def test_a_tall_montage_and_a_square_graph_sit_side_by_side():
    lay = ss.layout({"montage": (800, 1600), "graph": (1400, 1200)}, ss.SHEET_BOX)
    assert lay.mode == "side"


def test_a_wide_montage_and_a_wide_graph_are_stacked():
    lay = ss.layout({"montage": (3300, 1400), "graph": (4000, 1700)}, ss.SHEET_BOX)
    assert lay.mode == "stacked"
    m, g = lay.boxes["montage"], lay.boxes["graph"]
    assert g[1] > m[3]                        # graph below the montage
    assert lay.size[0] <= ss.SHEET_BOX[0] and lay.size[1] <= ss.SHEET_BOX[1]


def test_the_arrangement_follows_the_box_it_must_fill():
    sizes = {"montage": (1000, 1000), "graph": (1000, 1000)}
    assert ss.layout(sizes, (2000, 900)).mode == "side"
    assert ss.layout(sizes, (900, 2000)).mode == "stacked"


def test_text_never_pushes_the_sheet_past_its_box():
    sizes = {"montage": (3000, 1300), "graph": (3000, 1500),
             "header": (2400, 200), "footer": (2000, 40)}
    lay = ss.layout(sizes, (1300, 700), text_scale=(0.6, 1.0))
    assert lay.size[0] <= 1300 and lay.size[1] <= 700 + 1


def _sheet(tmp_path):
    pieces = {"montage": Image.new("RGB", (900, 400), (0, 0, 0)),
              "graph": Image.new("RGB", (800, 400), (200, 30, 30)),
              "header": Image.new("RGB", (600, 60), (40, 40, 40)),
              "footer": Image.new("RGB", (500, 20), (90, 90, 90))}
    im, lay = ss.arrange(pieces, ss.SHEET_BOX)
    return ss.save(im, lay, tmp_path / "sheet.png"), lay


def test_a_saved_sheet_gives_its_pieces_back(tmp_path):
    path, lay = _sheet(tmp_path)
    with Image.open(path) as im:
        pieces = ss.pieces_of(im)
        assert set(pieces) == {"header", "montage", "graph", "footer"}
        assert pieces["montage"].getpixel((10, 10)) == (0, 0, 0)
        assert pieces["graph"].getpixel((10, 10)) == (200, 30, 30)


def test_the_display_fills_a_pane_of_either_shape(tmp_path):
    path, _ = _sheet(tmp_path)
    with Image.open(path) as im:
        wide = ss.view(im, "fit", (1600, 500))
        tall = ss.view(im, "fit", (500, 1600))
    assert wide.width <= 1600 and wide.height <= 500
    assert tall.width <= 500 and tall.height <= 1600
    # Rearranged for each pane, not just shrunk: the two shapes differ.
    assert wide.width / wide.height > tall.width / tall.height
    assert ss.VIEWS == ("fit", "aligned")             # no header view any more


def test_a_redrawn_graph_never_covers_the_spots(tmp_path):
    from results_review.export import _combine_preview

    path, _ = _sheet(tmp_path)
    graph = tmp_path / "graph.png"
    Image.new("RGB", (1600, 800), (20, 160, 20)).save(graph)
    out = _combine_preview(path, graph, tmp_path / "combined.png")
    with Image.open(out) as im:
        pieces = ss.pieces_of(im)
        assert pieces["montage"].size == (900, 400)
        assert pieces["montage"].getpixel((899, 200)) == (0, 0, 0)
        assert pieces["graph"].getpixel((10, 10)) == (20, 160, 20)


def test_the_plate_is_turned_when_that_fills_the_space_better():
    wide = Image.new("RGB", (1200, 800))              # 12 x 8 as photographed
    turned = Image.new("RGB", (800, 1200))
    graph = Image.new("RGB", (2400, 1300), "white")   # a 24-strain graph
    pieces = {"montage": wide, "montage_rot": turned, "graph": graph}
    # A wide pane: the turned plate stands beside the graph.
    assert ss.best_montage(pieces, (2400, 1000))["montage"] is turned
    # A very wide, short pane: as photographed, beside the graph.
    assert ss.best_montage(pieces, (4000, 700))["montage"] is wide
    # The carried extras never reach a layout.
    assert "montage_rot" not in ss.best_montage(pieces, (2400, 1000))


def test_full_resolution_panels_travel_inside_the_sheet(tmp_path):
    pieces = {"montage": Image.new("RGB", (3000, 1300), (0, 0, 0)),
              "graph": Image.new("RGB", (3600, 3800), (200, 30, 30))}
    im, lay = ss.arrange(pieces, ss.SHEET_BOX)
    path = ss.save(im, lay, tmp_path / "s.png", panels=pieces)
    with Image.open(path) as sheet:
        assert sheet.size == lay.size                   # still a plain picture
        native = ss.native_panels(sheet)
    assert native["montage"].size == (3000, 1300)
    assert native["graph"].size == (3600, 3800)


def test_an_older_sheet_has_no_aligned_view_but_still_fits(tmp_path):
    path = tmp_path / "old.png"
    Image.new("RGB", (1000, 600), "white").save(path)
    with Image.open(path) as im:
        assert ss.view(im, "fit", (500, 500)).size == (500, 300)
        with pytest.raises(ss.ViewUnavailable):
            ss.view(im, "aligned", (500, 500))


def _aligned_sheet(tmp_path):
    """A 3-strain graph and a montage whose spots are solid colour squares."""
    graph = Image.new("RGB", (900, 600), "white")
    for x in range(900):
        graph.putpixel((x, 500), (0, 0, 0))             # the x-axis line
    montage = Image.new("RGB", (600, 400), (0, 0, 0))
    spots, colours = [], [(200, 200, 200), (150, 150, 150), (100, 100, 100)]
    for i, name in enumerate(("A", "B", "C")):
        for rep in (1, 2):
            x, y = 100 + 200 * i, 100 + 200 * (rep - 1)
            montage.paste(colours[i], (x - 40, y - 40, x + 40, y + 40))
            spots.append({"x": x, "y": y, "r": 40, "strain": name,
                          "slot": i + 1, "rep": rep, "level": 0})
    extra = {"level": 0,
             "montage_map": {"size": [600, 400], "spots": spots},
             "graph_map": {"size": [900, 600], "axis_y": 502, "pitch": 300,
                           "left": 0, "ylabel_half": 0,
                           "ticks": [{"name": n, "x": 150 + 300 * i}
                                     for i, n in enumerate("ABC")]}}
    pieces = {"montage": montage, "graph": graph}
    im, lay = ss.arrange(pieces, ss.SHEET_BOX)
    return ss.save(im, lay, tmp_path / "a.png", extra=extra, panels=pieces)


def test_each_strains_spots_sit_under_its_own_tick(tmp_path):
    with Image.open(_aligned_sheet(tmp_path)) as im:
        pieces = ss.pieces_of(im)
        pieces.update(ss.native_panels(im))
        out = ss.aligned(pieces, ss.extra_of(im))
    assert out.width == 900 and out.height > 600
    # The graph is whole, axis and all; the spots go below it, under the names.
    assert out.getpixel((450, 500)) == (0, 0, 0)
    assert out.getpixel((150, 499)) == (255, 255, 255)
    band = out.crop((0, 600, 900, out.height))
    for i, colour in enumerate([(200, 200, 200), (150, 150, 150),
                                (100, 100, 100)]):
        x = 150 + 300 * i
        column = [band.getpixel((x, y)) for y in range(band.height)]
        assert column.count(colour) > 100
    # One unbroken band: across its full width, below its top gap, no white.
    top = next(y for y in range(band.height)
               if band.getpixel((450, y)) != (255, 255, 255))
    body = band.crop((0, top, 900, band.height)).convert("L")
    assert body.getextrema()[1] < 250


def _run_spots(n=4, pitch=100, y=60, horizontal=True, reverse=False):
    """`n` neighbouring replicates, half-pitch r, inside one clip."""
    spots = []
    for k in range(n):
        pos = 60 + k * pitch
        rep = (n - k) if reverse else k + 1
        x, yy = (pos, y) if horizontal else (y, pos)
        spots.append({"x": x, "y": yy, "r": pitch / 2, "rep": rep,
                      "clip": [0, 0, 600, 600], "strain": "A", "level": 0})
    return spots


def test_neighbouring_replicates_are_cut_as_one_piece_of_plate():
    montage = Image.new("RGB", (600, 600), (12, 12, 12))     # agar
    for k in range(4):
        montage.paste((60 + 40 * k,) * 3, (40 + k * 100, 40, 80 + k * 100, 80))
    strip, centres = ss._strain_strip(_run_spots(), montage, 1.0)
    # Turned upright, rep 1 on top, one spot pitch apart, and no white.
    assert strip.height > strip.width
    assert [round(b - a) for a, b in zip(centres, centres[1:])] == [100] * 3
    assert strip.getpixel((strip.width // 2, int(centres[0])))[0] == 60
    assert strip.getpixel((strip.width // 2, int(centres[3])))[0] == 180
    assert strip.convert("L").getextrema()[1] < 250


def test_a_run_spotted_right_to_left_still_reads_rep_1_first():
    montage = Image.new("RGB", (600, 600), (12, 12, 12))
    for k in range(4):                               # rep 4 at the left
        montage.paste((60 + 40 * (3 - k),) * 3,
                      (40 + k * 100, 40, 80 + k * 100, 80))
    strip, centres = ss._strain_strip(_run_spots(reverse=True), montage, 1.0)
    assert strip.getpixel((strip.width // 2, int(centres[0])))[0] == 60


def test_separate_replicates_are_set_edge_to_edge_without_white():
    montage = Image.new("RGB", (600, 600), (12, 12, 12))
    spots = []
    for k, (x, y) in enumerate([(60, 60), (400, 500)]):   # not neighbours
        montage.paste((150,) * 3, (x - 20, y - 20, x + 20, y + 20))
        spots.append({"x": x, "y": y, "r": 50, "rep": k + 1,
                      "clip": [0, 0, 600, 600]})
    strip, centres = ss._strain_strip(spots, montage, 1.0)
    assert strip.height == 2 * strip.width
    assert all(strip.getpixel((strip.width // 2, int(c)))[0] == 150
               for c in centres)
    assert strip.convert("L").getextrema()[1] < 250


def test_an_outline_over_a_spot_is_left_out_of_its_cut():
    montage = Image.new("RGB", (300, 300), (12, 12, 12))
    montage.paste((150,) * 3, (100, 100, 200, 200))
    montage.paste((255, 176, 0), (90, 140, 210, 146))        # amber line
    cut, fill = ss._cut(montage, (90, 90, 210, 210), (0, 0, 300, 300))
    colours = {c for _, c in cut.getcolors(1 << 20)}
    assert (255, 176, 0) not in colours
    assert fill == (12, 12, 12)


def test_a_cut_past_the_plate_edge_is_filled_with_plate_background():
    montage = Image.new("RGB", (300, 300), (255, 255, 255))   # a label lane
    montage.paste((20, 20, 20), (100, 0, 300, 300))           # the plate
    cut, _ = ss._cut(montage, (60, 0, 160, 100), (100, 0, 300, 300))
    assert cut.getpixel((5, 50)) == (20, 20, 20)


def _rotatable_sheet(tmp_path):
    """The aligned test sheet, plus a sideways graph (strains down the left)."""
    graph_h = Image.new("RGB", (600, 900), "white")
    for y in range(900):
        graph_h.putpixel((100, y), (0, 0, 0))               # the value axis
    pieces = {"montage": Image.new("RGB", (600, 400)),
              "graph": Image.new("RGB", (900, 600), "white"),
              "graph_h": graph_h}
    montage = pieces["montage"]
    spots, tones = [], (200, 150, 100)
    for i, name in enumerate("ABC"):
        for rep in (1, 2):
            x, y = 100 + 200 * i, 100 + 200 * (rep - 1)
            montage.paste((tones[i],) * 3, (x - 40, y - 40, x + 40, y + 40))
            spots.append({"x": x, "y": y, "r": 40, "strain": name,
                          "slot": i + 1, "rep": rep, "level": 0})
    extra = {"level": 0,
             "montage_map": {"size": [600, 400], "spots": spots},
             "graph_map": {"size": [900, 600], "axis_y": 502, "pitch": 300,
                           "ticks": [{"name": n, "x": 150 + 300 * i}
                                     for i, n in enumerate("ABC")]},
             "graph_h_map": {"orientation": "horizontal", "size": [600, 900],
                             "axis_x": 100, "pitch": 300,
                             "ticks": [{"name": n, "y": 150 + 300 * i}
                                       for i, n in enumerate("ABC")]}}
    im, lay = ss.arrange({k: v for k, v in pieces.items() if k != "graph_h"},
                         ss.SHEET_BOX)
    return ss.save(im, lay, tmp_path / "r.png", extra=extra, panels=pieces)


def test_rotate_shows_the_graph_on_its_side(tmp_path):
    with Image.open(_rotatable_sheet(tmp_path)) as im:
        upright = ss.view(im, "fit", (3000, 3000))
        turned = ss.view(im, "fit", (3000, 3000), rotate=True)
    # Not the same picture turned: the sideways graph is its own drawing.
    assert upright != turned
    assert any(turned.getpixel((x, y)) == (0, 0, 0)
               for x in range(0, turned.width, 7)
               for y in range(0, turned.height, 50))


def test_rotated_aligned_puts_each_strains_replicates_in_a_row_left_of_its_name(
        tmp_path):
    with Image.open(_rotatable_sheet(tmp_path)) as im:
        pieces = ss.pieces_of(im)
        pieces.update(ss.native_panels(im))
        out = ss.aligned(pieces, ss.extra_of(im), rotate=True)
    graph_h = pieces["graph_h"]
    assert out.height > graph_h.height and out.width > graph_h.width
    head = out.height - graph_h.height
    # Strain A's row: two tiles side by side (200-grey spots), at A's tick.
    row = [out.getpixel((x, head + 150)) for x in range(out.width - graph_h.width)]
    assert row.count((200, 200, 200)) > 150
    # The graph itself is whole and to the right of the band.
    x0 = out.width - graph_h.width
    assert out.getpixel((x0 + 100, head + 400)) == (0, 0, 0)
    # One unbroken band: no white inside it.
    band = out.crop((0, head, x0 - 40, out.height)).convert("L")   # past the gap
    assert band.getextrema()[1] < 250


def test_rotate_on_a_sheet_without_a_sideways_graph_says_so(tmp_path):
    path, _ = _sheet(tmp_path)
    with Image.open(path) as im:
        with pytest.raises(ss.ViewUnavailable, match="sideways"):
            ss.view(im, "fit", (500, 500), rotate=True)


def test_montage_records_where_every_spot_landed(tmp_path, monkeypatch):
    """The spot map must point at the spots in the PNG actually written."""
    import types

    np = pytest.importorskip("numpy")
    import spotting_batch as sb
    import spotting_montage as sm
    import spotting_quant as sq

    plates = [types.SimpleNamespace(ref=types.SimpleNamespace(plate=p),
                                    centers=np.zeros((6, 8, 2))) for p in (1, 2)]

    def blocks(pd_, opts, radius=None, proc=None, layout=None, whole=False):
        h = int(round((2 + 2 * sm.PAD_CELLS) * sm.CELL_PX))
        w = int(round((7 + 2 * sm.PAD_CELLS) * sm.CELL_PX))
        yy, xx = np.mgrid[0:h, 0:w]
        img = np.zeros((h, w), np.float32)
        for i in range(3):
            for j in range(8):
                cy = (sm.PAD_CELLS + i) * sm.CELL_PX
                cx = (sm.PAD_CELLS + j) * sm.CELL_PX
                img[(yy - cy) ** 2 + (xx - cx) ** 2 < 50 ** 2] = 250
        return [img, img], [np.ones((h, w), bool)] * 2

    monkeypatch.setattr(sm, "plate_blocks", blocks)
    out = sm.build_montage("TC|x", plates, [f"S{i}" for i in range(1, 9)],
                           sq.MeasureOptions(), tmp_path / "m.png",
                           layout=sb.classic_layout(), dpi=100)
    with Image.open(out) as im:
        smap = ss.png_record(im, sm.SPOT_KEY)
        assert all(abs(a - b) <= 1 for a, b in zip(smap["size"], im.size))
        gray = im.convert("L")
        assert len(smap["spots"]) == 2 * 6 * 8
        for s in smap["spots"]:
            assert gray.getpixel((int(s["x"]), int(s["y"]))) > 200
        assert {s["strain"] for s in smap["spots"]} == {f"S{i}" for i in range(1, 9)}


def test_graph_records_its_ticks_and_axis(tmp_path):
    pd = pytest.importorskip("pandas")
    import spotting_plots as sp

    rows = []
    for col, name in enumerate(("WT", "a", "b"), start=1):
        for rep in range(4):
            v = 1.0 if col == 1 else 0.3 * col + 0.01 * rep
            rows.append({"treatment": "T", "replicate": rep, "strain": name,
                         "strain_col": col, "relative_growth": v, "value": v,
                         "control_raw": 10, "is_control": col == 1,
                         "artifact": False, "excluded": False,
                         "outlier": False, "control_ok": True})
    csv = tmp_path / "t.csv"
    pd.DataFrame(rows).to_csv(csv, index=False)
    png = sp.draw(csv, tmp_path, control="WT")[0]
    with Image.open(png) as im:
        gmap = ss.png_record(im, sp.GRAPH_KEY)
        assert gmap["size"] == list(im.size)
        assert [t["name"] for t in gmap["ticks"]] == ["WT", "a", "b"]
        gray = im.convert("L")
        # The x-axis line runs just above the recorded cut.
        x = int((gmap["ticks"][0]["x"] + gmap["ticks"][1]["x"]) / 2)
        line = [gray.getpixel((x, y)) for y in range(gmap["axis_y"] - 12,
                                                     gmap["axis_y"])]
        assert min(line) < 80
        assert gray.getpixel((x, gmap["axis_y"] + 2)) > 200


def test_an_older_wide_sheet_keeps_its_whole_montage(tmp_path):
    from results_review.export import _combine_preview

    old = Image.new("RGB", (1000, 600), "white")
    old.paste(Image.new("RGB", (700, 400), (0, 0, 0)), (10, 100))   # wide montage
    path = tmp_path / "old.png"
    old.save(path)
    graph = tmp_path / "graph.png"
    Image.new("RGB", (400, 300), (20, 160, 20)).save(graph)
    out = _combine_preview(path, graph, tmp_path / "combined.png")
    with Image.open(out) as im:
        assert im.getpixel((700, 300)) == (0, 0, 0)
